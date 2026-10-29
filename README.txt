MTG TERMINAL
============
A Bloomberg-style price monitor for recent Magic: The Gathering sets.
  - Prices: TCGplayer market prices via TCGCSV (https://tcgcsv.com), refreshed daily
  - Format legality (Standard, Modern, Pauper, ...): Scryfall, joined on TCGplayer product id
  - Trending: 1-day / 7-day / 30-day change, computed from daily snapshots the workflow saves

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
  5. Actions tab > "Update prices" > Run workflow. Takes a couple of minutes.

Using the site
  F1 / F2   top gainers / top losers (by 1-day change)      "/"  jump to search
  F3 / F4   most valuable / all cards                       Enter in search opens the top result
  Search matches card name, generated ticker, or set code/name; every word must match ("rift bolt", "dsk").
  Click any column header to sort. The URL updates, so a filtered view can be bookmarked or shared.

Important to know
  * Gainers/losers need two daily snapshots, so they appear the day after the first run. Until then the
    site opens on "Most valuable". Charts grow by one point per day (up to 30 shown).
  * Prices are TCGplayer MARKET price (recent sales), not the lowest listing. Low is shown in the detail panel.
  * Cards under $0.25 are skipped (--min-price). The default "Min price $1" filter on the site hides cheap
    cards from movers, where a 5c -> 10c jump is +100% noise; set it to 0 to see everything.
  * Tickers are generated from the card name, e.g. LIBO.DSK; a second printing in the same set gets a 2.
  * If a set can't be matched to Scryfall its cards still show, but format filters will exclude them.

Options
  python collect_tcgcsv.py --sets 20                  more sets
  python collect_tcgcsv.py --group-ids 24312,24317    exact TCGCSV group ids (see https://tcgcsv.com/tcgplayer/1/groups)
  python collect_tcgcsv.py --min-price 0.5
