"""
collector.py -- pullpush.io client for reddit-to-spreadsheet.

Fetches Reddit submissions and comments for one or more subreddits from the
public pullpush.io API and normalizes them into the plain-dict record schemas
shared across the app (see SHARED CONTRACT).

Design notes:
  * Paging walks BACKWARDS in time. We start with ``before=end_ts`` and, after
    each page, set ``before`` to the smallest ``created_utc`` we just saw, so we
    step down towards ``start_ts`` (which is passed as ``after`` on every call).
  * pullpush is flaky and frequently answers 502. All network reads go through a
    tenacity exponential-backoff retry (~6 attempts) that retries on ``requests``
    transport errors and on HTTP 429/5xx. If a page STILL fails after the
    retries, we append a human-readable error string to ``errors`` and move on to
    the next subreddit -- ``collect()`` never raises on network trouble.
  * We sleep ~0.6s between requests to stay polite.
  * Setting the env var ``RTS_FAKE=1`` bypasses the network entirely and returns
    a small deterministic synthetic set, so the whole app can be tested offline.
"""

from __future__ import annotations

import os
import time

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# --- Configuration ---------------------------------------------------------

SUBMISSION_URL = "https://api.pullpush.io/reddit/search/submission/"
COMMENT_URL = "https://api.pullpush.io/reddit/search/comment/"

PAGE_SIZE = 100          # size= per page (pullpush max)
REQUEST_SLEEP = 0.6      # polite pause between requests, seconds
REQUEST_TIMEOUT = 30     # per-request socket timeout, seconds
MAX_ATTEMPTS = 6         # tenacity attempts per request

# A shared session keeps the TCP connection warm across pages.
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "reddit-to-spreadsheet/1.0 (pullpush client)"})


class _RetryableHTTPError(Exception):
    """Raised for HTTP 429/5xx so tenacity retries the request."""


# --- Low-level fetch (retried) --------------------------------------------

@retry(
    reraise=True,
    stop=stop_after_attempt(MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    retry=retry_if_exception_type(
        (requests.exceptions.RequestException, _RetryableHTTPError)
    ),
)
def _fetch(url: str, params: dict) -> list[dict]:
    """
    Perform one GET and return the raw ``data`` list from pullpush.

    Retries (via tenacity) on ``requests`` transport errors and on HTTP 429/5xx
    -- pullpush loves to answer 502. Other 4xx responses raise a non-retryable
    ``RuntimeError`` that surfaces immediately. On success returns the (possibly
    empty) list of raw row dicts.
    """
    resp = _SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)

    # Rate-limited or server-side hiccup -> ask tenacity to retry.
    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        raise _RetryableHTTPError(f"HTTP {resp.status_code}")
    # Any other client error is not going to fix itself; fail fast (not retried).
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")

    payload = resp.json()
    data = payload.get("data", [])
    return data if isinstance(data, list) else []


# --- Normalization ---------------------------------------------------------

def _norm_author(raw: dict, exclude_usernames: bool) -> str | None:
    """Return a clean author, or None for deleted/missing/excluded authors."""
    if exclude_usernames:
        return None
    author = raw.get("author")
    if not author or author == "[deleted]" or author == "[removed]":
        return None
    return author


def _norm_post(raw: dict, subreddit: str, exclude_usernames: bool) -> dict:
    return {
        "kind": "post",
        "subreddit": raw.get("subreddit", subreddit),
        "id": str(raw.get("id", "")),
        "created_utc": int(raw.get("created_utc", 0)),
        "author": _norm_author(raw, exclude_usernames),
        "title": raw.get("title") or "",
        "selftext": raw.get("selftext") or "",
        "score": int(raw.get("score", 0) or 0),
        "num_comments": int(raw.get("num_comments", 0) or 0),
        "permalink": raw.get("permalink") or "",
    }


def _norm_comment(raw: dict, subreddit: str, exclude_usernames: bool) -> dict:
    return {
        "kind": "comment",
        "subreddit": raw.get("subreddit", subreddit),
        "id": str(raw.get("id", "")),
        "created_utc": int(raw.get("created_utc", 0)),
        "author": _norm_author(raw, exclude_usernames),
        "body": raw.get("body") or "",
        "score": int(raw.get("score", 0) or 0),
        "link_id": raw.get("link_id") or "",
        "parent_id": raw.get("parent_id") or "",
        "permalink": raw.get("permalink") or "",
    }


# --- Per-subreddit backwards paging ---------------------------------------

def _collect_kind(
    subreddit: str,
    url: str,
    kind: str,
    start_ts: int,
    end_ts: int,
    cap: int | None,
    exclude_usernames: bool,
    progress,
    errors: list[str],
) -> list[dict]:
    """
    Page one endpoint (submission or comment) for one subreddit, backwards in
    time from ``end_ts`` down to ``start_ts``.

    Termination -- the loop stops on the FIRST of:
      * an empty page (no more data in range),
      * a row whose created_utc < start_ts (we've walked past the window),
      * the per-sub ``cap`` being reached,
      * a page that fails even after retries (error recorded, loop breaks).
    """
    normalize = _norm_post if kind == "post" else _norm_comment
    records: list[dict] = []
    seen_ids: set[str] = set()
    before = end_ts

    while True:
        if cap is not None and len(records) >= cap:
            break

        params = {
            "subreddit": subreddit,
            "size": PAGE_SIZE,
            "before": before,
            "after": start_ts,
        }

        try:
            rows = _fetch(url, params)
        except Exception as exc:  # noqa: BLE001 - deliberate: never crash collect()
            errors.append(
                f"{subreddit}/{kind}s: gave up after {MAX_ATTEMPTS} attempts "
                f"(before={before}): {type(exc).__name__}: {exc}"
            )
            break

        # Polite pause between requests.
        time.sleep(REQUEST_SLEEP)

        # Empty page -> nothing left in range.
        if not rows:
            break

        page_min: int | None = None
        stop = False

        for raw in rows:
            cu = raw.get("created_utc")
            if cu is None:
                continue
            cu = int(cu)
            if page_min is None or cu < page_min:
                page_min = cu

            # Walked past the start of the window; stop after this page.
            if cu < start_ts:
                stop = True
                continue

            rid = str(raw.get("id", ""))
            if rid and rid in seen_ids:
                continue
            if rid:
                seen_ids.add(rid)

            records.append(normalize(raw, subreddit, exclude_usernames))

            if cap is not None and len(records) >= cap:
                stop = True
                break

        if progress:
            progress(f"  {subreddit}: {len(records)} {kind}s so far")

        if stop or page_min is None:
            break

        # Step the window down. `before` is exclusive on pullpush, so setting it
        # to the page minimum walks strictly further back each iteration. Guard
        # against a stuck cursor (e.g. many rows sharing one timestamp).
        next_before = page_min
        if next_before >= before:
            next_before = before - 1
        if next_before < start_ts:
            break
        before = next_before

    return records


# --- Synthetic test data (RTS_FAKE=1) -------------------------------------

def _fake_dataset(
    subreddits: list[str],
    start_ts: int,
    end_ts: int,
    include_comments: bool,
    max_posts_per_sub: int | None,
    max_comments_per_sub: int | None,
    exclude_usernames: bool,
    progress,
) -> dict:
    """
    Deterministic offline dataset: 3 posts + 5 comments per subreddit, all
    timestamped inside the requested window. Exercises author normalization
    (one deleted author per group) and respects caps + include_comments.
    """
    posts: list[dict] = []
    comments: list[dict] = []
    span = max(end_ts - start_ts, 1)

    for si, sub in enumerate(subreddits):
        if progress:
            progress(f"[fake] generating data for r/{sub}")

        n_posts = 3 if max_posts_per_sub is None else min(3, max_posts_per_sub)
        for i in range(n_posts):
            created = start_ts + (span * (i + 1)) // (n_posts + 1)
            raw = {
                "subreddit": sub,
                "id": f"fp_{si}_{i}",
                "created_utc": created,
                # Every 3rd author is deleted -> should normalize to None.
                "author": "[deleted]" if i == 2 else f"user_{si}_{i}",
                "title": f"[{sub}] Synthetic post #{i}",
                "selftext": f"This is fake body text for post {i} in r/{sub}.",
                "score": 10 * (i + 1),
                "num_comments": 5,
                "permalink": f"/r/{sub}/comments/fp_{si}_{i}/synthetic_post_{i}/",
            }
            posts.append(_norm_post(raw, sub, exclude_usernames))

        if include_comments:
            n_comments = 5 if max_comments_per_sub is None else min(5, max_comments_per_sub)
            for j in range(n_comments):
                created = start_ts + (span * (j + 1)) // (n_comments + 1)
                raw = {
                    "subreddit": sub,
                    "id": f"fc_{si}_{j}",
                    "created_utc": created,
                    "author": "[deleted]" if j == 4 else f"commenter_{si}_{j}",
                    "body": f"Fake comment {j} on a post in r/{sub}.",
                    "score": j + 1,
                    "link_id": f"t3_fp_{si}_0",
                    "parent_id": f"t3_fp_{si}_0",
                    "permalink": f"/r/{sub}/comments/fp_{si}_0/synthetic_post_0/fc_{si}_{j}/",
                }
                comments.append(_norm_comment(raw, sub, exclude_usernames))

    return {"posts": posts, "comments": comments, "errors": []}


# --- Public API ------------------------------------------------------------

def collect(
    subreddits: list[str],
    start_ts: int,
    end_ts: int,
    include_comments: bool = True,
    max_posts_per_sub: int | None = 500,
    max_comments_per_sub: int | None = 2000,
    exclude_usernames: bool = False,
    progress=None,
) -> dict:
    """
    Collect posts (and optionally comments) for each subreddit from pullpush.io.

    Returns ``{"posts": [...], "comments": [...], "errors": [...]}``. Never
    raises on network failure -- a subreddit/endpoint that keeps failing after
    retries contributes an entry to ``errors`` and collection continues.

    ``progress``, if supplied, is called as ``progress(str)`` for logging.
    """
    # Offline test hook: return deterministic synthetic data, no network.
    if os.environ.get("RTS_FAKE") == "1":
        return _fake_dataset(
            subreddits, start_ts, end_ts, include_comments,
            max_posts_per_sub, max_comments_per_sub, exclude_usernames, progress,
        )

    posts: list[dict] = []
    comments: list[dict] = []
    errors: list[str] = []

    for sub in subreddits:
        sub = sub.strip().lstrip("/").removeprefix("r/").strip("/")
        if not sub:
            continue

        if progress:
            progress(f"Collecting posts for r/{sub} ...")
        posts.extend(
            _collect_kind(
                sub, SUBMISSION_URL, "post", start_ts, end_ts,
                max_posts_per_sub, exclude_usernames, progress, errors,
            )
        )

        if include_comments:
            if progress:
                progress(f"Collecting comments for r/{sub} ...")
            comments.extend(
                _collect_kind(
                    sub, COMMENT_URL, "comment", start_ts, end_ts,
                    max_comments_per_sub, exclude_usernames, progress, errors,
                )
            )

    if progress:
        progress(
            f"Done: {len(posts)} posts, {len(comments)} comments, "
            f"{len(errors)} error(s)."
        )

    return {"posts": posts, "comments": comments, "errors": errors}


# --- Guarded demo ----------------------------------------------------------

if __name__ == "__main__":
    # Tiny, guarded demo. By default it runs OFFLINE using the synthetic hook so
    # nobody accidentally hammers pullpush. Set RTS_LIVE=1 to hit the network
    # with a small one-hour slice and tiny caps.
    import json

    now = int(time.time())
    window_start = now - 3600  # last hour
    window_end = now

    if os.environ.get("RTS_LIVE") == "1":
        print("LIVE demo: fetching a tiny slice from pullpush.io ...")
        result = collect(
            ["endometriosis"],
            window_start,
            window_end,
            include_comments=True,
            max_posts_per_sub=5,
            max_comments_per_sub=5,
            progress=print,
        )
    else:
        print("OFFLINE demo (set RTS_LIVE=1 for a real request):")
        os.environ["RTS_FAKE"] = "1"
        result = collect(
            ["endometriosis", "PCOS"],
            window_start,
            window_end,
            progress=print,
        )

    print(
        f"\nposts={len(result['posts'])} "
        f"comments={len(result['comments'])} "
        f"errors={len(result['errors'])}"
    )
    if result["posts"]:
        print("sample post:", json.dumps(result["posts"][0], indent=2))
    if result["errors"]:
        print("errors:", result["errors"])
