"""Read-only LAN dashboard for the balancer's live state.

Runs in a background thread inside the same process as the balancer loop,
so it reads the shared BalancerState directly instead of polling Shelly or
DEFA itself - there's never a second, competing source of Modbus writes.
"""

from __future__ import annotations

import threading

from flask import Flask, jsonify, render_template

from .state import BalancerState


def create_app(state: BalancerState) -> Flask:
    app = Flask(__name__)

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
                        "enabled": c.enabled,
                        "actual": {"l1": c.actual_l1_a, "l2": c.actual_l2_a, "l3": c.actual_l3_a},
                        "installation_max_a": c.installation_max_a,
                    }
                    for c in snapshot.chargers
                ],
            }
        )

    return app


def start_in_background(state: BalancerState, host: str, port: int) -> None:
    app = create_app(state)

    def _serve() -> None:
        app.run(host=host, port=port, threaded=True, use_reloader=False)

    threading.Thread(target=_serve, daemon=True, name="ev-balancer-web").start()
