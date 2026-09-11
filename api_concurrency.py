"""Shared concurrency gate for translation and metadata-recognition API calls."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

import config

_DEFAULT_MAX_CONCURRENCY = 2
_MAX_ALLOWED_CONCURRENCY = 32


def _read_limit() -> int:
    raw = os.environ.get(
        "PAPER_TRANSLATE_API_MAX_CONCURRENCY",
        getattr(config, "TRANSLATE_API_MAX_CONCURRENCY", _DEFAULT_MAX_CONCURRENCY),
    )
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("TRANSLATE_API_MAX_CONCURRENCY must be an integer") from exc
    if not 1 <= limit <= _MAX_ALLOWED_CONCURRENCY:
        raise ValueError(
            f"TRANSLATE_API_MAX_CONCURRENCY must be between 1 and {_MAX_ALLOWED_CONCURRENCY}"
        )
    return limit


MAX_CONCURRENCY = _read_limit()
_api_semaphore = threading.BoundedSemaphore(MAX_CONCURRENCY)


@contextmanager
def api_call_slot() -> Iterator[None]:
    """Hold one shared slot for the complete lifetime of an LLM API call."""
    with _api_semaphore:
        yield
