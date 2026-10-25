"""Transport-independent API routing.

Shared by the HTTP server (``orca/server.py``) and the in-browser runtime
(``web/backend.js`` runs this package under Pyodide), so both expose exactly
the same endpoints and behaviour.
"""
from __future__ import annotations

import json
import os
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_JSON = os.path.join(ROOT, "reports", "evaluation.json")


def handle_get(engine, path: str, query: str = "") -> tuple[int, dict]:
    if path == "/api/meta":
        return 200, engine.meta()
    if path == "/api/state":
        return 200, engine.full_state()
    if path == "/api/postmortem":
        q = parse_qs(query)
        try:
            return 200, engine.postmortem(int(q["id"][0]) if "id" in q else None)
        except ValueError as exc:
            return 404, {"ok": False, "error": str(exc)}
    if path == "/api/evaluation":
        if not os.path.exists(EVAL_JSON):
            return 404, {"ok": False, "error": "run python -m orca.evaluate first"}
        with open(EVAL_JSON, encoding="utf-8") as fh:
            return 200, json.load(fh)
    return 404, {"ok": False, "error": "not found"}


def handle_post(engine, path: str, body: dict) -> tuple[int, dict]:
    try:
        if path == "/api/inject":
            return 200, engine.inject(body["kind"], body["target"], float(body.get("severity", 1.0)))
        if path == "/api/scenario":
            return 200, engine.preset(body["id"])
        if path == "/api/strategy":
            return 200, engine.apply_strategy(int(body["id"]))
        if path == "/api/action":
            return 200, engine.apply_action(body["kind"], body["target"])
        if path == "/api/control":
            return 200, engine.control(body)
    except (KeyError, ValueError, TypeError) as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 404, {"ok": False, "error": "not found"}
