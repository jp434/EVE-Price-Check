"""
EVE Online Market Depth Lookup
===============================

Reusable tool for pulling order-book depth for an item across one or more
regions, using CCP's public ESI API (no authentication required).

Usage:
    from market_depth import market_depth

    market_depth("Tritanium", ["The Forge", "Domain"])
    market_depth("PLEX", ["Jita", "Amarr"], levels=15)

Or from the command line:
    python market_depth.py "Tritanium" "The Forge" "Domain"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import requests

ESI_BASE = "https://esi.evetech.net/latest"
USER_AGENT = "market-depth-tool (personal use; contact: none)"

# A few common trade-hub aliases -> the system/region name ESI actually
# expects. ESI's /universe/ids/ resolves region names exactly, but people
# often think in terms of the hub *station* (e.g. "Jita" is a system in
# "The Forge" region), so we translate the common ones here.
HUB_ALIASES = {
    "jita": "The Forge",
    "amarr": "Domain",
    "dodixie": "Sinq Laison",
    "rens": "Heimatar",
    "hek": "Metropolis",
}

# PLEX trades on a single unified order book (region_id 19000001), not in
# regular regional markets. Any regional /orders/ query for PLEX returns
# empty, regardless of which region you ask. We detect it by type_id and
# transparently redirect to the global PLEX region instead.
PLEX_TYPE_ID = 44992
GLOBAL_PLEX_REGION_ID = 19000001
GLOBAL_PLEX_REGION_NAME = "Global PLEX Market"


@dataclass
class DepthLevel:
    price: float
    volume: int
    cumulative: int
    orders: int


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def resolve_ids(names: list[str], session: requests.Session) -> dict:
    """
    Resolve a list of names (item names, region names, etc.) to IDs using
    POST /universe/ids/. Returns the raw categorized response, e.g.:
        {"inventory_types": [...], "regions": [...], ...}
    """
    resp = session.post(f"{ESI_BASE}/universe/ids/", json=names, params={"datasource": "tranquility"})
    resp.raise_for_status()
    return resp.json()


def resolve_type_id(item_name: str, session: requests.Session) -> int:
    data = resolve_ids([item_name], session)
    types = data.get("inventory_types") or []
    if not types:
        raise ValueError(f"Could not find an item type matching {item_name!r}")
    return types[0]["id"]


def resolve_region_id(region_name: str, session: requests.Session) -> int:
    lookup_name = HUB_ALIASES.get(region_name.strip().lower(), region_name)
    data = resolve_ids([lookup_name], session)
    regions = data.get("regions") or []
    if not regions:
        raise ValueError(
            f"Could not find a region matching {region_name!r} "
            f"(tried looking up {lookup_name!r})"
        )
    return regions[0]["id"]


def fetch_orders(region_id: int, type_id: int, session: requests.Session) -> list[dict]:
    """Fetch all orders (buy + sell) for a type in a region, handling pagination."""
    orders: list[dict] = []
    page = 1
    while True:
        resp = session.get(
            f"{ESI_BASE}/markets/{region_id}/orders/",
            params={
                "datasource": "tranquility",
                "order_type": "all",
                "type_id": type_id,
                "page": page,
            },
        )
        if resp.status_code == 404:
            # No orders at all for this type/region.
            break
        resp.raise_for_status()
        batch = resp.json()
        orders.extend(batch)

        total_pages = int(resp.headers.get("X-Pages", 1))
        if page >= total_pages:
            break
        page += 1

    return orders


def build_depth(orders: list[dict], is_buy: bool) -> list[DepthLevel]:
    """
    Aggregate raw orders into price levels with cumulative volume.
    Buy side sorted highest price first (best bid first).
    Sell side sorted lowest price first (best ask first).
    """
    side_orders = [o for o in orders if o["is_buy_order"] == is_buy]

    by_price: dict[float, dict] = {}
    for o in side_orders:
        bucket = by_price.setdefault(o["price"], {"volume": 0, "orders": 0})
        bucket["volume"] += o["volume_remain"]
        bucket["orders"] += 1

    prices = sorted(by_price.keys(), reverse=is_buy)

    levels: list[DepthLevel] = []
    running = 0
    for price in prices:
        running += by_price[price]["volume"]
        levels.append(
            DepthLevel(
                price=price,
                volume=by_price[price]["volume"],
                cumulative=running,
                orders=by_price[price]["orders"],
            )
        )
    return levels


def _fmt_table(title: str, levels: list[DepthLevel], levels_to_show: int) -> str:
    lines = [title, "-" * len(title)]
    if not levels:
        lines.append("  (no orders)")
        return "\n".join(lines)

    header = f"{'Price (ISK)':>16} {'Volume':>12} {'Cumulative':>14} {'# Orders':>9}"
    lines.append(header)
    lines.append("-" * len(header))
    for lvl in levels[:levels_to_show]:
        lines.append(
            f"{lvl.price:>16,.2f} {lvl.volume:>12,} {lvl.cumulative:>14,} {lvl.orders:>9}"
        )
    if len(levels) > levels_to_show:
        lines.append(f"  ... and {len(levels) - levels_to_show} more price level(s)")
    return "\n".join(lines)


def resolve_system_names(system_ids: list[int], session: requests.Session) -> dict[int, str]:
    """
    Resolve a list of system_ids to system names using POST /universe/names/.
    Returns {system_id: system_name}. Unresolvable IDs are omitted silently.
    """
    if not system_ids:
        return {}
    try:
        resp = session.post(
            f"{ESI_BASE}/universe/names/",
            json=list(set(system_ids)),
            params={"datasource": "tranquility"},
        )
        resp.raise_for_status()
        return {
            entry["id"]: entry["name"]
            for entry in resp.json()
            if entry.get("category") == "solar_system"
        }
    except requests.HTTPError:
        return {}


def get_region_depth(region_id: int, region_label: str, type_id: int, session: requests.Session) -> dict:
    """
    Fetch and aggregate depth for one region/type combo. Returns a plain-dict
    structure (JSON-serializable) used by both the CLI report and the GUI:

        {
            "region_name": str,
            "region_id": int,
            "best_bid": float | None,
            "best_ask": float | None,
            "best_bid_system": str | None,
            "best_ask_system": str | None,
            "spread": float | None,
            "buy_levels": [{"price", "volume", "cumulative", "orders"}, ...],
            "sell_levels": [...],
        }
    """
    orders = fetch_orders(region_id, type_id, session)
    buy_levels = build_depth(orders, is_buy=True)
    sell_levels = build_depth(orders, is_buy=False)

    best_bid = buy_levels[0].price if buy_levels else None
    best_ask = sell_levels[0].price if sell_levels else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

    # Find which system the best bid/ask order is in, then resolve to a name.
    # We only resolve the two system_ids we actually need (best bid/ask),
    # not all systems in the entire order book - keeps the extra ESI call minimal.
    system_ids_needed = []
    best_bid_system_id = None
    best_ask_system_id = None

    if buy_levels and best_bid is not None:
        best_bid_order = next((o for o in orders if o["is_buy_order"] and o["price"] == best_bid), None)
        if best_bid_order:
            best_bid_system_id = best_bid_order.get("system_id")
            if best_bid_system_id:
                system_ids_needed.append(best_bid_system_id)

    if sell_levels and best_ask is not None:
        best_ask_order = next((o for o in orders if not o["is_buy_order"] and o["price"] == best_ask), None)
        if best_ask_order:
            best_ask_system_id = best_ask_order.get("system_id")
            if best_ask_system_id:
                system_ids_needed.append(best_ask_system_id)

    system_names = resolve_system_names(system_ids_needed, session) if system_ids_needed else {}

    return {
        "region_name": region_label,
        "region_id": region_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_system": system_names.get(best_bid_system_id) if best_bid_system_id else None,
        "best_ask_system": system_names.get(best_ask_system_id) if best_ask_system_id else None,
        "spread": spread,
        "buy_levels": [vars(l) for l in buy_levels],
        "sell_levels": [vars(l) for l in sell_levels],
    }


def get_depth_data(item_name: str, regions: list[str]) -> dict:
    """
    Structured (JSON-friendly) version of a depth lookup, used by the GUI.
    Returns:
        {
            "item_name": str,
            "type_id": int,
            "is_plex": bool,
            "note": str | None,
            "results": [ <region dict from get_region_depth>, ... ],
            "errors": [ {"region_name": str, "error": str}, ... ],
        }
    """
    session = _session()
    type_id = resolve_type_id(item_name, session)

    data = {
        "item_name": item_name,
        "type_id": type_id,
        "is_plex": type_id == PLEX_TYPE_ID,
        "note": None,
        "results": [],
        "errors": [],
    }

    if type_id == PLEX_TYPE_ID:
        data["note"] = (
            "PLEX trades on a single unified Global PLEX Market, not in "
            "regional markets. Showing the global PLEX order book instead "
            "of the requested regions."
        )
        data["results"].append(
            get_region_depth(GLOBAL_PLEX_REGION_ID, GLOBAL_PLEX_REGION_NAME, type_id, session)
        )
        return data

    for region_name in regions:
        try:
            region_id = resolve_region_id(region_name, session)
        except ValueError as e:
            data["errors"].append({"region_name": region_name, "error": str(e)})
            continue
        data["results"].append(get_region_depth(region_id, region_name, type_id, session))

    return data


def market_depth(item_name: str, regions: list[str], levels: int = 10) -> str:
    """
    Look up and pretty-print order book depth for `item_name` across each
    region in `regions`.

    Args:
        item_name: e.g. "Tritanium", "PLEX"
        regions: list of region names, or common hub aliases like "Jita"
        levels: number of price levels to show per side (default 10)

    Returns:
        The pretty-printed report (also printed to stdout).
    """
    data = get_depth_data(item_name, regions)

    output_blocks = [f"\n=== Market Depth: {data['item_name']} (type_id {data['type_id']}) ===\n"]

    if data["note"]:
        output_blocks.append(data["note"] + "\n")

    for err in data["errors"]:
        output_blocks.append(f"\n## {err['region_name']} ##\n  ERROR: {err['error']}\n")

    for region in data["results"]:
        best_bid = region["best_bid"]
        best_ask = region["best_ask"]
        spread = region["spread"]

        buy_levels = [DepthLevel(**lvl) for lvl in region["buy_levels"]]
        sell_levels = [DepthLevel(**lvl) for lvl in region["sell_levels"]]

        block = [
            f"\n## {region['region_name']} (region_id {region['region_id']}) ##",
            f"Best bid: {best_bid:,.2f} ISK" if best_bid is not None else "Best bid: n/a",
            f"Best ask: {best_ask:,.2f} ISK" if best_ask is not None else "Best ask: n/a",
            f"Spread:   {spread:,.2f} ISK" if spread is not None else "Spread:   n/a",
            "",
            _fmt_table("BUY ORDERS (bids)", buy_levels, levels),
            "",
            _fmt_table("SELL ORDERS (asks)", sell_levels, levels),
        ]
        output_blocks.append("\n".join(block))

    report = "\n".join(output_blocks)
    print(report)
    return report


# ---------------------------------------------------------------------------
# Fees and cross-regional arbitrage
# ---------------------------------------------------------------------------
#
# Two separate fees apply to EVE market trading (confirmed against CCP's
# support docs and EVE University wiki, current as of mid-2026):
#
#   Sales tax: paid by the seller, only when an order THEY PLACED is
#   matched. Base rate 7.5%, reduced by 11% (relative) per level of the
#   Accounting skill, down to ~3.37% at level 5.
#       rate = 7.5% * (1 - 0.11 * accounting_level)
#
#   Broker fee: paid by whoever PLACES an order (buy or sell), at creation
#   time, regardless of whether it ever fills. Base rate 3%, reduced by a
#   flat 0.3 percentage points per level of Broker Relations, with further
#   small reductions from station-owner standings, down to a 1% floor.
#       rate = 3% - 0.3%*broker_relations_level - 0.03%*faction_standing
#                  - 0.02%*corp_standing
#
# Important nuance: these fees only apply when YOU place a limit order.
# Trading "instantly" against an existing order on the book (the common
# way to execute a quick flip) incurs NEITHER fee. We model both scenarios
# via `fee_mode`:
#   "limit"   - conservative default: assumes you place orders on both
#               ends (the standard "station trading" assumption), so
#               broker fee applies on both legs and sales tax applies on
#               the sell leg. This is the safer default since it never
#               under-counts costs.
#   "instant" - assumes you take the best available order on both ends
#               (market-order style). No broker fee, no sales tax, but
#               you don't control your execution price beyond the
#               current best bid/ask.
#
# We deliberately do NOT model transport cost (freight, jumps, cargo,
# risk) - that depends on ship, route, and risk tolerance in ways this
# tool has no visibility into. The numbers below are gross-of-freight
# profit; treat freight as a separate judgment call.

SALES_TAX_BASE = 0.075
SALES_TAX_ACCOUNTING_REDUCTION_PER_LEVEL = 0.11  # relative, i.e. *11% off the rate* per level

BROKER_FEE_BASE = 0.03
BROKER_FEE_RELATIONS_REDUCTION_PER_LEVEL = 0.003  # flat percentage points per level
BROKER_FEE_FACTION_STANDING_REDUCTION = 0.0003  # per standing point (0-10)
BROKER_FEE_CORP_STANDING_REDUCTION = 0.0002  # per standing point (0-10)
BROKER_FEE_FLOOR = 0.01


def calc_fee_rates(
    accounting_level: int = 0,
    broker_relations_level: int = 0,
    faction_standing: float = 0.0,
    corp_standing: float = 0.0,
) -> dict:
    """
    Compute effective sales tax and broker fee rates from skill levels and
    standings. Levels are clamped to 0-5 (skill levels in EVE), standings
    to 0-10 (only positive standings reduce broker fee).

    Returns: {"sales_tax_rate": float, "broker_fee_rate": float}
    (both as decimals, e.g. 0.075 = 7.5%)
    """
    accounting_level = max(0, min(5, accounting_level))
    broker_relations_level = max(0, min(5, broker_relations_level))
    faction_standing = max(0.0, min(10.0, faction_standing))
    corp_standing = max(0.0, min(10.0, corp_standing))

    sales_tax_rate = SALES_TAX_BASE * (1 - SALES_TAX_ACCOUNTING_REDUCTION_PER_LEVEL * accounting_level)
    sales_tax_rate = max(0.0, sales_tax_rate)

    broker_fee_rate = (
        BROKER_FEE_BASE
        - BROKER_FEE_RELATIONS_REDUCTION_PER_LEVEL * broker_relations_level
        - BROKER_FEE_FACTION_STANDING_REDUCTION * faction_standing
        - BROKER_FEE_CORP_STANDING_REDUCTION * corp_standing
    )
    broker_fee_rate = max(BROKER_FEE_FLOOR, broker_fee_rate)

    return {"sales_tax_rate": sales_tax_rate, "broker_fee_rate": broker_fee_rate}


def _available_volume_for_arb(buy_levels: list, sell_levels: list, depth_levels: int = 5) -> int:
    """
    Estimate how many units could realistically move in this arbitrage
    direction: limited by the smaller of (sell-side liquidity in the
    source region, i.e. what you can buy) and (buy-side liquidity in the
    destination region, i.e. what you can sell into), looking only at the
    top `depth_levels` price tiers to stay close to the quoted best price.
    """
    source_liquidity = sum(lvl["volume"] for lvl in sell_levels[:depth_levels]) if sell_levels else 0
    dest_liquidity = sum(lvl["volume"] for lvl in buy_levels[:depth_levels]) if buy_levels else 0
    return min(source_liquidity, dest_liquidity)


def find_arbitrage(
    item_name: str,
    regions: list[str],
    fee_mode: str = "limit",
    accounting_level: int = 0,
    broker_relations_level: int = 0,
    faction_standing: float = 0.0,
    corp_standing: float = 0.0,
    min_net_margin_pct: float = 0.0,
) -> dict:
    """
    Find cross-regional arbitrage opportunities for `item_name` across all
    pairs of `regions`. For each ordered pair (source -> destination), buy
    at the source's best ask and sell at the destination's best bid.

    Does NOT account for transport cost, travel time, or risk - only the
    raw price spread and EVE's market fees.

    Returns:
        {
            "item_name": str, "type_id": int, "is_plex": bool,
            "fee_mode": str,
            "sales_tax_rate": float, "broker_fee_rate": float,
            "opportunities": [
                {
                    "source_region", "dest_region",
                    "buy_price", "sell_price",
                    "gross_margin_per_unit", "gross_margin_pct",
                    "buy_side_fee", "sell_side_fee", "sales_tax",
                    "net_margin_per_unit", "net_margin_pct",
                    "available_volume", "est_total_profit",
                }, ...
            ],
            "errors": [...],
        }
    """
    if fee_mode not in ("limit", "instant"):
        raise ValueError("fee_mode must be 'limit' or 'instant'")

    depth_data = get_depth_data(item_name, regions)

    fees = calc_fee_rates(accounting_level, broker_relations_level, faction_standing, corp_standing)
    sales_tax_rate = fees["sales_tax_rate"] if fee_mode == "limit" else 0.0
    broker_fee_rate = fees["broker_fee_rate"] if fee_mode == "limit" else 0.0

    result = {
        "item_name": depth_data["item_name"],
        "type_id": depth_data["type_id"],
        "is_plex": depth_data["is_plex"],
        "fee_mode": fee_mode,
        "sales_tax_rate": sales_tax_rate,
        "broker_fee_rate": broker_fee_rate,
        "opportunities": [],
        "errors": depth_data["errors"],
    }

    if depth_data["is_plex"]:
        # PLEX trades on a single global order book - there are no separate
        # regions to arbitrage between.
        result["note"] = (
            "PLEX trades on a single unified Global PLEX Market. There is "
            "only one order book, so cross-regional arbitrage isn't "
            "applicable to PLEX."
        )
        return result

    regions_data = depth_data["results"]

    for source in regions_data:
        for dest in regions_data:
            if source["region_id"] == dest["region_id"]:
                continue
            if source["best_ask"] is None or dest["best_bid"] is None:
                continue

            buy_price = source["best_ask"]
            sell_price = dest["best_bid"]

            gross_margin_per_unit = sell_price - buy_price
            if gross_margin_per_unit <= 0:
                continue  # not even gross-profitable, skip

            # Fees: broker fee on placing the buy order (source) and the
            # sell order (dest), sales tax on the dest sale. In "instant"
            # mode both rates are zeroed out above.
            buy_side_fee = buy_price * broker_fee_rate
            sell_side_fee = sell_price * broker_fee_rate
            sales_tax = sell_price * sales_tax_rate

            total_cost_per_unit = buy_price + buy_side_fee + sell_side_fee + sales_tax
            net_margin_per_unit = sell_price - total_cost_per_unit
            net_margin_pct = (net_margin_per_unit / total_cost_per_unit * 100) if total_cost_per_unit > 0 else 0.0
            gross_margin_pct = (gross_margin_per_unit / buy_price * 100) if buy_price > 0 else 0.0

            if net_margin_pct < min_net_margin_pct:
                continue

            available_volume = _available_volume_for_arb(dest["buy_levels"], source["sell_levels"])

            result["opportunities"].append({
                "source_region": source["region_name"],
                "source_region_id": source["region_id"],
                "source_system": source.get("best_ask_system"),
                "dest_region": dest["region_name"],
                "dest_region_id": dest["region_id"],
                "dest_system": dest.get("best_bid_system"),
                "buy_price": buy_price,
                "sell_price": sell_price,
                "gross_margin_per_unit": gross_margin_per_unit,
                "gross_margin_pct": gross_margin_pct,
                "buy_side_fee": buy_side_fee,
                "sell_side_fee": sell_side_fee,
                "sales_tax": sales_tax,
                "net_margin_per_unit": net_margin_per_unit,
                "net_margin_pct": net_margin_pct,
                "available_volume": available_volume,
                "est_total_profit": net_margin_per_unit * available_volume,
            })

    result["opportunities"].sort(key=lambda o: o["net_margin_pct"], reverse=True)
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print('Usage: python market_depth.py "<item name>" "<region 1>" ["<region 2>" ...]')
        print('Example: python market_depth.py "Tritanium" "Jita" "Amarr"')
        sys.exit(1)

    item = sys.argv[1]
    region_args = sys.argv[2:]
    market_depth(item, region_args)