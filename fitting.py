"""
EVE Online Fitting Price Calculator
=====================================

Parses EFT-format fitting blocks (the standard copy-paste format from the
in-game fitting window) and prices every component across one or more
regions using live ESI order data.

EFT format reference:
    [Ship Type, Fit Name]
    <blank line>
    Low slot module        <- one per line, repeats for each fitted copy
    Low slot module
    [Empty Low slot]       <- skipped
    <blank line>
    Mid slot module
    Mid slot module, Loaded Charge Name   <- comma-separated charge
    <blank line>
    High slot module
    <blank line>
    Rig Slot
    <blank line>
    Subsystems (T3 only)
    <blank line>
    <blank line>
    Drone Name x5          <- drones/cargo, x<qty> suffix
    Cargo Item x10

Key rules confirmed against CCP developer docs:
  - Duplicate module lines aggregate (8 turrets → qty 8, one entry)
  - Charges appear on the same line as a module after a comma (in-game
    format) or as their own line after the module (some tools)
  - /offline suffix is stripped; item still included (still needs buying)
  - [Empty * slot] lines are skipped
  - Hull itself is included as an item (qty 1) since you have to buy it
"""

from __future__ import annotations

import re
from collections import defaultdict

import requests

from market_depth import (
    GLOBAL_PLEX_REGION_NAME,
    GLOBAL_PLEX_REGION_ID,
    PLEX_TYPE_ID,
    _session,
    resolve_region_id,
    fetch_orders,
)

ESI_BASE = "https://esi.evetech.net/latest"


# ---------------------------------------------------------------------------
# EFT parser
# ---------------------------------------------------------------------------

def parse_eft(text: str) -> dict:
    """
    Parse an EFT fitting block into a structured dict:

        {
            "hull": str,         # ship type name
            "fit_name": str,
            "items": [
                {"name": str, "qty": int, "role": str},
                ...
            ],
            "parse_warnings": [str, ...],   # non-fatal oddities found
        }

    Items are aggregated — if the same module appears in 4 slots it becomes
    one entry with qty=4. The hull itself is included as an item (qty 1).
    "role" is one of: hull, module, charge, drone, cargo.
    Roles are informational only (all items get priced the same way).
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    warnings = []

    # ---- header line -------------------------------------------------------
    header_line = ""
    body_lines = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[Empty"):
            header_line = stripped
            body_lines = lines[i + 1:]
            break

    if not header_line:
        raise ValueError(
            "Could not find a fitting header line. Make sure the text "
            "starts with [Ship Type, Fit Name]."
        )

    # Strip brackets and split on the first comma
    inner = header_line.strip("[]")
    comma_idx = inner.index(",") if "," in inner else len(inner)
    hull = inner[:comma_idx].strip()
    fit_name = inner[comma_idx + 1:].strip() if "," in inner else ""

    # ---- body: accumulate items --------------------------------------------
    # Track (name, role) -> qty using a list to preserve first-seen order.
    item_order: list[tuple[str, str]] = []
    item_qty: dict[tuple[str, str], int] = defaultdict(int)

    def _add(name: str, qty: int, role: str):
        key = (name, role)
        if key not in item_qty:
            item_order.append(key)
        item_qty[key] += qty

    # Two consecutive blank lines signal the start of the drones/cargo section
    blank_run = 0
    in_drones_cargo = False

    for raw_line in body_lines:
        line = raw_line.strip()

        if not line:
            blank_run += 1
            if blank_run >= 2:
                in_drones_cargo = True
            continue
        blank_run = 0

        # Skip empty slot markers
        if line.lower().startswith("[empty") and line.endswith("]"):
            continue

        if in_drones_cargo:
            # Drones / cargo: "Item Name xN"
            m = re.match(r"^(.+?)\s+x(\d+)\s*$", line, re.IGNORECASE)
            if m:
                name, qty = m.group(1).strip(), int(m.group(2))
            else:
                name, qty = line, 1
            # Heuristic: if it has "Drone" in the name it's a drone, else cargo
            role = "drone" if "drone" in name.lower() else "cargo"
            _add(name, qty, role)
        else:
            # Module line. Strip /offline suffix.
            line = re.sub(r"\s*/offline\s*$", "", line, flags=re.IGNORECASE).strip()

            # Check for inline charge: "Module Name, Charge Name"
            # The comma separates module from charge in some EFT variants.
            # Real EFT uses a separate line for charges, but in-game export
            # sometimes embeds them. We handle both.
            if "," in line and not line.startswith("["):
                parts = line.split(",", 1)
                module_name = parts[0].strip()
                charge_name = parts[1].strip()
                if module_name:
                    _add(module_name, 1, "module")
                if charge_name:
                    _add(charge_name, 1, "charge")
            else:
                if line:
                    _add(line, 1, "module")

    # Add hull as first item
    items = [{"name": hull, "qty": 1, "role": "hull"}]
    for (name, role) in item_order:
        items.append({"name": name, "qty": item_qty[(name, role)], "role": role})

    return {
        "hull": hull,
        "fit_name": fit_name,
        "items": items,
        "parse_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Bulk name -> type_id resolution
# ---------------------------------------------------------------------------

def resolve_type_ids_bulk(names: list[str], session: requests.Session) -> dict[str, int]:
    """
    Resolve a list of item names to type_ids using ESI's /universe/ids/.
    Returns {name: type_id}. Names that don't resolve are omitted.
    ESI accepts up to 500 names per request.
    """
    out: dict[str, int] = {}
    chunk_size = 500
    for i in range(0, len(names), chunk_size):
        chunk = names[i:i + chunk_size]
        try:
            resp = session.post(
                f"{ESI_BASE}/universe/ids/",
                json=chunk,
                params={"datasource": "tranquility"},
            )
            resp.raise_for_status()
            data = resp.json()
            for entry in data.get("inventory_types", []):
                out[entry["name"]] = entry["id"]
        except requests.HTTPError:
            # Retry one at a time for this chunk
            for name in chunk:
                try:
                    resp = session.post(
                        f"{ESI_BASE}/universe/ids/",
                        json=[name],
                        params={"datasource": "tranquility"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    for entry in data.get("inventory_types", []):
                        out[entry["name"]] = entry["id"]
                except requests.HTTPError:
                    continue
    return out


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def _best_ask(orders: list[dict]) -> float | None:
    """Return the lowest sell order price from a list of ESI orders."""
    sell = [o["price"] for o in orders if not o["is_buy_order"]]
    return min(sell) if sell else None


def price_fitting(
    eft_text: str,
    regions: list[str],
    session: requests.Session | None = None,
) -> dict:
    """
    Parse an EFT fitting block and return regional price data for every
    component.

    Returns:
    {
        "hull": str, "fit_name": str,
        "regions": [str, ...],
        "items": [
            {
                "name": str,
                "qty": int,
                "role": str,
                "type_id": int | None,
                "unresolved": bool,   # True if name didn't resolve to a type_id
                "prices": {           # keyed by region name
                    "<region>": {
                        "unit_price": float | None,
                        "total_price": float | None,
                    },
                    ...
                },
            },
            ...
        ],
        "region_totals": {
            "<region>": {
                "total": float,
                "missing_items": int,  # items with no sell order in this region
            },
            ...
        },
        "errors": [str, ...],
    }
    """
    session = session or _session()

    parsed = parse_eft(eft_text)
    items = parsed["items"]
    errors = list(parsed["parse_warnings"])

    # --- Resolve region names -----------------------------------------------
    resolved_regions: list[tuple[str, int]] = []  # (display_name, region_id)
    for region_name in regions:
        try:
            region_id = resolve_region_id(region_name, session)
            resolved_regions.append((region_name, region_id))
        except ValueError as e:
            errors.append(str(e))

    if not resolved_regions:
        raise ValueError("No valid regions could be resolved. " + " ".join(errors))

    # --- Resolve all item names to type_ids in one bulk call ----------------
    all_names = [item["name"] for item in items]
    name_to_id = resolve_type_ids_bulk(all_names, session)

    for item in items:
        tid = name_to_id.get(item["name"])
        item["type_id"] = tid
        item["unresolved"] = tid is None
        item["prices"] = {}

    # --- Fetch prices per region per item -----------------------------------
    # Group items by type_id to avoid redundant ESI calls if the same type
    # appears in multiple roles (unlikely but possible e.g. same ammo as
    # charge and cargo).
    type_ids_needed = list({item["type_id"] for item in items if item["type_id"] is not None})

    # region_prices[region_name][type_id] = best_ask or None
    region_prices: dict[str, dict[int, float | None]] = {}

    for region_name, region_id in resolved_regions:
        region_prices[region_name] = {}
        for type_id in type_ids_needed:
            try:
                orders = fetch_orders(region_id, type_id, session)
                region_prices[region_name][type_id] = _best_ask(orders)
            except Exception:
                region_prices[region_name][type_id] = None

    # --- Assemble per-item prices -------------------------------------------
    for item in items:
        tid = item["type_id"]
        for region_name, _ in resolved_regions:
            if tid is None:
                item["prices"][region_name] = {"unit_price": None, "total_price": None}
            else:
                unit = region_prices[region_name].get(tid)
                item["prices"][region_name] = {
                    "unit_price": unit,
                    "total_price": unit * item["qty"] if unit is not None else None,
                }

    # --- Regional totals ----------------------------------------------------
    region_totals: dict[str, dict] = {}
    for region_name, _ in resolved_regions:
        total = 0.0
        missing = 0
        for item in items:
            tp = item["prices"][region_name]["total_price"]
            if tp is not None:
                total += tp
            else:
                missing += 1
        region_totals[region_name] = {"total": total, "missing_items": missing}

    return {
        "hull": parsed["hull"],
        "fit_name": parsed["fit_name"],
        "regions": [r[0] for r in resolved_regions],
        "items": items,
        "region_totals": region_totals,
        "errors": errors,
    }
