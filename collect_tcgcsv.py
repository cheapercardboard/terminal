#!/usr/bin/env python3
"""MTG Terminal collector.

Prices come from TCGCSV (https://tcgcsv.com, a daily mirror of TCGplayer's catalog and prices).
Format legality comes from Scryfall's "oracle cards" bulk file (TCGCSV has none) and is joined on card NAME,
so promos, Secret Lair, World Championship cards and every other set get legality too.

Writes:
  site/data.js        what the website loads (window.MTG = {...})
  data/history.json   one price per card per day, so price changes can be computed; commit it back each run

Usage:
  python collect_tcgcsv.py                      # every set TCGCSV lists (all card types)
  python collect_tcgcsv.py --sets 30            # only the 30 most recently released sets
  python collect_tcgcsv.py --real-sets-only     # skip promos / Secret Lair / The List etc.
  python collect_tcgcsv.py --group-ids 24312    # exact TCGCSV group ids
"""
import argparse, datetime, difflib, io, json, os, random, re, shutil, subprocess, sys, tempfile, time, unicodedata
import urllib.error, urllib.parse, urllib.request

TCGCSV = "https://tcgcsv.com/tcgplayer/1"        # category 1 = Magic: The Gathering
SCRYFALL = "https://api.scryfall.com"
HISTORY_FILE = os.path.join("data", "history.json")
OUT_FILE = os.path.join("site", "data.js")
ARCHIVE = "https://tcgcsv.com/archive/tcgplayer/prices-{date}.ppmd.7z"   # one immutable file per day, from 2024-02-08
PRICE_FILE = re.compile(r"(?:^|/)1/(\d+)/prices(?:\.json)?$")             # <date>/<category 1 = Magic>/<groupId>/prices
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
def fetch(url, tries=5, timeout=TIMEOUT):
    """GET json. Waits and retries on 429/503 (honouring Retry-After) and on network hiccups."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
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


# ---------- legality (Scryfall oracle bulk file, keyed by card name) ----------
def load_legalities():
    """Returns {normalised name: bit mask}. Both 'Front // Back' and the front face alone map to the card."""
    bulk = fetch(f"{SCRYFALL}/bulk-data")["data"]
    uri = next(b["download_uri"] for b in bulk if b["type"] == "oracle_cards")
    print("Downloading Scryfall oracle cards (one-off, a few minutes at most)...")
    cards = fetch(uri, tries=3, timeout=600)
    out = {}
    for c in cards:
        mask = 0
        for i, f in enumerate(FORMATS):
            if (c.get("legalities") or {}).get(f) in ("legal", "restricted"):
                mask |= 1 << i
        name = c.get("name") or ""
        out[norm(name)] = mask
        if " // " in name:
            out.setdefault(norm(name.split(" // ")[0]), mask)
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


# ---------- history backfill from TCGCSV's daily archives ----------
def download(url, dest, tries=4):
    """Save a file. Returns False if it doesn't exist (404). Retries on 429/503."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            if e.code not in (429, 503) or attempt == tries - 1:
                raise
            time.sleep(min(10 * 2 ** attempt, 120) + random.uniform(0, 3))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def extract_magic_prices(path):
    """Open a .ppmd.7z archive and return the raw bytes of every Magic (category 1) group 'prices' file inside."""
    try:
        import py7zr
    except ImportError:
        py7zr = None
    if py7zr:
        with py7zr.SevenZipFile(path) as z:
            names = [n for n in z.getnames() if PRICE_FILE.search(n)]
            got = z.read(names) if names else {}
        return [v.read() if hasattr(v, "read") else v for v in got.values()]
    exe = shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
    if not exe:
        raise RuntimeError("cannot read .7z files: run `pip install py7zr` (or install 7-Zip)")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([exe, "x", "-y", f"-o{tmp}", path], check=True, capture_output=True)
        blobs = []
        for root, _, files in os.walk(tmp):
            for fn in files:
                full = os.path.join(root, fn)
                if PRICE_FILE.search(os.path.relpath(full, tmp).replace(os.sep, "/")):
                    blobs.append(open(full, "rb").read())
        return blobs


def archive_day_prices(path, tracked):
    """{product id (str): market price} for the tracked products on the archived day."""
    rows = {}
    for blob in extract_magic_prices(path):
        doc = json.loads(blob.decode("utf-8", errors="replace"))
        for r in (doc.get("results", []) if isinstance(doc, dict) else doc):
            pid = str(r.get("productId"))
            if pid in tracked:
                rows.setdefault(pid, {})[(r.get("subTypeName") or "Normal").lower()] = r
    out = {}
    for pid, by_sub in rows.items():
        price = pick_price(by_sub)[1]
        if price:
            out[pid] = round(price, 2)
    return out


def backfill(hist, today, tracked, days, delay=1.0):
    """Fill in missing past days from the archives so charts and price changes work from the first run.
    Returns (days added, whether archives were really downloaded and readable, so the one-off can be marked done)."""
    added = downloaded = blocked = 0
    with tempfile.TemporaryDirectory() as tmp:
        for k in range(days, 0, -1):
            day = (datetime.date.fromisoformat(today) - datetime.timedelta(days=k)).isoformat()
            if day in hist["dates"]:
                continue
            dest = os.path.join(tmp, f"{day}.7z")
            try:
                polite(delay)
                if not download(ARCHIVE.format(date=day), dest):
                    print(f"  {day}: no archive published")
                    continue
                downloaded += 1
                prices = archive_day_prices(dest, tracked)
                os.remove(dest)
            except Exception as e:
                print(f"  {day}: skipped ({type(e).__name__}: {str(e)[:100]})")
                if isinstance(e, RuntimeError):
                    blocked = 1
                    break  # no 7z reader available: every day would fail the same way
                continue
            if prices:
                add_snapshot(hist, day, prices)
                added += 1
                print(f"  {day}: {len(prices):,} prices")
            else:
                print(f"  {day}: archive had no matching Magic prices")
    return added, downloaded > 0 and not blocked


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
    ap.add_argument("--backfill-days", type=int, default=30, help="on the first run, fill history from this many past daily archives (0 = off)")
    ap.add_argument("--force-backfill", action="store_true", help="backfill even if it was already done once")
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
            polite(args.delay)
            prices = fetch(f"{TCGCSV}/{gid}/prices")["results"]
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

    hist = load_history(args.history)
    tracked = {str(c["id"]) for c in cards if c["p"] >= args.history_min_price}
    if args.backfill_days > 0 and (args.force_backfill or not hist.get("backfilled")):
        print(f"Backfilling up to {args.backfill_days} days of history from TCGCSV archives (one-off)...")
        n, attempted = backfill(hist, today, tracked, args.backfill_days)
        if attempted:
            hist["backfilled"] = today   # one-off: do not download the archives again on later runs
        print(f"  backfilled {n} day(s)" + ("" if attempted else " (will try again next run)"))
    add_snapshot(hist, today, {str(c["id"]): c["p"] for c in cards if c["p"] >= args.history_min_price})
    dates = hist["dates"]
    for c in cards:
        series = hist["p"].get(str(c["id"]))
        if not series:
            continue
        c["d1"] = pct_since(dates, series, today, 1)
        c["d7"] = pct_since(dates, series, today, 7)
        c["d30"] = pct_since(dates, series, today, 30)
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
