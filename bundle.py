"""Zip bundle builder for reddit-to-spreadsheet.

Packages one collection run into a single downloadable .zip:

    <base_name>.xlsx                     the formatted workbook (spreadsheet.py)
    raw/<subreddit>_posts.ndjson.zst     raw post records, one JSON per line
    raw/<subreddit>_comments.ndjson.zst  raw comment records, one JSON per line

The .zst members use the pushshift/arctic_shift dump convention
(zstandard-compressed NDJSON of the normalized record dicts from collector.py)
so they can be fed straight into existing dump tooling. Empty groups produce no
file; the xlsx is always present.

Public interface:
    build_zip_bytes(posts, comments, xlsx_bytes, base_name) -> bytes
"""

from __future__ import annotations

import io
import json
import zipfile
from collections import defaultdict

import zstandard


def _safe_component(name: str) -> str:
    """Filename-safe subreddit component (same charset as app._safe_filename)."""
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
    return safe or "unknown"


def _group_by_sub(records: list[dict]) -> dict[str, list[dict]]:
    # Case-insensitive: sources disagree on casing (arctic_shift lowercases
    # subreddit names, pullpush preserves them) and must not split one
    # subreddit into two files. First-seen casing names the file.
    groups: dict[str, list[dict]] = defaultdict(list)
    canonical: dict[str, str] = {}
    for rec in records or []:
        name = rec.get("subreddit") or "unknown"
        key = canonical.setdefault(name.lower(), name)
        groups[key].append(rec)
    return groups


def ndjson_zst_bytes(records: list[dict]) -> bytes:
    """One zstd frame of NDJSON. Frames concatenate into a valid .ndjson.zst
    stream, which is what the chunked deep-pull API relies on."""
    lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    return zstandard.ZstdCompressor().compress(lines.encode("utf-8"))


def build_zip_bytes(
    posts: list[dict],
    comments: list[dict],
    xlsx_bytes: bytes,
    base_name: str,
) -> bytes:
    """Build the export bundle and return it as raw .zip bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{base_name}.xlsx", xlsx_bytes)
        for kind, records in (("posts", posts), ("comments", comments)):
            groups = _group_by_sub(records)
            for sub in sorted(groups, key=str.lower):
                zf.writestr(
                    f"raw/{_safe_component(sub)}_{kind}.ndjson.zst",
                    ndjson_zst_bytes(groups[sub]),
                    # Already zstd-compressed; deflating again just wastes CPU.
                    compress_type=zipfile.ZIP_STORED,
                )
    return buf.getvalue()
