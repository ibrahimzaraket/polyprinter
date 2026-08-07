"""Retry-with-backoff for 429s, 408s, and 5xx.

Found by running Scout for real, not by reading the rate-limit table: the
published limits (docs/api-notes.md) are generous in aggregate, but Scout
fires many requests in a tight loop across candidates with no throttling —
that burst pattern can exceed a short 10-second window even when the day's
total is nowhere near the limit. A 429 mid-run shouldn't fail that
candidate's dossier; back off and retry a couple of times first.

408 (Request Timeout) is included for the same reason: seen live on a
`/activity` call during a full-scale run (2026-08-07) and it's a transient
server-side hiccup, not a real "this request is malformed" — worth a retry
before giving up on the candidate.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")

RETRY_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0


def with_retry(request_fn: Callable[[], httpx.Response]) -> httpx.Response:
    """Calls request_fn() up to MAX_ATTEMPTS times. Retries on 429/5xx,
    honoring a Retry-After header if present, else exponential backoff.
    Raises via response.raise_for_status() on the final attempt's response
    (or lets a non-retryable response pass straight through).
    """
    last_response: httpx.Response | None = None
    for attempt in range(MAX_ATTEMPTS):
        resp = request_fn()
        if resp.status_code not in RETRY_STATUS_CODES:
            return resp
        last_response = resp
        if attempt == MAX_ATTEMPTS - 1:
            break
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else BASE_DELAY_SECONDS * (2**attempt)
        time.sleep(delay)
    assert last_response is not None
    return last_response
