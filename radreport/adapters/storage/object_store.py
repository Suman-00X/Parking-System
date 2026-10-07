"""Stores and retrieves recorded audio in S3-compatible object storage.

Defines: ObjectStore, the interface; S3ObjectStore for real buckets; LocalFileObjectStore, a folder,
for developer machines without S3; InMemoryObjectStore for tests; UnconfiguredObjectStore, which refuses
every operation (StorageUnavailable) when no S3 is configured; object_store, which picks one from
settings; and audio_key, which builds the storage path for a recording.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, BinaryIO, Protocol

from radreport.core import fallbacks
from radreport.core.config import StorageSettings
from radreport.core.logging import get_logger

log = get_logger(__name__)

#: the two stores, kept apart by prefix as well as by policy.
CLINICAL_PREFIX = "clinical"
TRAINING_PREFIX = "training"
"""The scrubbed copy. Muting person-name spans happens in the training copy only; the clinical archive stays intact."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    content_hash: str


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject: ...
    def get(self, key: str) -> bytes: ...
    def open(self, key: str) -> BinaryIO: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


def audio_key(tenant_id: uuid.UUID, recording_id: uuid.UUID, audio_format: str, *, uploaded_at: dt.datetime | None = None, store: str = CLINICAL_PREFIX) -> str:
    """`<store>/<tenant>/<yyyy>/<mm>/<recording>.<ext>`"""
    when = uploaded_at or dt.datetime.now(dt.UTC)
    return f"{store}/{tenant_id}/{when:%Y/%m}/{recording_id}.{audio_format}"


class S3ObjectStore:
    """boto3-backed store with SSE-KMS."""

    def __init__(self, settings: StorageSettings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._bucket = settings.bucket
        if client is not None:
            self._client = client
        else:
            import boto3

            self._client = boto3.client("s3", endpoint_url=settings.endpoint_url, aws_access_key_id=settings.access_key_id, aws_secret_access_key=settings.secret_access_key, region_name=settings.region)
        if not settings.sse_kms_key_id:
            log.warning("storage_sse_kms_unset", detail="falling back to SSE-S3; set a KMS key before handling real audio")

    def _encryption_args(self) -> dict[str, str]:
        if self._settings.sse_kms_key_id:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self._settings.sse_kms_key_id}
        return {"ServerSideEncryption": "AES256"}

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        from radreport.core.hashing import hash_bytes

        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type, **self._encryption_args())
        return StoredObject(key=key, size_bytes=len(data), content_hash=hash_bytes(data))

    def get(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    def open(self, key: str) -> BinaryIO:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"]

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError:
            return False
        return True

    def delete(self, key: str) -> None:
        """Only for the erasure cascade."""
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def signed_url(self, key: str, *, expires_seconds: int, content_type: str) -> str:
        """A short-lived link straight to the object, with no-store forced on the response so nothing between keeps a copy."""
        return str(self._client.generate_presigned_url("get_object", Params={"Bucket": self._bucket, "Key": key, "ResponseCacheControl": "private, no-store, max-age=0", "ResponseContentType": content_type}, ExpiresIn=expires_seconds))


class InMemoryObjectStore:
    """For tests and the synthetic-data dev path. No PHI ever reaches it."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        from radreport.core.hashing import hash_bytes

        self._data[key] = data
        return StoredObject(key=key, size_bytes=len(data), content_hash=hash_bytes(data))

    def get(self, key: str) -> bytes:
        return self._data[key]

    def open(self, key: str) -> BinaryIO:
        import io

        return io.BytesIO(self._data[key])

    def exists(self, key: str) -> bool:
        return key in self._data

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class LocalFileObjectStore:
    """Objects as files under one folder. Developer machines only: nothing is encrypted."""

    def __init__(self, root: str) -> None:
        from pathlib import Path

        self._root = Path(root).resolve()

    def _path(self, key: str) -> Any:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError(f"object key escapes the store: {key!r}")
        return path

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        from radreport.core.hashing import hash_bytes

        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(key=key, size_bytes=len(data), content_hash=hash_bytes(data))

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class StorageUnavailable(RuntimeError):
    """No object store is configured, so audio can be neither kept nor read."""


class UnconfiguredObjectStore:
    """Stands in when no S3 is configured: the rest of the app runs, and every audio operation fails with StorageUnavailable."""

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise StorageUnavailable("audio storage is not configured: set RADREPORT_STORAGE__BUCKET with S3 credentials (or RADREPORT_STORAGE__ENDPOINT_URL)")

    put = get = open = delete = _refuse

    def exists(self, key: str) -> bool:
        return False


@lru_cache(maxsize=1)
def _aws_credentials_found() -> bool:
    """Whether boto3's own chain (environment, profile, container or instance role) finds credentials."""
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001 - no boto3 or a broken profile both mean no credentials
        return False


def _s3_configured(config: StorageSettings) -> bool:
    return bool(config.endpoint_url or config.access_key_id) or _aws_credentials_found()


def object_store(settings: StorageSettings | None = None) -> ObjectStore:
    """The store the settings name; the local folder only where the environment allows it, and a refusing stand-in when no S3 is configured."""
    from radreport.core.config import get_settings

    config = settings or get_settings().storage
    if config.backend == "local":
        if get_settings().environment not in ("local", "test", "development"):
            raise RuntimeError("RADREPORT_STORAGE__BACKEND=local is for developer machines; use S3 here")
        return LocalFileObjectStore(config.local_path)
    if not _s3_configured(config):
        fallbacks.note("storage", "no S3 credentials or endpoint; audio upload and playback answer 503 until RADREPORT_STORAGE__* is set")
        return UnconfiguredObjectStore()
    return S3ObjectStore(config)
