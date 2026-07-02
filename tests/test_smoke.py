"""Offline smoke tests for reddit-to-spreadsheet.

Every test here runs fully offline -- no network is ever touched. The
collector's ``RTS_FAKE`` hook lets us exercise the whole stack (Flask route
-> collector -> spreadsheet -> .xlsx bytes) deterministically.

Run from the repo root::

    pytest -q
"""

from __future__ import annotations

import io
import os
import sys

import openpyxl
import pytest

# Make the repo root importable no matter where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import collector  # noqa: E402
import spreadsheet  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _stub_posts():
    return [
        {
            "kind": "post", "subreddit": "endometriosis", "id": "p1",
            "created_utc": 1_700_000_000, "author": "alice",
            "title": "First post", "selftext": "hello world",
            "score": 10, "num_comments": 2,
            "permalink": "https://reddit.com/r/endometriosis/p1",
        },
        {
            "kind": "post", "subreddit": "endometriosis", "id": "p2",
            "created_utc": 1_700_100_000, "author": None,
            "title": "Second post", "selftext": "more text here",
            "score": 5, "num_comments": 0,
            "permalink": "https://reddit.com/r/endometriosis/p2",
        },
    ]


def _stub_comments():
    return [
        {
            "kind": "comment", "subreddit": "endometriosis", "id": "c1",
            "created_utc": 1_700_000_500, "author": "bob", "body": "nice",
            "score": 3, "link_id": "t3_p1", "parent_id": "t3_p1",
            "permalink": "https://reddit.com/r/endometriosis/p1/c1",
        },
        {
            "kind": "comment", "subreddit": "endometriosis", "id": "c2",
            "created_utc": 1_700_000_600, "author": None, "body": "thanks",
            "score": 1, "link_id": "t3_p1", "parent_id": "t1_c1",
            "permalink": "https://reddit.com/r/endometriosis/p1/c2",
        },
        {
            "kind": "comment", "subreddit": "endometriosis", "id": "c3",
            "created_utc": 1_700_100_500, "author": "carol", "body": "hi",
            "score": 0, "link_id": "t3_p2", "parent_id": "t3_p2",
            "permalink": "https://reddit.com/r/endometriosis/p2/c3",
        },
    ]


# --------------------------------------------------------------------------- #
# spreadsheet.build_workbook_bytes
# --------------------------------------------------------------------------- #
def test_build_workbook_bytes_sheets_and_rows():
    posts = _stub_posts()
    comments = _stub_comments()

    raw = spreadsheet.build_workbook_bytes(posts, comments)
    assert isinstance(raw, (bytes, bytearray)) and len(raw) > 0

    wb = openpyxl.load_workbook(io.BytesIO(raw))
    names = wb.sheetnames
    assert "Posts" in names
    assert "Comments" in names
    assert "Summary" in names

    # Header row + one row per record.
    assert wb["Posts"].max_row == len(posts) + 1
    assert wb["Comments"].max_row == len(comments) + 1

    # Summary has a header, at least the one subreddit row + a TOTAL row.
    assert wb["Summary"].max_row >= 3


def test_build_workbook_bytes_omits_empty_comments_sheet():
    # Per the contract the Comments sheet is only present when there are
    # comments; Posts + Summary must always exist.
    raw = spreadsheet.build_workbook_bytes(_stub_posts(), [])
    wb = openpyxl.load_workbook(io.BytesIO(raw))
    assert "Posts" in wb.sheetnames
    assert "Summary" in wb.sheetnames
    assert "Comments" not in wb.sheetnames


# --------------------------------------------------------------------------- #
# collector RTS_FAKE hook (offline)
# --------------------------------------------------------------------------- #
def test_collector_fake_hook_is_offline(monkeypatch):
    monkeypatch.setenv("RTS_FAKE", "1")

    result = collector.collect(["endometriosis"], 1_700_000_000, 1_700_200_000)

    assert set(result.keys()) >= {"posts", "comments", "errors"}
    posts = result["posts"]
    comments = result["comments"]

    assert len(posts) > 0
    assert len(comments) > 0
    assert all(p["kind"] == "post" for p in posts)
    assert all(c["kind"] == "comment" for c in comments)
    assert all(p["subreddit"] == "endometriosis" for p in posts)
    # Synthetic data must satisfy the Post schema well enough to spreadsheet.
    for p in posts:
        assert {"id", "created_utc", "title", "score"} <= set(p.keys())


def test_collector_fake_hook_multiple_subs(monkeypatch):
    monkeypatch.setenv("RTS_FAKE", "1")
    result = collector.collect(["endometriosis", "PCOS"], 0, 2_000_000_000)
    subs = {p["subreddit"] for p in result["posts"]}
    assert subs == {"endometriosis", "PCOS"}


# --------------------------------------------------------------------------- #
# app POST /api/collect end-to-end (offline via RTS_FAKE)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_collect_endpoint_returns_valid_xlsx(client, monkeypatch):
    monkeypatch.setenv("RTS_FAKE", "1")

    resp = client.post(
        "/api/collect",
        json={
            "subreddits": ["endometriosis"],
            "start_date": "2023-11-01",
            "end_date": "2023-11-30",
            "include_comments": True,
            "exclude_usernames": False,
            "max_posts_per_sub": 50,
            "max_comments_per_sub": 100,
        },
    )

    assert resp.status_code == 200
    assert resp.mimetype == XLSX_MIME
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert resp.headers["Content-Disposition"].endswith('.xlsx"')

    # The body must reopen as a valid workbook with the expected sheets.
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))
    assert "Posts" in wb.sheetnames
    assert "Summary" in wb.sheetnames


def test_collect_endpoint_empty_subreddits_is_400(client, monkeypatch):
    monkeypatch.setenv("RTS_FAKE", "1")
    resp = client.post(
        "/api/collect",
        json={
            "subreddits": [],
            "start_date": "2023-11-01",
            "end_date": "2023-11-30",
        },
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_collect_endpoint_bad_date_is_400(client, monkeypatch):
    monkeypatch.setenv("RTS_FAKE", "1")
    resp = client.post(
        "/api/collect",
        json={
            "subreddits": ["endometriosis"],
            "start_date": "not-a-date",
            "end_date": "2023-11-30",
        },
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_subreddits_endpoint(client):
    resp = client.get("/api/subreddits")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "categories" in data
    assert "Health" in data["categories"]
