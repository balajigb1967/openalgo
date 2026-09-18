# Agent bridge for the mobile app.
#
# The desktop agent (blueprints/agent.py) is a session-only, SSE-streamed
# surface. The phone authenticates with an API key and speaks plain HTTP, so
# bridging means three things: key auth instead of a session, buffered frames
# instead of SSE, and one non-streamed round trip per turn instead of a socket.
#
# The turn itself is the same builder + stream_run the web client uses — same
# conversations, same tools, same safety switches — so answers cannot drift
# between surfaces. Frames are collected to the end and returned as JSON;
# streaming over phone networks adds reconnect handling for little perceived
# gain at answer lengths a chat produces.

import json
from typing import Any

from flask import Response, jsonify, request, stream_with_context
from flask import session as flask_session

from services.agent import attachments as agent_attachments
from services.agent import builder
from services.agent import stream as agent_stream
from services.agent.frames import SSE_HEADERS
from utils.logging import get_logger

from .scalper_orderflow import app_key_required, scalper_orderflow_bp

logger = get_logger(__name__)


def agent_bridge_routes():
    """Import-time marker only — importing this module decorates the routes
    below onto scalper_orderflow_bp, which app.py registers anyway."""


def _agent_username() -> str | None:
    """The agent module keys conversations to the session username. The app
    authenticates with an API key, which maps to a user — reuse that."""
    u = flask_session.get("user")
    if u:
        return u
    key = request.headers.get("X-API-KEY") or request.args.get("apikey")
    if key:
        try:
            from database.auth_db import verify_api_key

            return verify_api_key(key)
        except Exception:  # noqa: BLE001 — treated as no user below
            return None
    return None


def _openalgo_key_for(username: str) -> str | None:
    """The platform key the agent's tools run with — the way the desktop
    route resolves it for the signed-in user."""
    try:
        from database.auth_db import get_first_available_api_key

        return get_first_available_api_key() or None
    except Exception:  # noqa: BLE001 — reported as a config error by the caller
        return None


def _collect_stream(chunks) -> tuple[list[dict[str, Any]], bool]:
    """Consume an SSE text stream into decoded frames.

    Returns (frames, error). A stream that yields malformed SSE is reported
    rather than silently truncated: the mobile client shows the message and a
    retry affordance either way.
    """
    frames: list[dict[str, Any]] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                frames.append(json.loads(line[6:]))
            except (ValueError, TypeError):
                continue
    return frames, False


@scalper_orderflow_bp.route("/agent/status", methods=["GET"])
@app_key_required
def agent_bridge_status():
    """What the phone needs before showing the chat UI: is a model
    configured, and which conversations already exist."""
    from database import agent_db

    username = _agent_username()
    if not username:
        return jsonify({"status": "error", "message": "unknown user for key"}), 401
    return jsonify(
        {
            "status": "success",
            "data": {
                "configured": agent_db.is_configured(),
                "surface": "chat",
            },
        }
    )


@scalper_orderflow_bp.route("/agent/conversations", methods=["GET"])
@app_key_required
def agent_bridge_conversations():
    """The key owner's conversations, newest first — the phone's chat history."""
    from database import agent_db

    username = _agent_username()
    if not username:
        return jsonify({"status": "error", "message": "unknown user for key"}), 401
    rows = agent_db.list_conversations(username, surface=None, limit=50)
    return jsonify(
        {
            "status": "success",
            "data": [
                {
                    "id": c.get("id") if isinstance(c, dict) else getattr(c, "id", None),
                    "title": c.get("title") if isinstance(c, dict) else getattr(c, "title", ""),
                    "surface": c.get("surface") if isinstance(c, dict) else getattr(c, "surface", "chat"),
                    "updated_at": c.get("updated_at") if isinstance(c, dict) else getattr(c, "updated_at", None),
                }
                for c in rows
            ],
        }
    )


@scalper_orderflow_bp.route("/agent/messages", methods=["GET"])
@app_key_required
def agent_bridge_messages():
    """One conversation's transcript (query: conversation_id)."""
    from database import agent_db

    username = _agent_username()
    if not username:
        return jsonify({"status": "error", "message": "unknown user for key"}), 401
    try:
        conversation_id = int(request.args.get("conversation_id"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "conversation_id is required"}), 400
    conversation = agent_db.get_conversation(conversation_id, username)
    if conversation is None:
        return jsonify({"status": "error", "message": "conversation not found"}), 404
    rows = agent_db.list_messages(conversation_id)
    return jsonify({"status": "success", "data": rows})


@scalper_orderflow_bp.route("/agent/chat", methods=["POST"])
@app_key_required
def agent_bridge_chat():
    """One full agent turn, buffered.

    Body: {message, conversation_id?} — omit conversation_id to start a new
    conversation. Response data: {conversation_id, message: {role, content,
    tools, notices}, frames} where `frames` keeps the raw wire vocabulary for
    a client that wants tool activity; the composed `message` is what a chat
    bubble renders.
    """
    from database import agent_db
    from blueprints.agent import _build_context, _model_id_of

    username = _agent_username()
    if not username:
        return jsonify({"status": "error", "message": "unknown user for key"}), 401
    if not agent_db.is_configured():
        return jsonify(
            {"status": "error", "message": "No model is configured for the agent", "kind": "config"}
        ), 409
    api_key = _openalgo_key_for(username)
    if not api_key:
        return jsonify(
            {"status": "error", "message": "No OpenAlgo API key available for agent tools", "kind": "config"}
        ), 409

    body = request.get_json(silent=True) or {}
    for k, v in request.args.items():
        body.setdefault(k, v)
    message = str(body.get("message") or "").strip()
    if not message:
        return jsonify({"status": "error", "message": "message is required"}), 400
    if len(message) > 20_000:
        return jsonify({"status": "error", "message": "message too long"}), 400

    raw_conversation = body.get("conversation_id")
    opened_here = raw_conversation in (None, "")
    if opened_here:
        created, store_message = agent_db.create_conversation(
            username, title=message[:80], surface="chat"
        )
        if created is None:
            return jsonify({"status": "error", "message": store_message or "Could not create conversation"}), 500
        conversation_id = created["id"]
        conversation = agent_db.get_conversation(conversation_id, username)
        if conversation is None:
            return jsonify({"status": "error", "message": "Could not create conversation"}), 500
    else:
        try:
            conversation_id = int(raw_conversation)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "conversation_id must be a number"}), 400
        conversation = agent_db.get_conversation(conversation_id, username)
        if conversation is None:
            return jsonify({"status": "error", "message": "conversation not found"}), 404

    session_id = conversation.agno_session_id
    if not conversation.title:
        agent_db.update_conversation(conversation_id, username, title=message[:80])

    viz_sink: list = []
    context = _build_context(
        username,
        api_key,
        body,
        conversation_id,
        "chat",
        operator_message=message,
        viz_sink=viz_sink,
        web_search=bool(body.get("web_search", True)),
    )
    # The phone owns its latency budget: it sends `reasoning_effort` ("off"
    # for snappy answers, low/medium/high when it wants depth) and
    # `web_search` (False by default from the app — a search round trip is
    # seconds the chat UI does not owe every turn). Unvalidated values fall
    # back to the builder's own resolution, exactly like the desktop route.
    requested_effort = str(body.get("reasoning_effort") or "").strip().lower()
    if requested_effort not in ("", "off", "low", "medium", "high"):
        requested_effort = None
    try:
        agent = builder.build_agent(
            context,
            model_id=body.get("model_id"),
            session_id=session_id,
            reasoning_effort=requested_effort or None,
            extra_runtime_lines=[],
        )
    except builder.AgentBuildError as exc:
        if opened_here:
            _discard(conversation_id, username)
        return jsonify({"status": "error", "message": exc.message or "Could not start the agent"}), 502
    except Exception:  # noqa: BLE001
        logger.exception("agent bridge: build failed for conversation %s", conversation_id)
        if opened_here:
            _discard(conversation_id, username)
        return jsonify({"status": "error", "message": "Could not start the agent"}), 500

    stored_user, _err = agent_db.add_message(conversation_id, "user", message)
    user_message_id = (stored_user or {}).get("id") or ""

    try:
        chunks = agent_stream.stream_run(
            agent,
            message,
            conversation_id=conversation_id,
            session_id=session_id,
            user_id=username,
            model=_model_id_of(agent),
            tool_frames=_viz_hook(viz_sink),
            user_message_id=user_message_id,
        )
        frames, _stream_error = _collect_stream(chunks)
    except Exception:  # noqa: BLE001
        logger.exception("agent bridge: run failed for conversation %s", conversation_id)
        return jsonify({"status": "error", "message": "The agent run failed"}), 502

    composed = _compose(frames)
    if composed is None:
        # The stream produced nothing legible — nothing was stored for the
        # assistant side, but the user row above already is. Report, don't 500.
        return jsonify(
            {
                "status": "error",
                "message": "The agent returned no answer",
                "data": {"conversation_id": conversation_id},
            }
        ), 502

    # Persist the assistant side exactly the way the desktop route does, so
    # the transcript is the same conversation whichever surface asked. The
    # session binding is a conversation update, not a db call of its own.
    try:
        if composed.get("session_id"):
            agent_db.update_conversation(
                conversation_id, username, agno_session_id=composed["session_id"]
            )
        agent_db.add_message(
            conversation_id,
            "assistant",
            composed["content"],
            tools=composed["tools"] or None,
            notices=composed["notices"] or None,
        )
    except Exception:  # noqa: BLE001 — the answer still returns
        logger.exception("agent bridge: could not persist the turn")

    return jsonify(
        {
            "status": "success",
            "data": {
                "conversation_id": conversation_id,
                "message": composed,
                "frames": frames,
            },
        }
    )


def _viz_hook(viz_sink: list):
    """Frame hook for chart payloads a tool leaves on the sink."""

    def hook(tool_call_id: str, result: Any):
        if not viz_sink:
            return ()
        payloads, viz_sink[:] = list(viz_sink), []
        return ({"type": "viz", "tool_call_id": tool_call_id, "payload": p} for p in payloads)

    return hook


def _discard(conversation_id: int, username: str) -> None:
    """A conversation opened by a failed attempt must not fill the sidebar."""
    try:
        from database import agent_db

        agent_db.delete_conversation(conversation_id, username)
    except Exception:  # noqa: BLE001
        pass


def _compose(frames: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Fold the wire frames into one chat message the phone can render.

    Same shape as the desktop transcript: prose, tool calls, and the notices
    list (errors, usage, confirmations) beside the answer.
    """
    text: list[str] = []
    tools: list[dict[str, Any]] = []
    notices: list[dict[str, Any]] = []
    run_id = ""
    session_id = ""
    paused = False
    error_message = None
    open_tools: dict[str, dict[str, Any]] = {}

    for f in frames:
        kind = f.get("type")
        if kind == "start":
            run_id = str(f.get("run_id") or "")
            session_id = str(f.get("session_id") or "")
        elif kind == "token":
            text.append(str(f.get("delta") or ""))
        elif kind == "reasoning":
            continue
        elif kind == "tool_start":
            call = {
                "id": str(f.get("tool_call_id") or ""),
                "tool": str(f.get("tool_name") or f.get("tool") or ""),
                "args": f.get("args") or {},
            }
            tools.append(call)
            open_tools[call["id"]] = call
        elif kind == "tool_end":
            call = open_tools.get(str(f.get("tool_call_id") or ""))
            if call is not None:
                call["result"] = str(f.get("result") or "")[:2000]
        elif kind in ("notice", "usage", "viz"):
            notices.append(f)
        elif kind == "confirm":
            paused = True
            notices.append(f)
        elif kind == "error":
            error_message = str(f.get("message") or "agent error")
            notices.append(f)
        elif kind == "done":
            continue

    if error_message and not text:
        return None

    return {
        "role": "assistant",
        "content": "".join(text).strip(),
        "tools": tools,
        "notices": notices,
        "paused": paused,
        "run_id": run_id,
        "session_id": session_id,
    }


# ---------------------------------------------------------------------------
# Streaming bridge: the same turn as SSE, for clients that want the answer
# to appear as it is generated rather than after the whole turn completes.
# The wire format is exactly the desktop `/agent/api/chat/stream` frames, so
# a phone can reuse the same rendering rules.
# ---------------------------------------------------------------------------


def _bridge_preconditions(body: dict):
    """Everything that can fail before the stream opens. Returns
    (username, api_key, conversation, error_response)."""
    from database import agent_db

    username = _agent_username()
    if not username:
        return None, None, None, (jsonify({"status": "error", "message": "unknown user for key"}), 401)
    if not agent_db.is_configured():
        return None, None, None, (
            jsonify(
                {"status": "error", "message": "No model is configured for the agent", "kind": "config"}
            ),
            409,
        )
    api_key = _openalgo_key_for(username)
    if not api_key:
        return None, None, None, (
            jsonify({"status": "error", "message": "No OpenAlgo API key available for agent tools", "kind": "config"}),
            409,
        )

    message = str(body.get("message") or "").strip()
    if not message:
        return None, None, None, (jsonify({"status": "error", "message": "message is required"}), 400)

    raw_conversation = body.get("conversation_id")
    if raw_conversation in (None, ""):
        created, store_message = agent_db.create_conversation(username, title=message[:80], surface="chat")
        if created is None:
            return None, None, None, (jsonify({"status": "error", "message": store_message or "Could not create conversation"}), 500)
        conversation = agent_db.get_conversation(created["id"], username)
        if conversation is None:
            return None, None, None, (jsonify({"status": "error", "message": "Could not create conversation"}), 500)
    else:
        try:
            conversation_id = int(raw_conversation)
        except (TypeError, ValueError):
            return None, None, None, (jsonify({"status": "error", "message": "conversation_id must be a number"}), 400)
        conversation = agent_db.get_conversation(conversation_id, username)
        if conversation is None:
            return None, None, None, (jsonify({"status": "error", "message": "conversation not found"}), 404)
    return username, api_key, conversation, None


@scalper_orderflow_bp.route("/agent/chat/stream", methods=["POST"])
@app_key_required
def agent_bridge_chat_stream():
    """One agent turn as Server-Sent Events — the phone's fast path.

    Same body as /agent/chat. Frames arrive as they are generated: token
    deltas paint the answer while tools run, so the first pixels land in
    ~1s instead of after the whole turn. The final frame is `done`.
    """
    from blueprints.agent import _TurnRecorder, _build_context, _model_id_of, _persist_turn
    from database import agent_db

    body = request.get_json(silent=True) or {}
    for k, v in request.args.items():
        body.setdefault(k, v)

    username, api_key, conversation, error = _bridge_preconditions(body)
    if error:
        return error

    message = str(body.get("message") or "").strip()
    conversation_id = conversation.id
    session_id = conversation.agno_session_id
    if not conversation.title:
        agent_db.update_conversation(conversation_id, username, title=message[:80])

    requested_effort = str(body.get("reasoning_effort") or "").strip().lower()
    if requested_effort not in ("", "off", "low", "medium", "high"):
        requested_effort = None

    viz_sink: list = []
    context = _build_context(
        username,
        api_key,
        body,
        conversation_id,
        "chat",
        operator_message=message,
        viz_sink=viz_sink,
        web_search=bool(body.get("web_search", True)),
    )
    try:
        agent = builder.build_agent(
            context,
            model_id=body.get("model_id"),
            session_id=session_id,
            reasoning_effort=requested_effort or None,
            extra_runtime_lines=[],
        )
    except builder.AgentBuildError as exc:
        return jsonify({"status": "error", "message": exc.message or "Could not start the agent"}), 502
    except Exception:  # noqa: BLE001
        logger.exception("agent bridge stream: build failed for conversation %s", conversation_id)
        return jsonify({"status": "error", "message": "Could not start the agent"}), 500

    stored_user, _err = agent_db.add_message(conversation_id, "user", message)
    user_message_id = (stored_user or {}).get("id") or ""

    recorder = _TurnRecorder()

    def _generate():
        try:
            chunks = agent_stream.stream_run(
                agent,
                message,
                conversation_id=conversation_id,
                session_id=session_id,
                user_id=username,
                model=_model_id_of(agent),
                tool_frames=_viz_hook(viz_sink),
                user_message_id=user_message_id,
            )
            for chunk in chunks:
                if chunk.startswith("data: "):
                    try:
                        recorder.observe(json.loads(chunk[6:]))
                    except (ValueError, TypeError):
                        pass
                yield chunk
        finally:
            _persist_turn(recorder, conversation_id, username)

    response = Response(stream_with_context(_generate()), mimetype="text/event-stream")
    for header, value in SSE_HEADERS.items():
        response.headers[header] = value
    return response
