"""Flask app for reddit-to-spreadsheet.

Wires together the three building blocks:
  * ``subreddits`` -- the curated starter list shown in the picker UI
  * ``collector``  -- pulls posts/comments from pullpush.io
  * ``spreadsheet``-- turns those records into an .xlsx workbook

Routes
------
GET  /                serve the single-page UI (static/index.html)
GET  /api/subreddits  JSON {"categories": {...}} for the picker
POST /api/collect     collect data and stream back an .xlsx download
GET  /static/*        static assets (served automatically by Flask)

Run directly (``python app.py``) to start a local server on
http://127.0.0.1:5000 with debug turned off.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

import collector
import spreadsheet
import subreddits

# XLSX MIME type used both for the response Content-Type and by the tests.
XLSX_MIMETYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# One day in seconds -- used to make the end date *exclusive* (see below).
_ONE_DAY = 86400

# Sane upper bounds so a caller cannot ask us to page forever.
_MAX_CAP = 100_000

app = Flask(__name__, static_folder="static", static_url_path="/static")
# Preserve THEMES insertion order in JSON (Flask alphabetizes keys by default),
# so themes stay in the curated order (Women's health first).
app.json.sort_keys = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class ValidationError(Exception):
    """Raised for bad client input -> surfaced to the caller as HTTP 400."""


def _parse_date(value, field):
    """Parse a ``YYYY-MM-DD`` string into a UTC ``datetime`` (midnight)."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"'{field}' is required (YYYY-MM-DD).")
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        raise ValidationError(
            f"'{field}' must be a valid date in YYYY-MM-DD format."
        )
    return d.replace(tzinfo=timezone.utc)


def _parse_cap(value, field, default):
    """Validate an optional integer cap. ``None``/missing -> ``default``."""
    if value is None:
        return default
    # Reject bools (bool is a subclass of int) and non-numeric junk.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"'{field}' must be a positive integer.")
    if value < 1 or value > _MAX_CAP:
        raise ValidationError(
            f"'{field}' must be between 1 and {_MAX_CAP}."
        )
    return value


def _clean_subreddits(value):
    """Normalise the requested subreddit list; raise if effectively empty."""
    if not isinstance(value, list):
        raise ValidationError("'subreddits' must be a non-empty list.")
    cleaned = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip().lstrip("/")  # tolerate "r/foo" and "/r/foo"
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        cleaned.append(name)
    if not cleaned:
        raise ValidationError("Select at least one subreddit.")
    return cleaned


def _safe_filename(subs):
    """Build a filesystem/header safe .xlsx filename from the sub names."""
    parts = []
    for name in subs:
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
        if safe:
            parts.append(safe)
    joined = "-".join(parts) if parts else "export"
    if len(joined) > 80:  # keep the Content-Disposition header sensible
        joined = f"{joined[:80]}_and_more"
    return f"reddit_export_{joined}.xlsx"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    """Serve the single-page UI."""
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/subreddits")
def api_subreddits():
    """Return condition themes, the autocomplete pool, and starter suggestions."""
    return jsonify(
        {
            "themes": subreddits.THEMES,
            "pool": subreddits.POOL,
            "popular": subreddits.POPULAR,
        }
    )


@app.post("/api/collect")
def api_collect():
    """Validate input, collect the data, and return an .xlsx download."""
    # --- parse + validate input (any failure -> HTTP 400) ----------------- #
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValidationError("Request body must be a JSON object.")

        subs = _clean_subreddits(payload.get("subreddits"))

        start_dt = _parse_date(payload.get("start_date"), "start_date")
        end_dt = _parse_date(payload.get("end_date"), "end_date")
        if end_dt < start_dt:
            raise ValidationError("'start_date' must be on or before 'end_date'.")

        # Convert to unix timestamps. The end date is treated as *exclusive*:
        # we add one day so the whole of end_date is included in the half-open
        # window [start_ts, end_ts).
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp()) + _ONE_DAY

        include_comments = bool(payload.get("include_comments", True))
        exclude_usernames = bool(payload.get("exclude_usernames", False))
        max_posts = _parse_cap(
            payload.get("max_posts_per_sub"), "max_posts_per_sub", 500
        )
        max_comments = _parse_cap(
            payload.get("max_comments_per_sub"), "max_comments_per_sub", 2000
        )
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    # --- collect + build workbook (any failure -> HTTP 500) --------------- #
    started = time.monotonic()
    try:
        result = collector.collect(
            subs,
            start_ts,
            end_ts,
            include_comments=include_comments,
            max_posts_per_sub=max_posts,
            max_comments_per_sub=max_comments,
            exclude_usernames=exclude_usernames,
            progress=lambda msg: app.logger.info("collect: %s", msg),
        )
        posts = result.get("posts", [])
        comments = result.get("comments", [])
        errors = result.get("errors", [])
        xlsx_bytes = spreadsheet.build_workbook_bytes(posts, comments)
    except Exception as exc:  # noqa: BLE001 - any collect failure is a 500
        app.logger.exception("collection failed")
        return jsonify({"error": f"Collection failed: {exc}"}), 500

    elapsed = time.monotonic() - started
    app.logger.info(
        "collect done: %d posts, %d comments, %d error(s) in %.1fs (%s)",
        len(posts), len(comments), len(errors), elapsed, ",".join(subs),
    )

    filename = _safe_filename(subs)
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        # Surface per-subreddit (non-fatal) errors + counts + timing to the UI.
        "X-Collect-Errors": str(len(errors)),
        "X-Collect-Posts": str(len(posts)),
        "X-Collect-Comments": str(len(comments)),
        "X-Collect-Seconds": f"{elapsed:.1f}",
    }
    return Response(xlsx_bytes, mimetype=XLSX_MIMETYPE, headers=headers)


if __name__ == "__main__":
    # Show INFO logs (incl. the per-request "collect done ... in N.Ns" timing).
    import logging

    logging.basicConfig(level=logging.INFO)
    app.logger.setLevel(logging.INFO)
    # Local-only server; debug off per the project contract.
    app.run(host="127.0.0.1", port=5000, debug=False)
