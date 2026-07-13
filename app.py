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
import re
import time
import urllib.parse
from datetime import datetime, timezone

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import bundle
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


# --- Topic filter: free text -> keyword list ------------------------------- #
_KEYWORD_STOPWORDS = {
    "a", "an", "and", "about", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "how", "i", "in", "is", "it", "its", "me", "my", "of", "on",
    "only", "or", "posts", "talk", "that", "the", "their", "them", "they",
    "thing", "things", "this", "to", "was", "were", "what", "when", "which",
    "will", "with",
}


def _mercury_topic_plan(topic: str, subs: list[str]) -> dict | None:
    """Ask Mercury 2 for search keywords AND which of the requested subreddits
    are already dedicated to the topic (those get exported unfiltered).

    Returns ``{"keywords": [...], "dedicated": {lowercase sub names}}`` or None.
    """
    if os.environ.get("RTS_FAKE") == "1":
        return None
    key = _mercury_key()
    if not key:
        return None
    prompt = (
        'A researcher only wants Reddit posts/comments about: "' + topic + '". '
        "1) List 8 to 15 short lowercase keywords or phrases to match such "
        "text: the core terms plus common synonyms, abbreviations, and "
        "everyday patient wording. Text matching ANY keyword "
        "(case-insensitive substring) is kept, so keep each keyword specific "
        "to the topic. "
        "2) They are collecting from these subreddits: " + ", ".join(subs) + ". "
        "Name the ones that are ENTIRELY dedicated to that topic (nearly "
        "every post there is about it, so keyword filtering would only lose "
        "relevant posts). A broader community that merely includes the topic "
        "does not count. Respond as JSON: "
        '{"keywords":["..."],"dedicated_subreddits":["..."]}'
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
                    {"role": "system", "content": "You build keyword filters for Reddit health-community text. Respond as JSON."},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:  # noqa: BLE001 - any failure falls back
        app.logger.exception("mercury topic plan failed")
        return None
    if not isinstance(data, dict):
        return None

    keywords, seen = [], set()
    for item in data.get("keywords", []):
        kw = str(item).strip().lower()
        if kw and kw not in seen:
            seen.add(kw)
            keywords.append(kw)

    # Only exempt subreddits the user actually asked for.
    requested = {s.lower() for s in subs}
    dedicated = set()
    raw = data.get("dedicated_subreddits", [])
    for item in raw if isinstance(raw, list) else []:
        name = str(item).strip().lstrip("/").removeprefix("r/").strip("/").lower()
        if name in requested:
            dedicated.add(name)

    if not keywords:
        return None
    return {"keywords": keywords[:20], "dedicated": dedicated}


def _squash(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _heuristic_dedicated(topic: str, subs: list[str]) -> set[str]:
    """Subs whose name contains the (squashed) topic are dedicated to it —
    e.g. topic 'breast cancer' -> r/BreastCancer, r/breastcancerawareness,
    but NOT r/cancer. Backstop for when Mercury is unavailable."""
    t = _squash(topic)
    if len(t) < 4:  # too short to be a meaningful name match
        return set()
    return {s.lower() for s in subs if t in _squash(s)}


def _topic_plan(topic: str, subs: list[str]) -> tuple[list[str], set[str]]:
    """Expand a topic into (keywords, exempt-lowercase-sub-names)."""
    if not topic:
        return [], set()
    plan = _mercury_topic_plan(topic, subs)
    keywords = (plan or {}).get("keywords") or _fallback_keywords(topic)
    exempt = _heuristic_dedicated(topic, subs) | (plan or {}).get("dedicated", set())
    return keywords, exempt


def _fallback_keywords(topic: str) -> list[str]:
    """Keyword list from the topic text itself when Mercury is unavailable."""
    topic = topic.strip().lower()
    if not topic:
        return []
    out, seen = [], set()
    words = re.findall(r"[a-z0-9']+", topic)
    if len(words) > 1:  # the full phrase matches most precisely; try it first
        seen.add(topic)
        out.append(topic)
    for w in words:
        if len(w) >= 3 and w not in _KEYWORD_STOPWORDS and w not in seen:
            seen.add(w)
            out.append(w)
    return out or [topic]


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

# XLSX MIME type of the workbook inside the bundle (also used by the tests).
XLSX_MIMETYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
# The download itself is a .zip bundle: the .xlsx plus raw .ndjson.zst files.
ZIP_MIMETYPE = "application/zip"

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
    """Build a filesystem/header safe base filename (no extension) from the
    sub names; callers append ``.zip`` / ``.xlsx`` as needed."""
    parts = []
    for name in subs:
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
        if safe:
            parts.append(safe)
    joined = "-".join(parts) if parts else "export"
    if len(joined) > 80:  # keep the Content-Disposition header sensible
        joined = f"{joined[:80]}_and_more"
    return f"reddit_export_{joined}"


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


@app.post("/api/expand_topic")
def api_expand_topic():
    """Expand a topic once for a deep pull: keywords + dedicated subreddits."""
    payload = request.get_json(silent=True) or {}
    topic = str(payload.get("topic") or "").strip()[:200]
    try:
        subs = _clean_subreddits(payload.get("subreddits"))
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    keywords, exempt = _topic_plan(topic, subs)
    return jsonify({
        "keywords": keywords,
        "dedicated": [s for s in subs if s.lower() in exempt],
    })


# One deep-pull chunk must finish well inside Vercel's 300s kill window;
# compression and response overhead get the remaining slack.
_CHUNK_BUDGET = float(os.environ.get("RTS_CHUNK_BUDGET", "220"))


@app.post("/api/collect_chunk")
def api_collect_chunk():
    """One resumable slice of a deep pull: raw zstd NDJSON + a resume cursor.

    The browser drives many of these sequentially (each well under the
    serverless time limit) and concatenates the response bodies -- zstd frames
    concatenate into one valid .ndjson.zst stream. ``X-Chunk-Done: 1`` means
    the window is exhausted; otherwise resume with ``before`` set to
    ``X-Chunk-Next-Before``.
    """
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValidationError("Request body must be a JSON object.")
        sub = _clean_subreddits([payload.get("subreddit")])[0]
        kind = payload.get("kind")
        if kind not in ("post", "comment"):
            raise ValidationError("'kind' must be 'post' or 'comment'.")
        before = payload.get("before")
        after = payload.get("after")
        if (
            isinstance(before, bool) or not isinstance(before, int)
            or isinstance(after, bool) or not isinstance(after, int)
            or after < 0 or before <= after
        ):
            raise ValidationError(
                "'before'/'after' must be unix timestamps with after < before."
            )
        exclude_usernames = bool(payload.get("exclude_usernames", False))
        raw_kw = payload.get("keywords")
        keywords = None
        if isinstance(raw_kw, list):
            cleaned_kw = [str(k).strip().lower() for k in raw_kw if str(k).strip()]
            keywords = cleaned_kw[:30] or None
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    errors: list[str] = []
    try:
        if os.environ.get("RTS_FAKE") == "1":
            fake = collector.collect(
                [sub], after, before,
                include_comments=(kind == "comment"),
                exclude_usernames=exclude_usernames,
                keywords=keywords,
            )
            records = fake["posts"] if kind == "post" else fake["comments"]
            state = {"next_before": after, "done": True}
        else:
            deadline = time.monotonic() + _CHUNK_BUDGET
            records, state = collector._collect_kind(
                sub, kind, after, before,
                None, exclude_usernames, keywords, deadline,
                lambda msg: app.logger.info("chunk: %s", msg), errors,
            )
        body = bundle.ndjson_zst_bytes(records) if records else b""
    except Exception as exc:  # noqa: BLE001 - any chunk failure is a 500
        app.logger.exception("chunk failed")
        return jsonify({"error": f"Chunk failed: {exc}"}), 500

    # Hitting the chunk budget is the expected way a chunk ends, not an error.
    real_errors = [e for e in errors if collector._BUDGET_NOTE not in e]
    app.logger.info(
        "chunk done: %s/%ss %d records, done=%s, next_before=%s, %d error(s)",
        sub, kind, len(records), state["done"], state["next_before"], len(real_errors),
    )
    return Response(
        body,
        mimetype="application/zstd",
        headers={
            "X-Chunk-Records": str(len(records)),
            "X-Chunk-Done": "1" if state["done"] else "0",
            "X-Chunk-Next-Before": str(state["next_before"]),
            "X-Chunk-Errors": str(len(real_errors)),
        },
    )


@app.post("/api/collect")
def api_collect():
    """Validate input, collect the data, and return a .zip download
    (the .xlsx workbook plus raw per-subreddit .ndjson.zst files)."""
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
        topic = str(payload.get("topic") or "").strip()[:200]
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    # Optional topic filter: expand the description into keywords (Mercury 2,
    # falling back to the words the user typed) and figure out which requested
    # subreddits are already dedicated to the topic -- those are exported in
    # full, since keyword-filtering a dedicated community only loses relevant
    # posts that don't happen to name the topic.
    keywords, exempt = _topic_plan(topic, subs)
    if topic:
        app.logger.info(
            "collect: topic %r -> keywords %s, unfiltered subs %s",
            topic, keywords, sorted(exempt) or "none",
        )

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
            keywords=keywords or None,
            filter_exempt=exempt or None,
            progress=lambda msg: app.logger.info("collect: %s", msg),
        )
        posts = result.get("posts", [])
        comments = result.get("comments", [])
        errors = result.get("errors", [])
        base_name = _safe_filename(subs)
        xlsx_bytes = spreadsheet.build_workbook_bytes(posts, comments)
        zip_bytes = bundle.build_zip_bytes(posts, comments, xlsx_bytes, base_name)
    except Exception as exc:  # noqa: BLE001 - any collect failure is a 500
        app.logger.exception("collection failed")
        return jsonify({"error": f"Collection failed: {exc}"}), 500

    elapsed = time.monotonic() - started
    app.logger.info(
        "collect done: %d posts, %d comments, %d error(s) in %.1fs (%s)",
        len(posts), len(comments), len(errors), elapsed, ",".join(subs),
    )

    headers = {
        "Content-Disposition": f'attachment; filename="{base_name}.zip"',
        # Surface per-subreddit (non-fatal) errors + counts + timing to the UI.
        "X-Collect-Errors": str(len(errors)),
        "X-Collect-Posts": str(len(posts)),
        "X-Collect-Comments": str(len(comments)),
        "X-Collect-Seconds": f"{elapsed:.1f}",
    }
    if keywords:
        # URL-encoded so the header stays latin-1 safe; the UI decodes it.
        headers["X-Collect-Keywords"] = urllib.parse.quote(", ".join(keywords))[:1000]
        if exempt:
            # Preserve the casing the user asked with for display.
            shown = [s for s in subs if s.lower() in exempt]
            headers["X-Collect-Unfiltered"] = urllib.parse.quote(", ".join(shown))[:1000]
    if any(collector._BUDGET_NOTE in e for e in errors):
        # Collection stopped at the server's time budget (Vercel 300s limit).
        headers["X-Collect-Partial"] = "1"
    return Response(zip_bytes, mimetype=ZIP_MIMETYPE, headers=headers)


if __name__ == "__main__":
    # Show INFO logs (incl. the per-request "collect done ... in N.Ns" timing).
    import logging

    logging.basicConfig(level=logging.INFO)
    app.logger.setLevel(logging.INFO)
    # Local-only server; debug off per the project contract.
    app.run(host="127.0.0.1", port=5000, debug=False)
