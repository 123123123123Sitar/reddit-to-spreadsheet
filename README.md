# reddit-to-spreadsheet

A local web app to pull Reddit posts and comments from selected subreddits (via pullpush.io, with an automatic arctic_shift fallback) and download them as a `.zip` bundle: a formatted `.xlsx` spreadsheet plus raw `.ndjson.zst` data files.

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000 in your browser.

## How it works

Pick one or more subreddits (search, tap a suggestion, or **describe a condition in the chat box** and the relevant communities are auto-selected), set a date window and options, click collect — the app fetches the data and hands you back a `.zip` bundle.

## What's in the download

Every collect returns one `reddit_export_<subs>.zip` containing:

- `reddit_export_<subs>.xlsx` — the formatted workbook (Posts / Comments / Summary sheets)
- `raw/<subreddit>_posts.ndjson.zst` and `raw/<subreddit>_comments.ndjson.zst` — the raw records in the pushshift/arctic_shift dump convention: zstandard-compressed NDJSON, one JSON object per line. These feed straight into existing dump tooling (`zstdcat file.ndjson.zst | jq .`). Empty groups produce no file.

## Topic filter

The window step has an optional **topic filter** ("only keep posts about…"). The description is expanded into a keyword list — via Mercury 2 when a key is configured (synonyms, abbreviations, patient wording), otherwise the significant words you typed — and only posts/comments whose title/body contains at least one keyword (case-insensitive) are exported. Filtering happens before the per-subreddit caps are counted, so caps count *matching* records.

**Dedicated communities are exported in full.** A subreddit that is entirely about the topic (topic "breast cancer" → r/BreastCancer, r/breastcancerawareness) skips the keyword filter — nearly every post there is on-topic, and posts like "surgery tomorrow, so scared" would otherwise be lost for not naming the condition. Mercury classifies which of the requested subreddits are dedicated (backstop: name matching against the topic); the general ones (r/cancer, r/health, …) stay keyword-filtered. The `X-Collect-Keywords` / `X-Collect-Unfiltered` response headers (URL-encoded) report the expanded list and the exempted subreddits, and the UI shows both in the status line.

## AI suggestions & chat (Mercury 2)

The "Suggested" panel and the chat box ("describe a condition → auto-select communities") are powered by **Mercury 2** (Inception Labs, OpenAI-compatible). Provide the key via the `MERCURY_API_KEY` environment variable:

```bash
export MERCURY_API_KEY=sk_...      # or place it in ~/mercury_key.txt for local dev
```

Without a key, both features fall back to a built-in static keyword/theme matcher. Suggestion names and post counts are model **estimates**, not live Reddit data.

## Deploy to Vercel

The app is Vercel-ready (Python/Flask via `@vercel/python`, config in `vercel.json`).

```bash
npm i -g vercel           # if needed
vercel                    # first deploy (preview) — links/creates the project
vercel env add MERCURY_API_KEY production   # paste the key when prompted
vercel --prod             # promote to production
```

Set `MERCURY_API_KEY` in the Vercel project (CLI above or Project → Settings → Environment Variables).

### Hosted time limit & partial exports

Vercel hard-kills the function at 300 s (the Hobby-plan maximum). When running on Vercel the collector therefore switches to a faster profile automatically (fewer retries, shorter waits) and stops collecting after ~250 s, returning **partial results** — the download still arrives, the status line says the export is partial, and the `X-Collect-Partial: 1` header is set. The budget is split **fairly across every subreddit × kind task** (task *i* of *N* must finish by *budget·(i+1)/N*, unused time rolls forward), so one huge subreddit's posts can't starve the comment collection behind it. For big pulls, narrow the window / lower the caps / split subreddits across runs, or just run the app locally (no budget applies there by default).

Note that a **topic filter combined with huge caps** means scanning the *entire* window of every subreddit (caps count matching records), which rarely fits in 300 s across many subreddits — expect balanced-but-partial hosted results for that shape of run. For complete large pulls, use **deep pull** below.

## Deep pull (unlimited size, works on the hosted version)

The export step has a **Deep pull** toggle for runs the 300 s limit can't hold — hundreds of thousands or millions of records. Instead of one request, the browser drives many `POST /api/collect_chunk` calls, each safely under the serverless limit; every chunk returns raw zstd-NDJSON plus a resume cursor (`X-Chunk-Next-Before`, `X-Chunk-Done`), and the client concatenates the frames — concatenated zstd frames are a valid `.ndjson.zst` stream (verified against `zstd -dc`). You get one complete `reddit_<sub>_<posts|comments>.ndjson.zst` per subreddit per kind; caps are ignored, no spreadsheet is built (Excel tops out at ~1 M rows anyway), and the topic filter / dedicated-subreddit logic applies as usual via `POST /api/expand_topic`.

Throughput comes from arctic_shift's `limit=auto` (up to 1 000 rows/page): measured ~8 000–40 000 records per chunk, so ~1 M comments is a 1–2 h browser session — keep the tab open, click the button again to stop early and keep what's collected, and approve the browser's "download multiple files" prompt. `RTS_CHUNK_BUDGET` (default 220 s) bounds each chunk server-side.

The profile is env-tunable everywhere: `RTS_TIME_BUDGET` (seconds, 0 = unlimited), `RTS_MAX_ATTEMPTS`, `RTS_RETRY_MAX_WAIT`, `RTS_TIMEOUT`, `RTS_SLEEP`.

## Offline demo mode (`RTS_FAKE=1`)

pullpush.io is a community mirror and is frequently down or slow (it loves to
answer `HTTP 502`). To run, test, or demo the app without touching the network,
set `RTS_FAKE=1`. The collector then returns a small, deterministic synthetic
dataset (3 posts + 5 comments per subreddit) so the whole stack — UI → collect
→ `.zip` export — works end to end offline:

```bash
# Run the app in offline demo mode:
RTS_FAKE=1 python app.py

# Run the offline test suite (no network is ever touched):
RTS_FAKE=1 pip install pytest && RTS_FAKE=1 python -m pytest -q
```

Note: on macOS, port 5000 may be occupied by the AirPlay Receiver
(System Settings → General → AirDrop & Handoff). Disable it or change the port
in `app.py` if `/` returns `403`.

## Data sources & the arctic_shift fallback

When `RTS_FAKE` is unset the app makes real requests to pullpush.io. That
service is flaky: individual pages often fail with `502`/timeouts even after the
built-in exponential-backoff retries (~6 attempts per request). When a
subreddit/endpoint keeps failing, the collector records a human-readable entry
in the result's `errors` list and **automatically continues from the same
point on the arctic_shift API** (`arctic-shift.photon-reddit.com`), which
serves the same Reddit record schema. Only when both sources fail does it move
on — it never crashes the collection — so a partial or empty export now needs
both mirrors to be down at once. The `X-Collect-Errors` / `X-Collect-Posts` /
`X-Collect-Comments` response headers report what actually came back, and the
errors list says which source failed and where the fallback kicked in.

## Data & ethics

This tool collects publicly available Reddit data, but "public" is not the same as "fair to republish." Please do not republish raw personal text or usernames scraped from sensitive communities (for example medical subreddits such as r/endometriosis or r/BreastCancer) — real people share vulnerable, identifying details there. Use the **"exclude usernames"** option to drop author names from your export, aggregate rather than quote where you can, and respect Reddit's content policy and each subreddit's rules.
