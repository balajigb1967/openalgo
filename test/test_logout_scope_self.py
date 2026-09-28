"""Regression tests for the scope=self logout.

A mobile logout used to call the same global /auth/logout as the web UI:
it revoked the shared broker auth token in the DB, deleted every session
row for the user and broadcast force_logout over the socket — so logging
out on the phone bounced the desktop and killed its broker session. The
scope=self mode must end ONLY the calling device's session row (its own
cookie is always cleared) and leave the auth token, the other device's
session and the socket broadcast alone.
"""

import atexit
import os
import sys
from pathlib import Path

import pytest
from flask import Flask, session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = Path(__file__).resolve().parents[1] / "tmp" / "test_logout_scope_self.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB.as_posix()}")
os.environ.setdefault("API_KEY_PEPPER", "a" * 64)
atexit.register(lambda: TEST_DB.unlink(missing_ok=True))

import blueprints.auth as auth_module  # noqa: E402
import database.auth_db as auth_db  # noqa: E402


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    auth_module.auth_bp  # touch import wiring
    app.register_blueprint(auth_module.auth_bp)

    # Keep the global path's heavy side effects observable but inert.
    auth_module.upsert_auth = lambda *a, **k: 1
    auth_module.socketio.emit = lambda *a, **k: None
    return app


def _seed(app, rows):
    """rows: list of (session_id, label). Registers sessions for balajigb."""
    with app.app_context():
        for sid, label in rows:
            auth_db.register_session(
                "balajigb", sid, device_info=label, ip_address="127.0.0.1"
            )


def _login_session(app, sid):
    """Forge a logged-in client session bound to the given session_id."""
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "balajigb"
        s["session_id"] = sid
    return client


def _session_ids(app):
    with app.app_context():
        return {s["session_id"] for s in auth_db.get_active_sessions("balajigb")}


def test_self_logout_keeps_other_devices_and_broker_token(app):
    _seed(app, [("mobile-sid", "phone"), ("desktop-sid", "browser")])
    client = _login_session(app, "mobile-sid")

    rv = client.post("/auth/logout?scope=self")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "success"
    assert body["scope"] == "self"

    # The caller's row is gone; the desktop's row survives.
    ids = _session_ids(app)
    assert "mobile-sid" not in ids
    assert "desktop-sid" in ids

    # The caller's own cookie was cleared.
    with client.session_transaction() as s:
        assert not s.get("logged_in")


def test_self_logout_does_not_revoke_auth_token(app, monkeypatch):
    _seed(app, [("mobile-sid", "phone"), ("desktop-sid", "browser")])
    client = _login_session(app, "mobile-sid")

    calls = []
    monkeypatch.setattr(
        auth_module, "upsert_auth", lambda *a, **k: calls.append(a) or 1
    )
    client.post("/auth/logout?scope=self", json={"scope": "self"})
    assert calls == []  # no revoke write on the shared broker token


def test_desktop_logout_still_revokes_everything(app, monkeypatch):
    _seed(app, [("mobile-sid", "phone"), ("desktop-sid", "browser")])
    client = _login_session(app, "desktop-sid")

    calls = []
    monkeypatch.setattr(
        auth_module, "upsert_auth", lambda *a, **k: calls.append(a) or 1
    )
    rv = client.post("/auth/logout")  # no scope → global teardown
    assert rv.status_code == 200

    assert len(calls) == 1 and calls[0][2] is True  # revoke=True
    assert _session_ids(app) == set()  # every device logged out


def test_self_logout_without_session_id_falls_back_to_global(app, monkeypatch):
    # A session without a registry row cannot scope the teardown; it must
    # behave like the historical global logout rather than silently doing
    # nothing.
    _seed(app, [("desktop-sid", "browser")])
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "balajigb"
        # no session_id set

    calls = []
    monkeypatch.setattr(
        auth_module, "upsert_auth", lambda *a, **k: calls.append(a) or 1
    )
    rv = client.post("/auth/logout?scope=self")
    assert rv.status_code == 200
    assert len(calls) == 1 and calls[0][2] is True
    assert _session_ids(app) == set()
