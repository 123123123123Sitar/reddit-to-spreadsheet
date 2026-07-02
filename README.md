# reddit-to-spreadsheet

A local web app to pull Reddit posts and comments from selected subreddits (via pullpush.io) and download them as an `.xlsx` spreadsheet.

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000 in your browser.

## How it works

Pick one or more subreddits (from the curated list or type your own), set a date window and options, click collect — the app fetches the data from pullpush.io and hands you back a formatted spreadsheet.

## Offline demo mode (`RTS_FAKE=1`)

pullpush.io is a community mirror and is frequently down or slow (it loves to
answer `HTTP 502`). To run, test, or demo the app without touching the network,
set `RTS_FAKE=1`. The collector then returns a small, deterministic synthetic
dataset (3 posts + 5 comments per subreddit) so the whole stack — UI → collect
→ `.xlsx` export — works end to end offline:

```bash
# Run the app in offline demo mode:
RTS_FAKE=1 python app.py

# Run the offline test suite (no network is ever touched):
RTS_FAKE=1 pip install pytest && RTS_FAKE=1 python -m pytest -q
```

Note: on macOS, port 5000 may be occupied by the AirPlay Receiver
(System Settings → General → AirDrop & Handoff). Disable it or change the port
in `app.py` if `/` returns `403`.

## A note on pullpush reliability

When `RTS_FAKE` is unset the app makes real requests to pullpush.io. That
service is flaky: individual pages often fail with `502`/timeouts even after the
built-in exponential-backoff retries (~6 attempts per request). When a
subreddit/endpoint keeps failing, the collector records a human-readable entry
in the result's `errors` list and moves on — it never crashes the collection —
so you may get a partial or empty export during a pullpush outage. Just retry
later. The `X-Collect-Errors` / `X-Collect-Posts` / `X-Collect-Comments`
response headers report what actually came back.

## Data & ethics

This tool collects publicly available Reddit data, but "public" is not the same as "fair to republish." Please do not republish raw personal text or usernames scraped from sensitive communities (for example medical subreddits such as r/endometriosis or r/BreastCancer) — real people share vulnerable, identifying details there. Use the **"exclude usernames"** option to drop author names from your export, aggregate rather than quote where you can, and respect Reddit's content policy and each subreddit's rules.
