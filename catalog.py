"""
EVE Online Tradeable Item Catalog
===================================

Builds a browsable tree of all tradeable items in EVE Online, using ESI's
market group endpoints (the same category tree the in-game market browser
uses), plus bulk name resolution for items and groups.

This is reference data, not live order-book data: it changes rarely (only
when CCP adds/removes items or reorganizes market categories), so we cache
the built tree in memory for the lifetime of the process rather than
re-fetching it on every request.

Endpoints used (all public, no auth required):
    GET  /markets/groups/                  -> list of all market_group_ids
    GET  /markets/groups/{market_group_id}/ -> one group's name, parent,
                                                description, and type_ids
    POST /universe/names/                  -> bulk id -> name resolution
                                                (types, and incidentally
                                                anything else, but we only
                                                use it for type_ids here)
"""

from __future__ import annotations

import threading
import time

import requests

ESI_BASE = "https://esi.evetech.net/latest"
USER_AGENT = "market-depth-tool (personal use; contact: none)"

# /universe/names/ documented limit is 1000 IDs per request, and a single
# invalid ID can fail the whole batch with no partial result - so we also
# keep chunks modest to limit blast radius of any one bad ID.
NAMES_CHUNK_SIZE = 500

# How long to keep the built tree cached in memory before allowing a
# rebuild. Market groups are reference data that essentially never change
# during a play session, so this is generous.
TREE_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_market_group_ids(session: requests.Session) -> list[int]:
    resp = session.get(f"{ESI_BASE}/markets/groups/", params={"datasource": "tranquility"})
    resp.raise_for_status()
    return resp.json()


def fetch_market_group(group_id: int, session: requests.Session) -> dict:
    """
    Returns: {"market_group_id", "name", "description", "parent_group_id"
    (may be absent for top-level groups), "types": [type_id, ...]}
    """
    resp = session.get(
        f"{ESI_BASE}/markets/groups/{group_id}/",
        params={"datasource": "tranquility"},
    )
    resp.raise_for_status()
    return resp.json()


def resolve_names_bulk(ids: list[int], session: requests.Session) -> dict[int, str]:
    """
    Resolve a list of IDs (of any ESI-supported category, but we only feed
    this type_ids) to names via POST /universe/names/, in chunks. If a
    chunk fails entirely (e.g. one bad ID poisons the batch), we fall back
    to resolving that chunk's IDs one at a time so a single bad ID doesn't
    cost us the whole chunk's names.

    Returns: {id: name, ...} - IDs that couldn't be resolved are omitted.
    """
    out: dict[int, str] = {}
    if not ids:
        return out

    for chunk in _chunked(ids, NAMES_CHUNK_SIZE):
        try:
            resp = session.post(
                f"{ESI_BASE}/universe/names/",
                json=chunk,
                params={"datasource": "tranquility"},
            )
            resp.raise_for_status()
            for entry in resp.json():
                out[entry["id"]] = entry["name"]
        except requests.HTTPError:
            # Fall back to one-at-a-time for this chunk so a single bad ID
            # doesn't sink names for everything else in the batch.
            for single_id in chunk:
                try:
                    resp = session.post(
                        f"{ESI_BASE}/universe/names/",
                        json=[single_id],
                        params={"datasource": "tranquility"},
                    )
                    resp.raise_for_status()
                    for entry in resp.json():
                        out[entry["id"]] = entry["name"]
                except requests.HTTPError:
                    continue  # truly unresolvable id (e.g. unpublished type), skip it

    return out


class _CatalogCache:
    """
    Simple in-memory cache for the built market group tree. Thread-safe
    enough for Flask's dev server (single process, a handful of threads);
    not meant to survive process restarts.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tree = None
        self._group_index = None  # group_id -> group dict, for fast lookup
        self._built_at = 0.0

    def get(self):
        with self._lock:
            if self._tree is not None and (time.time() - self._built_at) < TREE_CACHE_TTL_SECONDS:
                return self._tree, self._group_index
        return None, None

    def set(self, tree, group_index):
        with self._lock:
            self._tree = tree
            self._group_index = group_index
            self._built_at = time.time()


_cache = _CatalogCache()


def build_market_group_tree(session: requests.Session | None = None) -> tuple[list[dict], dict[int, dict]]:
    """
    Fetch and assemble the full market group tree. Returns:
        (top_level_groups, group_index)

    top_level_groups: list of group dicts (parent_group_id is None/missing),
        each with a "children" list nesting the same structure recursively.
    group_index: flat {group_id: group_dict} for O(1) lookup by id later
        (e.g. when expanding a node lazily on the frontend).

    Each group dict:
        {
            "group_id": int,
            "name": str,
            "description": str,
            "parent_group_id": int | None,
            "type_ids": [int, ...],   # items directly in this group
            "item_count": int,         # len(type_ids), for display before
                                        # names are resolved
            "children": [group_dict, ...],
        }

    Cached in memory after first build (see _CatalogCache).
    """
    cached_tree, cached_index = _cache.get()
    if cached_tree is not None:
        return cached_tree, cached_index

    session = session or _session()

    group_ids = fetch_market_group_ids(session)

    group_index: dict[int, dict] = {}
    for gid in group_ids:
        try:
            raw = fetch_market_group(gid, session)
        except requests.HTTPError:
            continue  # skip any group ESI fails to return; don't fail the whole tree

        group_index[gid] = {
            "group_id": gid,
            "name": raw.get("name", f"Group {gid}"),
            "description": raw.get("description", ""),
            "parent_group_id": raw.get("parent_group_id"),
            "type_ids": raw.get("types", []) or [],
            "item_count": len(raw.get("types", []) or []),
            "children": [],
        }

    # Wire up parent -> children. Some parent_group_ids may reference a
    # group that failed to fetch above; treat those as top-level rather
    # than dropping them.
    top_level: list[dict] = []
    for gid, group in group_index.items():
        parent_id = group["parent_group_id"]
        if parent_id is not None and parent_id in group_index:
            group_index[parent_id]["children"].append(group)
        else:
            top_level.append(group)

    def _sort_recursive(nodes: list[dict]):
        nodes.sort(key=lambda g: g["name"])
        for n in nodes:
            _sort_recursive(n["children"])

    _sort_recursive(top_level)

    _cache.set(top_level, group_index)
    return top_level, group_index


def get_group_items(group_id: int, session: requests.Session | None = None) -> dict:
    """
    Get the resolved (name-attached) list of items directly inside one
    market group (not including subgroups' items).

    Returns:
        {
            "group_id": int, "name": str, "description": str,
            "items": [{"type_id": int, "name": str}, ...]  # sorted by name
        }
    Raises ValueError if the group_id isn't found in the cached tree.
    """
    _, group_index = build_market_group_tree(session)
    group = group_index.get(group_id)
    if group is None:
        raise ValueError(f"No market group found with id {group_id}")

    session = session or _session()
    names = resolve_names_bulk(group["type_ids"], session)

    items = [
        {"type_id": tid, "name": names.get(tid, f"Unknown item {tid}")}
        for tid in group["type_ids"]
    ]
    items.sort(key=lambda it: it["name"])

    return {
        "group_id": group["group_id"],
        "name": group["name"],
        "description": group["description"],
        "items": items,
    }


def search_catalog(query: str, session: requests.Session | None = None, limit: int = 50) -> list[dict]:
    """
    Search across ALL tradeable items by name substring (case-insensitive).

    This resolves names for every item in every market group the first
    time it's called (expensive - potentially tens of thousands of names
    across many bulk requests), then caches that full id->name map in the
    catalog cache for subsequent searches. Subsequent calls are fast
    in-memory substring scans.

    Returns up to `limit` matches: [{"type_id": int, "name": str,
    "group_name": str}, ...], sorted by name.
    """
    session = session or _session()
    top_level, group_index = build_market_group_tree(session)

    all_index = _get_or_build_full_name_index(group_index, session)

    q = query.strip().lower()
    if not q:
        return []

    matches = [
        entry for entry in all_index.values()
        if q in entry["name"].lower()
    ]
    matches.sort(key=lambda e: (len(e["name"]), e["name"]))  # shorter/closer matches first
    return matches[:limit]


# Separate cache just for the full name index, since building it is the
# expensive part of search and we don't want to redo it per group lookup.
_full_index_cache: dict[int, dict] | None = None
_full_index_lock = threading.Lock()


def _get_or_build_full_name_index(group_index: dict[int, dict], session: requests.Session) -> dict[int, dict]:
    global _full_index_cache

    with _full_index_lock:
        if _full_index_cache is not None:
            return _full_index_cache

        all_type_ids: list[int] = []
        type_to_group_name: dict[int, str] = {}
        for group in group_index.values():
            for tid in group["type_ids"]:
                all_type_ids.append(tid)
                type_to_group_name[tid] = group["name"]

        names = resolve_names_bulk(all_type_ids, session)

        index = {
            tid: {
                "type_id": tid,
                "name": names.get(tid, f"Unknown item {tid}"),
                "group_name": type_to_group_name.get(tid, ""),
            }
            for tid in all_type_ids
            if tid in names  # skip unresolvable ids entirely for search
        }

        _full_index_cache = index
        return index


def catalog_tree_summary(session: requests.Session | None = None) -> list[dict]:
    """
    Lightweight version of the tree for initial page load: just names,
    ids, item counts, and nesting - no per-item names resolved yet (those
    are fetched lazily per-group via get_group_items when a node expands).
    """
    top_level, _ = build_market_group_tree(session)

    def _strip(node: dict) -> dict:
        return {
            "group_id": node["group_id"],
            "name": node["name"],
            "item_count": node["item_count"],
            "children": [_strip(c) for c in node["children"]],
        }

    return [_strip(n) for n in top_level]
