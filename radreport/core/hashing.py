"""Content hashing, used to recognise a file or a config the system has already seen.

Order: hash bytes, a stream or a file (hash_bytes, hash_stream, hash_file, hash_text), and for
settings, turn them into a stable form first and then hash that (canonical_json, hash_config).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_CHUNK = 1024 * 1024


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_stream(chunks: Iterable[bytes]) -> str:
    h = hashlib.sha256()
    for chunk in chunks:
        h.update(chunk)
    return h.hexdigest()


def hash_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def hash_config(config: Mapping[str, Any]) -> str:
    """`asr_run.config_hash` — hash of the keyterm list plus params."""
    return hash_text(canonical_json(config))
