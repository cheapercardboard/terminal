#!/usr/bin/env python3
"""MTG Terminal collector.

Prices come from TCGCSV (https://tcgcsv.com, a daily mirror of TCGplayer's catalog and prices).
Format legality comes from Scryfall's "oracle cards" bulk file (TCGCSV has none) and is joined on card NAME,
so promos, Secret Lair, World Championship cards and every other set get legality too.

Follows TCGCSV's usage guidelines: a custom User-Agent, a pause between requests, the Magic prices are requested
ONCE per TCGCSV update (never twice within 24 hours; see PriceCache), and nothing is fetched when last-updated.txt
shows no new data. TCGCSV no longer publishes price archives, so price history is built from our own daily snapshots.

Writes:
  site/data.js        what the website loads (window.MTG = {...})
  data/history.json   one price per card per day, so price changes can be computed; commit it back each run
  cache/              today's price files (restored by the workflow's cache step so a re-run never re-requests them)

Usage:
  python collect_tcgcsv.py                      # every set TCGCSV lists (all card types)
  python collect_tcgcsv.py --sets 30            # only the 30 most recently released sets
  python collect_tcgcsv.py --real-sets-only     # skip promos / Secret Lair / The List etc.
  python collect_tcgcsv.py --group-ids 24312    # exact TCGCSV group ids
"""
import argparse, datetime, difflib, gzip, io, json, os, random, re, shutil, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request

TCGCSV = "https://tcgcsv.com/tcgplayer/1"        # category 1 = Magic: The Gathering
SCRYFALL = "https://api.scryfall.com"
HISTORY_FILE = os.path.join("data", "history.json")
OUT_FILE = os.path.join("site", "data.js")
LAST_UPDATED = "https://tcgcsv.com/last-updated.txt"
CACHE_DIR = "cache"
HISTORY_DAYS = 45
CHART_DAYS = 30
TIMEOUT = 30
CONTACT = os.environ.get("CONTACT_EMAIL") or "contact@example.com"
AGENT = f"MTGTerminal/1.0 (community project; contact: {CONTACT})"

# Scryfall legality keys exposed in the format filter. The site reads a card's `fm` number as a bit mask over this list.
FORMATS = ["standard", "pioneer", "modern", "legacy", "vintage", "commander", "pauper", "paupercommander",
           "brawl", "standardbrawl", "historic", "timeless", "alchemy", "oathbreaker", "premodern", "penny"]
REAL_SET_TYPES = {"core", "expansion", "masters", "draft_innovation", "commander"}


# ---------- fetching ----------
def fetch(url, tries=5, timeout=TIMEOUT, text=False):
    """GET json (or plain text with text=True). Waits and retries on 429/503 (honouring Retry-After) and on network hiccups."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":   # some download hosts send the file gzip-compressed even when not asked
                raw = gzip.decompress(raw)
            body = raw.decode("utf-8", errors="replace")
            return body if text else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == tries - 1:
                raise
            try:
                wait = float(e.headers.get("Retry-After", ""))
            except ValueError:
                wait = 0
            time.sleep(min(max(wait, 5 * 2 ** attempt), 120) + random.uniform(0, 3))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def polite(delay):
    if delay:
        time.sleep(delay)


# ---------- small helpers ----------
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def card_key(name):
    """Normalised card name with TCGplayer's variant notes like '(Borderless)' or '(0123) (Showcase)' removed."""
    return norm(re.sub(r"\(.*?\)", " ", name or ""))


def legal_mask(legal, name):
    """Look a TCGplayer product name up in the legality table, trying a few cleaned-up spellings."""
    base = re.sub(r"\(.*?\)", " ", name or "")
    for cand in (base, base.split(" - ")[0], base.split(" // ")[0]):   # '(Borderless)' already removed; also 'Name - Variant', 'Front // Back'
        m = legal.get(norm(cand))
        if m is not None:
            return m
    return None


def ext(product, field):
    for e in product.get("extendedData") or []:
        if e.get("name") == field:
            return e.get("value")
    return None


def num(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def pick_price(rows):
    """rows: {subtype name lowercased: TCGCSV price row}. Returns (row used, market price or None). Normal printing wins."""
    main = rows.get("normal") or next(iter(rows.values()))
    return main, num(main.get("marketPrice")) or num(main.get("midPrice"))


# ---------- legality (Scryfall; keyed by card name) ----------
def _name_keys(name):
    keys = [norm(name or "")]
    if " // " in (name or ""):
        keys.append(norm(name.split(" // ")[0]))   # TCGplayer sometimes lists only the front face
    return keys


def bulk_download_uri():
    """Find the download link of Scryfall's 'oracle cards' file without assuming the exact shape of the listing."""
    entry = next((b for b in fetch(f"{SCRYFALL}/bulk-data")["data"] if b.get("type") == "oracle_cards"), None)
    if entry is None:
        raise RuntimeError("Scryfall's bulk-data list has no oracle_cards entry")
    for obj in (entry, None):
        if obj is None:   # the listing entry did not carry the link: ask for the entry itself
            obj = fetch(entry.get("uri") or f"{SCRYFALL}/bulk-data/oracle_cards")
        for k, v in obj.items():
            if "download" in k and isinstance(v, str) and v.startswith("http"):
                return v
    raise RuntimeError(f"Scryfall's bulk-data entry has no download link (its keys: {', '.join(sorted(entry))})")


def legalities_from_bulk():
    uri = bulk_download_uri()
    print("Downloading Scryfall oracle cards (one-off, a few minutes at most)...")
    out = {}
    for c in fetch(uri, tries=3, timeout=600):
        mask = 0
        for i, f in enumerate(FORMATS):
            if (c.get("legalities") or {}).get(f) in ("legal", "restricted"):
                mask |= 1 << i
        for k in _name_keys(c.get("name")):
            out.setdefault(k, mask)
    return out


def legalities_by_search(delay=0.12):
    """Slower fallback that needs no big download: ask Scryfall which cards are legal in each format, a page at a time.
    Cards legal in no format at all are simply absent (the site shows them as 'Legality unknown')."""
    out = {}
    for i, f in enumerate(FORMATS):
        url = f"{SCRYFALL}/cards/search?" + urllib.parse.urlencode({"q": f"f:{f}", "unique": "cards", "order": "name"})
        while url:
            try:
                page = fetch(url)
            except urllib.error.HTTPError as e:
                if e.code == 404:   # a search with no results is a 404
                    break
                raise
            for c in page.get("data", []):
                for k in _name_keys(c.get("name")):
                    out[k] = out.get(k, 0) | (1 << i)
            url = page.get("next_page") if page.get("has_more") else None
            polite(delay)
        print(f"  {f}: {sum(1 for m in out.values() if (m >> i) & 1):,} card names")
    return out


def load_legalities():
    """Returns {normalised card name: bit mask of legal formats}."""
    try:
        out = legalities_from_bulk()
    except Exception as e:
        print(f"  Scryfall's bulk file was not usable ({type(e).__name__}: {str(e)[:160]}); using per-format searches instead (slower).")
        out = legalities_by_search()
    print(f"  {len(out):,} card names with legality")
    return out


# ---------- choosing sets ----------
def scryfall_set_for(group, by_group, by_code, by_name):
    """Only used with --real-sets-only: find the Scryfall set for a TCGCSV group (group id, then code, then name)."""
    s = by_group.get(group.get("groupId"))
    if s:
        return s
    code = (group.get("abbreviation") or "").lower()
    if code in by_code:
        return by_code[code]
    name = norm(group.get("name"))
    tries = [name]
    if name.startswith("commander "):  # TCGplayer says "Commander: Foo", Scryfall says "Foo Commander"
        tries.append(name[len("commander "):] + " commander")
    for t in tries:
        if t in by_name:
            return by_name[t]
    close = difflib.get_close_matches(name, list(by_name), n=1, cutoff=0.92)
    return by_name[close[0]] if close else None


def pick_groups(groups, count, today, real_only=False):
    """Released groups, newest first (count 0 = all). With real_only, promos/tokens/etc. are dropped via Scryfall set types."""
    elig = [g for g in groups if (g.get("publishedOn") or "")[:10] and g["publishedOn"][:10] <= today]
    elig.sort(key=lambda g: g["publishedOn"], reverse=True)
    if real_only:
        sets = fetch(f"{SCRYFALL}/sets")["data"]
        by_code = {s["code"]: s for s in sets}
        by_name = {norm(s["name"]): s for s in sets}
        by_group = {s["tcgplayer_id"]: s for s in sets if s.get("tcgplayer_id")}
        elig = [g for g in elig if (scryfall_set_for(g, by_group, by_code, by_name) or {}).get("set_type") in REAL_SET_TYPES]
    return elig[:count] if count else elig


# ---------- tickers / set codes ----------
STOP = {"of", "the", "a", "an", "and", "to", "in", "on", "for"}


def ticker_base(name):
    plain = re.sub(r"\(.*?\)", " ", name or "")
    plain = plain.split(",")[0] if "," in plain and len(plain.split(",")[0]) >= 3 else plain
    words = [re.sub(r"[^A-Za-z]", "", w) for w in plain.split()]
    words = [w for w in words if w]
    if not words:
        return "CARD"
    sig = [w for w in words if w.lower() not in STOP] or words
    if len(sig) == 1:
        return sig[0][:4].upper()
    if len(sig) == 2:
        return (sig[0][:2] + sig[1][:2]).upper()
    return "".join(w[0] for w in sig[:4]).upper()


def assign_tickers(cards):
    seen = {}
    for c in sorted(cards, key=lambda c: (c["s"], c["n"], c["id"])):
        base = f"{ticker_base(c['n'])}.{c['s']}"
        seen[base] = seen.get(base, 0) + 1
        c["t"] = base if seen[base] == 1 else f"{base}{seen[base]}"


def set_code(group, used):
    """Short unique code for a group. TCGplayer sometimes has no abbreviation or reuses one."""
    code = re.sub(r"[^A-Z0-9]", "", (group.get("abbreviation") or "").upper())[:8]
    if not code or code in used and used[code] != group["groupId"]:
        code = f"{code or 'G'}{group['groupId']}"
    used[code] = group["groupId"]
    return code


# ---------- history ----------
def load_history(path):
    try:
        h = json.load(open(path, encoding="utf-8"))
        if isinstance(h.get("dates"), list) and isinstance(h.get("p"), dict):
            return h
    except Exception:
        pass
    return {"dates": [], "p": {}}


def add_snapshot(hist, day, prices):
    """prices: {product id (str): market price}. Adds (or overwrites) one day's column, in date order, and trims old days."""
    dates = hist["dates"]
    if day in dates:
        i = dates.index(day)
    else:
        i = sum(1 for d in dates if d < day)   # insert position that keeps dates ascending
        dates.insert(i, day)
        for lst in hist["p"].values():
            lst.insert(i, None)
    for pid, price in prices.items():
        lst = hist["p"].setdefault(pid, [None] * len(dates))
        lst[i] = price
    if len(dates) > HISTORY_DAYS:
        cut = len(dates) - HISTORY_DAYS
        del dates[:cut]
        for pid in list(hist["p"]):
            hist["p"][pid] = hist["p"][pid][cut:]
    for pid in [k for k, v in hist["p"].items() if all(x is None for x in v)]:
        del hist["p"][pid]


def pct_since(dates, series, today, days):
    """% change from the latest price at least `days` days old to the latest price."""
    if not series or series[-1] is None:
        return None
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=days)).isoformat()
    for i in range(len(dates) - 2, -1, -1):
        if dates[i] <= cutoff and series[i]:
            return round((series[-1] / series[i] - 1) * 100, 1)
    return None


# ---------- once-per-update fetching ----------
def get_stamp():
    """TCGCSV's own 'last updated' timestamp (None if unavailable)."""
    try:
        t = fetch(LAST_UPDATED, tries=3, text=True).strip()
        return t or None
    except Exception as e:
        print(f"WARNING: could not read {LAST_UPDATED} ({type(e).__name__}); cannot tell whether TCGCSV has new data.", file=sys.stderr)
        return None


def stamp_day(stamp, fallback):
    """The calendar date (UTC) of a stamp like '2026-10-29T20:05:11+0000'; used as the history column for that data."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", stamp or "")
    return m.group(0) if m else fallback


def stamp_key(stamp, fallback):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stamp or fallback)


def trim_prices(rows):
    keep = ("productId", "marketPrice", "lowPrice", "midPrice", "subTypeName")
    return [{k: r.get(k) for k in keep} for r in rows]


class PriceCache:
    """Remembers each set's price file for one TCGCSV update, so running the collector again before TCGCSV publishes
    new data never asks for the same price file twice (TCGCSV asks for at most once per 24 hours)."""

    def __init__(self, root, stamp):
        self.root, self.stamp, self.hit = root, stamp, False
        self.dir = os.path.join(root, "prices")

    def open(self):
        try:
            have = open(os.path.join(self.root, "stamp.txt"), encoding="utf-8").read().strip()
        except OSError:
            have = None
        self.hit = bool(self.stamp) and have == self.stamp
        if not self.hit:
            shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        if not self.hit and self.stamp:
            open(os.path.join(self.root, "stamp.txt"), "w", encoding="utf-8").write(self.stamp)

    def get(self, gid):
        if not self.hit:
            return None
        try:
            return json.load(open(os.path.join(self.dir, f"{gid}.json"), encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, gid, rows):
        with open(os.path.join(self.dir, f"{gid}.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, separators=(",", ":"))


def set_output(name, value):
    """Tell the GitHub workflow something (steps can read it as steps.<id>.outputs.<name>)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


# ---------- main build ----------
def build_cards(code, products, prices, legal, min_price):
    px = {}
    for p in prices:
        px.setdefault(p["productId"], {})[(p.get("subTypeName") or "Normal").lower()] = p
    cards = []
    for p in products:
        pid = p["productId"]
        rows = px.get(pid)
        number = ext(p, "Number")
        mask = legal_mask(legal, p.get("name"))
        if not rows or not (number or ext(p, "Rarity") or mask is not None):
            continue  # no price, or a sealed product / accessory rather than a single card
        main, price = pick_price(rows)
        if price is None or price < min_price:
            continue
        foil = rows.get("foil") if rows.get("normal") else None
        cards.append({
            "id": pid, "n": (p.get("name") or "").strip(), "s": code, "num": str(number or "").split("/")[0],
            "r": (ext(p, "Rarity") or "")[:1].upper(),
            "p": round(price, 2),
            "f": (round(num(foil.get("marketPrice")) or 0, 2) or None) if foil else None,
            "lo": round(num(main.get("lowPrice")) or 0, 2) or None,
            "fm": mask or 0, "lg": 1 if mask is not None else 0,
        })
    return cards


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", type=int, default=0, help="only the N most recently released sets (0 = all sets)")
    ap.add_argument("--real-sets-only", action="store_true", help="skip promos, Secret Lair, The List, tokens etc.")
    ap.add_argument("--group-ids", default="", help="comma separated TCGCSV group ids (overrides --sets)")
    ap.add_argument("--min-price", type=float, default=0.5, help="drop cards cheaper than this (keeps the site fast)")
    ap.add_argument("--history-min-price", type=float, default=1.0, help="only keep daily history for cards at least this price")
    ap.add_argument("--delay", type=float, default=0.3, help="seconds between TCGCSV requests")
    ap.add_argument("--print-stamp", action="store_true", help="print TCGCSV's last-updated stamp as a cache key, then exit")
    ap.add_argument("--cache-dir", default=CACHE_DIR)
    ap.add_argument("--out", default=OUT_FILE)
    ap.add_argument("--history", default=HISTORY_FILE)
    args = ap.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()

    stamp = get_stamp()                       # TCGCSV asks us to check this first and only sync when it has changed
    if args.print_stamp:
        print(stamp_key(stamp, today))
        return
    hist = load_history(args.history)
    day = stamp_day(stamp, today)             # the history column this data belongs to
    cache = PriceCache(args.cache_dir, stamp)
    cache.open()
    if stamp and not cache.hit and hist.get("last_sync") == stamp:
        print(f"TCGCSV has not published new prices since the last run (last updated: {stamp}), and the saved copy of them is gone.\n"
              "Fetching them again would request the same price files twice within 24 hours, which TCGCSV asks us not to do.\n"
              "Nothing to update; the site keeps showing the last published data.")
        set_output("skipped", "true")
        return
    print(f"TCGCSV last updated: {stamp or 'unknown'}" + ("; reusing today's saved price files (no new price requests)." if cache.hit else ""))

    groups = fetch(f"{TCGCSV}/groups")["results"]
    if args.group_ids:
        want = {int(x) for x in args.group_ids.split(",") if x.strip()}
        chosen = [g for g in groups if g["groupId"] in want]
    else:
        chosen = pick_groups(groups, args.sets, today, args.real_sets_only)
    if not chosen:
        sys.exit("No sets selected; TCGCSV returned nothing usable.")
    print(f"{len(chosen)} sets to read.")

    try:
        legal = load_legalities()
    except Exception as e:  # prices are still useful without legality
        print(f"WARNING: could not load Scryfall legality ({type(e).__name__}: {e}); format filters will be empty.")
        legal = {}

    cards, sets_out, used, failed = [], [], {}, []
    for n, g in enumerate(chosen, 1):
        gid, code = g["groupId"], set_code(g, used)
        try:
            polite(args.delay)
            products = fetch(f"{TCGCSV}/{gid}/products")["results"]
            prices = cache.get(gid)           # each price file is requested at most once per TCGCSV update
            if prices is None:
                polite(args.delay)
                prices = trim_prices(fetch(f"{TCGCSV}/{gid}/prices")["results"])
                cache.put(gid, prices)
        except Exception as e:
            failed.append(code)
            print(f"[{n}/{len(chosen)}] {code}: FAILED ({type(e).__name__}: {e})")
            continue
        got = build_cards(code, products, prices, legal, args.min_price)
        if got:
            cards.extend(got)
            sets_out.append({"c": code, "name": g["name"], "date": (g.get("publishedOn") or "")[:10]})
        print(f"[{n}/{len(chosen)}] {code} {g['name']}: {len(got)} cards, {sum(c['lg'] for c in got)} with legality")
    if failed:
        print(f"{len(failed)} sets failed and are missing from this run: {', '.join(failed[:20])}")

    if not cards:
        sys.exit("No cards collected; refusing to overwrite the site data.")
    assign_tickers(cards)
    known = sum(c["lg"] for c in cards)
    print(f"Legality matched for {known:,} of {len(cards):,} cards ({100 * known // len(cards)}%).")
    if known < len(cards) * 0.5:
        miss = sorted({c["n"] for c in cards if not c["lg"]})[:15]
        print("  WARNING: most cards have no legality. Sample of names that did not match: " + "; ".join(miss))

    add_snapshot(hist, day, {str(c["id"]): c["p"] for c in cards if c["p"] >= args.history_min_price})
    if stamp:
        hist["last_sync"] = stamp
    dates = hist["dates"]
    for c in cards:
        series = hist["p"].get(str(c["id"]))
        if not series:
            continue
        c["d1"] = pct_since(dates, series, day, 1)
        c["d7"] = pct_since(dates, series, day, 7)
        c["d30"] = pct_since(dates, series, day, 30)
        c["h"] = series[-CHART_DAYS:]

    sets_out.sort(key=lambda s: s["date"], reverse=True)
    payload = {"sample": False, "updated": now.strftime("%Y-%m-%dT%H:%MZ"), "dates": dates[-CHART_DAYS:],
               "formats": FORMATS, "sets": sets_out, "cards": cards}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(args.history) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("window.MTG = " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";\n")
    with open(args.history, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"))
    print(f"Wrote {len(cards):,} cards from {len(sets_out)} sets to {args.out} "
          f"({os.path.getsize(args.out) / 1e6:.1f} MB); history has {len(dates)} day(s), {len(hist['p']):,} tracked cards.")


if __name__ == "__main__":
    main()
