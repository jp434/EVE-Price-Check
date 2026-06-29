"""
EVE Online Market Depth - Local Web GUI
=========================================

Run with:
    python app.py

Then open http://127.0.0.1:5000 in your browser.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

import market_depth as md
import catalog
import fitting as ft

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/depth")
def api_depth():
    item_name = request.args.get("item", "").strip()
    regions_raw = request.args.get("regions", "").strip()

    if not item_name:
        return jsonify({"error": "Please enter an item name."}), 400

    regions = [r.strip() for r in regions_raw.split(",") if r.strip()]
    if not regions:
        return jsonify({"error": "Please enter at least one region (comma-separated)."}), 400

    try:
        data = md.get_depth_data(item_name, regions)
    except ValueError as e:
        # e.g. item name didn't resolve to a type_id
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500

    return jsonify(data)


@app.route("/api/arbitrage")
def api_arbitrage():
    item_name = request.args.get("item", "").strip()
    regions_raw = request.args.get("regions", "").strip()
    fee_mode = request.args.get("fee_mode", "limit").strip()

    def _int_arg(name, default=0):
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    def _float_arg(name, default=0.0):
        try:
            return float(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    accounting_level = _int_arg("accounting", 0)
    broker_relations_level = _int_arg("broker_relations", 0)
    faction_standing = _float_arg("faction_standing", 0.0)
    corp_standing = _float_arg("corp_standing", 0.0)

    if not item_name:
        return jsonify({"error": "Please enter an item name."}), 400

    regions = [r.strip() for r in regions_raw.split(",") if r.strip()]
    if len(regions) < 2:
        return jsonify({"error": "Please enter at least two regions (comma-separated) to compare."}), 400

    if fee_mode not in ("limit", "instant"):
        return jsonify({"error": "fee_mode must be 'limit' or 'instant'."}), 400

    try:
        data = md.find_arbitrage(
            item_name,
            regions,
            fee_mode=fee_mode,
            accounting_level=accounting_level,
            broker_relations_level=broker_relations_level,
            faction_standing=faction_standing,
            corp_standing=corp_standing,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500

    return jsonify(data)


@app.route("/api/catalog/tree")
def api_catalog_tree():
    try:
        tree = catalog.catalog_tree_summary()
    except Exception as e:
        return jsonify({"error": f"Could not load item catalog: {e}"}), 500
    return jsonify({"tree": tree})


@app.route("/api/catalog/group/<int:group_id>")
def api_catalog_group(group_id):
    try:
        data = catalog.get_group_items(group_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Could not load group: {e}"}), 500
    return jsonify(data)


@app.route("/api/catalog/search")
def api_catalog_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Please enter a search term."}), 400
    if len(query) < 2:
        return jsonify({"error": "Search term must be at least 2 characters."}), 400

    try:
        results = catalog.search_catalog(query)
    except Exception as e:
        return jsonify({"error": f"Search failed: {e}"}), 500

    return jsonify({"query": query, "results": results})


@app.route("/api/fitting/price", methods=["POST"])
def api_fitting_price():
    body = request.get_json(silent=True) or {}
    eft_text = (body.get("eft_text") or "").strip()
    regions_raw = (body.get("regions") or "").strip()

    if not eft_text:
        return jsonify({"error": "Please paste an EFT fitting block."}), 400

    regions = [r.strip() for r in regions_raw.split(",") if r.strip()]
    if not regions:
        return jsonify({"error": "Please enter at least one region."}), 400

    try:
        data = ft.price_fitting(eft_text, regions)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500

    return jsonify(data)


if __name__ == "__main__":
    print("Starting EVE Market Depth GUI at http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
