"""Read-only LAN dashboard for the balancer's live state.

Runs in a background thread inside the same process as the balancer loop,
so it reads the shared BalancerState directly instead of polling Shelly or
DEFA itself - there's never a second, competing source of Modbus writes.
"""

from __future__ import annotations

import threading

from flask import Flask, jsonify, render_template, request

from .state import BalancerState
from .stations import DuplicateStationError, StationManager, StationNotFoundError


def create_app(state: BalancerState, station_manager: StationManager, settings_token: str) -> Flask:
    app = Flask(__name__)

    def _settings_authorized() -> bool:
        return bool(settings_token) and request.headers.get("X-Settings-Token") == settings_token

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    def api_status():
        snapshot = state.get()
        if snapshot is None:
            return jsonify({"ready": False})
        return jsonify(
            {
                "ready": True,
                "updated_at": snapshot.updated_at,
                "have_fresh_data": snapshot.have_fresh_data,
                "settings_enabled": bool(settings_token),
                "main_fuse": {
                    "l1": snapshot.main_fuse_l1_a,
                    "l2": snapshot.main_fuse_l2_a,
                    "l3": snapshot.main_fuse_l3_a,
                },
                "ev_total": {
                    "l1": snapshot.ev_total_l1_a,
                    "l2": snapshot.ev_total_l2_a,
                    "l3": snapshot.ev_total_l3_a,
                },
                "allocated_a": snapshot.allocated_a,
                "chargers": [
                    {
                        "name": c.name,
                        "host": c.host,
                        "port": c.port,
                        "unit_id": c.unit_id,
                        "enabled": c.enabled,
                        "actual": {"l1": c.actual_l1_a, "l2": c.actual_l2_a, "l3": c.actual_l3_a},
                        "installation_max_a": c.installation_max_a,
                        "online": c.online,
                        "last_seen": c.last_seen,
                    }
                    for c in snapshot.chargers
                ],
            }
        )

    @app.route("/api/chargers", methods=["POST"])
    def add_charger():
        if not settings_token:
            return jsonify({"error": "Settings are disabled - set web.settings_token in config.yaml to enable them"}), 403
        if not _settings_authorized():
            return jsonify({"error": "Invalid settings token"}), 403

        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        host = str(payload.get("host", "")).strip()
        if not name or not host:
            return jsonify({"error": "name and host are required"}), 400
        try:
            port = int(payload.get("port", 502))
            unit_id = int(payload.get("unit_id", 255))
        except (TypeError, ValueError):
            return jsonify({"error": "port and unit_id must be integers"}), 400
        if not (1 <= port <= 65535):
            return jsonify({"error": "port must be between 1 and 65535"}), 400
        if not (0 <= unit_id <= 255):
            return jsonify({"error": "unit_id must be between 0 and 255"}), 400

        try:
            station_manager.add(name, host, port, unit_id)
        except DuplicateStationError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True}), 201

    @app.route("/api/chargers/<name>", methods=["DELETE"])
    def remove_charger(name: str):
        if not settings_token:
            return jsonify({"error": "Settings are disabled - set web.settings_token in config.yaml to enable them"}), 403
        if not _settings_authorized():
            return jsonify({"error": "Invalid settings token"}), 403

        try:
            station_manager.remove(name)
        except StationNotFoundError:
            return jsonify({"error": f"Charger '{name}' not found"}), 404
        return jsonify({"ok": True})

    return app


def start_in_background(
    state: BalancerState,
    station_manager: StationManager,
    settings_token: str,
    host: str,
    port: int,
) -> None:
    app = create_app(state, station_manager, settings_token)

    def _serve() -> None:
        app.run(host=host, port=port, threaded=True, use_reloader=False)

    threading.Thread(target=_serve, daemon=True, name="ev-balancer-web").start()
