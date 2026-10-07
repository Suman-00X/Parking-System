"""The developer-machine store keeps objects in a folder, never outside it, and is refused outside local environments."""

from __future__ import annotations

import pytest

from radreport.adapters.storage import object_store as store_module
from radreport.adapters.storage.object_store import LocalFileObjectStore, S3ObjectStore, object_store
from radreport.core.config import StorageSettings


def test_round_trip_and_containment(tmp_path) -> None:
    store = LocalFileObjectStore(str(tmp_path))
    stored = store.put("clinical/lab/2026/10/r.flac", b"fLaC-bytes", content_type="audio/flac")
    assert stored.size_bytes == 10 and store.exists("clinical/lab/2026/10/r.flac") and store.get("clinical/lab/2026/10/r.flac") == b"fLaC-bytes"
    with store.open("clinical/lab/2026/10/r.flac") as handle:
        assert handle.read() == b"fLaC-bytes"
    store.delete("clinical/lab/2026/10/r.flac")
    assert not store.exists("clinical/lab/2026/10/r.flac")
    with pytest.raises(ValueError):
        store.put("../escape.txt", b"x", content_type="text/plain")


def test_backend_choice(tmp_path, monkeypatch) -> None:
    assert isinstance(object_store(StorageSettings(backend="local", local_path=str(tmp_path))), LocalFileObjectStore)
    assert isinstance(object_store(StorageSettings(sse_kms_key_id="k", endpoint_url="http://127.0.0.1:1")), S3ObjectStore)

    class Production:
        environment = "production"

    monkeypatch.setattr("radreport.core.config.get_settings", lambda: Production())
    with pytest.raises(RuntimeError):
        store_module.object_store(StorageSettings(backend="local", local_path=str(tmp_path)))
