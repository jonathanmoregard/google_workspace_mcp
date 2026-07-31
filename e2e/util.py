"""Small shared helpers for the e2e suite."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


def poll_until(
    check: Callable[[], Any],
    *,
    timeout: float = 30.0,
    interval: float = 1.0,
    description: str = "condition",
) -> Any:
    """Poll ``check`` until it returns a truthy value (bounded retries).

    This is the only sanctioned way to wait on eventual consistency in
    the suite - never bare sleeps as synchronization. Raises
    TimeoutError with ``description`` after ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out after {timeout:.0f}s ({attempts} attempts) "
                f"waiting for: {description}"
            )
        time.sleep(interval)


def find_key_paths(
    payload: Any, needles: tuple[str, ...], _path: str = "$"
) -> list[str]:
    """Return JSON paths whose key name contains any needle (case-insensitive).

    Used to record empirically WHERE preview thread objects surface in
    documents.get payloads (a plan unknown resolved by the e2e phase).
    """
    hits: list[str] = []
    lowered = tuple(n.lower() for n in needles)
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{_path}.{key}"
            if any(n in key.lower() for n in lowered):
                hits.append(child)
            hits.extend(find_key_paths(value, needles, child))
    elif isinstance(payload, list):
        for idx, value in enumerate(payload[:50]):
            hits.extend(find_key_paths(value, needles, f"{_path}[{idx}]"))
    return hits
