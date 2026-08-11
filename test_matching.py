#!/usr/bin/env python3
"""
Proves the watcher fires, stays quiet, and ignores the backlog.

A monitor that can never trigger is worse than no monitor, and you cannot wait
around for a real listing to appear to find out.

The cases here use invented references, on purpose: this repo is public, and a
test suite full of the references you are actually hunting would publish your
want-list as surely as the config would. They exercise every matching mechanic
— spacing, punctuation, longer references, nickname-only listings, near-misses.

If `test_cases.local.json` exists (gitignored) its cases are run too, against
your real config. That is where listing titles you actually care about belong.

    python3 test_matching.py [--offline]
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import check_watchexchange as w

NOW = datetime.now(timezone.utc)
HERE = os.path.dirname(os.path.abspath(__file__))

# A fictional catalogue, shaped exactly like a real one.
DEMO = {
    "watch_listings_after": "2026-01-01T00:00:00Z",
    "targets": [
        {"name": "Acme XY7788", "refs": ["XY7788", "123.45.67.89.00.001"],
         "aliases": ["Frost Meadow"]},
        {"name": "Acme QQ0042", "refs": ["QQ0042"], "aliases": []},
    ],
}

DEMO_FIRE = [
    ("[WTS] Acme XY7788 full set", "XY7788"),
    ("[WTS] Acme XY 7788, box and papers", "XY7788"),          # space in the ref
    ("[WTS] acme xy-7788 hand wound", "XY7788"),               # hyphen + lowercase
    ("[WTS] Acme ref. XY7788G mint", "XY7788"),                # longer ref, same watch
    ("[WTS] Acme Frost Meadow 38mm, full kit $4,000", "XY7788"),  # nickname only
    ("[WTS] Acme Numbered Edition (123.45.67.89.00.001)", "XY7788"),  # dotted ref
    ("[WTS] Acme QQ0042 excellent condition", "QQ0042"),
]

DEMO_QUIET = [
    "[WTS] Acme XY7789 full set",        # one digit off
    "[WTS] Acme XY778 vintage",          # shorter, different reference
    "[WTS] Acme Frost Valley 40mm",      # nickname that is not the nickname
    "[WTS] Acme QQ0043",
    "[WTS] Beta Watch Co 126660",
    "[WTS] Gamma Chronograph 3861",
]

DEMO_SKIP = [
    "[WTB] Acme XY7788 wanted, paying cash",
    "[WTB] Acme Frost Meadow",
]

PRICES = [
    ("Asking Price: 💲 8,300 USD", "$8,300"),
    ("asking price $8300 shipped", "$8300"),
    ("Price: 5,950 USD, trades welcome", "$5,950"),
    ("[WTS] Acme XY7788 $8,300", "$8,300"),
    ("no price here, DM me", ""),
    ("has 84 Transactions", ""),          # transaction counts are not prices
]


# Markup copied in shape from real old.reddit pages: a live listing carries a
# price-band flair, a finished one carries 'Sold'. Getting this backwards either
# floods you with dead listings or hides live ones, so it is pinned down here.
SOLD_CASES = [
    ('<body class="post-linkflair-sold single-page">', "[WTS] Acme XY7788", True, "body class"),
    ('<div class=" thing linkflair linkflair-sold link ">', "[WTS] Acme XY7788", True,
     "thing class"),
    ('<span class="linkflairlabel " title="Sold">Sold</span>', "[WTS] Acme XY7788", True,
     "flair label"),
    ('<span class="linkflairlabel " title="Traded">Traded</span>', "[WTS] Acme XY7788", True,
     "traded flair"),
    ('<span class="linkflairlabel " title="$7000-$8999">$7000-$8999</span>',
     "[WTS] Acme XY7788", False, "price band means live"),
    ("<html>a page with no flair</html>", "[WTS] Acme XY7788", False, "no flair means live"),
    (None, "[WTS] Acme XY7788 - SOLD", True, "seller edited SOLD into the title"),
    # Unknown status must fail OPEN: a false alarm costs a glance, staying quiet
    # on a live listing costs the watch.
    (None, "[WTS] Acme XY7788 $4,000", False, "page unreachable, status unknown"),
]


def post(title, posted=None, blob=None):
    return {"id": "x", "title": title, "url": "", "author": "", "blob": blob or title,
            "posted": posted}


def build(cfg_dict):
    """Turn a raw config dict into the matcher's target list."""
    return [{"name": t["name"],
             "needles": [n for n in (w.norm(x) for x in t["refs"] + t["aliases"]) if n]}
            for t in cfg_dict["targets"]]


def run_case_set(label, targets, fire, quiet, skip, failures):
    hit = 0
    for title, ref in fire:
        hits = w.match(post(title), targets)
        if not hits:
            failures.append("%s MISSED (%s): %s" % (label, ref, title))
        elif ref not in "".join(n for n, _ in hits) and ref not in "".join(
                t["name"] for t in targets if any(ref in n for n in t["needles"])):
            failures.append("%s WRONG TARGET (%s -> %s): %s" % (label, ref, hits[0][0], title))
        else:
            hit += 1
    print("%-9s fire:  %d/%d matched" % (label, hit, len(fire)))

    silent = 0
    for title in quiet:
        if w.match(post(title), targets):
            failures.append("%s FALSE ALARM: %s" % (label, title))
        else:
            silent += 1
    print("%-9s quiet: %d/%d stayed quiet" % (label, silent, len(quiet)))

    skipped = sum(1 for t in skip
                  if w.match(post(t), targets) and w.post_tag(post(t)) in w.SKIP_TAGS)
    if skipped != len(skip):
        failures.append("%s a [WTB] post was not skipped" % label)
    print("%-9s WTB:   %d/%d skipped" % (label, skipped, len(skip)))


def main():
    failures = []

    # --- mechanics, on invented data ---------------------------------------
    run_case_set("demo", build(DEMO), DEMO_FIRE, DEMO_QUIET, DEMO_SKIP, failures)

    # --- your real cases, if you keep any locally ---------------------------
    local = os.path.join(HERE, "test_cases.local.json")
    cfg = w.load_config()
    if os.path.exists(local):
        with open(local, encoding="utf-8") as fh:
            cases = json.load(fh)
        print()
        run_case_set("live", cfg["targets"],
                     [(c[0], c[1]) for c in cases.get("must_fire", [])],
                     cases.get("must_not_fire", []), cases.get("must_skip", []), failures)
    print()

    # --- the optional date cutoff -------------------------------------------
    # Off by default now: a listing still counts however old it is, as long as
    # it is not flaired sold. These prove the option still works if you set it.
    cut = NOW - timedelta(days=1)
    dated = {"since": cut, "max_age": 0}
    cases = [
        ("pre-cutoff listing ignored when a cutoff is set",
         w.too_old(post("x", posted=cut - timedelta(days=3)), dated, NOW) is True),
        ("post-cutoff listing alerts",
         w.too_old(post("x", posted=cut + timedelta(minutes=1)), dated, NOW) is False),
        ("undated listing alerts rather than being dropped",
         w.too_old(post("x", posted=None), dated, NOW) is False),
        ("with no cutoff configured, an old listing still alerts",
         w.too_old(post("x", posted=NOW - timedelta(days=30)),
                   {"since": None, "max_age": 0}, NOW) is False),
    ]
    for label, passed in cases:
        if not passed:
            failures.append("CUTOFF: %s" % label)
    print("cutoff:   %d/%d correct" % (sum(1 for _, p in cases if p), len(cases)))

    # --- sold listings must never reach the inbox ---------------------------
    good = 0
    for page, title, expected, why in SOLD_CASES:
        got, _ = w.is_sold(page, title)
        if got == expected:
            good += 1
        else:
            failures.append("SOLD: %s — expected sold=%s, got %s" % (why, expected, got))
    print("sold:     %d/%d correct" % (good, len(SOLD_CASES)))

    # --- price extraction for the email subject line ------------------------
    good = 0
    for text, expected in PRICES:
        got = w.extract_price(text)
        if got == expected:
            good += 1
        else:
            failures.append("PRICE: %r -> %r, wanted %r" % (text, got, expected))
    print("price:    %d/%d correct" % (good, len(PRICES)))

    # --- the feed itself ----------------------------------------------------
    if "--offline" not in sys.argv:
        xml, err = w.fetch(w.NEW_FEED)
        if xml is None:
            print("live feed: SKIPPED (%s)" % err)
        else:
            posts = w.parse_atom(xml)
            dated = sum(1 for p in posts if p["posted"])
            print("live feed: %d posts, %d with readable timestamps, %d matched"
                  % (len(posts), dated, len([p for p in posts if w.match(p, cfg["targets"])])))
            if len(posts) < 50:
                failures.append("live feed returned only %d posts — parser may be broken"
                                % len(posts))
            if dated < len(posts):
                failures.append("%d posts had no readable timestamp — the cutoff would not "
                                "protect you" % (len(posts) - dated))

    print()
    if failures:
        print("FAILED (%d):" % len(failures))
        for f in failures:
            print("  " + f)
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
