"""Checks every request against the access policy file before any route handler runs.

Order: read the policy, its roles and its declared parameters (load_policy, parse_policy,
_parse_param) -> confirm it lists exactly the routes the app serves (verify_coverage) -> for each
request, find its rule (AccessPolicy.match), refuse a cross-site admin write (_check_origin),
identify the caller for that rule's realm, check their role and rate limit (AccessMiddleware)
-> leave the result on `request.state.identity` and the rule on `request.state.access_rule`;
input_check.py then checks the parameters.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from radreport.admin import auth
from radreport.admin.auth import AuthenticatedAdmin
from radreport.auth.lab import ACCESS_COOKIE, REFRESH_COOKIE, TokenInvalid, verify_access_token
from radreport.core.config import get_settings
from radreport.core.logging import get_logger
from radreport.core.tenancy import Principal

log = get_logger(__name__)

POLICY_PATH: Final[Path] = Path(__file__).with_name("access_policy.xml")
REALMS: Final[frozenset[str]] = frozenset({"public", "admin", "lab"})
RATE_KEYS: Final[frozenset[str]] = frozenset({"ip", "principal"})
RATE_STORES: Final[frozenset[str]] = frozenset({"memory", "shared"})
PARAM_LOCATIONS: Final[frozenset[str]] = frozenset({"path", "query", "form", "file", "json"})
PARAM_TYPES: Final[frozenset[str]] = frozenset({"string", "uuid", "int", "number", "bool", "object", "array", "file"})
#: Applied to a string parameter that declares no max-length of its own.
DEFAULT_MAX_LENGTH: Final[int] = 2000
ADMIN_LOGIN_PATH: Final[str] = "/admin/login"
ADMIN_API_PREFIX: Final[str] = "/admin/api/"

_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::path)?\}")


class PolicyError(Exception):
    """The policy file is malformed, or disagrees with the routes the app serves."""


@dataclass(frozen=True, slots=True)
class RateLimit:
    id: str
    requests: int
    window_seconds: int
    key: str
    store: str = "memory"
    """memory: this worker's own sliding window. shared: a Postgres counter every worker sees."""


@dataclass(frozen=True, slots=True)
class ParamRule:
    name: str
    location: str
    """path, query, form, file or json (a top-level key of a JSON object body)."""

    type: str
    required: bool
    pattern: re.Pattern[str] | None
    """Must match the whole value (fullmatch)."""

    max_length: int | None
    multiple: bool
    """Whether the parameter may repeat (a list of files, a repeated query key)."""


@dataclass(frozen=True, slots=True)
class RouteRule:
    id: str
    method: str
    path: str
    realm: str
    roles: frozenset[str]
    rate_limit: RateLimit | None
    max_body_bytes: int | None
    environments: frozenset[str] | None
    description: str
    pattern: re.Pattern[str]
    params: tuple[ParamRule, ...] = ()

    def param(self, location: str, name: str) -> ParamRule | None:
        """The declared parameter, or None if this route does not accept it."""
        return next((p for p in self.params if p.location == location and p.name == name), None)


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    roles: dict[str, str]
    """Role id -> the realm it belongs to."""

    rate_limits: dict[str, RateLimit]
    routes: tuple[RouteRule, ...]
    """Literal paths first, so `/x/new` would win over `/x/{id}`."""

    def match(self, method: str, path: str) -> RouteRule | None:
        """The rule for this request, or None."""
        wanted = "GET" if method == "HEAD" else method
        for rule in self.routes:
            if rule.method == wanted and rule.pattern.match(path):
                return rule
        return None

    def allows(self, role: str, method: str, path: str) -> bool:
        """Whether `role` may call this route; used to hide controls a caller cannot use."""
        rule = self.match(method, path)
        return rule is not None and (rule.realm == "public" or role in rule.roles)


def _compile(path: str) -> re.Pattern[str]:
    """`/a/{id}/b` -> `^/a/(?P<id>[^/]+)/b$`, so the path parameters can be read back by name."""
    parts: list[str] = []
    position = 0
    for found in _PARAM.finditer(path):
        parts.append(re.escape(path[position : found.start()]))
        parts.append(f"(?P<{found.group(1)}>[^/]+)")
        position = found.end()
    parts.append(re.escape(path[position:]))
    return re.compile("^" + "".join(parts) + "$")


def _int(element: ET.Element, name: str, *, required: bool = True) -> int | None:
    raw = element.get(name)
    if raw is None:
        if required:
            raise PolicyError(f"<{element.tag} id={element.get('id')!r}> is missing {name}")
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise PolicyError(f"<{element.tag} id={element.get('id')!r}> {name}={raw!r} is not an integer") from exc
    if value <= 0:
        raise PolicyError(f"<{element.tag} id={element.get('id')!r}> {name} must be positive")
    return value


def _parse_param(element: ET.Element, rule_id: str, path: str) -> ParamRule:
    """One `<param>` of a route."""
    name, location, kind = element.get("name"), element.get("in"), element.get("type", "string")
    where = f"route {rule_id!r} param {name!r}"
    if not name or location not in PARAM_LOCATIONS:
        raise PolicyError(f"{where} needs a name and in= one of {sorted(PARAM_LOCATIONS)}")
    if kind not in PARAM_TYPES:
        raise PolicyError(f"{where} has type {kind!r}; use one of {sorted(PARAM_TYPES)}")
    if (location == "file") != (kind == "file"):
        raise PolicyError(f"{where}: type=file goes with in=file, and only there")
    required = element.get("required", "false")
    if required not in ("true", "false"):
        raise PolicyError(f"{where} required must be true or false")
    if location == "path" and (required != "true" or name not in _PARAM.findall(path)):
        raise PolicyError(f"{where} must be required and appear as {{{name}}} in {path}")
    raw_pattern = element.get("pattern")
    try:
        pattern = re.compile(raw_pattern) if raw_pattern is not None else None
    except re.error as exc:
        raise PolicyError(f"{where} pattern is not a valid regex: {exc}") from exc
    max_length = _int(element, "max-length", required=False)
    if kind == "string" and max_length is None:
        max_length = DEFAULT_MAX_LENGTH
    return ParamRule(name=name, location=location, type=kind, required=required == "true", pattern=pattern, max_length=max_length, multiple=element.get("multiple") == "true")


def parse_policy(text: str) -> AccessPolicy:
    """Build and validate a policy from the XML text."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PolicyError(f"access policy is not well-formed XML: {exc}") from exc
    if root.tag != "access-policy":
        raise PolicyError("the root element must be <access-policy>")

    roles: dict[str, str] = {}
    for element in root.iterfind("roles/role"):
        role_id, realm = element.get("id"), element.get("realm")
        if not role_id or realm not in REALMS - {"public"}:
            raise PolicyError(f"<role id={role_id!r}> needs an id and a realm of admin or lab")
        if role_id in roles:
            raise PolicyError(f"role {role_id!r} is declared twice")
        roles[role_id] = realm

    limits: dict[str, RateLimit] = {}
    for element in root.iterfind("rate-limits/rate-limit"):
        limit_id, key = element.get("id"), element.get("key")
        if not limit_id or key not in RATE_KEYS:
            raise PolicyError(f"<rate-limit id={limit_id!r}> needs an id and key of ip or principal")
        if limit_id in limits:
            raise PolicyError(f"rate limit {limit_id!r} is declared twice")
        store = element.get("store", "memory")
        if store not in RATE_STORES:
            raise PolicyError(f"<rate-limit id={limit_id!r}> store must be one of {sorted(RATE_STORES)}")
        limits[limit_id] = RateLimit(id=limit_id, requests=_int(element, "requests") or 0, window_seconds=_int(element, "window-seconds") or 0, key=key, store=store)

    rules: list[RouteRule] = []
    seen_ids: set[str] = set()
    seen_routes: set[tuple[str, str]] = set()
    for group in root.iterfind("routes"):
        realm = group.get("realm")
        if realm not in REALMS:
            raise PolicyError(f"<routes realm={realm!r}> must be one of {sorted(REALMS)}")
        for element in group.iterfind("route"):
            rule_id, method, path = element.get("id"), (element.get("method") or "").upper(), element.get("path")
            if not rule_id or not method or not path or not path.startswith("/"):
                raise PolicyError(f"<route id={rule_id!r}> needs an id, a method and a path starting with /")
            if rule_id in seen_ids:
                raise PolicyError(f"route id {rule_id!r} is used twice")
            if (method, path) in seen_routes:
                raise PolicyError(f"{method} {path} is listed twice")
            seen_ids.add(rule_id)
            seen_routes.add((method, path))

            allowed = frozenset(r.strip() for r in (element.get("roles") or "").split(",") if r.strip())
            if realm == "public" and allowed:
                raise PolicyError(f"route {rule_id!r} is public, so it takes no roles")
            if realm != "public" and not allowed:
                raise PolicyError(f'route {rule_id!r} allows nobody; give it roles="role_a,role_b"')
            for role in allowed:
                if roles.get(role) != realm:
                    raise PolicyError(f"route {rule_id!r} allows {role!r}, which is not a role of the {realm} realm")

            limit_id = element.get("rate-limit")
            if limit_id is not None and limit_id not in limits:
                raise PolicyError(f"route {rule_id!r} names an unknown rate limit {limit_id!r}")
            if realm == "public" and limit_id is not None and limits[limit_id].key != "ip":
                raise PolicyError(f"route {rule_id!r} is public, so its rate limit must be keyed by ip")

            params = tuple(_parse_param(p, rule_id, path) for p in element.iterfind("param"))
            if len({(p.location, p.name) for p in params}) != len(params):
                raise PolicyError(f"route {rule_id!r} declares a parameter twice")
            missing_path = set(_PARAM.findall(path)) - {p.name for p in params if p.location == "path"}
            if missing_path:
                raise PolicyError(f"route {rule_id!r} does not declare its path parameter(s) {sorted(missing_path)}")
            locations = {p.location for p in params}
            if "json" in locations and locations & {"form", "file"}:
                raise PolicyError(f"route {rule_id!r} mixes a JSON body with form fields")

            environments = element.get("environments")
            rules.append(RouteRule(id=rule_id, method=method, path=path, realm=realm, roles=allowed, rate_limit=limits[limit_id] if limit_id else None, max_body_bytes=_int(element, "max-body-bytes", required=False), environments=frozenset(environments.split()) if environments else None, description=element.get("description") or "", pattern=_compile(path), params=params))

    rules.sort(key=lambda r: (r.path.count("{"), -len(r.path)))
    return AccessPolicy(roles=roles, rate_limits=limits, routes=tuple(rules))


@lru_cache(maxsize=1)
def load_policy() -> AccessPolicy:
    """The policy shipped with the app, read once."""
    return parse_policy(POLICY_PATH.read_text(encoding="utf-8"))


def _served_routes(app: FastAPI) -> Iterator[tuple[str, str]]:
    """Every (method, full path) the app serves, looking inside included routers."""
    for route in app.routes:
        if isinstance(route, Route):
            for method in route.methods or ():
                yield method, route.path
        elif hasattr(route, "effective_route_contexts"):
            # Newer FastAPI wraps each included router; its contexts carry the full prefixed path.
            for context in route.effective_route_contexts():
                for method in context.methods or ():
                    yield method, context.path


def verify_coverage(app: FastAPI, policy: AccessPolicy) -> None:
    """Refuse to start if a served route is unlisted, or a listed route is not served."""
    served = {(method, path) for method, path in _served_routes(app) if method != "HEAD"}
    declared = {(r.method, r.path) for r in policy.routes}

    problems = [f"served but not in the access policy: {m} {p}" for m, p in sorted(served - declared)]
    problems += [f"in the access policy but not served: {m} {p}" for m, p in sorted(declared - served)]
    if problems:
        raise PolicyError("; ".join(problems))


class RateLimiter:
    """A sliding-window counter per (limit, caller), held in this process's memory."""

    _SWEEP_EVERY = 10_000

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[tuple[str, str], tuple[int, deque[float]]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def hit(self, limit: RateLimit, who: str) -> float | None:
        """Count one request; return the seconds to wait if it is over the limit, else None."""
        now = self._clock()
        cutoff = now - limit.window_seconds
        with self._lock:
            self._calls += 1
            if self._calls % self._SWEEP_EVERY == 0:
                self._sweep(now)
            _window, hits = self._hits.setdefault((limit.id, who), (limit.window_seconds, deque()))
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit.requests:
                return hits[0] + limit.window_seconds - now
            hits.append(now)
            return None

    def _sweep(self, now: float) -> None:
        """Drop callers with no hit inside their window, so memory tracks active callers only."""
        for key in [k for k, (window, hits) in self._hits.items() if not hits or hits[-1] <= now - window]:
            del self._hits[key]


def _count_in_postgres(limit_id: str, who: str, window_start: dt.datetime) -> int:
    """Add one hit to a shared window and return the new count, atomically."""
    from sqlalchemy import text

    from radreport.db.session import system_session

    with system_session() as session:
        hits = session.execute(text("INSERT INTO rate_limit_counter (limit_id, who, window_start, hits) VALUES (:l, :w, :s, 1) ON CONFLICT (limit_id, who, window_start) DO UPDATE SET hits = rate_limit_counter.hits + 1 RETURNING hits"), {"l": limit_id, "w": who, "s": window_start}).scalar_one()
        if random.random() < 0.001:
            # Old windows are useless; sweep them now and then instead of on a schedule.
            session.execute(text("DELETE FROM rate_limit_counter WHERE window_start < now() - interval '1 day'"))
        return int(hits)


class SharedRateLimiter:
    """A fixed-window counter in Postgres, so a limit holds across every worker and restart."""

    def __init__(self, counter: Callable[[str, str, dt.datetime], int] = _count_in_postgres, clock: Callable[[], float] = time.time) -> None:
        self._counter = counter
        self._clock = clock

    def hit(self, limit: RateLimit, who: str) -> float | None:
        """Count one request; return the seconds to wait if it is over the limit, else None."""
        now = self._clock()
        start = now - (now % limit.window_seconds)
        try:
            hits = self._counter(limit.id, who, dt.datetime.fromtimestamp(start, dt.UTC))
        except Exception as exc:  # noqa: BLE001 - a limiter outage must not become an app outage
            # Fail open: with the database unreachable the request cannot do anything anyway.
            log.warning("rate_limit_store_unavailable", limit=limit.id, error=type(exc).__name__)
            return None
        return start + limit.window_seconds - now if hits > limit.requests else None


@dataclass(frozen=True, slots=True)
class Identity:
    realm: str
    key: str
    """What principal-keyed rate limits count against."""

    roles: frozenset[str] = frozenset()
    admin: AuthenticatedAdmin | None = None
    principal: Principal | None = None


def _admin_from_cookie(token: str | None) -> AuthenticatedAdmin | None:
    """Resolve an admin session cookie against the database."""
    from radreport.db.session import system_session

    if not token:
        return None
    with system_session() as session:
        return auth.authenticate(session, token)


def _deny(status_code: int, detail: str, *, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


class AccessMiddleware(BaseHTTPMiddleware):
    """Authenticate, authorize and rate-limit each request from the policy, before routing."""

    def __init__(self, app: Callable, *, policy: AccessPolicy, limiter: RateLimiter | None = None, admin_resolver: Callable[[str | None], AuthenticatedAdmin | None] = _admin_from_cookie, shared_limiter: SharedRateLimiter | None = None) -> None:
        super().__init__(app)
        self.policy = policy
        self.limiter = limiter or RateLimiter()
        self.shared_limiter = shared_limiter or SharedRateLimiter()
        self.admin_resolver = admin_resolver

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        rule = self.policy.match(request.method, path)
        if rule is None or (rule.environments is not None and get_settings().environment not in rule.environments):
            return _deny(404, "Not Found")

        if (refused := self._check_origin(rule, request)) is not None:
            return refused

        if rule.max_body_bytes is not None:
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > rule.max_body_bytes:
                return _deny(413, f"request body over the {rule.max_body_bytes}-byte limit for this route")

        ip = request.client.host if request.client else "unknown"
        if rule.rate_limit is not None and rule.rate_limit.key == "ip":
            if (refused := await self._limit(rule, f"ip:{ip}")) is not None:
                return refused

        if rule.realm == "public":
            identity = Identity(realm="public", key=f"ip:{ip}")
        else:
            resolved = await run_in_threadpool(self._identify, rule, request)
            if isinstance(resolved, Response):
                return resolved
            identity = resolved
            if rule.rate_limit is not None and rule.rate_limit.key == "principal":
                if (refused := await self._limit(rule, identity.key)) is not None:
                    return refused

        request.state.identity = identity
        request.state.access_rule = rule
        return await call_next(request)

    def _check_origin(self, rule: RouteRule, request: Request) -> Response | None:
        """Refuse a cross-site write authenticated by a cookie, or to a sign-in form (CSRF)."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        lab_cookie = ACCESS_COOKIE in request.cookies and "authorization" not in request.headers
        if not (rule.path.startswith(("/admin", "/ui/")) or lab_cookie):
            # A bearer token is never attached by the browser on its own, so it cannot be forged cross-site.
            return None
        source = request.headers.get("origin") or request.headers.get("referer")
        if source is None:
            # No browser sends a cross-site POST without Origin, so this is a script, not a forged request.
            return None
        parts = urlsplit(source)
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        if parts.netloc.lower() == request.headers.get("host", "").lower() or origin in {o.rstrip("/").lower() for o in get_settings().trusted_origins}:
            return None
        log.warning("cross_site_request_refused", route=rule.id, origin=origin)
        return _deny(403, "cross-site request refused")

    async def _limit(self, rule: RouteRule, who: str) -> Response | None:
        assert rule.rate_limit is not None
        if rule.rate_limit.store == "shared":
            wait = await run_in_threadpool(self.shared_limiter.hit, rule.rate_limit, who)
        else:
            wait = self.limiter.hit(rule.rate_limit, who)
        if wait is None:
            return None
        log.warning("rate_limited", route=rule.id, limit=rule.rate_limit.id, who=who)
        return _deny(429, "too many requests; slow down", headers={"Retry-After": str(max(1, math.ceil(wait)))})

    def _identify(self, rule: RouteRule, request: Request) -> Identity | Response:
        return self._identify_admin(rule, request) if rule.realm == "admin" else self._identify_lab_user(rule, request)

    def _identify_admin(self, rule: RouteRule, request: Request) -> Identity | Response:
        admin = self.admin_resolver(request.cookies.get(auth.SESSION_COOKIE))
        if admin is None:
            # A browser opening a panel page goes to the sign-in form; anything else gets a status code.
            if request.method == "GET" and not request.url.path.startswith(ADMIN_API_PREFIX):
                return RedirectResponse(ADMIN_LOGIN_PATH, status_code=303)
            return _deny(401, f"sign in at {ADMIN_LOGIN_PATH}")
        if admin.role not in rule.roles:
            log.warning("access_refused", route=rule.id, realm="admin", role=admin.role, platform_user_id=str(admin.platform_user_id))
            return _deny(403, f"the {admin.role} role may not call this route")
        return Identity(realm="admin", key=f"admin:{admin.platform_user_id}", roles=frozenset({admin.role}), admin=admin)

    def _identify_lab_user(self, rule: RouteRule, request: Request) -> Identity | Response:
        scheme, _, header_token = request.headers.get("authorization", "").partition(" ")
        token = header_token.strip() if scheme.lower() == "bearer" else request.cookies.get(ACCESS_COOKIE, "")
        claims = None
        if token:
            try:
                claims = verify_access_token(token)
            except TokenInvalid:
                claims = None
        if claims is None:
            if request.method == "GET" and request.url.path.startswith("/ui/") and not header_token:
                # A browser page: renew from the refresh cookie if it has one, else sign in.
                target = "/ui/refresh" if request.cookies.get(REFRESH_COOKIE) is not None or token else "/ui/login"
                return RedirectResponse(f"{target}?{urlencode({'next': request.url.path})}", status_code=303)
            detail = "access token is invalid or expired; refresh it at /auth/refresh" if token else "sign in at /auth/login and send Authorization: Bearer <access token>"
            return _deny(401, detail, headers={"WWW-Authenticate": 'Bearer error="invalid_token"' if token else "Bearer"})
        if not claims.roles & rule.roles:
            log.warning("access_refused", route=rule.id, realm="lab", roles=sorted(claims.roles), user_id=str(claims.user_id))
            return _deny(403, f"this route requires one of: {', '.join(sorted(rule.roles))}")
        principal = Principal(id=claims.user_id, kind="app_user", tenant_id=claims.tenant_id)
        return Identity(realm="lab", key=f"user:{claims.user_id}", roles=claims.roles, principal=principal)
