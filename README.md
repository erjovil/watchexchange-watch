# Reddit listing watcher

Watches [r/watchexchange](https://reddit.com/r/watchexchange) and emails you the
moment a reference you care about is listed — with the asking price in the
subject line and the seller's full description in the body, so you can judge it
from your inbox without opening Reddit.

**Nothing in this repository says what is being watched, or for whom.** The
want-list lives in an encrypted repository secret, and alerts are filed in a
separate private repo. This repo holds only generic code, so it can stay public
and use free Actions minutes.

## How it works

Two independent sources, because one missed listing defeats the purpose:

- **`/r/watchexchange/new/.rss`** — the last 100 posts, visible seconds after
  submission. This is the one that catches things fast.
- **`/r/watchexchange/search.rss`** — Reddit's search index, which reaches more
  of the post than the title. A safety net, swept every 5th pass.

Reddit 403s every `.json` endpoint (`www`, `old`, `api`) for anything that looks
scripted, but serves the RSS feeds to a normal browser UA — verified 2026-08-11.
That's why this uses RSS.

r/watchexchange allows **link posts only**, so a listing has no body text: the
description and price live in a comment by the seller. On a match the watcher
reads that comment from `old.reddit.com` (the modern site is an empty JS shell)
and pulls out the asking price, the description, and the seller's transaction
count from the AutoModerator reply.

Matching ignores spaces, dots and hyphens, so `XY 7788`, `xy-7788` and
`Ref. XY7788,` all hit, and a longer reference that starts with yours also hits.
Nicknames are matched too, so a listing that never states the reference still
fires. `[WTB]` posts are skipped — that's someone hunting the same watch, not
one for sale.

**Listings already marked sold are never alerted on.** The sub flairs a live
listing with a price band (`$7000-$8999`) and a finished one with `Sold`, so age
is not the test — availability is. A week-old listing that nobody bought is
still worth your email; one that sold an hour ago is not.

If the post can't be read, the watcher alerts anyway. A false alarm costs a
glance; staying quiet about a live listing costs the watch.

## Setup

### 1. A private repo for the alerts

Create a second repo, **private**, named anything (`watch-alerts` will do).
Nothing gets pushed to it — it exists so its issues, and therefore your alert
emails, are visible only to you. Make sure **Issues** are enabled on it.

### 2. A token that can write issues there

github.com → Settings → Developer settings → **Fine-grained personal access
tokens** → Generate new token:

| Field | Value |
|---|---|
| Repository access | Only select repositories → *your private alerts repo* |
| Permissions → Issues | **Read and write** |
| Expiration | 1 year (note the date — alerts stop when it expires) |

Copy the token once; GitHub won't show it again.

### 3. Three secrets on this repo

Settings → Secrets and variables → **Actions** → New repository secret:

| Secret | Value |
|---|---|
| `ALERT_TOKEN` | the token from step 2 |
| `ALERT_REPO` | `your-username/your-private-alerts-repo` |
| `TARGETS_JSON` | the whole contents of your `targets.local.json` |

Secrets are encrypted, are not readable from the repo, and are redacted from
logs — so what you're hunting stays private even though the code is public.

### 4. Start it

Push, or Actions → **Run workflow**. Within a minute you get an email titled
**"Watcher is live"** listing what it's watching. That email is the proof the
whole chain works.

If it doesn't arrive, GitHub emails you a failed-run notice instead — the
watcher deliberately goes red when it can see Reddit but cannot reach you,
rather than sitting there looking healthy.

## Configuring what it watches

`targets.local.json` is gitignored, so your real list can live on your machine
for local runs and never reach the repo. Paste the same content into the
`TARGETS_JSON` secret for the cloud run. `targets.json` in the repo is a
placeholder example only.

```json
{
  "targets": [
    {
      "name": "Some Watch (nickname)",
      "refs": ["REF123", "111.22.33.44.55.001"],
      "aliases": ["Seller Nickname"]
    }
  ]
}
```

- `refs` — reference numbers, matched loosely.
- `aliases` — nicknames used *instead* of the reference. Keep these specific: a
  broad alias like a brand name will alert on everything that brand lists.
- `watch_listings_after` — *optional* ISO8601 UTC cutoff, off by default.
  Ignores listings posted before it, regardless of whether they sold.
- `skip_listings_older_than_hours` — *optional* rolling age limit, off by
  default. Both exist for the case where you'd rather miss a stale listing than
  read about one; neither is needed now that sold listings are filtered.

## Privacy

The threat this is built against is that a public repo publishes what you're
buying, which is worth money to the person selling to you.

- The want-list is only ever in an encrypted secret, or in a gitignored local
  file. It is not in the code, the README, or the git history.
- Alerts — titles, prices, seller names, descriptions — go to a **private** repo.
  The watcher has no fallback to filing them here; if the alert repo isn't
  configured it refuses to write issues at all rather than leak them.
- This repo's Actions logs are public, so the script runs with `QUIET=1` and
  logs only that *a* match occurred, never what it was. Set `QUIET=0` locally
  when you want full detail.
- The workflow's built-in token is scoped to `contents: write` only — enough for
  the daily heartbeat commit, and nothing else.

What this cannot hide: that an account owns a repo named after a watch forum.
Rename the repo if that bothers you — nothing in the code depends on its name.

## Verifying it still works

```bash
python3 test_matching.py
```

Real listing titles must fire, near-misses must stay quiet, `[WTB]` must be
skipped, sold listings must be filtered while price-band flair is read as live,
prices must parse, and the live feed must still yield 100 posts. Run it after editing
your list, or if Reddit changes its feed format — a watcher that silently
stopped matching is the failure mode that costs you the watch.

```bash
python3 check_watchexchange.py --once      # one sweep, printed locally
```

## Known limits

- **GitHub's free scheduler is unreliable.** A `*/5` cron measurably produced
  runs roughly every 2.5 hours. Worked around by making each run last 5.5 hours
  so one is always live; coverage comes from the long run, not from cron.
- **If Reddit blocks the runner's IP**, every fetch fails, the watcher files a
  `monitor-blind` issue and fails the run — so you find out you're unprotected
  instead of assuming silence means nothing was listed.
- The description comment is sometimes posted a minute after the listing. The
  alert never waits for it; it goes out immediately saying the description
  isn't up yet.
- Deleted-and-reposted listings alert twice. Dedupe is per post ID.
- A listing the seller sold privately but never flaired `Sold` still alerts.
  Nothing readable distinguishes it from a live one, and erring the other way
  would mean silence on listings that are genuinely available.
