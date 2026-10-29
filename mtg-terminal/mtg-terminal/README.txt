MTG TERMINAL
============
A Bloomberg-style price monitor for every Magic: The Gathering card TCGplayer lists (all sets, promos included).
  - Prices: TCGplayer market prices via TCGCSV (https://tcgcsv.com), refreshed daily
  - Format legality (Standard, Modern, Pauper, ...): Scryfall's oracle card file, joined on card NAME
  - Price history + trending: 1-day / 7-day / 30-day change. The first run backfills ~30 days from TCGCSV's daily
    archives; after that the workflow saves one snapshot a day.

Files
  collect_tcgcsv.py        collector: writes site/data.js and data/history.json
  site/index.html          the website (search, filters, tabs, price chart)
  site/data.js             SAMPLE data until the first workflow run replaces it
  data/history.json        one price per card per day; the workflow commits it back each run
  .github/workflows/update.yml   daily run + publish to GitHub Pages

Setup (same as your other repo)
  1. Upload everything (including .github and site) to a repository.
  2. Settings > Pages > Source: GitHub Actions.
  3. Settings > Actions > General > Workflow permissions: Read and write.
  4. Optional but polite: Settings > Secrets and variables > Actions > Variables > CONTACT_EMAIL = a project address
     (it goes in the User-Agent sent to TCGCSV and Scryfall).
  5. Actions tab > "Update prices" > Run workflow. Allow 15 to 40 minutes: it reads every set, politely, one request at a time.

Using the site
  F1 / F2   top gainers / top losers (by 1-day change)      "/"  jump to search
  F3 / F4   most valuable / all cards                       Enter in search opens the top result
  Search matches card name, generated ticker, or set code/name; every word must match ("rift bolt", "dsk").
  Click any column header to sort. The URL updates, so a filtered view can be bookmarked or shared.

Important to know
  * Charts and gainers/losers work from the first run: it downloads the last 30 daily price archives from TCGCSV
    ONCE (about 3 MB each) and records the result in data/history.json. TCGCSV asks people not to re-download
    archives day after day, so it never does that again; later runs add just one new day. If the backfill can't
    run (see the log), the site opens on "Most valuable" and changes appear after the second daily run.
  * Prices are TCGplayer MARKET price (recent sales), not the lowest listing. Low is shown in the detail panel.
  * To keep the site fast, cards under $0.50 are not listed (--min-price) and daily price history is only
    saved for cards of $1 or more (--history-min-price). Both are options below. Expect roughly 30,000 cards
    and a data.js of about 10 MB (about 2 MB over the wire once GitHub compresses it).
  * The site's "Min price $" box defaults to 1 so cheap cards don't dominate movers (5c -> 10c is +100%).
    Set it to 0 to see everything.
  * Tickers are generated from the card name, e.g. LIBO.DSK; a second printing in the same set gets a 2.
  * Format legality is by card name, so every printing of a card shows the same legality. A name Scryfall
    doesn't know (some art cards, oddly named promos) shows "Legality unknown" and is left out of format filters.
  * data/history.json grows by a few MB per day of git history. If that ever bothers you, raise --history-min-price.

Options
  python collect_tcgcsv.py                            everything (default)
  python collect_tcgcsv.py --sets 30                  only the 30 most recently released sets
  python collect_tcgcsv.py --real-sets-only           skip promos, Secret Lair, The List, tokens
  python collect_tcgcsv.py --group-ids 24312,24317    exact TCGCSV group ids (see https://tcgcsv.com/tcgplayer/1/groups)
  python collect_tcgcsv.py --min-price 1 --history-min-price 2   smaller site
  python collect_tcgcsv.py --backfill-days 0          skip the one-off history backfill
  python collect_tcgcsv.py --force-backfill           redo the backfill (only if you reset data/history.json)
