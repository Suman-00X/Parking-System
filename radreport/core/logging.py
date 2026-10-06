"""Structured logging, with a block on writing patient information into a log line.

Order: configure the logger once at start-up (configure_logging), then take one per module
(get_logger). FORBIDDEN_KEYS lists the field names that are refused.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from radreport.core.config import get_settings

#: Keys that must never be logged. `_redact` drops them rather than masking, so
#: a mistake is visible as an absence rather than as a plausible value.
FORBIDDEN_KEYS = frozenset({"mrn", "patient_name", "name_enc", "audio_bytes", "raw_audio"})


def _redact(_logger: Any, _name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key in FORBIDDEN_KEYS:
            event_dict[key] = "<redacted:phi>"
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    renderer = structlog.processors.JSONRenderer() if settings.observability.json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(processors=[structlog.contextvars.merge_contextvars, structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso"), _redact, structlog.processors.StackInfoRenderer(), structlog.processors.format_exc_info, renderer], wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.observability.log_level.upper(), logging.INFO)), cache_logger_on_first_use=True)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
