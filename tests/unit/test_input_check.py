"""Parameter allowlisting from the access policy, through both middlewares, without a database."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Body, FastAPI, File, Form, Query, UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel

from radreport.api.access import AccessMiddleware, PolicyError, load_policy, parse_policy
from radreport.api.app import create_app
from radreport.api.input_check import InputValidationMiddleware, verify_params

POLICY = """<access-policy version="1">
  <roles><role id="product_admin" realm="admin"/></roles>
  <routes realm="public">
    <route id="search" method="GET" path="/items/{item_id}">
      <param name="item_id" in="path" type="uuid" required="true"/>
      <param name="sort" in="query" pattern="asc|desc"/>
      <param name="tag" in="query" multiple="true" max-length="5"/>
    </route>
    <route id="form" method="POST" path="/form" max-body-bytes="1000">
      <param name="name" in="form" required="true" max-length="10"/>
      <param name="age" in="form" type="int"/>
    </route>
    <route id="json" method="POST" path="/json" max-body-bytes="1000">
      <param name="title" in="json" required="true" pattern="[a-z]+"/>
      <param name="count" in="json" type="int"/>
      <param name="tags" in="json" type="array" max-length="2"/>
    </route>
    <route id="upload" method="POST" path="/upload" max-body-bytes="100000">
      <param name="files" in="file" type="file" required="true" multiple="true" max-length="2"/>
      <param name="note" in="form"/>
    </route>
    <route id="nobody" method="POST" path="/nobody"/>
  </routes>
</access-policy>"""


class JsonIn(BaseModel):
    title: str
    count: int | None = None
    tags: list[str] | None = None


def _app(policy_text: str = POLICY) -> FastAPI:
    router = APIRouter()

    @router.get("/items/{item_id}")
    def search(item_id: uuid.UUID, sort: str | None = None, tag: Annotated[list[str] | None, Query()] = None) -> dict[str, Any]:
        return {"item_id": str(item_id), "sort": sort, "tag": tag}

    @router.post("/form")
    def form(name: Annotated[str, Form()], age: Annotated[int | None, Form()] = None) -> dict[str, Any]:
        return {"name": name, "age": age}

    @router.post("/json")
    def json_route(body: JsonIn) -> dict[str, Any]:
        return body.model_dump()

    @router.post("/upload")
    async def upload(files: Annotated[list[UploadFile], File()], note: Annotated[str | None, Form()] = None) -> dict[str, Any]:
        return {"sizes": [len(await f.read()) for f in files], "note": note}

    @router.post("/nobody")
    def nobody() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    policy = parse_policy(policy_text)
    app.add_middleware(InputValidationMiddleware)
    app.add_middleware(AccessMiddleware, policy=policy)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


ITEM = f"/items/{uuid.uuid4()}"


# ================================================================ startup ===
def test_the_test_policy_matches_its_handlers() -> None:
    verify_params(_app(), parse_policy(POLICY))


def test_a_handler_parameter_missing_from_the_policy_stops_startup() -> None:
    with pytest.raises(PolicyError, match="'age'.*does not declare"):
        verify_params(_app(), parse_policy(POLICY.replace('<param name="age" in="form" type="int"/>', "")))


def test_a_policy_parameter_the_handler_lacks_stops_startup() -> None:
    with pytest.raises(PolicyError, match="'ghost'.*does not accept"):
        verify_params(_app(), parse_policy(POLICY.replace('<param name="age" in="form" type="int"/>', '<param name="age" in="form" type="int"/><param name="ghost" in="form"/>')))


def test_a_required_or_type_mismatch_stops_startup() -> None:
    with pytest.raises(PolicyError, match="required"):
        verify_params(_app(), parse_policy(POLICY.replace('<param name="name" in="form" required="true"', '<param name="name" in="form"')))
    with pytest.raises(PolicyError, match="type int in the handler"):
        verify_params(_app(), parse_policy(POLICY.replace('<param name="age" in="form" type="int"/>', '<param name="age" in="form"/>')))


def test_the_shipped_policy_matches_every_handler() -> None:
    verify_params(create_app(), load_policy())


# ==================================================================== url ===
def test_a_well_formed_request_reaches_the_handler(client: TestClient) -> None:
    response = client.get(ITEM, params=[("sort", "asc"), ("tag", "a"), ("tag", "b")])
    assert response.status_code == 200
    assert response.json()["tag"] == ["a", "b"]


@pytest.mark.parametrize(("url", "message"), [("/items/not-a-uuid", "must be a UUID"), (f"{ITEM}?sort=sideways", "not in the allowed format"), (f"{ITEM}?sort=asc&sort=desc", "only once"), (f"{ITEM}?tag=toolong", "longer than 5"), (f"{ITEM}?debug=1", "'debug' is not accepted")])
def test_a_bad_path_or_query_is_refused(client: TestClient, url: str, message: str) -> None:
    response = client.get(url)
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_a_refusal_never_repeats_the_value(client: TestClient) -> None:
    response = client.get(f"{ITEM}?sort=<script>alert(1)</script>")
    assert response.status_code == 400
    assert "script" not in response.text


def test_an_unprintable_parameter_name_is_not_echoed(client: TestClient) -> None:
    response = client.get(f"{ITEM}?%3Cimg%3E=1")
    assert response.status_code == 400
    assert "img" not in response.text


# =================================================================== form ===
def test_a_good_form_reaches_the_handler(client: TestClient) -> None:
    assert client.post("/form", data={"name": "ada", "age": "36"}).json() == {"name": "ada", "age": 36}


@pytest.mark.parametrize(("data", "message"), [({"age": "3"}, "'name' is required"), ({"name": "ada", "admin": "true"}, "'admin' is not accepted"), ({"name": "ada", "age": "old"}, "whole number"), ({"name": "a" * 11}, "longer than 10")])
def test_a_bad_form_is_refused(client: TestClient, data: dict[str, str], message: str) -> None:
    response = client.post("/form", data=data)
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_a_form_route_refuses_json(client: TestClient) -> None:
    assert client.post("/form", json={"name": "ada"}).status_code == 400


# =================================================================== json ===
def test_good_json_reaches_the_handler(client: TestClient) -> None:
    assert client.post("/json", json={"title": "abc", "count": 2, "tags": ["x"]}).json() == {"title": "abc", "count": 2, "tags": ["x"]}


@pytest.mark.parametrize(("body", "message"), [({"count": 1}, "'title' is required"), ({"title": "abc", "is_admin": True}, "'is_admin' is not accepted"), ({"title": "ABC"}, "not in the allowed format"), ({"title": "abc", "count": "2"}, "JSON int"), ({"title": "abc", "count": True}, "JSON int"), ({"title": None}, "may not be null"), ({"title": "abc", "tags": ["a", "b", "c"]}, "longer than 2 items")])
def test_bad_json_is_refused(client: TestClient, body: dict[str, Any], message: str) -> None:
    response = client.post("/json", json=body)
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_json_that_is_not_an_object_or_not_json_is_refused(client: TestClient) -> None:
    assert client.post("/json", json=["title"]).status_code == 400
    assert client.post("/json", content=b"{nope", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/json", data={"title": "abc"}).status_code == 400


# ================================================================== files ===
def test_files_and_fields_reach_the_handler_intact(client: TestClient) -> None:
    response = client.post("/upload", files=[("files", ("a.txt", b"x" * 300)), ("files", ("b.txt", b"yy"))], data={"note": "hi"})
    assert response.status_code == 200
    assert response.json() == {"sizes": [300, 2], "note": "hi"}


def test_bad_uploads_are_refused(client: TestClient) -> None:
    three = [("files", (f"{i}.txt", b"x")) for i in range(3)]
    assert "more than 2 files" in client.post("/upload", files=three).json()["detail"]
    assert "'extra' is not accepted" in client.post("/upload", files=[("files", ("a", b"x")), ("extra", ("b", b"y"))]).json()["detail"]
    assert "'files' is required" in client.post("/upload", data={"note": "only a note"}).json()["detail"]


# =================================================================== body ===
def test_a_route_with_no_parameters_refuses_a_body(client: TestClient) -> None:
    assert client.post("/nobody").status_code == 200
    assert client.post("/nobody", content=b"surprise").status_code == 400


def test_a_body_over_the_cap_is_refused_even_without_a_content_length(client: TestClient) -> None:
    def chunks():
        for _ in range(20):
            yield b"x" * 100

    response = client.post("/json", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_an_open_json_body_accepts_only_its_declared_keys() -> None:
    policy = POLICY.replace('<route id="nobody" method="POST" path="/nobody"/>', '<route id="nobody" method="POST" path="/nobody"/><route id="opts" method="POST" path="/opts" max-body-bytes="1000"><param name="level" in="json" type="int"/></route>')
    app = _app(policy)

    @app.post("/opts")
    def opts(options: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, Any]:
        return options or {}

    client = TestClient(app)
    assert client.post("/opts", json={"level": 3}).json() == {"level": 3}
    assert client.post("/opts", json={"level": 3, "other": 1}).status_code == 400
