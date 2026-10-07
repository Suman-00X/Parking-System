"""What a CDN may keep: versioned static assets for a year, nothing else, and audio only on short-lived signed links."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radreport.api.app import create_app
from radreport.api.routes.review_ui import asset_url

pytestmark = pytest.mark.db


def test_a_versioned_asset_is_immutable_and_a_bare_one_is_short_lived() -> None:
    client = TestClient(create_app())
    versioned = client.get(asset_url("app.css"))
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/ui/static/app.css").headers["cache-control"] == "public, max-age=300"
    again = client.get(asset_url("app.css"), headers={"If-None-Match": versioned.headers["etag"]})
    assert again.status_code == 304


def test_pages_link_the_versioned_assets_and_are_never_cached(migrated_db: str) -> None:
    page = TestClient(create_app()).get("/admin/login")
    assert asset_url("app.css") in page.text and asset_url("app.js") in page.text
    assert page.headers["cache-control"] == "private, no-store"


def test_audio_is_served_from_a_short_lived_signed_link(monkeypatch: pytest.MonkeyPatch) -> None:
    from radreport.adapters.storage.object_store import S3ObjectStore
    from radreport.core.config import StorageSettings

    class FakeS3:
        def generate_presigned_url(self, op: str, *, Params: dict, ExpiresIn: int) -> str:  # noqa: N803 - boto3's names
            assert op == "get_object" and Params["ResponseCacheControl"].startswith("private, no-store")
            return f"https://bucket.example/{Params['Key']}?X-Amz-Expires={ExpiresIn}"

    store = S3ObjectStore(StorageSettings(sse_kms_key_id="k"), client=FakeS3())
    url = store.signed_url("tenants/x/audio.flac", expires_seconds=60, content_type="audio/flac")
    assert url.endswith("X-Amz-Expires=60")


def test_the_audio_route_redirects_to_the_signed_link(migrated_db: str, two_tenants, monkeypatch: pytest.MonkeyPatch) -> None:
    from radreport.api.routes import review as review_route
    from radreport.db.session import tenant_session
    from tests.db.helpers import lab_headers
    from tests.db.review_factory import build_signed_report

    lab, _ = two_tenants
    with tenant_session(lab, url=migrated_db) as session:
        draft_id = build_signed_report(session, lab)["draft_id"]

    class SigningStore:
        def __init__(self, _settings) -> None:  # type: ignore[no-untyped-def]
            pass

        def signed_url(self, key: str, *, expires_seconds: int, content_type: str) -> str:
            return f"https://bucket.example/{key}?ttl={expires_seconds}"

    monkeypatch.setattr(review_route, "S3ObjectStore", SigningStore)
    response = TestClient(create_app(), follow_redirects=False).get(f"/review/drafts/{draft_id}/audio", headers=lab_headers(migrated_db, lab, "radiologist"))
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://bucket.example/") and response.headers["location"].endswith("ttl=60")
    assert response.headers["cache-control"] == "private, no-store"
