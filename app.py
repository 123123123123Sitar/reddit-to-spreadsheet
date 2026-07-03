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

import json
import os
import pathlib
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import collector
import spreadsheet
import subreddits

# --- Mercury 2 (Inception Labs) suggestion API ----------------------------- #
MERCURY_URL = "https://api.inceptionlabs.ai/v1/chat/completions"
MERCURY_MODEL = "mercury-2"
# Cache suggestions per selection set (local single-process dev server).
_SUGGEST_CACHE: dict[frozenset, dict] = {}


def _mercury_key() -> str | None:
    """Load the Mercury key from the env or a local key file (never committed)."""
    key = os.environ.get("MERCURY_API_KEY")
    if key:
        return key.strip()
    for p in (
        pathlib.Path(__file__).resolve().parent / "mercury_key.txt",
        pathlib.Path.home() / "mercury_key.txt",
    ):
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return None


def _mercury_suggest(selected: list[str]) -> list[dict] | None:
    """Ask Mercury 2 for related health communities. None on skip/failure."""
    if os.environ.get("RTS_FAKE") == "1":
        return None  # offline / tests -> use the static fallback
    key = _mercury_key()
    if not key:
        return None

    if selected:
        ask = (
            "The user selected these health subreddits: " + ", ".join(selected) +
            ". Suggest 14 closely related health or patient-support subreddits in the "
            "same condition domains, excluding the ones already selected."
        )
    else:
        ask = (
            "Suggest 14 popular health / patient-support subreddits spanning common "
            "conditions (women's health, cancer, autoimmune, diabetes and endocrine, "
            "digestive, chronic pain, respiratory, mental health)."
        )
    prompt = (
        ask + " Use only real subreddits. For each, give the exact subreddit name "
        "(no r/ prefix) and approx_posts, a rough integer estimate of how many posts "
        'the subreddit has. Respond as JSON: '
        '{"suggestions":[{"name":"...","approx_posts":12345}]}'
    )
    try:
        resp = requests.post(
            MERCURY_URL,
            timeout=20,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": MERCURY_MODEL,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You know Reddit health and patient-support communities. Return only real subreddits, as JSON."},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except Exception:  # noqa: BLE001 - any failure just falls back
        app.logger.exception("mercury suggest failed")
        return None

    out: list[dict] = []
    for item in data.get("suggestions", []) if isinstance(data, dict) else []:
        name = str(item.get("name", "")).strip().lstrip("/")
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip("/").strip()
        if not name:
            continue
        posts = item.get("approx_posts")
        posts = int(posts) if isinstance(posts, (int, float)) and posts > 0 else None
        out.append({"name": name, "posts": posts})
    return out or None


def _fallback_suggest(selected_lower: set[str]) -> list[dict]:
    """Static theme-based suggestions when Mercury is unavailable (no counts)."""
    sub_themes: dict[str, list[str]] = {}
    for theme, subs in subreddits.THEMES.items():
        for s in subs:
            sub_themes.setdefault(s.lower(), []).append(theme)

    if selected_lower:
        active = {t for s in selected_lower for t in sub_themes.get(s, [])}
        names, seen = [], set()
        for theme, subs in subreddits.THEMES.items():
            if theme in active:
                for s in subs:
                    if s.lower() not in selected_lower and s.lower() not in seen:
                        seen.add(s.lower())
                        names.append(s)
        if not names:
            names = [p for p in subreddits.POPULAR if p.lower() not in selected_lower]
    else:
        names = list(subreddits.POPULAR)
    return [{"name": n, "posts": None} for n in names]


# --- Chat: free-text condition -> relevant subreddits ---------------------- #
_THEME_KEYWORDS = {
    "women": "Women's health", "gyneco": "Women's health", "reproduct": "Women's health",
    "endo": "Women's health", "fertil": "Women's health", "menopause": "Women's health",
    "cancer": "Cancer", "tumor": "Cancer", "oncolog": "Cancer", "leukemia": "Cancer",
    "lymphoma": "Cancer", "autoimmun": "Autoimmune & rheumatic", "lupus": "Autoimmune & rheumatic",
    "arthrit": "Autoimmune & rheumatic", "rheumat": "Autoimmune & rheumatic",
    "diabet": "Diabetes & endocrine", "insulin": "Diabetes & endocrine",
    "thyroid": "Diabetes & endocrine", "endocrine": "Diabetes & endocrine",
    "gut": "Digestive & GI", "bowel": "Digestive & GI", "digest": "Digestive & GI",
    "ibs": "Digestive & GI", "crohn": "Digestive & GI", "colitis": "Digestive & GI",
    "pain": "Chronic pain & neurological", "migrain": "Chronic pain & neurological",
    "fibro": "Chronic pain & neurological", "neuro": "Chronic pain & neurological",
    "asthma": "Respiratory", "lung": "Respiratory", "breath": "Respiratory",
    "copd": "Respiratory", "mental": "Mental health", "depress": "Mental health",
    "anxiety": "Mental health", "adhd": "Mental health", "bipolar": "Mental health",
}


def _mercury_chat(message: str) -> dict | None:
    """Map a free-text condition description to subreddits via Mercury 2."""
    if os.environ.get("RTS_FAKE") == "1":
        return None
    key = _mercury_key()
    if not key:
        return None
    prompt = (
        'A researcher wrote: "' + message + '". Identify the health condition(s) '
        "they mean and list the most relevant real health / patient-support "
        "subreddits to analyze for it (between 5 and 12, exact names, no r/ prefix). "
        "Also write a one-sentence friendly reply naming the condition and how many "
        'communities you selected. Respond as JSON: '
        '{"reply":"...","subreddits":["..."]}'
    )
    try:
        resp = requests.post(
            MERCURY_URL,
            timeout=20,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": MERCURY_MODEL,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You help researchers find real Reddit health and patient-support communities. Respond as JSON."},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:  # noqa: BLE001 - any failure falls back
        app.logger.exception("mercury chat failed")
        return None
    if not isinstance(data, dict):
        return None
    subs = data.get("subreddits")
    return {
        "reply": str(data.get("reply", "")),
        "subreddits": subs if isinstance(subs, list) else [],
    }


def _fallback_chat(message: str) -> dict:
    """Keyword match a condition description to subreddits when Mercury is off."""
    msg = message.lower()
    names, seen = [], set()

    # Directly named communities.
    for n in subreddits.POOL:
        if len(n) >= 4 and n.lower() in msg and n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)

    # Expand to the themes those hits (and any keyword) belong to.
    sub_themes: dict[str, list[str]] = {}
    for theme, subs in subreddits.THEMES.items():
        for s in subs:
            sub_themes.setdefault(s.lower(), []).append(theme)
    active = {t for n in names for t in sub_themes.get(n.lower(), [])}
    for kw, theme in _THEME_KEYWORDS.items():
        if kw in msg:
            active.add(theme)
    for theme in subreddits.THEMES:
        if theme in active:
            for s in subreddits.THEMES[theme]:
                if s.lower() not in seen:
                    seen.add(s.lower())
                    names.append(s)

    names = names[:14]
    if names:
        reply = f"Selected {len(names)} communities matching your description."
    else:
        reply = "I couldn't map that to a community — try naming a condition, e.g. lupus."
    return {"reply": reply, "subreddits": names}

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


@app.post("/api/suggest")
def api_suggest():
    """Suggest related health communities (Mercury 2, with a static fallback)."""
    payload = request.get_json(silent=True) or {}
    raw = payload.get("selected")
    selected, seen = [], set()
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, str):
            continue
        name = x.strip().lstrip("/")
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip("/").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            selected.append(name)

    cache_key = frozenset(seen)
    if cache_key in _SUGGEST_CACHE:
        return jsonify(_SUGGEST_CACHE[cache_key])

    suggestions = _mercury_suggest(selected)
    source = "mercury"
    if not suggestions:
        suggestions = _fallback_suggest(seen)
        source = "fallback"

    # Never suggest something already selected; de-dupe and cap.
    out, picked = [], set()
    for s in suggestions:
        low = s["name"].lower()
        if low in seen or low in picked:
            continue
        picked.add(low)
        out.append(s)
        if len(out) >= 16:
            break

    result = {"suggestions": out, "source": source}
    _SUGGEST_CACHE[cache_key] = result
    return jsonify(result)


@app.post("/api/chat")
def api_chat():
    """Turn a free-text condition description into subreddits to auto-select."""
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()[:500]
    if not message:
        return jsonify({"reply": "Tell me what condition you're researching.", "subreddits": []})

    result = _mercury_chat(message) or _fallback_chat(message)

    clean, seen = [], set()
    for n in result.get("subreddits", []):
        name = str(n).strip().lstrip("/")
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip("/").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            clean.append(name)

    return jsonify({"reply": result.get("reply", ""), "subreddits": clean[:16]})


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
