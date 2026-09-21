# FCC AI blueprint: the fno-trader AI feature set (chat, live commentary,
# coding-agent runner) served from OpenAlgo with openalgo-grounded data.
#
# Auth mirrors the agent bridge: the desktop web client uses its session,
# the mobile app uses its OpenAlgo API key (app_key_required). The chat
# endpoint additionally injects a live market context built from the
# platform's own quote / option-chain / scalper services.
#
# The coding agents (fcc-claude, fcc-codex, ...) run on the server box with
# the platform's MCP servers registered, so a phone can safely delegate
# project-level tasks and watch the output stream.

import asyncio
import json
from typing import Any

from flask import Response, jsonify, request
from flask import session as flask_session

from services import fcc_ai_service as fcc
from utils.logging import get_logger

from .scalper_orderflow import app_key_required, scalper_orderflow_bp

logger = get_logger(__name__)


def _username_for_key() -> str | None:
    """Resolve the signed-in identity for either auth style."""
    try:
        if flask_session.get("user"):
            return flask_session.get("user")
    except Exception:
        pass
    return None


def _resolve_api_key() -> str | None:
    """The caller's OpenAlgo API key: body/header first (mobile), then the
    signed-in user's stored key (desktop)."""
    key = (request.headers.get("X-API-KEY")
           or (request.get_json(silent=True) or {}).get("api_key") or "")
    if key:
        return key
    try:
        from blueprints.agent_bridge import _openalgo_key_for
        return _openalgo_key_for(_username_for_key())
    except Exception:
        return None


@scalper_orderflow_bp.route("/fcc/status", methods=["GET"])
def fcc_status():
    return jsonify(fcc.get_status(force=request.args.get("force") == "1"))


@scalper_orderflow_bp.route("/fcc/agents", methods=["GET"])
def fcc_agents():
    return jsonify({"agents": fcc.list_agents()})


@scalper_orderflow_bp.route("/fcc/chat", methods=["POST"])
@app_key_required
def fcc_chat():
    """Grounded chat turn. Body: {messages: [{role, content}...], model?,
    focus?, context?} — context=false skips the live-market system prompt."""
    try:
        body = request.get_json(silent=True) or {}
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return jsonify({"status": "error", "message": "messages are required"}), 400
        if len(messages) > 40:
            messages = messages[-40:]
        api_key = _resolve_api_key()
        focus = body.get("focus") or body.get("symbol") or None
        result = fcc.chat(
            messages,
            model=(body.get("model") or None) or None,
            use_project_context=body.get("context", True) is not False,
            focus=focus,
            api_key=api_key,
        )
        return jsonify({"status": "success", **result})
    except Exception as e:
        logger.exception("fcc chat failed")
        return jsonify({"status": "error", "message": str(e)[:300]}), 502


@scalper_orderflow_bp.route("/fcc/commentary", methods=["POST"])
@app_key_required
def fcc_commentary():
    """Fresh squawk bullet for a symbol. Body: {symbol?, model?}

    Event-driven: returns 204 (no content) when nothing new happened since
    the last read — silence means no new trading event, not a failure."""
    try:
        body = request.get_json(silent=True) or {}
        item = fcc.generate_commentary(
            symbol=body.get("symbol"), model=body.get("model"),
            api_key=_resolve_api_key(),
        )
        if not item:
            return "", 204
        return jsonify({"status": "success", "item": item})
    except Exception as e:
        logger.exception("fcc commentary failed")
        return jsonify({"status": "error", "message": str(e)[:300]}), 500


@scalper_orderflow_bp.route("/fcc/commentary/history", methods=["GET"])
@app_key_required
def fcc_commentary_history():
    limit = min(int(request.args.get("limit", 30) or 30), 60)
    return jsonify({"status": "success", "history": fcc.get_commentary_history(limit)})


@scalper_orderflow_bp.route("/fcc/commentary/auto", methods=["POST"])
@app_key_required
def fcc_commentary_auto():
    """Start/stop the auto-squawk loop. Body: {enabled, symbol?, interval?}

    No session timer — the loop runs until switched off (it pauses itself
    outside market hours and survives restarts via persisted state)."""
    try:
        body = request.get_json(silent=True) or {}
        st = fcc.set_auto_squawk(
            enabled=bool(body.get("enabled")),
            symbol=body.get("symbol"),
            interval=body.get("interval"),
        )
        return jsonify({"status": "success", "auto": st})
    except Exception as e:
        logger.exception("fcc auto squawk toggle failed")
        return jsonify({"status": "error", "message": str(e)[:300]}), 500


@scalper_orderflow_bp.route("/fcc/commentary/auto", methods=["GET"])
@app_key_required
def fcc_commentary_auto_status():
    return jsonify({"status": "success", "auto": fcc.auto_squawk_status()})


@scalper_orderflow_bp.route("/fcc/lb-status", methods=["GET"])
@app_key_required
def fcc_lb_status():
    """OpenAlgo load-balancer node health + which node served each route.
    The LB lives in the terminal process, so proxy its status endpoint."""
    try:
        import os
        import urllib.request
        term = (os.getenv("FNO_TERMINAL_URL") or "http://127.0.0.1:8000").rstrip("/")
        with urllib.request.urlopen(f"{term}/api/lb-status", timeout=4) as r:
            return jsonify({"status": "success", "lb": json.loads(r.read().decode())})
    except Exception as e:
        return jsonify({"status": "success", "lb": {"error": str(e)[:120], "nodes": {}, "last_served": {}}})


@scalper_orderflow_bp.route("/fcc/agent", methods=["POST"])
@app_key_required
def fcc_agent():
    """Run an FCC coding agent on this project. Body: {agent, prompt, cwd?,
    timeout?, model?}"""
    try:
        body = request.get_json(silent=True) or {}
        prompt = str(body.get("prompt") or "")
        symbol = (body.get("symbol") or body.get("focus") or "").strip()
        if symbol:
            # Map the operator's active chart onto the agent: it starts every
            # run already pointed at the instrument being analysed.
            prompt = (f"The operator's active chart is {symbol.split(':')[-1]}. "
                      f"Analyse that instrument unless the task says otherwise.\n\n{prompt}")
        run = fcc.run_agent(
            agent=str(body.get("agent") or "claude"),
            prompt=prompt,
            cwd=body.get("cwd"), timeout=body.get("timeout"),
            model=body.get("model"),
        )
        return jsonify({"status": "success", "run": run})
    except FileNotFoundError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.exception("fcc agent launch failed")
        return jsonify({"status": "error", "message": str(e)[:300]}), 500


@scalper_orderflow_bp.route("/fcc/agent/runs", methods=["GET"])
@app_key_required
def fcc_agent_runs():
    limit = min(int(request.args.get("limit", 20) or 20), 50)
    return jsonify({"status": "success", "runs": fcc.list_runs(limit)})


@scalper_orderflow_bp.route("/fcc/agent/<run_id>", methods=["GET"])
@app_key_required
def fcc_agent_status(run_id: str):
    try:
        return jsonify({"status": "success", "run": fcc.agent_status(run_id)})
    except KeyError:
        return jsonify({"status": "error", "message": "run not found"}), 404


@scalper_orderflow_bp.route("/fcc/agent/<run_id>/stream", methods=["GET"])
@app_key_required
def fcc_agent_stream(run_id: str):
    """SSE stream of a run's live output."""
    def _generate():
        last_len = -1
        last_status = None
        ticks = 0
        for _ in range(3600):  # hard cap 30 min
            try:
                st = fcc.agent_status(run_id)
            except KeyError:
                yield f"data: {json.dumps({'error': 'run not found'})}\n\n"
                return
            out, status = st.get("output", ""), st.get("status")
            if len(out) != last_len or status != last_status:
                last_len, last_status = len(out), status
                yield f"data: {json.dumps({'run_id': run_id, 'status': status, 'output': out[-20_000:], 'error': st.get('error')})}\n\n"
            else:
                ticks += 1
                if ticks >= 4:
                    ticks = 0
                    yield ": ping\n\n"
            if status not in ("running", None):
                return
            import time as _t
            _t.sleep(0.5)

    return Response(_generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@scalper_orderflow_bp.route("/fcc/agent/<run_id>/stop", methods=["POST"])
@app_key_required
def fcc_agent_stop(run_id: str):
    try:
        fcc.stop_agent(run_id)
        return jsonify({"status": "success"})
    except KeyError:
        return jsonify({"status": "error", "message": "run not found"}), 404
