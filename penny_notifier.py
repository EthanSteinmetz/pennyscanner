#!/usr/bin/env python3
"""
Home Depot penny-item daily notifier — Bergen County (NJ) edition.

Each day it:
  1. Downloads PennyCentral's live penny list.
  2. Keeps items relevant to NJ (reported there, or so widespread NJ is a
     near-certainty).
  3. Opens each item's detail page to read the per-town "Recent sightings".
  4. Stars items with a Bergen County town; shows the town when one exists.
  5. Sends only what's new to your phone via ntfy.

Reportable fields per alert: item name, SKU, and town/location (when present).
NOTE: the source records a TOWN only when a reporter adds one (no store address
ever exists), so some NJ items will show "town not reported."

Environment variables (set in GitHub — see README):
  NTFY_TOPIC       (required) your secret ntfy topic
  NTFY_SERVER      (optional) default https://ntfy.sh
  PENNY_STATE      (optional) default NJ
  STATE_THRESHOLD  (optional) widespread cutoff, default 12
  BERGEN_ONLY      (optional) "1" = only alert items with a Bergen town
                   (far fewer alerts), default "0" (all NJ, Bergen starred)
  NOTIFY_EMPTY     (optional) "1" = daily ping even when nothing new
  PENNY_DEBUG      (optional) "1" = verbose logging
"""

import os
import re
import sys
import json
import gzip
import html
import hashlib
import urllib.request

NTFY_TOPIC      = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER     = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
STATE           = os.environ.get("PENNY_STATE", "NJ").strip().upper()
PENNY_URL       = os.environ.get("PENNY_URL", "https://www.pennycentral.com/penny-list").strip()
STATE_THRESHOLD = int(os.environ.get("STATE_THRESHOLD", "12"))
BERGEN_ONLY     = os.environ.get("BERGEN_ONLY", "0") == "1"
NOTIFY_EMPTY    = os.environ.get("NOTIFY_EMPTY", "0") == "1"
DEBUG           = os.environ.get("PENNY_DEBUG", "0") == "1"
SEEN_FILE       = "seen.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Bergen County, NJ municipalities (lowercased). Used to match a reported town.
BERGEN_CITIES = {c.lower() for c in [
    "Allendale", "Alpine", "Bergenfield", "Bogota", "Carlstadt", "Cliffside Park",
    "Closter", "Cresskill", "Demarest", "Dumont", "East Rutherford", "Edgewater",
    "Elmwood Park", "Emerson", "Englewood", "Englewood Cliffs", "Fair Lawn",
    "Fairview", "Fort Lee", "Franklin Lakes", "Garfield", "Glen Rock",
    "Hackensack", "Harrington Park", "Hasbrouck Heights", "Haworth", "Hillsdale",
    "Ho-Ho-Kus", "Leonia", "Little Ferry", "Lodi", "Lyndhurst", "Mahwah",
    "Maywood", "Midland Park", "Montvale", "Moonachie", "New Milford", "Northvale",
    "Norwood", "Oakland", "Old Tappan", "Oradell", "Palisades Park", "Paramus",
    "Park Ridge", "Ramsey", "Ridgefield", "Ridgefield Park", "Ridgewood",
    "River Edge", "River Vale", "Rochelle Park", "Rockleigh", "Rutherford",
    "Saddle Brook", "Saddle River", "South Hackensack", "Teaneck", "Tenafly",
    "Teterboro", "Upper Saddle River", "Waldwick", "Wallington", "Washington Township",
    "Westwood", "Woodcliff Lake", "Wood-Ridge", "Wyckoff",
]}


def log(*a):
    print(*a, flush=True)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


# --- parse the main penny list -------------------------------------------------
BLOCK = re.compile(
    r"\$0\.01"
    r"(?s:.*?)SKU[\s:]*([0-9][0-9\-]+)"
    r"(?s:.*?)(\d+)\s*reports?(?s:.{0,6}?)(\d+)\s*states?"
    r"(?s:.*?)([A-Z]{2}\d+(?:[A-Z]{2}\d+)*)(?:\s*\+\s*(\d+)\s*more)?"
    r"(?s:.*?)(https?://www\.homedepot\.com/[ps]/[^\s\"'<>)\]]+)"
)


def name_from_url(url):
    m = re.search(r"/p/(?:sets/)?([^/]+)/\d+", url)
    if m and not m.group(1).isdigit():
        return m.group(1).replace("-", " ").strip()
    return ""


def parse_list(page):
    page = html.unescape(page)
    finds = []
    for m in BLOCK.finditer(page):
        sku, reports, states, statedist, _more, url = m.groups()
        states = int(states)
        njm = re.search(rf"{STATE}(\d+)", statedist)
        nj_reports = int(njm.group(1)) if njm else 0
        if nj_reports == 0 and states < STATE_THRESHOLD:
            continue
        finds.append({
            "sku": sku,
            "name": name_from_url(url) or "(name not listed — tap link)",
            "url": url,
            "states": states,
            "nj_reports": nj_reports,
        })
    return finds


# --- parse a detail page's per-town sightings ---------------------------------
SIGHTING = re.compile(
    r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s+([A-Z]{2})\s+(.+?)"
    r"(?=[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}|Load more|Related penny|$)"
)


def parse_towns(detail_html, state):
    """Return the list of towns reported for `state` on a detail page.
    Blank/'—' entries are dropped."""
    text = html.unescape(detail_html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("|", " ")
    text = re.sub(r"\s+", " ", text)
    towns = []
    for m in SIGHTING.finditer(text):
        _date, st, city = m.groups()
        if st != state:
            continue
        city = city.strip(" |").strip("–—-").strip()
        if city and city not in ("—", "–", "-"):
            towns.append(city)
    return towns


def detail_url(sku):
    return f"https://www.pennycentral.com/sku/{sku.replace('-', '')}"


def enrich_with_towns(find):
    try:
        page = fetch(detail_url(find["sku"]), timeout=12)
    except Exception as e:
        log(f"[detail] {find['sku']}: {e}")
        find["towns"] = []
        find["bergen"] = []
        return
    towns = parse_towns(page, STATE)
    find["towns"] = sorted(set(towns))
    find["bergen"] = sorted({t for t in towns if t.lower() in BERGEN_CITIES})


# --- formatting / state -------------------------------------------------------
def find_id(d):
    return hashlib.sha1(d["sku"].encode()).hexdigest()[:16]


def dedupe(finds):
    out, seen = [], set()
    for d in finds:
        fid = find_id(d)
        if fid not in seen:
            seen.add(fid)
            out.append(d)
    return out


def location_line(d):
    if d.get("bergen"):
        return "Location: ★ BERGEN COUNTY — " + ", ".join(d["bergen"])
    if d.get("towns"):
        return "Location: NJ towns — " + ", ".join(d["towns"]) + " (not Bergen)"
    return "Location: NJ (town not reported)"


def describe(d):
    lines = [
        f"Item:  {d['name']}",
        f"SKU:   {d['sku']}",
        location_line(d),
        d["url"],
    ]
    return "\n".join(lines)


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-5000:], f)


def ntfy(title, message, priority="default", tags=None):
    if not NTFY_TOPIC:
        log(f"[ntfy] no topic set; would have sent:\n{title}\n{message}")
        return
    headers = {"Title": title, "Priority": priority,
               "Content-Type": "text/plain; charset=utf-8"}
    if tags:
        headers["Tags"] = tags
    req = urllib.request.Request(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                                 data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"[ntfy] sent ({r.status})")
    except Exception as e:
        log(f"[ntfy] FAILED: {e}")


def main():
    log(f"=== penny notifier: state={STATE} bergen_only={BERGEN_ONLY} ===")
    try:
        page = fetch(PENNY_URL)
    except Exception as e:
        ntfy("Penny notifier error", f"Couldn't download the list:\n{e}",
             priority="high", tags="warning")
        log(f"[fetch] FAILED: {e}")
        return 0

    finds = dedupe(parse_list(page))
    log(f"[result] NJ-relevant finds: {len(finds)}")

    if not finds:
        log(f"[debug] page len {len(page)}")
        for tok in ["$0.01", "SKU", "homedepot.com", "__next_f", "pennycentral"]:
            log(f"[debug] contains {tok!r}: {tok in page}")
        for marker in ["homedepot.com/p/", "homedepot.com/s/"]:
            i = page.find(marker)
            if i != -1:
                log(f"[debug] near {marker}: "
                    f"{page[max(0, i - 700):i + 200].replace(chr(10), ' ')}")
                break
        skum = re.search(r"\d{3,4}-\d{3}-\d{3}", page)
        if skum:
            j = skum.start()
            log(f"[debug] card from sku: "
                f"{page[max(0, j - 100):j + 2200].replace(chr(10), ' ')}")
        ntfy("Penny notifier needs a tweak",
             "Ran but parsed 0 items. Send Claude the [debug] lines from the "
             "Actions log.", priority="high", tags="hammer_and_wrench")
        return 0

    # Town lookups are the slow part, so only open detail pages for items
    # actually reported in NJ (the only ones that can carry an NJ/Bergen town).
    # The big nationwide items get labeled without a lookup.
    to_check = [d for d in finds if d["nj_reports"] > 0][:30]
    log(f"[detail] looking up towns for {len(to_check)} NJ-reported item(s)")
    for d in to_check:
        enrich_with_towns(d)
    for d in finds:
        d.setdefault("towns", [])
        d.setdefault("bergen", [])

    if BERGEN_ONLY:
        finds = [d for d in finds if d["bergen"]]
        log(f"[result] Bergen-only finds: {len(finds)}")

    if DEBUG:
        for d in finds[:5]:
            log("[debug] " + json.dumps(d))

    seen = load_seen()
    new = [d for d in finds if find_id(d) not in seen]
    log(f"[result] new since last run: {len(new)}")

    if not new:
        if NOTIFY_EMPTY:
            ntfy(f"No new {STATE} penny finds", "Nothing new today.", tags="coffee")
        for d in finds:
            seen.add(find_id(d))
        save_seen(seen)
        return 0

    # Put Bergen-starred items first.
    new.sort(key=lambda d: (not d.get("bergen"), d["name"]))
    body = "\n\n".join(describe(d) for d in new[:25])
    if len(new) > 25:
        body += f"\n\n…and {len(new) - 25} more."
    body += "\n\nLeads only — scan the UPC at the store to confirm $0.01."
    bergen_count = sum(1 for d in new if d.get("bergen"))
    title = f"{len(new)} new {STATE} find(s)"
    if bergen_count:
        title = f"★ {bergen_count} Bergen + {len(new) - bergen_count} NJ find(s)"
    ntfy(title, body, priority="high", tags="moneybag")

    for d in new:
        seen.add(find_id(d))
    save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
