#!/usr/bin/env python3
"""
Home Depot penny-item daily notifier.

What it does, in plain terms:
  1. Downloads the community penny-find list (PennyCentral by default).
  2. Keeps only finds reported in YOUR state (default: NJ).
  3. Remembers what it already told you (seen.json) so you only get NEW finds.
  4. Sends the new finds to your phone as a push notification via ntfy.

It is meant to be run once a day by a scheduler (GitHub Actions).
Nothing here is Home Depot-specific scraping -- it reads a public,
community-maintained leads list and forwards it to you.

Configuration is via environment variables (set in GitHub, see README):
  NTFY_TOPIC   (required) the secret ntfy topic your phone is subscribed to
  NTFY_SERVER  (optional) defaults to https://ntfy.sh
  PENNY_STATE  (optional) defaults to NJ
  PENNY_URL    (optional) defaults to the PennyCentral penny list
  NOTIFY_EMPTY (optional) set to "1" to get a daily "nothing new" ping
  PENNY_DEBUG  (optional) set to "1" for extra logging
"""

import os
import re
import sys
import json
import html
import hashlib
import urllib.request
import urllib.error

# ---------- settings (read from environment, with sensible defaults) ----------
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER  = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
STATE        = os.environ.get("PENNY_STATE", "NJ").strip()
PENNY_URL    = os.environ.get("PENNY_URL", "https://www.pennycentral.com/penny-list").strip()
NOTIFY_EMPTY = os.environ.get("NOTIFY_EMPTY", "0") == "1"
DEBUG        = os.environ.get("PENNY_DEBUG", "0") == "1"
LOOKUP_NAMES = os.environ.get("LOOKUP_NAMES", "1") == "1"  # fill item name from SKU
SEEN_FILE    = "seen.json"

# state can be written as "NJ", "New Jersey", "N.J." -- accept all
STATE_ALIASES = {
    "NJ": {"nj", "n.j.", "new jersey"},
}
WANT_STATE_FORMS = STATE_ALIASES.get(STATE.upper(), {STATE.lower()})
WANT_STATE_FORMS = set(WANT_STATE_FORMS) | {STATE.lower()}

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/json,*/*",
}


def log(*a):
    print(*a, flush=True)


# ----------------------------- fetching ---------------------------------------
def fetch(url):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8", "replace")
    except Exception:
        return raw.decode("latin-1", "replace")


# ----------------------------- parsing ----------------------------------------
# We don't know the exact page structure, so we try several strategies and
# stop at the first that yields finds. Strategy 1 (embedded JSON) is the most
# reliable on modern sites; the others are fallbacks.

def _walk(obj):
    """Yield every dict found anywhere inside a nested JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _looks_like_find(d):
    """Heuristic: a 'find' dict usually has a sku/upc AND a state/location."""
    keys = {k.lower() for k in d.keys()}
    has_id = bool(keys & {"sku", "upc", "internetnumber", "item", "itemid", "modelnumber"})
    has_loc = bool(keys & {"state", "location", "store", "region", "statecode", "city"})
    return has_id and has_loc


def _state_matches(d):
    text_bits = []
    for k in ("state", "statecode", "location", "store", "region", "city", "address"):
        for kk in d:
            if kk.lower() == k and d[kk]:
                text_bits.append(str(d[kk]).lower())
    blob = " ".join(text_bits)
    return any(form in blob for form in WANT_STATE_FORMS)


def parse_embedded_json(htmltext):
    """Strategy 1: pull <script> JSON blobs (Next.js __NEXT_DATA__, etc.)."""
    finds = []
    # grab the big known blobs first, then any application/json script
    patterns = [
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    ]
    blobs = []
    for pat in patterns:
        blobs += re.findall(pat, htmltext, re.DOTALL)
    for blob in blobs:
        blob = blob.strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        for d in _walk(data):
            if _looks_like_find(d) and _state_matches(d):
                finds.append(d)
    return finds


def parse_text_fallback(htmltext):
    """Strategy 2: very rough text scan for our state near a SKU-like number.
    Only used if the JSON strategy finds nothing. Low precision on purpose."""
    finds = []
    text = re.sub(r"<[^>]+>", " ", htmltext)
    text = html.unescape(text)
    lines = [ln.strip() for ln in re.split(r"[\n\r]+", text) if ln.strip()]
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(form in low for form in WANT_STATE_FORMS):
            window = " ".join(lines[max(0, i - 2): i + 3])
            sku = re.search(r"\b\d{6,12}\b", window)
            if sku:
                finds.append({"raw": window[:200], "sku": sku.group(0), "state": STATE})
    return finds


def _dedupe(finds):
    """Remove finds that resolve to the same identity (e.g. a blob parsed twice)."""
    out, seen_ids = [], set()
    for d in finds:
        fid = find_id(d)
        if fid not in seen_ids:
            seen_ids.add(fid)
            out.append(d)
    return out


def get_finds(htmltext):
    finds = _dedupe(parse_embedded_json(htmltext))
    if finds:
        log(f"[parse] embedded-JSON strategy found {len(finds)} {STATE} finds")
        return finds, "json"
    finds = _dedupe(parse_text_fallback(htmltext))
    if finds:
        log(f"[parse] text-fallback strategy found {len(finds)} {STATE} finds")
        return finds, "text"
    return [], "none"


# ----------------------------- identity / dedup -------------------------------
def find_id(d):
    for k in ("id", "_id", "uuid"):
        for kk in d:
            if kk.lower() == k and d[kk]:
                return str(d[kk])
    norm = json.dumps(d, sort_keys=True, default=str)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    # cap the file so it can't grow forever
    trimmed = list(seen)[-5000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


# ----------------------------- formatting -------------------------------------
# Field names we look for, in priority order. Comparison is case-insensitive,
# so "storeName", "store_name", "STORE" all match "storename" etc.
NAME_KEYS  = ("title", "name", "product", "productname", "product_name",
              "item", "itemname", "item_name", "description", "desc")
SKU_KEYS   = ("sku", "upc", "internetnumber", "internet_number",
              "modelnumber", "model", "itemid", "item_id")
STORE_KEYS = ("store", "storename", "store_name", "storenumber", "store_number",
              "city", "location", "address")
STATE_KEYS = ("state", "statecode", "state_code")
DATE_KEYS  = ("date", "reportedat", "reported_at", "createdat", "created_at",
              "time", "timestamp", "reported")
LINK_KEYS  = ("url", "link", "permalink", "href", "source")

# Keys we never want to print (internal IDs, image blobs, etc.)
SKIP_KEYS = {"id", "_id", "uuid", "__typename", "key", "slug", "image",
             "images", "img", "thumbnail", "photo", "hash", "raw", "icon"}


def field(d, names):
    for n in names:
        for kk in d:
            if kk.lower() == n and d[kk] not in (None, ""):
                return str(d[kk])
    return ""


def _useful(v):
    """A value worth printing: short, non-empty, not a nested structure."""
    if v is None or isinstance(v, (dict, list)):
        return False
    s = str(v).strip()
    return bool(s) and len(s) <= 80 and s.lower() not in ("none", "null")


def _label(k):
    return k.replace("_", " ").strip().title()


def lookup_product_name(code):
    """Best-effort: turn a 12-13 digit UPC into a product name using upcitemdb's
    free (no-key, rate-limited) endpoint. Returns '' on any failure -- a missing
    name should never break the notification."""
    if not LOOKUP_NAMES:
        return ""
    digits = re.sub(r"\D", "", code or "")
    if len(digits) not in (12, 13):
        return ""  # not a UPC we can look up (e.g. a Home Depot internal SKU)
    try:
        url = f"https://api.upcitemdb.com/prod/trial/lookup?upc={digits}"
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        items = data.get("items") or []
        if items and items[0].get("title"):
            return str(items[0]["title"]).strip()
    except Exception as e:
        log(f"[lookup] could not resolve {digits}: {e}")
    return ""


def describe(d):
    name  = field(d, NAME_KEYS)
    sku   = field(d, SKU_KEYS)
    store = field(d, STORE_KEYS)
    state = field(d, STATE_KEYS)
    date  = field(d, DATE_KEYS)
    link  = field(d, LINK_KEYS)

    # If the report didn't include a name, try to resolve it from the SKU.
    if not name and sku:
        name = lookup_product_name(sku)

    where = store or state or "not listed in report"

    lines = [
        f"Item:  {name or 'not listed in report'}",
        f"Store: {where}",
        f"SKU:   {sku or 'n/a'}",
    ]
    if date:
        lines.append(f"Date:  {date}")
    if link:
        lines.append(link)
    return "\n".join(lines)


# ----------------------------- notifying --------------------------------------
def ntfy(title, message, priority="default", tags=None):
    if not NTFY_TOPIC:
        log("[ntfy] NTFY_TOPIC is not set -- cannot send. (printing instead)")
        log(f"       {title}\n{message}")
        return
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = tags
    req = urllib.request.Request(url, data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"[ntfy] sent ({resp.status})")
    except Exception as e:
        log(f"[ntfy] FAILED to send: {e}")


# ----------------------------- main -------------------------------------------
def main():
    log(f"=== penny notifier: state={STATE} url={PENNY_URL} ===")

    try:
        htmltext = fetch(PENNY_URL)
    except Exception as e:
        ntfy("Penny notifier error",
             f"Could not download the penny list:\n{e}",
             priority="high", tags="warning")
        log(f"[fetch] FAILED: {e}")
        return 0  # exit clean so the scheduler doesn't email you a failure

    finds, strategy = get_finds(htmltext)

    if DEBUG and finds:
        log("[debug] sample raw finds (first 3) -- send these to Claude to "
            "fine-tune field names:")
        for d in finds[:3]:
            log("  " + json.dumps(d, default=str)[:600])

    if strategy == "none":
        # Nothing parsed -- almost always means the page layout differs from my
        # guess, OR the data is loaded by JavaScript and isn't in the raw HTML.
        # Print clues so they can be pasted back to fix the parser.
        log("[debug] No finds parsed. Diagnostics below.")
        log(f"[debug] page length: {len(htmltext)} chars")
        log(f"[debug] has __NEXT_DATA__: {'__NEXT_DATA__' in htmltext}")
        log(f"[debug] mentions state '{STATE}': "
            f"{any(f in htmltext.lower() for f in WANT_STATE_FORMS)}")
        snippet = htmltext[:1500].replace("\n", " ")
        log(f"[debug] first 1500 chars:\n{snippet}")
        ntfy("Penny notifier needs a tweak",
             "The script ran but couldn't read any finds. Open the GitHub "
             "Actions log, copy the [debug] lines, and send them to Claude to "
             "fix the parser.",
             priority="high", tags="hammer_and_wrench")
        return 0

    seen = load_seen()
    new = [d for d in finds if find_id(d) not in seen]

    log(f"[result] total {STATE} finds on page: {len(finds)}; new: {len(new)}")

    if not new:
        if NOTIFY_EMPTY:
            ntfy(f"No new {STATE} penny finds today",
                 "Nothing new on the list. Check again tomorrow.",
                 tags="coffee")
        # still record current ids so the file stays fresh
        for d in finds:
            seen.add(find_id(d))
        save_seen(seen)
        return 0

    blocks = [describe(d) for d in new[:30]]
    body = "\n\n".join(blocks)
    if len(new) > 30:
        body += f"\n\n…and {len(new) - 30} more."
    body += "\n\nLeads only — verify with a UPC scan at the store."

    ntfy(f"{len(new)} new {STATE} penny find(s)", body,
         priority="high", tags="moneybag")

    for d in new:
        seen.add(find_id(d))
    save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
