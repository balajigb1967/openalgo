# services/broker_autologin_service.py
"""In-app TOTP auto-login for both brokers — dual-broker sessions without a
browser, driven by the same endpoint chains the standalone /home/ubuntu/
broker_login.py has proven daily (its flows were lifted almost verbatim).

  fyers      -> vagator send_login_otp_v2 -> verify_otp (TOTP, window-aligned)
                -> verify_pin_v2 -> app token mint (api/v2/token, 308) ->
                validate-authcode (v3)
  flattrade  -> auth/session -> ftauth (sha256 password + TOTP, Override=Y) ->
                RedirectURL request code -> trade/apitoken

What the standalone script does by seeding the sqlite DB directly, this
service does IN-PROCESS against the running app (no restart dance):

  1. handle_auth_success() writes the auth row (upsert_auth), registers the
     session, and triggers the smart master-contract download in a daemon
     thread — the same code path a normal web login takes.
  2. The in-memory auth caches (auth_cache / feed_token_cache in
     database.auth_db) are dropped for the user so the next API call reads
     the fresh token.
  3. /api/cache/reload is called on our own instance (forged admin session,
     same trick the standalone script uses) so the symbol cache reloads
     without a service restart.

Dual-broker design: this instance (self) runs its own login; the peer
instance (the other broker's OpenAlgo on 127.0.0.1) is driven over HTTP via
PEER_OPENALGO_URL + PEER_OPENALGO_API_KEY using the /autologin/run+job
endpoints served by the same blueprint on both instances. Each instance
therefore only ever touches its own session/DB; the orchestrator merely
relays commands and collects status.

Fyers caveat (documented in broker_login.py): a vagator TOTP login revokes
the previous Fyers session — the operator has opted in via
FYERS_TOTP_AUTOMATION=1. Flattrade's ftauth login is non-destructive.

Progress feedback: every run is a background job with a step log
({ts, step, status, detail}) that the mobile app and the desktop page poll
via GET /autologin/job/<id>.
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from utils.logging import get_logger

logger = get_logger(__name__)

# In-process job registry (per instance; jobs are tiny, capped, and only
# kept for polling — 10 minutes of history is plenty).
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_SEQ = 0
_KEEP_JOBS = 20


# ---------------------------------------------------------------------------
# env / config helpers (same parsing rules as the standalone script)
# ---------------------------------------------------------------------------
def _read_env_file(path):
    out = {}
    try:
        pat = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")
        for line in open(path, encoding="utf-8"):
            m = pat.match(line)
            if not m:
                continue
            val = m.group(2).strip()
            if not (len(val) >= 2 and val[0] == val[-1] and val[0] in "'\""):
                val = val.split(" #")[0].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            val = val.rstrip("?").strip()
            out[m.group(1)] = val
    except OSError:
        pass
    return out


def _load_all_env():
    """Instance .env first, then broker_creds.env (operator overrides)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {}
    env.update(_read_env_file(os.path.join(here, ".env")))
    env.update(_read_env_file("/home/ubuntu/broker_creds.env"))
    return env


def _totp(secret):
    """RFC 6238 code without pyotp (30s step, SHA1, 6 digits)."""
    import base64
    import hashlib
    import hmac
    import struct

    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    counter = int(time.time()) // 30
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def _totp_now(secret):
    try:
        import pyotp  # noqa: F401

        import pyotp as _pyotp
        return _pyotp.TOTP(secret).now()
    except ImportError:
        return _totp(secret)


def _b64(s):
    import base64

    return base64.b64encode(s.encode()).decode()


# ---------------------------------------------------------------------------
# HTTP helper (urllib keeps this service dependency-free)
# ---------------------------------------------------------------------------
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class _HttpSession:
    """Minimal cookie-keeping HTTP client over urllib.

    The broker login chains are session-coupled: Fyers' vagator steps set
    cookies (the token-mint 308 hands back a _FYERS cookie that
    validate-authcode requires) and Flattrade's auth session does the same,
    so every step of one login must share a cookie jar — exactly what the
    proven requests.Session() flow in broker_login.py does.
    """

    _SKIP = {"path", "expires", "domain", "max-age", "secure", "httponly", "samesite"}

    def __init__(self):
        self.cookies = {}

    def _cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _absorb(self, set_cookie_headers):
        for sc in set_cookie_headers or []:
            pair = sc.split(";", 1)[0]
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            name = name.strip()
            if not name or name.lower() in self._SKIP or not value.strip():
                continue
            self.cookies[name] = value.strip()

    def _request(self, url, payload, headers, timeout, json_mode=True):
        data = json.dumps(payload).encode() if payload is not None else b""
        hdrs = {
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.cookies:
            hdrs["Cookie"] = self._cookie_header()
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                setc = r.headers.get_all("Set-Cookie")
        except urllib.error.HTTPError as e:  # keep the error body for diagnosis
            body = e.read().decode("utf-8", "replace")
            setc = e.headers.get_all("Set-Cookie") if getattr(e, "headers", None) else None
            self._absorb(setc)
            if json_mode:
                # Fyers' token-mint step answers HTTP 308 with the auth-code
                # URL in the JSON BODY — parse non-2xx bodies as JSON when
                # possible instead of hiding them behind an error wrapper.
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        parsed["_http"] = e.code
                        return parsed
                except Exception:
                    pass
                return {"_http": e.code, "_raw": body[:400], "_url": url}
            return body
        self._absorb(setc)
        if not json_mode:
            return body
        try:
            return json.loads(body)
        except Exception:
            return {"_raw": body[:400], "_url": url}

    def post(self, url, payload=None, headers=None, timeout=25):
        return self._request(url, payload, headers, timeout, json_mode=True)

    def post_text(self, url, payload=None, headers=None, timeout=25):
        return self._request(url, payload, headers, timeout, json_mode=False)


def _post_json(url, payload, headers=None, timeout=25):
    data = json.dumps(payload).encode()
    hdrs = {
        "User-Agent": _UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # keep the error body for diagnosis
        body = e.read().decode("utf-8", "replace")
        # Fyers' token-mint step answers HTTP 308 with the auth-code URL in
        # the JSON BODY (no Location header) — parse non-2xx bodies as JSON
        # whenever possible instead of hiding them behind an error wrapper.
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                parsed["_http"] = e.code
                return parsed
        except Exception:
            pass
        return {"_http": e.code, "_raw": body[:400], "_url": url}
    try:
        return json.loads(body)
    except Exception:
        return {"_raw": body[:400], "_url": url}


# ---------------------------------------------------------------------------
# broker drivers — endpoint chains proven in /home/ubuntu/broker_login.py
# ---------------------------------------------------------------------------
def fyers_login(env, log):
    """Vagator TOTP+PIN flow -> app auth_code -> access_token (API v3)."""
    import hashlib

    app_id = env.get("FYERS_CLIENT_ID", "").strip()
    pin = env.get("FYERS_PIN", "").strip()
    totp_key = (
        env.get("FYERS_TOTP_KEY") or env.get("FYERS_TOTP_SECREAT") or ""
    ).replace(" ", "").upper()
    app_secret = env.get("FYERS_SECRET_KEY", "").strip()
    fy_id = env.get("FYERS_FY_ID", "").strip() or (app_id.split("-")[0] if app_id else "")
    if not (app_id and pin and totp_key and app_secret):
        return None, "missing FYERS_CLIENT_ID / FYERS_PIN / FYERS_TOTP_KEY / FYERS_SECRET_KEY"
    if not fy_id:
        return None, "FYERS_FY_ID not set (personal login id required, not the API app id)"

    redirect = env.get("REDIRECT_URL", "").strip().strip("'\"").rstrip("?").strip()
    if not redirect:
        redirect = "http://127.0.0.1:15000/fyers/callback"
    app_type = app_id.split("-")[1] if "-" in app_id else "100"

    s = _HttpSession()
    s_headers = {"User-Agent": _UA, "Accept": "application/json"}

    log("fyers", "sending TOTP challenge to Fyers (vagator)")
    r1 = s.post(
        "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
        {"fy_id": _b64(fy_id), "app_id": "2"},
        headers=s_headers,
    )
    rk1 = r1.get("request_key")
    if not rk1:
        return None, f"send_login_otp_v2: {json.dumps(r1)[:200]}"

    rk2, last = "", {}
    for _attempt in range(2):
        # align to a fresh TOTP window — a code that rolls between send and
        # verify is rejected with -1003
        wait = 30 - (int(time.time()) % 30) + 1
        if 0 < wait < 30:
            log("fyers", "aligning to a fresh TOTP window (~%ds)" % wait)
            time.sleep(wait)
        r2 = s.post(
            "https://api-t2.fyers.in/vagator/v2/verify_otp",
            {"request_key": rk1, "otp": _totp_now(totp_key)},
            headers=s_headers,
        )
        last = r2
        rk2 = r2.get("request_key", "")
        if rk2:
            break
        if r2.get("code") == -1003:
            continue
        if r2.get("code") in (-1002, -1004):
            r1 = s.post(
                "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
                {"fy_id": _b64(fy_id), "app_id": "2"},
                headers=s_headers,
            )
            rk1 = r1.get("request_key") or rk1
    if not rk2:
        return None, f"verify_otp (TOTP rejected): {json.dumps(last)[:200]}"
    log("fyers", "TOTP accepted")

    r3 = s.post(
        "https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
        {"request_key": rk2, "identity_type": "pin", "identifier": _b64(pin)},
        headers=s_headers,
    )
    vagator = (r3.get("data") or {}).get("access_token", "")
    if not vagator:
        return None, f"verify_pin_v2 (PIN rejected): {json.dumps(r3)[:200]}"
    log("fyers", "PIN verified — minting app auth code")

    # A wrong appIdHash BURNS the auth code, so never reuse one: mint a
    # FRESH auth code for each candidate secret and validate immediately.
    # Order: the OpenAlgo instance's BROKER_API_SECRET (the app the daily
    # web login uses) first, then FYERS_SECRET_KEY (broker_creds.env).
    oa_secret = env.get("BROKER_API_SECRET", "").strip().strip("'\"")
    secrets = [s for s in dict.fromkeys([oa_secret, app_secret]) if s]
    last_err = "no secret available"
    mint_err_holder = [""]

    def _mint_code(vagator_token):
        for apt in dict.fromkeys([app_type, "100"]):
            r4 = s.post(
                "https://api.fyers.in/api/v2/token",
                {
                    "fyers_id": fy_id,
                    "app_id": app_id[:-4],
                    "redirect_uri": redirect,
                    "appType": apt,
                    "code_challenge": "",
                    "state": "abcdefg",
                    "scope": "",
                    "nonce": "",
                    "response_type": "code",
                    "create_cookie": True,
                },
                headers={"authorization": f"Bearer {vagator_token}"},
            )
            url = r4.get("Url", "") or ""
            code = ""
            if url:
                code = urllib.parse.parse_qs(
                    urllib.parse.urlparse(url).query
                ).get("auth_code", [""])[0]
            if code:
                return code
            mint_err = f"token step (appType {apt}): {json.dumps(r4)[:160]}"
            if "-348" not in mint_err and "-352" not in mint_err:
                mint_err_holder[0] = mint_err
                return None
        return None

    for i, sec in enumerate(secrets):
        label = (
            "instance BROKER_API_SECRET" if sec == oa_secret else "FYERS_SECRET_KEY"
        )
        log("fyers", f"minting auth code (attempt {i + 1}, will validate with {label})")
        auth_code = _mint_code(vagator)
        if not auth_code:
            return None, mint_err_holder[0] or "token step failed"
        csrf = hashlib.sha256(f"{app_id}:{sec}".encode()).hexdigest()
        r5 = s.post(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            {"grant_type": "authorization_code", "appIdHash": csrf, "code": auth_code},
            headers=s_headers,
        )
        access = r5.get("access_token", "")
        if access:
            log("fyers", f"access token received ({label} was the right secret)")
            return access, None
        last_err = f"validate-authcode ({label}): {json.dumps(r5)[:200]}"
        log("fyers", last_err)
    return None, last_err


def flattrade_login(env, log):
    """No-browser login: /auth/session -> /ftauth (sha256 pwd + TOTP) ->
    RedirectURL code -> /trade/apitoken session token."""
    import hashlib

    api_key = (
        env.get("FLATTRADE_API_KEY") or env.get("BROKER_API_KEY") or ""
    ).strip().split(":::")[-1]
    api_secret = (env.get("FLATTRADE_API_SECRET") or env.get("BROKER_API_SECRET") or "").strip()
    uid = (env.get("FLATTRADE_USER_ID") or env.get("BROKER_USER_ID") or "").strip()
    pwd = (env.get("FLATTRADE_PASSWORD") or env.get("BROKER_USER_PASSWORD") or "").strip()
    totp_val = (
        env.get("FLATTRADE_TOTP_KEY") or env.get("BROKER_TOTP_KEY") or ""
    ).replace(" ", "").upper()
    if not (api_key and api_secret and uid and pwd and totp_val):
        return None, "missing FLATTRADE_USER_ID / PASSWORD / TOTP_KEY / API_KEY / API_SECRET"

    s = _HttpSession()
    headers = {"User-Agent": _UA, "Referer": "https://auth.flattrade.in/"}

    log("flattrade", "opening Flattrade auth session")
    sid = (s.post_text(
        "https://authapi.flattrade.in/auth/session",
        payload={},
        headers=headers,
        timeout=30,
    ) or "").strip().strip('"')
    if not sid:
        return None, "auth/session returned no sid"

    log("flattrade", "submitting credentials + TOTP")
    r = s.post(
        "https://authapi.flattrade.in/ftauth",
        {
            "UserName": uid,
            "Rd": "",
            "Password": hashlib.sha256(pwd.encode()).hexdigest(),
            "PAN_DOB": _totp_now(totp_val),
            "App": "",
            "ClientID": "",
            "Key": "",
            "APIKey": api_key,
            "Sid": sid,
            "Override": "Y",
            "Source": "AUTHPAGE",
        },
        headers={**headers, "Origin": "https://auth.flattrade.in"},
    )
    if r.get("emsg"):
        return None, f"ftauth rejected (credentials/TOTP): {r['emsg']}"
    redirect = r.get("RedirectURL", "") or ""
    code = ""
    if redirect:
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(redirect).query
        ).get("code", [""])[0]
    if not code:
        return None, f"no request code: {json.dumps(r)[:160]}"
    log("flattrade", "login accepted — exchanging request code for session token")

    digest = hashlib.sha256(f"{api_key}{code}{api_secret}".encode()).hexdigest()
    tok = s.post(
        "https://authapi.flattrade.in/trade/apitoken",
        {"api_key": api_key, "request_code": code, "api_secret": digest},
        headers=headers,
    )
    token = tok.get("token", "")
    if not token:
        return None, f"apitoken rejected: {json.dumps(tok)[:160]}"
    log("flattrade", "session token received")
    return token, None


# ---------------------------------------------------------------------------
# in-process session wiring (replaces the standalone script's DB seeding)
# ---------------------------------------------------------------------------
def _apply_token_inprocess(broker, token, username, log):
    """Persist the fresh broker token the same way a web login would —
    upsert_auth writes/refreshes the auth row, init_broker_status + the
    smart master-contract download run, and the in-memory auth caches are
    dropped so the next API call reads the new token.

    NOTE: this runs in the job THREAD, so there is deliberately no request
    context and no session mutation here (handle_auth_success also touches
    the request session — that part belongs to the caller's own request;
    see promote_session(), which the job-poll endpoint calls). The session
    id registered here is a synthetic autologin one, tracked in
    active_sessions like any other device.
    """
    from database.auth_db import auth_cache, feed_token_cache, upsert_auth
    from database.master_contract_status_db import init_broker_status
    from utils.auth_utils import (  # noqa: F401 — import validates wiring
        should_download_master_contract,
    )

    if not username:
        return False, "could not resolve the OpenAlgo user for this request"

    inserted = upsert_auth(username, token, broker)
    if not inserted:
        return False, "upsert_auth failed (auth row not written)"
    init_broker_status(broker)
    log(broker, "auth row updated (token stored server-side, never sent to clients)")

    # Smart master-contract download on a daemon thread, exactly like
    # handle_auth_success does after a web login.
    try:
        from database.master_contract_status_db import get_last_download_time
        from utils.auth_utils import async_master_contract_download

        should_download, reason = should_download_master_contract(broker)
        log(broker, f"master contracts: download={should_download} ({reason})")
        if should_download:
            t = threading.Thread(
                target=async_master_contract_download, args=(broker,), daemon=True
            )
            t.start()
        else:
            _reload_symbol_cache(broker, username, log)
    except Exception as e:  # noqa: BLE001
        log(broker, f"master contract trigger failed (non-fatal): {e}")

    # drop in-memory auth caches so the next API call reads the fresh token
    try:
        auth_cache.pop(f"auth-{username}", None)
        feed_token_cache.pop(f"feed-{username}", None)
    except Exception as e:  # noqa: BLE001
        log(broker, f"cache clear skipped: {e}")

    return True, "ok"


def promote_session(username):
    """Promote the CALLER's web session to logged-in after a successful
    autologin. Called from the job-poll endpoint INSIDE the polling request,
    so Flask writes the updated session onto that response's cookie — the
    same mechanism as the browser login. Must be idempotent and cheap."""
    from flask import session

    from database.auth_db import get_auth_token

    if session.get("user") != username:
        return
    if get_auth_token(username) is None:
        return
    session["logged_in"] = True
    session["broker"] = _instance_broker()
    if not session.get("login_time"):
        from utils.session import set_session_login_time

        set_session_login_time()


def _instance_broker():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return "flattrade" if "flattrade" in here.lower() else "fyers"


def _reload_symbol_cache(broker, username, log):
    """POST /api/cache/reload on OUR instance with a forged admin session —
    the running app only loads its symbol cache at web-login time or via
    this endpoint (same trick the standalone script uses after seeding)."""
    try:
        from datetime import datetime

        import pytz
        from flask import current_app
        from flask.sessions import SecureCookieSessionInterface

        class _FA:
            secret_key = current_app.config.get("SECRET_KEY")
            config = {
                "SESSION_COOKIE_NAME": current_app.config.get(
                    "SESSION_COOKIE_NAME", "session"
                ),
                "SECRET_KEY_FALLBACKS": current_app.config.get(
                    "SECRET_KEY_FALLBACKS", []
                ),
            }

        s = SecureCookieSessionInterface().get_signing_serializer(_FA())
        now = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
        forged = s.dumps(
            {
                "logged_in": True,
                "login_time": now,
                "broker": broker,
                "name": username,
                "user": username,
            }
        )
        cookie_name = current_app.config.get("SESSION_COOKIE_NAME", "session")
        req = urllib.request.Request(
            "http://127.0.0.1/api/cache/reload",
            b"",
            {
                "Content-Type": "application/json",
                "Cookie": f"{cookie_name}={forged}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read()[:120]
        log(broker, f"symbol cache reload: {body.decode('utf-8', 'replace')}")
    except Exception as e:  # noqa: BLE001 — best effort
        log(broker, f"symbol cache reload failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# peer instance control (dual-broker orchestration)
# ---------------------------------------------------------------------------
def _peer_base(env):
    return (env.get("PEER_OPENALGO_URL") or "").strip().rstrip("/")


def _peer_headers(env):
    key = (env.get("PEER_OPENALGO_API_KEY") or "").strip()
    return {"X-API-KEY": key} if key else {}


def _peer_get(env, path, timeout=15):
    base = _peer_base(env)
    if not base:
        return None
    req = urllib.request.Request(
        f"{base}{path}", headers=_peer_headers(env), method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 — surfaced to the caller/log
        logger.warning(f"peer call {path} failed: {e}")
        return {"_error": str(e)}


def _peer_post(env, path, payload, timeout=20):
    base = _peer_base(env)
    if not base:
        return None
    return _post_json(
        f"{base}{path}", payload, headers=_peer_headers(env), timeout=timeout
    )


# ---------------------------------------------------------------------------
# job plumbing
# ---------------------------------------------------------------------------
def _job_step(job, step, status, detail=""):
    # Driver helpers log positionally as log(broker, message); fold those
    # into the standard (step, status, detail) shape so the UI renders them.
    if status not in ("run", "success", "error"):
        detail = detail or status
        status = "run"
    entry = {"ts": time.strftime("%H:%M:%S"), "step": step, "status": status, "detail": detail}
    job["steps"].append(entry)
    if status == "run":
        job["current_step"] = step
    logger.info(f"[autologin {job['id']}] {step}: {status} {detail}"[:220])


def _finish(job, ok, message):
    job["state"] = "success" if ok else "error"
    job["message"] = message
    job["current_step"] = None
    job["done"] = True


def _register_job() -> dict:
    global _JOB_SEQ
    with _JOBS_LOCK:
        _JOB_SEQ += 1
        job_id = f"auto-{int(time.time())}-{_JOB_SEQ}"
        job = {
            "id": job_id,
            "state": "running",
            "steps": [],
            "current_step": None,
            "message": "",
            "done": False,
            "started": time.time(),
        }
        _JOBS[job_id] = job
        # cap history
        while len(_JOBS) > _KEEP_JOBS:
            _JOBS.pop(next(iter(_JOBS)))
    return job


def get_job(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


# ---------------------------------------------------------------------------
# single-broker run (executed on the instance that owns that broker)
# ---------------------------------------------------------------------------
def _run_local_broker(job, broker, env, username, log):
    try:
        if broker == "fyers":
            opted_in = str(env.get("FYERS_TOTP_AUTOMATION", "")).strip().lower() in (
                "1", "true", "yes",
            )
            if not opted_in:
                _finish(job, False, "Fyers TOTP automation disabled (set FYERS_TOTP_AUTOMATION=1)")
                return
            token, err = fyers_login(env, log)
        else:
            token, err = flattrade_login(env, log)
        if not token:
            _job_step(job, f"{broker} login", "error", err or "broker login failed")
            _finish(job, False, err or "broker login failed")
            return

        ok, msg = _apply_token_inprocess(broker, token, username, log)
        if not ok:
            _finish(job, False, msg)
            return
        _finish(job, True, f"{broker} session established")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"autologin {broker} failed")
        _finish(job, False, f"{type(e).__name__}: {e}")


def start_local(broker, username) -> str:
    """Kick a background job that TOTP-logs THIS instance's broker.

    [username] is the OpenAlgo user whose auth row receives the token —
    resolved by the blueprint from the session cookie or the API key."""
    env = _load_all_env()
    job = _register_job()
    job["broker"] = broker
    job["scope"] = "local"

    def _log(step, status, detail=""):
        _job_step(job, step, status, detail)

    _job_step(job, f"{broker} login", "run")
    t = threading.Thread(
        target=_run_local_broker, args=(job, broker, env, username, _log), daemon=True
    )
    t.start()
    return job["id"]


# ---------------------------------------------------------------------------
# dual-broker orchestration
# ---------------------------------------------------------------------------
def _peer_broker(env):
    """The peer instance's broker, from its /autologin/status answer.
    nested=1 stops the peer from probing ITS peer back (mutual recursion)."""
    st = _peer_get(env, "/autologin/status?nested=1")
    if st and st.get("status") == "success":
        return st.get("broker")
    return None


def _probe_local_api(env):
    """Live probe through this instance's own REST API (proves the token)."""
    key = (
        env.get("OPENALGO_API_KEY")
        or env.get("OPENALGO_FYERS_API_KEY")
        or env.get("PEER_OPENALGO_API_KEY")
        or ""
    )
    if not key:
        return False, "no minted API key in env"
    payload = json.dumps({"apikey": key, "symbol": "RELIANCE", "exchange": "NSE"}).encode()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    port = 5001 if "flattrade" in here.lower() else 5000
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/quotes",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        if d.get("status") == "success":
            return True, "quote ok"
        return False, f"api rejected: {json.dumps(d)[:140]}"
    except Exception as e:
        return False, str(e)[:140]


def start_dual(username) -> str:
    """Login BOTH brokers: this instance's broker locally, the peer's via
    its /autologin/run endpoint, streaming both step logs into one job."""
    env = _load_all_env()
    job = _register_job()
    job["scope"] = "dual"
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local_broker = "flattrade" if "flattrade" in here.lower() else "fyers"
    job["broker"] = f"{local_broker}+peer"

    def _log(step, status, detail=""):
        _job_step(job, step, status, detail)

    def _runner():
        results = {}
        # --- local broker first (keeps the operator's instance fresh) ---
        _job_step(job, f"local: {local_broker}", "run")
        local_job_id = start_local(local_broker, username)
        while True:
            lj = get_job(local_job_id)
            if lj and lj["done"]:
                for s in lj["steps"]:
                    if s not in job["steps"]:
                        _job_step(job, f"[{local_broker}] {s['step']}", s["status"], s["detail"])
                results[local_broker] = lj["state"]
                break
            time.sleep(1)

        # --- peer broker over HTTP ---
        peer_broker = _peer_broker(env)
        if not peer_broker:
            _job_step(job, "peer", "error", "peer instance unreachable (PEER_OPENALGO_URL)")
            results["peer"] = "error"
        else:
            _job_step(job, f"peer ({peer_broker}): triggering login", "run")
            resp = _peer_post(env, "/autologin/run", {"broker": peer_broker})
            pjob = (resp or {}).get("job_id")
            if not pjob:
                _job_step(job, "peer", "error", f"peer refused: {json.dumps(resp)[:140]}")
                results[peer_broker] = "error"
            else:
                while True:
                    presp = _peer_get(env, f"/autologin/job/{pjob}")
                    pj = (presp or {}).get("job") or {}
                    if pj and pj.get("done"):
                        for s in pj.get("steps", []):
                            _job_step(
                                job,
                                f"[{peer_broker}] {s['step']}",
                                s["status"],
                                s["detail"],
                            )
                        results[peer_broker] = pj.get("state", "error")
                        break
                    time.sleep(1.5)

        # --- final probe ---
        ok, detail = _probe_local_api(env)
        _job_step(job, "api probe", "success" if ok else "error", detail)
        all_ok = all(v == "success" for v in results.values())
        _finish(
            job,
            all_ok,
            "dual login complete: " + ", ".join(f"{k}={v}" for k, v in results.items()),
        )

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return job["id"]


def status_snapshot(username=None, nested=False):
    """Everything the UI needs in one call: which broker is this instance,
    is the broker session live, are creds present for auto-login, and the
    peer's mirror snapshot.

    [username] is the request's resolved OpenAlgo user — API-key callers
    have no Flask session, so the broker-token check keys off the auth row
    directly instead of the session. [nested] is set when the caller is
    the OTHER OpenAlgo instance: its answer must not probe back or the two
    instances would recurse into each other until timeout."""
    env = _load_all_env()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    broker = "flattrade" if "flattrade" in here.lower() else "fyers"

    creds_ok = {
        "fyers": bool(
            env.get("FYERS_CLIENT_ID") and env.get("FYERS_PIN")
            and (env.get("FYERS_TOTP_KEY") or env.get("FYERS_TOTP_SECREAT"))
            and env.get("FYERS_SECRET_KEY")
        ),
        "flattrade": bool(
            (env.get("FLATTRADE_USER_ID") or env.get("BROKER_USER_ID"))
            and (env.get("FLATTRADE_PASSWORD") or env.get("BROKER_USER_PASSWORD"))
            and (env.get("FLATTRADE_TOTP_KEY") or env.get("BROKER_TOTP_KEY"))
            and (env.get("FLATTRADE_API_KEY") or env.get("BROKER_API_KEY"))
        ),
    }

    from flask import session as flask_session

    logged_in = bool(flask_session.get("logged_in"))
    session_broker = flask_session.get("broker")
    token_live = False
    try:
        from database.auth_db import get_auth_token

        if username:
            # API-key (or resolved) caller: the auth row is the truth.
            token_live = get_auth_token(username) is not None
            logged_in = logged_in or token_live
        elif logged_in:
            token_live = get_auth_token(flask_session.get("user")) is not None
    except Exception:
        token_live = False

    snap = {
        "status": "success",
        "broker": broker,
        "logged_in": logged_in,
        "user": username or flask_session.get("user"),
        "broker_session": {
            "logged_in": logged_in,
            "broker": session_broker,
            "token_present": token_live,
        },
        "creds_ok": creds_ok,
        "totp_supported": creds_ok[broker],
        "fyers_automation_optin": str(env.get("FYERS_TOTP_AUTOMATION", "")).strip()
        in ("1", "true", "yes"),
        "dual_supported": bool(_peer_base(env)) and creds_ok.get(broker),
    }
    peer = None if nested else _peer_get(env, "/autologin/status?nested=1")
    if peer and peer.get("status") == "success":
        snap["peer"] = {
            "broker": peer.get("broker"),
            "logged_in": (peer.get("broker_session") or {}).get("logged_in"),
            "token_present": (peer.get("broker_session") or {}).get("token_present"),
            "creds_ok": peer.get("creds_ok"),
            "totp_supported": peer.get("totp_supported"),
        }
    return snap
