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
    retries, we record a human-readable error string in ``errors`` and fall back
    to the arctic_shift API for the remaining window; only when that fails too do
    we move on to the next subreddit -- ``collect()`` never raises on network
    trouble.
  * An optional ``keywords`` list filters records by content: a post/comment is
    kept only when at least one keyword appears (case-insensitive) in its
    title+selftext / body. Filtering happens BEFORE the caps are counted.
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

# arctic_shift (arctic-shift.photon-reddit.com) -- same Reddit record schema as
# pullpush, used as an automatic fallback when pullpush keeps failing.
AS_SUBMISSION_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
AS_COMMENT_URL = "https://arctic-shift.photon-reddit.com/api/comments/search"

# On Vercel the function is hard-killed at 300s (Hobby max), so default to a
# faster/less patient profile there and stop collecting before the platform
# kills us (TIME_BUDGET), returning partial results instead of a timeout error.
# Every knob can be overridden explicitly via its RTS_* env var.
_ON_VERCEL = bool(os.environ.get("VERCEL"))

PAGE_SIZE = 100          # size= per page (pullpush max)
# polite pause between requests, seconds
REQUEST_SLEEP = float(os.environ.get("RTS_SLEEP", "0.3" if _ON_VERCEL else "0.6"))
# per-request socket timeout, seconds
REQUEST_TIMEOUT = int(os.environ.get("RTS_TIMEOUT", "15" if _ON_VERCEL else "30"))
# tenacity attempts per request
MAX_ATTEMPTS = int(os.environ.get("RTS_MAX_ATTEMPTS", "3" if _ON_VERCEL else "6"))
# max exponential-backoff wait between attempts, seconds
RETRY_MAX_WAIT = int(os.environ.get("RTS_RETRY_MAX_WAIT", "5" if _ON_VERCEL else "60"))
# wall-clock budget for one collect() call, seconds; 0 = unlimited
TIME_BUDGET = float(os.environ.get("RTS_TIME_BUDGET", "250" if _ON_VERCEL else "0"))

_BUDGET_NOTE = "server time budget reached; results are partial"

# A shared session keeps the TCP connection warm across pages.
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "reddit-to-spreadsheet/1.0 (pullpush client)"})


class _RetryableHTTPError(Exception):
    """Raised for HTTP 429/5xx so tenacity retries the request."""


# --- Low-level fetch (retried) --------------------------------------------

@retry(
    reraise=True,
    stop=stop_after_attempt(MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=1, max=RETRY_MAX_WAIT),
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
    if isinstance(payload, list):  # tolerate a bare-list response
        return payload
    data = payload.get("data", []) if isinstance(payload, dict) else []
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


# --- Keyword (topic) filtering ---------------------------------------------

def _matches(record: dict, keywords: list[str] | None) -> bool:
    """True when the record's text contains any keyword (or no filter is set).

    Keywords are expected pre-lowercased (``collect()`` does this once).
    """
    if not keywords:
        return True
    if record["kind"] == "post":
        text = f"{record.get('title') or ''} {record.get('selftext') or ''}"
    else:
        text = record.get("body") or ""
    text = text.lower()
    return any(kw in text for kw in keywords)


# --- Per-subreddit backwards paging ---------------------------------------

def _sources_for(kind: str) -> list[tuple[str, str]]:
    """Ordered (name, url) data sources for one kind: pullpush, then fallback."""
    if kind == "post":
        return [("pullpush", SUBMISSION_URL), ("arctic_shift", AS_SUBMISSION_URL)]
    return [("pullpush", COMMENT_URL), ("arctic_shift", AS_COMMENT_URL)]


def _page_params(source: str, subreddit: str, before: int, after: int) -> dict:
    """Query params for one page. Both APIs page backwards via before/after."""
    if source == "pullpush":
        return {"subreddit": subreddit, "size": PAGE_SIZE, "before": before, "after": after}
    # arctic_shift: newest-first so the shared cursor stepping works unchanged.
    # limit=auto returns 100-1000 rows per page depending on server capacity --
    # roughly 10x pullpush's throughput, which is what makes big pulls viable.
    return {
        "subreddit": subreddit, "limit": "auto",
        "before": before, "after": after, "sort": "desc",
    }


def _collect_kind(
    subreddit: str,
    kind: str,
    start_ts: int,
    end_ts: int,
    cap: int | None,
    exclude_usernames: bool,
    keywords: list[str] | None,
    deadline: float | None,
    progress,
    errors: list[str],
) -> tuple[list[dict], dict]:
    """
    Page one kind (post or comment) for one subreddit, backwards in time from
    ``end_ts`` down to ``start_ts``. Starts on pullpush; if a page fails even
    after retries, continues from the same cursor on arctic_shift.

    A source also gets swapped out when it answers the very first page(s) with
    NO rows at all: pullpush is sometimes "up" but serving an empty index, which
    is indistinguishable from a genuinely quiet window -- so before believing
    an empty result we ask the next source.

    Termination -- the loop stops on the FIRST of:
      * an empty page on the LAST source, or after any rows have been seen,
      * a row whose created_utc < start_ts (we've walked past the window),
      * the per-sub ``cap`` being reached,
      * a page failing even after retries on the LAST source (error recorded).

    Returns ``(records, state)`` where ``state`` is
    ``{"next_before": int, "done": bool}`` -- ``done`` is True when the window
    is genuinely exhausted; otherwise a follow-up call with
    ``end_ts=next_before`` resumes where this one stopped (the chunked
    deep-pull API relies on this).
    """
    normalize = _norm_post if kind == "post" else _norm_comment
    sources = _sources_for(kind)
    src_idx = 0
    records: list[dict] = []
    seen_ids: set[str] = set()
    before = end_ts
    raw_rows_seen = 0  # in-window rows fetched so far, across all sources
    exhausted = False  # True only when the window has no more data

    while True:
        if cap is not None and len(records) >= cap:
            break

        # Out of wall-clock time (e.g. Vercel's 300s kill) -> stop cleanly.
        if deadline is not None and time.monotonic() >= deadline:
            what = "stopped early" if raw_rows_seen else "skipped"
            errors.append(f"{subreddit}/{kind}s: {what} -- {_BUDGET_NOTE}")
            break

        src_name, url = sources[src_idx]
        params = _page_params(src_name, subreddit, before, start_ts)

        try:
            rows = _fetch(url, params)
        except Exception as exc:  # noqa: BLE001 - deliberate: never crash collect()
            errors.append(
                f"{subreddit}/{kind}s: {src_name} gave up after {MAX_ATTEMPTS} "
                f"attempts (before={before}): {type(exc).__name__}: {exc}"
            )
            if src_idx + 1 < len(sources):
                src_idx += 1
                errors.append(
                    f"{subreddit}/{kind}s: falling back to {sources[src_idx][0]} "
                    f"for the rest of the window (before={before})"
                )
                if progress:
                    progress(
                        f"  {subreddit}: {src_name} failing, switching to "
                        f"{sources[src_idx][0]} for {kind}s"
                    )
                continue
            break

        # Polite pause between requests.
        time.sleep(REQUEST_SLEEP)

        # Empty page -> nothing left in range... unless this source never
        # produced a single row, in which case double-check on the next one.
        if not rows:
            if raw_rows_seen == 0 and src_idx + 1 < len(sources):
                src_idx += 1
                if progress:
                    progress(
                        f"  {subreddit}: {src_name} returned no {kind}s, "
                        f"double-checking on {sources[src_idx][0]}"
                    )
                continue
            exhausted = True
            break

        raw_rows_seen += len(rows)

        page_min: int | None = None
        past_start = False
        cap_hit = False

        for raw in rows:
            cu = raw.get("created_utc")
            if cu is None:
                continue
            cu = int(cu)
            if page_min is None or cu < page_min:
                page_min = cu

            # Walked past the start of the window; stop after this page.
            if cu < start_ts:
                past_start = True
                continue

            rid = str(raw.get("id", ""))
            if rid and rid in seen_ids:
                continue
            if rid:
                seen_ids.add(rid)

            record = normalize(raw, subreddit, exclude_usernames)
            if not _matches(record, keywords):
                continue
            records.append(record)

            if cap is not None and len(records) >= cap:
                cap_hit = True
                break

        if progress:
            progress(f"  {subreddit}: {len(records)} {kind}s so far")

        if cap_hit:
            break  # cap reached mid-page; the window may hold more
        if past_start or page_min is None:
            exhausted = True
            break

        # Step the window down. `before` is exclusive on pullpush, so setting it
        # to the page minimum walks strictly further back each iteration. Guard
        # against a stuck cursor (e.g. many rows sharing one timestamp).
        next_before = page_min
        if next_before >= before:
            next_before = before - 1
        if next_before < start_ts:
            exhausted = True
            break
        before = next_before

    return records, {"next_before": before, "done": exhausted}


# --- Synthetic test data (RTS_FAKE=1) -------------------------------------

def _fake_dataset(
    subreddits: list[str],
    start_ts: int,
    end_ts: int,
    include_comments: bool,
    max_posts_per_sub: int | None,
    max_comments_per_sub: int | None,
    exclude_usernames: bool,
    keywords: list[str] | None,
    filter_exempt: set[str],
    progress,
) -> dict:
    """
    Deterministic offline dataset: 3 posts + 5 comments per subreddit, all
    timestamped inside the requested window. Exercises author normalization
    (one deleted author per group) and respects caps, include_comments, and
    the keyword filter (applied through the same ``_matches`` helper, with
    ``filter_exempt`` subreddits skipping it just like the live path).
    """
    posts: list[dict] = []
    comments: list[dict] = []
    span = max(end_ts - start_ts, 1)

    for si, sub in enumerate(subreddits):
        if progress:
            progress(f"[fake] generating data for r/{sub}")
        kw = None if sub.lower() in filter_exempt else keywords

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
            record = _norm_post(raw, sub, exclude_usernames)
            if _matches(record, kw):
                posts.append(record)

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
                record = _norm_comment(raw, sub, exclude_usernames)
                if _matches(record, kw):
                    comments.append(record)

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
    keywords: list[str] | None = None,
    filter_exempt: set[str] | None = None,
    progress=None,
) -> dict:
    """
    Collect posts (and optionally comments) for each subreddit, from pullpush.io
    with an automatic per-subreddit fallback to arctic_shift.

    ``keywords``, when given, keeps only records whose text contains at least
    one keyword (case-insensitive); caps count the records that MATCH.
    ``filter_exempt`` names subreddits (case-insensitive) whose records skip
    the keyword filter entirely -- communities already dedicated to the topic.

    Returns ``{"posts": [...], "comments": [...], "errors": [...]}``. Never
    raises on network failure -- a subreddit/endpoint that keeps failing after
    retries (on both sources) contributes entries to ``errors`` and collection
    continues.

    ``progress``, if supplied, is called as ``progress(str)`` for logging.
    """
    # Lowercase the filter once; _matches() assumes pre-lowercased keywords.
    keywords = [k.lower() for k in keywords if k and k.strip()] if keywords else None
    exempt = {s.lower() for s in filter_exempt} if filter_exempt else set()

    # Offline test hook: return deterministic synthetic data, no network.
    if os.environ.get("RTS_FAKE") == "1":
        return _fake_dataset(
            subreddits, start_ts, end_ts, include_comments,
            max_posts_per_sub, max_comments_per_sub, exclude_usernames,
            keywords, exempt, progress,
        )

    posts: list[dict] = []
    comments: list[dict] = []
    errors: list[str] = []

    # One task per (subreddit, kind), in the order they run.
    tasks: list[tuple[str, str, int | None]] = []
    for sub in subreddits:
        sub = sub.strip().lstrip("/").removeprefix("r/").strip("/")
        if not sub:
            continue
        tasks.append((sub, "post", max_posts_per_sub))
        if include_comments:
            tasks.append((sub, "comment", max_comments_per_sub))

    # Wall-clock budget (0/unset = no deadline). On Vercel this stops
    # collection ~50s before the platform's 300s hard kill so the workbook and
    # zip still get built and the user receives partial data, not an error.
    # The budget is split FAIRLY across tasks: task i must finish by
    # t0 + budget*(i+1)/N, so one huge subreddit's posts cannot starve every
    # comment task behind it. A task that finishes early rolls its unused time
    # forward to the tasks after it.
    t0 = time.monotonic()

    for i, (sub, kind, cap) in enumerate(tasks):
        deadline = (
            t0 + TIME_BUDGET * (i + 1) / len(tasks) if TIME_BUDGET > 0 else None
        )
        if progress:
            progress(f"Collecting {kind}s for r/{sub} ...")
        # Dedicated communities skip the topic filter -- everything is on-topic.
        kw = None if sub.lower() in exempt else keywords
        found, _state = _collect_kind(
            sub, kind, start_ts, end_ts,
            cap, exclude_usernames, kw, deadline, progress, errors,
        )
        (posts if kind == "post" else comments).extend(found)

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
