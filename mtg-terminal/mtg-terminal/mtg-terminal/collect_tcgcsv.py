#!/usr/bin/env python3
"""MTG Terminal collector.

Prices come from TCGCSV (https://tcgcsv.com, a daily mirror of TCGplayer's catalog and prices).
Format legality comes from Scryfall (TCGCSV has none). The two are joined on TCGplayer's product id.

Writes:
  site/data.js        what the website loads (window.MTG = {...})
  data/history.json   one price per card per day, so price changes can be computed; commit it back each run

Usage:
  python collect_tcgcsv.py                 # 12 most recent sets
  python collect_tcgcsv.py --sets 20       # more sets
  python collect_tcgcsv.py --group-ids 24312,24317   # exact TCGCSV group ids
"""
import argparse, datetime, difflib, json, os, random, re, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request

TCGCSV = "https://tcgcsv.com/tcgplayer/1"        # category 1 = Magic: The Gathering
SCRYFALL = "https://api.scryfall.com"
HISTORY_FILE = os.path.join("data", "history.json")
OUT_FILE = os.path.join("site", "data.js")
HISTORY_DAYS = 45
CHART_DAYS = 30
TIMEOUT = 30
CONTACT = os.environ.get("CONTACT_EMAIL") or "contact@example.com"
AGENT = f"MTGTerminal/1.0 (community project; contact: {CONTACT})"

# Scryfall legality keys we expose in the format filter.
FORMATS = ["standard", "pioneer", "modern", "legacy", "vintage", "commander", "pauper", "paupercommander",
           "brawl", "standardbrawl", "historic", "timeless", "alchemy", "oathbreaker", "premodern", "penny"]
SKIP_SET_WORDS = ("art series", "token", "oversized", "minigame", "heroes of the realm", "memorabilia", "collector's edition")


# ---------- fetching ----------
def fetch(url, tries=5):
    """GET json. Waits and retries on 429/503 (honouring Retry-After)."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == tries - 1:
                raise
            try:
                wait = float(e.headers.get("Retry-After", ""))
            except ValueError:
                wait = 0
            time.sleep(min(max(wait, 5 * 2 ** attempt), 120) + random.uniform(0, 3))
        except (urllib.error.URLError, TimeoutError):
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


def num_key(n):
    n = (n or "").split("/")[0].strip().lower()
    return n.lstrip("0") or n


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


# ---------- choosing sets ----------
def pick_groups(groups, count, today):
    out = []
    for g in groups:
        pub = (g.get("publishedOn") or "")[:10]
        if not pub or pub > today:
            continue
        if any(w in (g.get("name") or "").lower() for w in SKIP_SET_WORDS):
            continue
        out.append(g)
    out.sort(key=lambda g: g["publishedOn"], reverse=True)
    return out[:count]


def scryfall_code_for(group, sets_by_code, sets_by_name):
    code = (group.get("abbreviation") or "").lower()
    if code in sets_by_code:
        return code
    name = norm(group.get("name"))
    if name in sets_by_name:
        return sets_by_name[name]
    close = difflib.get_close_matches(name, list(sets_by_name), n=1, cutoff=0.9)
    return sets_by_name[close[0]] if close else None


def scryfall_cards(code, delay):
    cards = []
    url = f"{SCRYFALL}/cards/search?" + urllib.parse.urlencode(
        {"q": f"e:{code}", "unique": "prints", "include_variations": "true", "order": "set"})
    while url:
        try:
            page = fetch(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # Scryfall answers 404 when a search has no results
                break
            raise
        cards.extend(page.get("data", []))
        url = page.get("next_page") if page.get("has_more") else None
        polite(delay)
    return cards


# ---------- tickers ----------
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


# ---------- history ----------
def load_history(path):
    try:
        h = json.load(open(path, encoding="utf-8"))
        if isinstance(h.get("dates"), list) and isinstance(h.get("p"), dict):
            return h
    except Exception:
        pass
    return {"dates": [], "p": {}}


def add_snapshot(hist, today, prices):
    """prices: {product id (str): market price}. Adds/overwrites today's column and trims old ones."""
    dates = hist["dates"]
    if today in dates:
        i = dates.index(today)
    else:
        dates.append(today)
        order = sorted(range(len(dates)), key=lambda k: dates[k])  # keep dates ascending
        if order != list(range(len(dates))):
            raise ValueError("history dates out of order")
        for lst in hist["p"].values():
            lst.append(None)
        i = len(dates) - 1
    for pid, price in prices.items():
        lst = hist["p"].setdefault(pid, [None] * len(dates))
        lst[i] = price
    if len(dates) > HISTORY_DAYS:
        cut = len(dates) - HISTORY_DAYS
        hist["dates"] = dates[cut:]
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


# ---------- main build ----------
def build_cards(group, products, prices, scry, min_price):
    by_id, by_name_num = {}, {}
    for c in scry:
        for k in ("tcgplayer_id", "tcgplayer_etched_id"):
            if c.get(k):
                by_id[int(c[k])] = c
        by_name_num[(norm(c.get("name")), num_key(c.get("collector_number")))] = c

    px = {}
    for p in prices:
        px.setdefault(p["productId"], {})[(p.get("subTypeName") or "Normal").lower()] = p

    cards = []
    for p in products:
        pid = p["productId"]
        rows = px.get(pid)
        if not rows:
            continue
        number = ext(p, "Number")
        sc = by_id.get(pid) or by_name_num.get((norm(re.sub(r"\(.*?\)", "", p.get("name", ""))), num_key(number)))
        if not number and not sc:
            continue  # sealed product, not a single card
        main = rows.get("normal") or next(iter(rows.values()))
        price = num(main.get("marketPrice")) or num(main.get("midPrice"))
        if price is None or price < min_price:
            continue
        foil = rows.get("foil")
        legal = [f for f in FORMATS if sc and (sc.get("legalities") or {}).get(f) in ("legal", "restricted")]
        cards.append({
            "id": pid, "n": p.get("name", "").strip(), "s": (group.get("abbreviation") or "").upper(),
            "num": number or (sc or {}).get("collector_number") or "",
            "r": (ext(p, "Rarity") or (sc or {}).get("rarity") or "")[:1].upper(),
            "p": round(price, 2),
            "f": round(num(foil.get("marketPrice")) or 0, 2) or None if foil and rows.get("normal") else None,
            "lo": round(num(main.get("lowPrice")) or 0, 2) or None,
            "fm": legal, "lg": bool(sc),
            "u": p.get("url") or f"https://www.tcgplayer.com/product/{pid}",
            "img": p.get("imageUrl") or "",
        })
    return cards


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", type=int, default=12, help="how many of the most recent sets to include")
    ap.add_argument("--group-ids", default="", help="comma separated TCGCSV group ids (overrides --sets)")
    ap.add_argument("--min-price", type=float, default=0.25, help="drop cards cheaper than this")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--out", default=OUT_FILE)
    ap.add_argument("--history", default=HISTORY_FILE)
    args = ap.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()

    groups = fetch(f"{TCGCSV}/groups")["results"]
    if args.group_ids:
        want = {int(x) for x in args.group_ids.split(",") if x.strip()}
        chosen = [g for g in groups if g["groupId"] in want]
    else:
        chosen = pick_groups(groups, args.sets, today)
    if not chosen:
        sys.exit("No sets selected; TCGCSV returned nothing usable.")
    print(f"{len(chosen)} sets: " + ", ".join(g.get("abbreviation") or g["name"] for g in chosen))

    sets_meta = fetch(f"{SCRYFALL}/sets")["data"]
    by_code = {s["code"]: s for s in sets_meta}
    by_name = {norm(s["name"]): s["code"] for s in sets_meta}

    cards, sets_out = [], []
    for g in chosen:
        gid = g["groupId"]
        polite(args.delay)
        products = fetch(f"{TCGCSV}/{gid}/products")["results"]
        polite(args.delay)
        prices = fetch(f"{TCGCSV}/{gid}/prices")["results"]
        code = scryfall_code_for(g, by_code, by_name)
        scry = scryfall_cards(code, 0.12) if code else []
        got = build_cards(g, products, prices, scry, args.min_price)
        matched = sum(1 for c in got if c["lg"])
        print(f"  {g.get('abbreviation')}: {len(got)} cards priced, {matched} matched to Scryfall"
              + ("" if code else " (no Scryfall set found: formats unknown)"))
        cards.extend(got)
        sets_out.append({"c": (g.get("abbreviation") or "").upper(), "name": g["name"], "date": (g.get("publishedOn") or "")[:10]})

    if not cards:
        sys.exit("No cards collected; refusing to overwrite the site data.")
    assign_tickers(cards)

    hist = load_history(args.history)
    add_snapshot(hist, today, {str(c["id"]): c["p"] for c in cards})
    dates = hist["dates"]
    for c in cards:
        series = hist["p"].get(str(c["id"]), [])
        c["d1"] = pct_since(dates, series, today, 1)
        c["d7"] = pct_since(dates, series, today, 7)
        c["d30"] = pct_since(dates, series, today, 30)
        c["h"] = series[-CHART_DAYS:]
    chart_dates = dates[-CHART_DAYS:]

    present = [f for f in FORMATS if any(f in c["fm"] for c in cards)]
    payload = {"sample": False, "updated": now.strftime("%Y-%m-%dT%H:%MZ"), "dates": chart_dates,
               "formats": present, "sets": sets_out, "cards": cards}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(args.history) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("window.MTG = " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";\n")
    with open(args.history, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"))
    print(f"Wrote {len(cards)} cards to {args.out}; history has {len(dates)} day(s).")


if __name__ == "__main__":
    main()
