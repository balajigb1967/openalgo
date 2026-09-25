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
            user_id=_username_for_key(),
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
            force=bool(body.get("force", True)),
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


@scalper_orderflow_bp.route("/fcc/commentary/scan-watchlist", methods=["POST"])
@app_key_required
def fcc_commentary_scan_watchlist():
    """Scan and synthesize trading ideas across all watchlist symbols."""
    try:
        body = request.get_json(silent=True) or {}
        items = fcc.scan_watchlist_commentary(
            watchlist_symbols=body.get("symbols"),
            model=body.get("model"),
            api_key=_resolve_api_key(),
        )
        return jsonify({"status": "success", "items": items, "count": len(items)})
    except Exception as e:
        logger.exception("fcc scan watchlist commentary failed")
        return jsonify({"status": "error", "message": str(e)[:300]}), 500


@scalper_orderflow_bp.route("/fcc/lb-status", methods=["GET"])
@app_key_required
def fcc_lb_status():
    """Dual-instance OpenAlgo health.  Reports local + peer status."""
    import os

    peer_url = os.getenv("PEER_OPENALGO_URL", "").strip()
    peer_key = os.getenv("PEER_OPENALGO_API_KEY", "").strip()
    port = os.getenv("PORT", os.getenv("FLASK_PORT", "5000"))

    nodes = {
        f"local:{port}": {"status": "up", "broker": os.getenv("BROKER_API_NAME", "unknown")},
    }

    if peer_url:
        try:
            import httpx
            r = httpx.post(
                peer_url.rstrip("/") + "/api/v1/quotes",
                json={"apikey": peer_key, "symbol": "NIFTY", "exchange": "NSE_INDEX"},
                timeout=4,
            )
            nodes[peer_url] = {
                "status": "up" if r.status_code in (200, 400) else "degraded",
                "http": r.status_code,
            }
        except Exception as e:
            nodes[peer_url] = {"status": "down", "error": str(e)[:100]}

    return jsonify({"status": "success", "lb": {"nodes": nodes}})


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


# ---------------------------------------------------------------- FCC Academy

from services import fcc_education_service as edu


@scalper_orderflow_bp.route("/fcc/education/curriculum", methods=["GET"])
@app_key_required
def fcc_education_curriculum():
    """Lesson index: id, title, subtitle, minutes, live flag."""
    return jsonify({"status": "success", **edu.get_curriculum()})


@scalper_orderflow_bp.route("/fcc/education/lesson/<lesson_id>", methods=["GET"])
@app_key_required
def fcc_education_lesson(lesson_id: str):
    """One lesson: explanation + quiz + LIVE data snapshot for the requested
    instrument (query: symbol, exchange — defaults to NIFTY)."""
    try:
        return jsonify({"status": "success", **edu.get_lesson(
            lesson_id,
            symbol=request.args.get("symbol"),
            exchange=request.args.get("exchange"),
            api_key=_resolve_api_key(),
        )})
    except KeyError:
        return jsonify({"status": "error", "message": "unknown lesson"}), 404
    except Exception as e:
        logger.exception("fcc education lesson failed")
        return jsonify({"status": "error", "message": str(e)[:300]}), 500


@scalper_orderflow_bp.route("/fcc/education/quiz/<lesson_id>", methods=["POST"])
@app_key_required
def fcc_education_quiz(lesson_id: str):
    """Grade an attempt. Body: {answers: {"0": 1, ...}} — indices per the
    lesson's quiz block. Records progress for signed-in users."""
    try:
        body = request.get_json(silent=True) or {}
        answers = body.get("answers") or {}
        if not isinstance(answers, dict):
            return jsonify({"status": "error", "message": "answers must be an object"}), 400
        return jsonify({"status": "success", **edu.grade_quiz(
            lesson_id, answers, user_id=_username_for_key())})
    except KeyError:
        return jsonify({"status": "error", "message": "unknown lesson"}), 404
    except Exception as e:
        logger.exception("fcc education quiz failed")
        return jsonify({"status": "error", "message": str(e)[:300]}), 500


@scalper_orderflow_bp.route("/fcc/education/ask", methods=["POST"])
@app_key_required
def fcc_education_ask():
    """Ask the tutor. Body: {question, lesson?, symbol?, exchange?} — the
    tutor teaches with the lesson's live snapshot in context."""
    try:
        body = request.get_json(silent=True) or {}
        result = edu.ask_tutor(
            str(body.get("question") or ""),
            lesson_id=body.get("lesson"),
            symbol=body.get("symbol"), exchange=body.get("exchange"),
            api_key=_resolve_api_key(),
        )
        return jsonify({"status": "success", **result})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.exception("fcc education tutor failed")
        return jsonify({"status": "error", "message": str(e)[:300]}), 502


@scalper_orderflow_bp.route("/fcc/education/progress", methods=["GET"])
@app_key_required
def fcc_education_progress():
    """Per-user completion map. API-key (mobile) callers are ephemeral."""
    return jsonify({"status": "success", **edu.get_progress(_username_for_key())})
