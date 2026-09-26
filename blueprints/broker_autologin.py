# blueprints/broker_autologin.py
"""Broker TOTP auto-login endpoints — dual-broker sessions without a browser.

Auth: the same dual scheme as the scalper plugin (blueprints/scalper_orderflow.py)
— a valid web session cookie OR the OpenAlgo API key (X-API-KEY header or
?apikey=). The mobile app authenticates with its session cookie, the peer
OpenAlgo instance with its stored peer API key, and the desktop page with
the normal login.

Routes (served by BOTH instances, so /autologin always addresses "this
instance's broker" and the dual orchestrator relays to the peer):
  GET  /autologin            — desktop control page (React-style JSON if requested)
  GET  /autologin/status     — broker identity + session/creds snapshot (self + peer)
  POST /autologin/run        — start a job: {"scope": "local"|"dual", "broker": ...}
  GET  /autologin/job/<id>   — poll job progress (steps stream live)
"""

import os
import sys

from flask import Blueprint, jsonify, redirect, request, session

from services.broker_autologin_service import (
    get_job,
    promote_session,
    start_dual,
    start_local,
    status_snapshot,
)
from utils.logging import get_logger
from utils.session import is_session_valid

logger = get_logger(__name__)

autologin_bp = Blueprint("autologin_bp", __name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Broker Auto-Login</title>
<style>
 :root{color-scheme:dark}
 body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;
      margin:0;padding:24px;max-width:880px;margin-inline:auto}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#8b949e;font-size:13px;margin-bottom:20px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px}
 .name{font-weight:700;font-size:15px;display:flex;align-items:center;gap:8px}
 .dot{width:9px;height:9px;border-radius:50%;background:#6e7681;display:inline-block}
 .dot.ok{background:#3fb950}.dot.bad{background:#f85149}
 .meta{color:#8b949e;font-size:12px;margin:6px 0 12px}
 button{background:#238636;color:#fff;border:0;border-radius:6px;padding:9px 14px;
        font-weight:600;cursor:pointer;font-size:13px;width:100%}
 button:disabled{opacity:.45;cursor:default}
 button.ghost{background:#21262d;border:1px solid #30363d}
 .log{background:#0d1117;border:1px solid #30363d;border-radius:8px;margin-top:12px;
      padding:10px;font:12px/1.55 ui-monospace,Consolas,monospace;max-height:230px;
      overflow:auto;display:none;white-space:pre-wrap}
 .step-ok{color:#3fb950}.step-err{color:#f85149}.step-run{color:#d29922}
 #dual{margin-top:16px}
 .msg{margin-top:14px;font-size:13px;color:#8b949e}
 @media(max-width:640px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>Broker Auto-Login</h1>
<div class="sub" id="instinfo">TOTP login for both brokers — no browser, no manual token entry.</div>
<div class="grid" id="cards"></div>
<button id="dual" class="ghost">Login BOTH brokers (dual auto-login)</button>
<div class="log" id="log"></div>
<div class="msg" id="msg"></div>
<script>
const log = document.getElementById('log');
const msg = document.getElementById('msg');
let busy = false;
function esc(s){return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function line(step, status, detail){
  const cls = status==='success'?'step-ok':status==='error'?'step-err':'step-run';
  const icon = status==='success'?'\u2713':status==='error'?'\u2717':'\u2022';
  return `<div class="${cls}">${icon} ${esc(step)}${detail?' \u2014 '+esc(detail):''}</div>`;
}
async function csrf(){
  const r = await fetch('/auth/csrf-token', {credentials:'include'});
  return (await r.json()).csrf_token;
}
async function refresh(){
  const st = await (await fetch('/autologin/status', {credentials:'include'})).json();
  if(st.status!=='success'){ msg.textContent='Status unavailable'; return; }
  document.getElementById('instinfo').textContent =
    `This instance: ${st.broker.toUpperCase()}${st.peer?` \u00b7 peer: ${(st.peer.broker||'').toUpperCase()}`:''}`;
  const brokers = [['self', st], ['peer', st.peer]];
  document.getElementById('cards').innerHTML = brokers.map(([scope, s]) => {
    if(!s) return '';
    const live = s.broker_session && s.broker_session.token_present;
    const canAuto = s.totp_supported;
    return `<div class="card">
      <div class="name"><span class="dot ${live?'ok':'bad'}"></span>${(s.broker||'').toUpperCase()}</div>
      <div class="meta">broker session: ${live?'live':'not connected'}
        \u00b7 TOTP creds: ${s.creds_ok && s.creds_ok[s.broker]?'ready':'missing'}</div>
      <button data-scope="local" data-broker="${s.broker}" ${(!canAuto||busy)?'disabled':''}>
        TOTP auto-login</button>
    </div>`;
  }).join('');
  document.querySelectorAll('button[data-scope]').forEach(b =>
    b.onclick = () => run({scope:'local', broker:b.dataset.broker}));
  document.getElementById('dual').disabled = busy;
}
async function run(body){
  busy = true; await refresh();
  log.style.display='block'; log.innerHTML=''; msg.textContent='';
  try{
    const resp = await fetch('/autologin/run', {
      method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','X-CSRFToken':await csrf()},
      body: JSON.stringify(body)});
    const j = await resp.json();
    if(j.status!=='success'){ msg.textContent = j.message || 'Failed to start'; busy=false; await refresh(); return; }
    await poll(j.job_id);
  }catch(e){ msg.textContent = 'Error: '+e; }
  busy = false; await refresh();
}
async function poll(id){
  while(true){
    const j = await (await fetch('/autologin/job/'+id, {credentials:'include'})).json();
    if(j.status!=='success'){ msg.textContent='Job lost'; return; }
    const job = j.job;
    log.innerHTML = job.steps.map(s => line(s.step, s.status, s.detail)).join('')
      + (job.current_step ? line(job.current_step+'\u2026','run') : '');
    log.scrollTop = log.scrollHeight;
    if(job.done){ msg.textContent = job.message; return; }
    await new Promise(r => setTimeout(r, 1200));
  }
}
document.getElementById('dual').onclick = () => run({scope:'dual'});
refresh(); setInterval(refresh, 10000);
</script></body></html>"""


def _resolve_user():
    """Web session first, then the OpenAlgo API key (header or query).
    Returns (username, error_response)."""
    username = session.get("user")
    if username and session.get("logged_in"):
        return username, None

    provided = (
        request.headers.get("X-API-KEY")
        or request.headers.get("X-Api-Key")
        or request.args.get("apikey")
        or ""
    ).strip()
    if not provided:
        return None, (
            jsonify(
                {
                    "status": "error",
                    "message": "Authentication required (web session or X-API-KEY)",
                }
            ),
            401,
        )

    from database.auth_db import verify_api_key

    user = verify_api_key(provided)
    if not user:
        return None, (
            jsonify({"status": "error", "message": "Invalid API key"}),
            401,
        )
    return user, None


@autologin_bp.route("/autologin", methods=["GET"])
def autologin_page():
    """Desktop control page for dual-broker TOTP login."""
    if request.headers.get("Accept") == "application/json":
        user, err = _resolve_user()
        if err:
            return err
        return jsonify(status_snapshot(user))

    # Browser: normal web session required (same bar as the dashboard).
    if not (session.get("user") and is_session_valid()):
        return redirect("/login")
    return _PAGE_HTML


@autologin_bp.route("/autologin/status", methods=["GET"])
def autologin_status():
    user, err = _resolve_user()
    if err:
        return err
    return jsonify(status_snapshot(user))


@autologin_bp.route("/autologin/run", methods=["POST"])
def autologin_run():
    user, err = _resolve_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    scope = (data.get("scope") or "local").lower()
    broker = (data.get("broker") or "").lower().strip()

    here_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local_broker = "flattrade" if "flattrade" in here_parent.lower() else "fyers"

    if scope == "dual":
        job_id = start_dual(user)
        return jsonify({"status": "success", "job_id": job_id, "scope": "dual"})

    target = broker or local_broker
    if target != local_broker:
        return jsonify(
            {
                "status": "error",
                "message": f"this instance serves {local_broker}; use scope=dual "
                f"or ask the peer instance for {target}",
            }
        ), 400
    job_id = start_local(target, user)
    return jsonify({"status": "success", "job_id": job_id, "scope": "local", "broker": target})


@autologin_bp.route("/autologin/job/<job_id>", methods=["GET"])
def autologin_job(job_id):
    user, err = _resolve_user()
    if err:
        return err
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "unknown job"}), 404
    # On success, promote THIS caller's web session to logged-in — the poll
    # request itself carries the session cookie, so Flask writes the updated
    # session onto the response and the next app page / API call is fully
    # broker-authenticated. Server-side token never leaves the process.
    if job.get("done") and job.get("state") == "success" and user:
        try:
            promote_session(user)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"session promotion failed: {e}")
    return jsonify({"status": "success", "job": job})
