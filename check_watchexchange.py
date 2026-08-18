#!/usr/bin/env python3
"""
r/watchexchange listing watcher — alerts the moment a watched reference is
posted, with the seller's asking price and description in the alert itself.

Configuration is deliberately NOT in this repo. What to hunt comes from the
TARGETS_JSON secret, and alerts are filed in a separate private repo named by
the ALERT_REPO secret. This repo can be public (free Actions minutes) without
publishing anything about who runs it or what they are looking for.

Environment:
    TARGETS_JSON   config as JSON (falls back to targets.local.json, targets.json)
    ALERT_REPO     owner/repo to file alerts in — keep this one PRIVATE
    ALERT_TOKEN    fine-grained PAT with Issues:write on ALERT_REPO
    PASSES / GAP_SECONDS / SEARCH_EVERY   loop shape

Exit codes:
    0  ran fine
    1  every fetch failed — the watcher is blind, and says so loudly
    2  no alert destination configured, so nothing could ever reach you
    3  found a listing but could not deliver the alert (the worst failure:
       something was there and you were not told, so the run goes red)

    Anything non-zero fails the workflow on purpose. A watcher that cannot
    reach you must never look healthy.
"""

import html as html_mod
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# Reddit serves the RSS feeds to a normal browser UA but 403s every .json
# endpoint (www, old, api) for anything that looks scripted. Verified 2026-08-11.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SUB = "watchexchange"
NEW_FEED = "https://www.reddit.com/r/%s/new/.rss?limit=100" % SUB
SEARCH_FEED = "https://www.reddit.com/r/%s/search.rss" % SUB
# old.reddit still renders a full server-side page; the modern one is an empty
# JS shell. This is the only way to read a listing's details without an API key.
POST_PAGE = "https://old.reddit.com/comments/%s/"

# [WTB] is someone hunting the same watch you are, not a watch for sale.
SKIP_TAGS = ("WTB",)

# Consecutive passes where every fetch failed before we call ourselves blind.
# With a 60s gap that is ten minutes of total failure — long enough to ride
# out a transient 429, short enough that you learn about a block quickly.
BLIND_AFTER = 10


# Actions logs on a public repo are public. Anything naming a watch, a listing
# or a seller is therefore treated as private and withheld unless QUIET=0.
QUIET = os.environ.get("QUIET", "1") not in ("0", "false", "")


def log(msg):
    print("%s  %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), msg), flush=True)


def log_private(msg, public_alternative):
    """Log detail locally; log only the shape of it where others can read."""
    log(public_alternative if QUIET else msg)


def norm(text):
    """Uppercase, strip everything that isn't a letter or digit.

    Turns 'XY 7788', 'xy-7788' and 'Ref. XY7788,' into one comparable token,
    which is how sellers actually write reference numbers — inconsistently.
    """
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def parse_time(value):
    """ISO8601 -> aware UTC datetime, or None if it can't be read."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def strip_html(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return html_mod.unescape(re.sub(r"\s+", " ", text)).strip()


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config():
    """Config from the secret if present, else a local file.

    targets.local.json is gitignored, so a real want-list can sit on your own
    machine for local runs without ever reaching the public repo.
    """
    raw = os.environ.get("TARGETS_JSON", "").strip()
    source = "TARGETS_JSON secret"
    if not raw:
        for name in ("targets.local.json", "targets.json"):
            path = os.path.join(HERE, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
                source = name
                break
    cfg = json.loads(raw)

    targets = []
    for t in cfg["targets"]:
        needles = [norm(x) for x in t.get("refs", []) + t.get("aliases", [])]
        targets.append({
            "name": t["name"],
            "needles": [n for n in needles if n],
        })

    since = parse_time(cfg.get("watch_listings_after"))
    max_age = float(cfg.get("skip_listings_older_than_hours") or 0)
    return {"targets": targets, "since": since, "max_age": max_age, "source": source}


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch(url, timeout=25, expect_min=500, retries=2):
    """Return (body, error). Retries 429s, which Reddit hands out freely."""
    err = "not attempted"
    for attempt in range(retries + 1):
        body, err = _fetch_once(url, timeout, expect_min)
        if body is not None or "429" not in (err or ""):
            return body, err
        if attempt < retries:
            time.sleep(4 * (attempt + 1))
    return None, err


def _fetch_once(url, timeout, expect_min):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/atom+xml,application/xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status != 200:
                return None, "HTTP %s" % resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return None, "HTTP %s" % exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "fetch error: %s" % exc
    if len(body) < expect_min:
        return None, "suspiciously short response (%d bytes)" % len(body)
    return body, None


def parse_atom(xml):
    """Pull posts out of a Reddit Atom feed.

    Deliberately regex, not an XML parser: Reddit occasionally serves a feed
    with a stray entity that makes a strict parser discard the whole document,
    and losing 100 posts to one bad character is not acceptable here.
    """
    posts = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        title = re.search(r"<title>(.*?)</title>", block, re.S)
        link = re.search(r'<link[^>]*href="([^"]+)"', block)
        pid = re.search(r"<id>(?:t3_)?([a-z0-9_]+)</id>", block, re.I)
        author = re.search(r"<name>(.*?)</name>", block, re.S)
        published = re.search(r"<published>(.*?)</published>", block, re.S)
        updated = re.search(r"<updated>(.*?)</updated>", block, re.S)
        if not (title and link and pid):
            continue
        stamp = (published or updated)
        posts.append({
            "id": pid.group(1).replace("t3_", ""),
            "title": html_mod.unescape(title.group(1)).strip(),
            "url": html_mod.unescape(link.group(1)).strip(),
            "author": html_mod.unescape(author.group(1)).strip() if author else "?",
            "posted": parse_time(stamp.group(1)) if stamp else None,
            # search.rss embeds more of the post, so keep the whole block as a
            # secondary haystack for references not present in the title.
            "blob": html_mod.unescape(re.sub(r"<[^>]+>", " ", block)),
        })
    return posts


def search_url(targets):
    """One OR'd search query covering every target — one request, not four."""
    terms = []
    for t in targets:
        terms.extend(t["needles"])
    q = " OR ".join(sorted(set(terms)))
    return SEARCH_FEED + "?" + urllib.parse.urlencode({
        "q": q, "restrict_sr": "on", "sort": "new", "t": "week", "limit": "50",
    })


# --------------------------------------------------------------------------
# listing details — price, description, seller history
# --------------------------------------------------------------------------

PRICE_PATTERNS = [
    r"asking\s*price[^0-9$]{0,20}\$?\s*([0-9][0-9,\.]{2,})",
    r"price[^0-9$]{0,15}\$?\s*([0-9][0-9,\.]{2,})",
    r"\$\s?([0-9][0-9,\.]{2,})",
]


def extract_price(*texts):
    for pattern in PRICE_PATTERNS:
        for text in texts:
            m = re.search(pattern, text or "", re.I)
            if m:
                value = m.group(1).rstrip(".,")
                # Ignore obvious non-prices (years, transaction counts).
                digits = re.sub(r"[^0-9]", "", value)
                if len(digits) >= 3:
                    return "$" + value
    return ""


# The sub marks a finished listing with link flair. "Sold" is the only
# end-of-life flair it actually uses (Expired/Traded/Closed return nothing),
# but the others cost nothing to cover in case that changes.
SOLD_FLAIRS = ("SOLD", "TRADED", "EXPIRED", "CLOSED", "COMPLETE", "GONE")


def is_sold(page, title):
    """True if this listing is done. Unknown status is NOT sold — fail open.

    A live listing carries a price-band flair ('$7000-$8999'); a finished one
    carries 'Sold', which old.reddit renders as both a `linkflair-sold` class
    and a flair label. Some sellers also just edit SOLD into the title.
    """
    if re.search(r"\bsold\b", title or "", re.I):
        return True, "SOLD (in the title)"
    if not page:
        return False, ""
    if re.search(r"linkflair-sold\b", page):
        return True, "Sold"
    flair = re.search(r'<span class="linkflairlabel[^"]*"[^>]*title="([^"]*)"', page)
    label = html_mod.unescape(flair.group(1)).strip() if flair else ""
    if any(tok in norm(label) for tok in SOLD_FLAIRS):
        return True, label
    return False, label


def listing_details(post_id, title=""):
    """Flair, sold status, the seller's own comment, transaction count, price.

    r/watchexchange allows link posts only, so there is no selftext — the
    description lives in a comment by the submitter. That comment sometimes
    lands a minute after the post, so a miss here is normal and must never
    delay or block the alert.
    """
    page, err = fetch(POST_PAGE % post_id, expect_min=2000, retries=1)
    if page is None:
        # Status unknown. Alerting on a sold listing wastes a glance; staying
        # quiet on a live one costs the watch, so this fails open.
        sold, flair = is_sold(None, title)
        return {"description": "", "transactions": "", "price": "", "flair": flair,
                "sold": sold, "note": "could not open the post (%s)" % err}

    description = ""
    transactions = ""
    for block in re.finditer(r'<div class="entry [^"]*">(.*?)<div class="child">', page, re.S):
        seg = block.group(1)
        author = re.search(r'<a[^>]*class="author([^"]*)"[^>]*>([^<]+)</a>', seg)
        body = re.search(r'<div class="md">(.*?)</div>', seg, re.S)
        if not (author and body):
            continue
        text = strip_html(body.group(1))
        if "submitter" in author.group(1) and not description:
            description = text
        m = re.search(r"has\s+([\d,]+)\s+Transaction", text, re.I)
        if m and not transactions:
            transactions = m.group(1)

    sold, flair = is_sold(page, title)
    return {
        "description": description,
        "transactions": transactions,
        # The price-band flair ('$7000-$8999') stands in when the seller has not
        # posted their description yet, which is common in the first minute.
        "price": extract_price(description) or (flair if flair.startswith("$") else ""),
        "flair": flair,
        "sold": sold,
        "note": "" if description else "seller has not posted their description comment yet",
    }


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def match(post, targets):
    """Which targets this post is offering, and what gave it away."""
    hay_title = norm(post["title"])
    hay_all = norm(post["blob"])
    hits = []
    for t in targets:
        for needle in t["needles"]:
            if needle in hay_title:
                hits.append((t["name"], "title"))
                break
            if needle in hay_all:
                hits.append((t["name"], "post body"))
                break
    return hits


def post_tag(post):
    m = re.match(r"\s*\[([^\]]{1,20})\]", post["title"])
    return m.group(1).upper().replace(" ", "") if m else ""


def too_old(post, cfg, now):
    """True if this listing predates the watch window.

    Anything posted before you switched the watcher on is backlog — very
    possibly already sold — and emailing you about it is noise that trains you
    to ignore the alerts that matter.
    """
    if post["posted"] is None:
        # Unreadable timestamp: alert anyway. A false alarm costs a glance;
        # a miss costs the watch.
        return False
    if cfg["since"] and post["posted"] < cfg["since"]:
        return True
    if cfg["max_age"] and post["posted"] < now - timedelta(hours=cfg["max_age"]):
        return True
    return False


# --------------------------------------------------------------------------
# alerting
# --------------------------------------------------------------------------

class Alerter(object):
    """Files an issue in a PRIVATE repo, which GitHub emails to you."""

    LABEL = "listing"

    def __init__(self):
        # ALERT_REPO is a separate private repo, so nothing about what you hunt
        # is ever written to the public repo this code runs in.
        self.repo = os.environ.get("ALERT_REPO", "").strip()
        self.token = os.environ.get("ALERT_TOKEN", "").strip()
        self.private = bool(self.repo and self.token)
        if not self.private:
            # Falling back would publish your want-list. Refuse instead.
            self.repo = ""
            self.token = ""
        self.owner = self.repo.split("/")[0] if self.repo else ""
        self.enabled = bool(self.repo and self.token)
        self.seen = set()
        self.failures = 0
        if self.enabled:
            self._ensure_labels()
            self._seed_seen()

    def _api(self, method, path, payload=None):
        req = urllib.request.Request(
            "https://api.github.com" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "User-Agent": "listing-watcher",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body) if body.strip() else {}

    def _ensure_labels(self):
        for name, color, desc in (
            (self.LABEL, "B60205", "A watched reference was listed"),
            ("monitor-blind", "D93F0B", "Watcher cannot reach the source"),
            ("alert-test", "0E8A16", "Setup confirmation"),
        ):
            try:
                self._api("POST", "/repos/%s/labels" % self.repo,
                          {"name": name, "color": color, "description": desc})
            except urllib.error.HTTPError:
                pass  # 422 = already exists, which is the normal case

    def _seed_seen(self):
        """Learn which posts were already alerted, so a restart stays quiet.

        Reads issue TITLES rather than a state file: the issues are the record,
        they survive a fresh clone, and there is nothing to keep in sync.
        """
        try:
            issues = self._api(
                "GET",
                "/repos/%s/issues?state=all&labels=%s&per_page=100" % (self.repo, self.LABEL),
            )
        except urllib.error.HTTPError as exc:
            log("could not read past alerts (%s) — may re-alert one old post" % exc)
            return
        for issue in issues if isinstance(issues, list) else []:
            m = re.search(r"\(([a-z0-9]{5,})\)\s*$", issue.get("title", ""))
            if m:
                self.seen.add(m.group(1))
        log("dedupe seeded with %d already-alerted listing(s)" % len(self.seen))

    def already_sent(self, post_id):
        return post_id in self.seen

    def _create(self, title, body, label):
        payload = {"title": title, "body": body, "labels": [label]}
        # Assignment is what makes GitHub email you. If it is rejected, send the
        # issue anyway — an unassigned issue beats no alert.
        for extra in ({"assignees": [self.owner]}, None):
            attempt = dict(payload)
            if extra:
                attempt.update(extra)
            try:
                return self._api("POST", "/repos/%s/issues" % self.repo, attempt)
            except urllib.error.HTTPError as exc:
                log("issue POST failed (%s)%s" % (exc, " — retrying unassigned" if extra else ""))
                if exc.code in (401, 403):
                    log("  -> ALERT_TOKEN looks wrong, expired, or lacks Issues:write on %s"
                        % self.repo)
        return None

    def send(self, post, hits, details):
        names = ", ".join(sorted(set(n for n, _ in hits)))
        where = ", ".join(sorted(set(w for _, w in hits)))
        self.seen.add(post["id"])

        price = details.get("price") or extract_price(post["title"])
        title = "%s%s — r/watchexchange (%s)" % (
            names, (" — " + price) if price else "", post["id"])

        rows = [
            ("Match", names),
            ("Asking price", price or "not stated in the description"),
            ("Status", details.get("flair") or "live (no flair yet)"),
            ("Seller", "%s%s" % (post["author"],
                                 "  ·  %s past transactions" % details["transactions"]
                                 if details.get("transactions") else "")),
            ("Posted", post["posted"].strftime("%Y-%m-%d %H:%M UTC") if post["posted"] else "just now"),
            ("Found in", where),
        ]
        body = ["### %s" % post["title"], "", post["url"], "", "| | |", "|---|---|"]
        body += ["| %s | %s |" % (k, v) for k, v in rows]
        body += ["", "---", ""]
        if details.get("description"):
            text = details["description"]
            if len(text) > 3000:
                text = text[:3000] + " …(truncated — open the post for the rest)"
            body += ["**Seller's description**", "", "> " + text.replace("\n", "\n> "), ""]
        else:
            body += ["_%s. Open the post in a minute for the details._"
                     % (details.get("note") or "No description found"), ""]
        body += ["Good ones go in minutes — comment first, negotiate after."]

        if not self.enabled:
            log("ALERT (local): %s %s — %s" % (names, price, post["url"]))
            if details.get("description"):
                log("  %s" % details["description"][:300])
            return True

        issue = self._create(title, "\n".join(body), self.LABEL)
        if issue:
            log_private("ALERT SENT -> issue #%s  (%s %s)" % (issue.get("number", "?"), names, price),
                        "ALERT SENT -> issue #%s" % issue.get("number", "?"))
            return True
        log_private("ALERT FAILED for %s" % post["url"], "ALERT FAILED for a matched listing")
        self.seen.discard(post["id"])
        self.failures += 1
        return False

    def hello(self, cfg):
        """Say hello once, so setup is confirmed without a real listing."""
        if not self.enabled:
            # This used to exit 0 on the theory that a just-published repo has
            # no secrets yet. That was wrong: it made "never configured" and
            # "working fine" look identical, and the watcher sat green for six
            # days while three listings came and went. Silence must be loud.
            missing = [n for n in ("ALERT_REPO", "ALERT_TOKEN") if not os.environ.get(n)]
            log("NOT CONFIGURED: missing secret(s) %s." % ", ".join(missing))
            log("Until these are set nothing can reach you, so this run fails on")
            log("purpose — a green run here would mean 'watching' when it is not.")
            log("")
            log("Settings -> Secrets and variables -> Actions:")
            log("  ALERT_TOKEN   fine-grained PAT, Issues: Read and write")
            log("  ALERT_REPO    your-username/your-private-alerts-repo")
            log("  TARGETS_JSON  the contents of targets.local.json")
            return 2
        try:
            existing = self._api(
                "GET", "/repos/%s/issues?state=all&labels=alert-test&per_page=1" % self.repo)
            if isinstance(existing, list) and existing:
                log("already said hello — staying quiet")
                return 0
        except urllib.error.HTTPError:
            pass  # better a duplicate than nothing

        since = (cfg["since"].strftime("%Y-%m-%d %H:%M UTC") if cfg["since"]
                 else "any still-live listing (no date cutoff)")
        body = "\n".join([
            "Your watcher is live, and this email proves the whole chain works.",
            "",
            "It is watching r/watchexchange around the clock for:",
            "",
        ] + ["- **%s**" % t["name"] for t in cfg["targets"]] + [
            "",
            "| | |",
            "|---|---|",
            "| Alerts on | %s |" % since,
            "| Never alerts on | listings the sub has flaired Sold |",
            "| Config source | %s |" % cfg["source"],
            "",
            "Alerts arrive as issues in this private repo, with the asking price in",
            "the subject line and the seller's full description in the body, so you",
            "can judge it from your inbox without opening Reddit. Anything already",
            "marked sold is filtered out before it reaches you.",
            "",
            "_Safe to close. It uses its own label and can never suppress a real alert._",
        ])
        if self._create("Watcher is live", body, "alert-test"):
            log("hello sent — check your inbox.")
            return 0
        log("")
        log("COULD NOT OPEN AN ISSUE in %s" % self.repo)
        log("Check that ALERT_TOKEN is a fine-grained token with Issues: Read and")
        log("write, that %s is selected under its repository access, and that it" % self.repo)
        log("has not expired.")
        return 2

    def report_blind(self):
        if not self.enabled:
            log("BLIND: every fetch failed.")
            return
        try:
            open_issues = self._api(
                "GET", "/repos/%s/issues?state=open&labels=monitor-blind" % self.repo)
            if isinstance(open_issues, list) and open_issues:
                log("already reported blind — staying quiet")
                return
        except urllib.error.HTTPError:
            return
        self._create(
            "Watcher can't reach Reddit",
            "Every fetch failed for a whole run.\n\n"
            "**You are not being watched right now.** Most likely Reddit is blocking "
            "the runner's IP or rate-limiting the feed. Check the Actions log. This "
            "closes itself once fetches work again.",
            "monitor-blind")

    def clear_blind(self):
        if not self.enabled:
            return
        try:
            issues = self._api(
                "GET", "/repos/%s/issues?state=open&labels=monitor-blind" % self.repo)
            for issue in issues if isinstance(issues, list) else []:
                self._api("PATCH", "/repos/%s/issues/%d" % (self.repo, issue["number"]),
                          {"state": "closed"})
                log("closed blind report #%d — source reachable again" % issue["number"])
        except urllib.error.HTTPError:
            pass


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def one_pass(cfg, alerter, do_search):
    """Sweep the feeds once. Returns (alerts, feeds_ok, posts_scanned)."""
    feeds = [("new", NEW_FEED)]
    if do_search:
        feeds.append(("search", search_url(cfg["targets"])))

    now = datetime.now(timezone.utc)
    ok = alerts = scanned = 0
    for label, url in feeds:
        xml, err = fetch(url)
        if xml is None:
            log("  %-6s FEED FAILED — %s" % (label, err))
            continue
        posts = parse_atom(xml)
        if not posts:
            log("  %-6s returned no posts — feed shape may have changed" % label)
            continue
        ok += 1
        scanned += len(posts)
        for post in posts:
            if alerter.already_sent(post["id"]):
                continue
            if not match(post, cfg["targets"]):
                continue
            if too_old(post, cfg, now):
                alerter.seen.add(post["id"])   # never look at it again
                continue
            tag = post_tag(post)
            if tag in SKIP_TAGS:
                alerter.seen.add(post["id"])
                log_private("  skipping [%s] (wanted, not for sale): %s" % (tag, post["title"][:60]),
                            "  skipping a [%s] post (wanted, not for sale)" % tag)
                continue
            log_private("  *** MATCH *** %s" % post["title"][:100], "  *** MATCH ***")
            details = listing_details(post["id"], post["title"])
            if details["sold"]:
                # Already gone. Never look at it again — a listing does not
                # come back from sold, and re-checking costs a request a minute.
                alerter.seen.add(post["id"])
                log_private("  already marked %s — skipping: %s"
                            % (details["flair"] or "sold", post["title"][:60]),
                            "  match already marked sold — skipping")
                continue
            if alerter.send(post, match(post, cfg["targets"]), details):
                alerts += 1
    return alerts, ok, scanned


def main():
    cfg = load_config()

    if "--hello" in sys.argv:
        return Alerter().hello(cfg)

    once = "--once" in sys.argv
    passes = 1 if once else int(os.environ.get("PASSES", "5"))
    gap = int(os.environ.get("GAP_SECONDS", "60"))
    # Search indexing lags the feed by minutes, so hitting it every pass buys
    # nothing and just spends rate limit. Every 5th pass is plenty as a net.
    search_every = int(os.environ.get("SEARCH_EVERY", "5"))

    log("config from %s — %d target(s)" % (cfg["source"], len(cfg["targets"])))
    log("skipping listings flaired sold; %s"
        % ("ignoring anything posted before %s" % cfg["since"].strftime("%Y-%m-%d %H:%M UTC")
           if cfg["since"] else "no date cutoff, so any still-live listing counts"))
    alerter = Alerter()
    if not alerter.enabled:
        # On a runner there is nobody reading stdout. Watching for five hours and
        # writing matches to a log is not a degraded service, it is a fake one:
        # it burns minutes and looks healthy while telling you nothing. Six days
        # and three missed listings went by exactly this way. Refuse instead.
        if os.environ.get("GITHUB_ACTIONS"):
            log("REFUSING TO RUN: no ALERT_REPO / ALERT_TOKEN, so a match could")
            log("not reach you. Nothing would be watching you, and this run would")
            log("look green while missing listings.")
            log("")
            log("Add all three secrets under Settings -> Secrets and variables ->")
            log("Actions:  ALERT_TOKEN, ALERT_REPO, TARGETS_JSON")
            return 2
        log("Local/console mode — no ALERT_REPO / ALERT_TOKEN in the environment.")
        log("Matches print here instead of being emailed.")

    # A wall-clock budget, not a pass count. Pass duration varies with fetch
    # time and 429 backoff, so counting passes overshot the runner's job timeout
    # every single time — the job was killed mid-loop and every step after it,
    # including the checks that tell you the watcher has failed, never ran.
    max_minutes = float(os.environ.get("MAX_MINUTES", "0"))
    deadline = time.time() + max_minutes * 60 if max_minutes else None
    if deadline:
        log("watching for up to %g minutes, then exiting cleanly for the next run"
            % max_minutes)

    total_ok = total_alerts = 0
    blind_streak = 0
    reported_blind = False
    for i in range(passes):
        alerts, ok, scanned = one_pass(cfg, alerter, do_search=(i % search_every == 0))
        total_ok += ok
        total_alerts += alerts
        log("pass %d/%d — %d scanned, %d feed(s) ok, %d alert(s)"
            % (i + 1, passes, scanned, ok, alerts))

        # Report blindness DURING the run, not after it. A run lasts hours; a
        # watcher that has been failing every fetch for ten minutes is already
        # not protecting you, and waiting until the end to say so is useless.
        if ok:
            blind_streak = 0
            if reported_blind:
                alerter.clear_blind()
                reported_blind = False
        else:
            blind_streak += 1
            if blind_streak == BLIND_AFTER and not reported_blind:
                log("ERROR: %d passes in a row with every fetch failing — reporting."
                    % blind_streak)
                alerter.report_blind()
                reported_blind = True

        if deadline and time.time() >= deadline:
            log("time budget reached after %d pass(es) — handing over to the next run."
                % (i + 1))
            break
        if i < passes - 1:
            time.sleep(gap)

    if total_ok == 0:
        log("ERROR: every fetch failed across all %d pass(es) — the watcher is blind." % passes)
        if not reported_blind:
            alerter.report_blind()
        return 1

    if not reported_blind:
        alerter.clear_blind()
    if alerter.failures:
        log("ERROR: %d listing(s) matched but the alert could not be delivered."
            % alerter.failures)
        return 3
    log("Done. %d alert(s) sent this run." % total_alerts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
