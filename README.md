# EVE Market Depth — Local Web GUI

A small local web app for looking up EVE Online order-book depth across
regions, using FC's public ESI API.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## Usage

### Depth tab
- Enter an item name (e.g. `Tritanium`, `PLEX`) and one or more regions,
  comma-separated (e.g. `Jita, Amarr, Dodixie`). Hub names like "Jita" are
  automatically mapped to their region ("The Forge").
- Click **Query** (or press Enter).
- Each region gets its own panel: best bid/ask/spread, a depth chart
  (bids stepping left, asks stepping right from the spread), and tables
  of the top 10 price levels per side.
- PLEX is a special case — it trades on a single unified global market,
  not per-region. The app detects this automatically and shows the
  global PLEX book regardless of which regions you typed in.

### Arbitrage tab
- Enter an item and **two or more** regions to compare. Every directional
  pair is checked (e.g. with 3 regions, that's 6 directions: A→B, B→A,
  A→C, C→A, B→C, C→B).
- For each pair, "buy" = the source region's best ask, "sell" = the
  destination region's best bid.
- Set your **Accounting** and **Broker Relations** skill levels (0–5) and
  optional standings (0–10) to get accurate fee rates. Defaults are 0
  (worst-case / new-character rates).
- Choose a **fee mode**:
  - **Limit orders** (default) — assumes you place a limit order on both
    ends. Broker fee applies to both legs, sales tax applies on the sell.
    This is the realistic, conservative assumption for most station
    trading.
  - **Instant trade** — assumes you trade immediately against existing
    orders on both ends (market-order style). No broker fee, no sales
    tax — EVE doesn't charge either for taking an existing order — but
    you don't control your execution price.
- Results are ranked by **net margin %** (profit after fees, as a
  percentage of total cost per unit).
- **Volume cap** is the smaller of what's available to buy at the source
  and what's available to sell into at the destination (top 5 price
  tiers on each side), so "est. total profit" isn't wildly optimistic.

**Important — what this does NOT account for:** transport cost, travel
time, jump risk, ship cargo capacity, or market volatility between when
you check this and when you actually execute the trade. The numbers are
gross-of-freight profit; treat freight/risk as your own judgment call on
top of these figures. Fee rates (sales tax base 7.5%, broker fee base 3%)
are current as of mid-2026 per CCP's support docs — if CCP changes these
in a future balance patch, update the constants at the top of the "Fees
and cross-regional arbitrage" section in `market_depth.py`.

### Catalog tab
- **Search**: type at least 2 characters and hit Search/Enter to find any
  tradeable item by name. The first search builds a full name index across
  every market group (this can take a little while — it's resolving names
  for potentially tens of thousands of items in batches), then caches it
  in memory, so later searches are fast.
- **Browse**: the tree below mirrors EVE's in-game market category
  structure (Ships → Frigates → ..., Minerals, etc.). Click a category to
  expand it; items inside load on first expand and are cached after that.
- Click any item (from search results or the tree) to jump to the **Depth**
  tab with that item pre-filled, ready to query.
- The category tree itself is built once per server run and cached in
  memory for 6 hours — it's reference data that essentially never changes
  mid-session, and building it requires walking every market group via
  ESI, so we don't want to redo that on every page load.

## Files

- `app.py` — Flask backend (serves the page + `/api/depth`,
  `/api/arbitrage`, and `/api/catalog/*` JSON endpoints)
- `market_depth.py` — ESI order-book fetching, depth aggregation, fees,
  and arbitrage logic (also usable standalone from the command line:
  `python market_depth.py "Tritanium" "Jita" "Amarr"`)
- `catalog.py` — tradeable item catalog: market group tree, bulk name
  resolution, search index
- `templates/index.html` — frontend (HTML/CSS/JS, no build step, no
  external JS dependencies beyond a Google Font)

## Notes

- No authentication needed — this only uses ESI's public market endpoints.
- ESI caches order data server-side for a few minutes, so don't expect
  faster-than-that refresh rates.
- This runs Flask's built-in dev server, which is fine for local/personal
  use but isn't meant to be exposed to the internet.
