"""Checks every request's parameters against the ones its route declares in the access policy.

Order: confirm each route's declared parameters match what its handler accepts (handler_params,
verify_params) -> for each request, check the path and query (InputValidationMiddleware._check_url),
read the body into a spool while enforcing the size cap, check the JSON or form fields and files
(_check_body) -> hand the spooled body to the app unchanged, or answer 400 / 413.
"""

from __future__ import annotations

import json
import re
import types
import typing
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from dataclasses import dataclass
from enum import Enum
from tempfile import SpooledTemporaryFile
from typing import IO, Any, Final
from urllib.parse import parse_qsl

from fastapi import FastAPI, UploadFile, params
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse

from radreport.api.access import AccessPolicy, ParamRule, PolicyError, RouteRule
from radreport.core.logging import get_logger

log = get_logger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: A body this large stays in memory; anything bigger spills to a temporary file.
SPOOL_IN_MEMORY: Final[int] = 1024 * 1024
#: The cap for a route that declares no max-body-bytes (it should take no body at all).
DEFAULT_BODY_CAP: Final[int] = 64 * 1024
_CHUNK: Final[int] = 64 * 1024

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_INT = re.compile(r"-?\d{1,18}")
_NUMBER = re.compile(r"-?\d{1,18}(\.\d{1,18})?([eE][-+]?\d{1,3})?")
_BOOL_TEXT: Final[frozenset[str]] = frozenset({"true", "false", "1", "0", "on", "off", "yes", "no"})
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.\-\[\]]{1,64}")


# ===================================================== what handlers accept ===
@dataclass(frozen=True, slots=True)
class HandlerParam:
    type: str
    required: bool
    multiple: bool


@dataclass(slots=True)
class HandlerParams:
    params: dict[tuple[str, str], HandlerParam]
    open_json: bool = False
    """The body is a free-form dict, so its keys are whatever the policy declares."""


def _strip_optional(annotation: Any) -> Any:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        remaining = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(remaining) == 1:
            return remaining[0]
    return annotation


def _type_of(annotation: Any) -> tuple[str, bool]:
    """Policy type and whether it repeats, for a handler annotation."""
    inner = _strip_optional(annotation)
    origin = typing.get_origin(inner)
    if origin in (list, tuple, set, frozenset):
        args = typing.get_args(inner)
        element, _ = _type_of(args[0]) if args else ("string", False)
        return ("file", True) if element == "file" else ("array", False)
    if origin is dict or inner is dict:
        return "object", False
    if isinstance(inner, type):
        if issubclass(inner, StarletteUploadFile) or inner is UploadFile:
            return "file", False
        if issubclass(inner, bool):
            return "bool", False
        if issubclass(inner, uuid.UUID):
            return "uuid", False
        if issubclass(inner, int) and not issubclass(inner, Enum):
            return "int", False
        if issubclass(inner, float):
            return "number", False
        if issubclass(inner, BaseModel):
            return "object", False
    return "string", False


def _query_type(annotation: Any) -> tuple[str, bool]:
    """A list-typed query parameter is a repeated scalar, not a JSON array."""
    inner = _strip_optional(annotation)
    if typing.get_origin(inner) in (list, tuple, set, frozenset):
        args = typing.get_args(inner)
        return (_type_of(args[0])[0] if args else "string"), True
    return _type_of(inner)


def _walk(dependant: Any) -> Iterator[Any]:
    yield dependant
    for child in dependant.dependencies:
        yield from _walk(child)


def _contexts(app: FastAPI) -> Iterator[tuple[str, str, Any]]:
    """(method, full path, dependant) for every API route, looking inside included routers."""
    for route in app.routes:
        candidates: list[Any] = list(route.effective_route_contexts()) if hasattr(route, "effective_route_contexts") else [route]
        for context in candidates:
            dependant = getattr(context, "dependant", None)
            if dependant is None:
                continue
            for method in context.methods or ():
                if method != "HEAD":
                    yield method, context.path, dependant


def handler_params(app: FastAPI) -> dict[tuple[str, str], HandlerParams]:
    """What each route's handler (and its dependencies) actually accepts."""
    found: dict[tuple[str, str], HandlerParams] = {}
    for method, path, dependant in _contexts(app):
        accepted = HandlerParams(params={})
        for node in _walk(dependant):
            for field in node.path_params:
                accepted.params[("path", field.alias)] = HandlerParam(_type_of(field.field_info.annotation)[0], True, False)
            for field in node.query_params:
                kind, multiple = _query_type(field.field_info.annotation)
                accepted.params[("query", field.alias)] = HandlerParam(kind, field.field_info.is_required(), multiple)
            for field in node.body_params:
                info, annotation = field.field_info, field.field_info.annotation
                if isinstance(info, params.File):
                    inner = _strip_optional(annotation)
                    multiple = typing.get_origin(inner) in (list, tuple, set)
                    accepted.params[("file", field.alias)] = HandlerParam("file", info.is_required(), multiple)
                elif isinstance(info, params.Form):
                    accepted.params[("form", field.alias)] = HandlerParam(_type_of(annotation)[0], info.is_required(), False)
                else:
                    model = _strip_optional(annotation)
                    if isinstance(model, type) and issubclass(model, BaseModel):
                        for name, model_field in model.model_fields.items():
                            accepted.params[("json", model_field.alias or name)] = HandlerParam(_type_of(model_field.annotation)[0], model_field.is_required(), False)
                    else:
                        accepted.open_json = True
        found[(method, path)] = accepted
    return found


def verify_params(app: FastAPI, policy: AccessPolicy) -> None:
    """Refuse to start if a route's declared parameters differ from what its handler accepts."""
    accepted = handler_params(app)
    problems: list[str] = []
    for rule in policy.routes:
        handler = accepted.get((rule.method, rule.path))
        if handler is None:
            if rule.params:
                problems.append(f"{rule.id}: declares parameters but its route takes none")
            continue
        declared = {(p.location, p.name): p for p in rule.params if not (handler.open_json and p.location == "json")}
        for key, param in handler.params.items():
            rule_param = declared.pop(key, None)
            if rule_param is None:
                problems.append(f"{rule.id}: handler accepts {key[0]} parameter {key[1]!r}, which the policy does not declare")
                continue
            if rule_param.required != param.required:
                problems.append(f"{rule.id}: {key[1]!r} is {'required' if param.required else 'optional'} in the handler but not in the policy")
            if rule_param.type != param.type:
                problems.append(f"{rule.id}: {key[1]!r} is type {param.type} in the handler but {rule_param.type} in the policy")
            if key[0] in ("file", "query") and rule_param.multiple != param.multiple:
                problems.append(f"{rule.id}: {key[1]!r} multiple= disagrees with the handler")
        problems += [f"{rule.id}: policy declares {loc} parameter {name!r}, which the handler does not accept" for loc, name in declared]
        if handler.open_json and not any(p.location == "json" for p in rule.params):
            problems.append(f"{rule.id}: handler takes a free-form JSON body; declare its allowed keys")
    if problems:
        raise PolicyError("; ".join(problems))


# ============================================================ value checks ===
def _check_text(param: ParamRule, text: str) -> str | None:
    """A path, query or form value, which always arrives as text."""
    if param.max_length is not None and param.type == "string" and len(text) > param.max_length:
        return f"is longer than {param.max_length} characters"
    if param.type == "uuid" and not _UUID.fullmatch(text):
        return "must be a UUID"
    if param.type == "int" and not _INT.fullmatch(text):
        return "must be a whole number"
    if param.type == "number" and not _NUMBER.fullmatch(text):
        return "must be a number"
    if param.type == "bool" and text.lower() not in _BOOL_TEXT:
        return "must be true or false"
    if param.type in ("object", "array"):
        return "must be sent in a JSON body"
    if param.pattern is not None and not param.pattern.fullmatch(text):
        return "is not in the allowed format"
    return None


def _check_json(param: ParamRule, value: Any) -> str | None:
    """A top-level value of a JSON object body."""
    if value is None:
        return "may not be null" if param.required else None
    expected: dict[str, Callable[[Any], bool]] = {"string": lambda v: isinstance(v, str), "uuid": lambda v: isinstance(v, str) and bool(_UUID.fullmatch(v)), "int": lambda v: isinstance(v, int) and not isinstance(v, bool), "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool), "bool": lambda v: isinstance(v, bool), "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list)}
    if not expected[param.type](value):
        return f"must be a JSON {param.type}"
    if param.max_length is not None and isinstance(value, str | list) and len(value) > param.max_length:
        return f"is longer than {param.max_length} {'items' if isinstance(value, list) else 'characters'}"
    if param.pattern is not None and isinstance(value, str | int | float) and not param.pattern.fullmatch(str(value)):
        return "is not in the allowed format"
    return None


def _shown(name: str) -> str:
    """A parameter name safe to repeat back; a value is never repeated back."""
    return repr(name) if _SAFE_NAME.fullmatch(name) else "an unnamed parameter"


def _group(pairs: list[tuple[str, Any]]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for name, value in pairs:
        grouped[name].append(value)
    return grouped


def _check_fields(rule: RouteRule, location: str, fields: dict[str, list[str]]) -> str | None:
    """Path, query or form fields: nothing unknown, nothing repeated, nothing malformed, nothing missing."""
    for name, values in fields.items():
        param = rule.param(location, name)
        if param is None:
            return f"{_shown(name)} is not accepted here ({location})"
        if len(values) > 1 and not param.multiple:
            return f"{_shown(name)} may appear only once"
        for value in values:
            if (problem := _check_text(param, value)) is not None:
                return f"{_shown(name)} {problem}"
    for param in rule.params:
        if param.location == location and param.required and param.name not in fields:
            return f"{_shown(param.name)} is required ({location})"
    return None


# ============================================================== middleware ===
def _bad_request(detail: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


class InputValidationMiddleware:
    """Reject a request whose parameters are not exactly what its route declares, before routing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        rule: RouteRule | None = scope.get("state", {}).get("access_rule") if scope["type"] == "http" else None
        if rule is None:
            await self.app(scope, receive, send)
            return

        if (problem := self._check_url(rule, scope)) is not None:
            await self._refuse(rule, problem, scope, receive, send)
            return

        spool: IO[bytes] = SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY)  # noqa: SIM115 - closed in finally
        try:
            size = await self._read_body(receive, spool, cap := rule.max_body_bytes or DEFAULT_BODY_CAP)
            if size is None:
                return  # the client went away mid-upload
            if size > cap:
                await self._refuse(rule, f"request body over the {cap}-byte limit for this route", scope, receive, send, status_code=413)
                return
            if (problem := await self._check_body(rule, scope, spool, size)) is not None:
                await self._refuse(rule, problem, scope, receive, send)
                return
            spool.seek(0)
            await self.app(scope, self._replay(spool, receive), send)
        finally:
            spool.close()

    async def _refuse(self, rule: RouteRule, problem: str, scope: Scope, receive: Receive, send: Send, *, status_code: int = 400) -> None:
        log.info("input_rejected", route=rule.id, reason=problem)
        await _bad_request(problem, status_code)(scope, receive, send)

    def _check_url(self, rule: RouteRule, scope: Scope) -> str | None:
        found = rule.pattern.match(scope["path"])
        path_fields = {name: [value] for name, value in (found.groupdict() if found else {}).items()}
        if (problem := _check_fields(rule, "path", path_fields)) is not None:
            return problem
        query = scope.get("query_string", b"").decode("latin-1")
        return _check_fields(rule, "query", _group(parse_qsl(query, keep_blank_values=True)))

    async def _read_body(self, receive: Receive, spool: IO[bytes], cap: int) -> int | None:
        """Copy the body into the spool, counting as it arrives; stop early once over the cap."""
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > cap:
                return size
            spool.write(chunk)
            if not message.get("more_body", False):
                return size

    async def _check_body(self, rule: RouteRule, scope: Scope, spool: IO[bytes], size: int) -> str | None:
        body_params = [p for p in rule.params if p.location in ("form", "file", "json")]
        if size == 0:
            missing = next((p for p in body_params if p.required), None)
            return f"{_shown(missing.name)} is required ({missing.location})" if missing else None
        if not body_params:
            return "this route takes no request body"

        headers = Headers(scope=scope)
        media = headers.get("content-type", "").split(";")[0].strip().lower()
        spool.seek(0)

        if any(p.location == "json" for p in body_params):
            if media != "application/json":
                return "send this request as application/json"
            try:
                data = json.loads(spool.read())
            except (ValueError, UnicodeDecodeError):
                return "the body is not valid JSON"
            if not isinstance(data, dict):
                return "the body must be a JSON object"
            for name, value in data.items():
                param = rule.param("json", name)
                if param is None:
                    return f"{_shown(name)} is not accepted here (json)"
                if (problem := _check_json(param, value)) is not None:
                    return f"{_shown(name)} {problem}"
            missing = next((p for p in body_params if p.required and p.name not in data), None)
            return f"{_shown(missing.name)} is required (json)" if missing else None

        if media == "application/x-www-form-urlencoded":
            try:
                pairs = parse_qsl(spool.read().decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                return "the form body is not valid UTF-8"
            return _check_fields(rule, "form", _group(pairs)) or _check_files(rule, {})
        if media == "multipart/form-data":
            return await self._check_multipart(rule, headers, spool)
        return "send this request as form data"

    async def _check_multipart(self, rule: RouteRule, headers: Headers, spool: IO[bytes]) -> str | None:
        async def stream() -> typing.AsyncGenerator[bytes, None]:
            while chunk := spool.read(_CHUNK):
                yield chunk

        try:
            form = await MultiPartParser(headers, stream()).parse()
        except MultiPartException:
            return "the multipart body is malformed"
        try:
            fields: dict[str, list[str]] = defaultdict(list)
            files: dict[str, list[StarletteUploadFile]] = defaultdict(list)
            for name, value in form.multi_items():
                if isinstance(value, StarletteUploadFile):
                    files[name].append(value)
                else:
                    fields[name].append(value)
            return _check_fields(rule, "form", dict(fields)) or _check_files(rule, dict(files))
        finally:
            await form.close()

    def _replay(self, spool: IO[bytes], receive: Receive) -> Receive:
        """A receive that hands the app the spooled body, then defers to the real one."""
        finished = False

        async def replay() -> Message:
            nonlocal finished
            if finished:
                return await receive()
            chunk = spool.read(_CHUNK)
            if chunk:
                return {"type": "http.request", "body": chunk, "more_body": True}
            finished = True
            return {"type": "http.request", "body": b"", "more_body": False}

        return replay


def _check_files(rule: RouteRule, files: dict[str, list[StarletteUploadFile]]) -> str | None:
    """Uploaded files: only declared names, a declared count, nothing required missing."""
    for name, uploads in files.items():
        param = rule.param("file", name)
        if param is None:
            return f"{_shown(name)} is not accepted here (file)"
        if len(uploads) > 1 and not param.multiple:
            return f"{_shown(name)} may carry only one file"
        if param.max_length is not None and len(uploads) > param.max_length:
            return f"{_shown(name)} carries more than {param.max_length} files"
    for param in rule.params:
        if param.location == "file" and param.required and param.name not in files:
            return f"{_shown(param.name)} is required (file)"
    return None
