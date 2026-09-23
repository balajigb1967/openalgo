#!/usr/bin/env python3
"""Add dual auth (API key OR session) to the scalper_orderflow plugin blueprint."""
import ast
import re
import sys

PATH = "/home/ubuntu/openalgo/blueprints/scalper_orderflow.py"
src = open(PATH).read()

if "def app_key_required" in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

# 1. Replace the session import line with session import + our decorator code
anchor = "from utils.session import check_session_validity"
assert anchor in src, "session import anchor not found"

decorator_code = '''from utils.session import check_session_validity, is_session_valid
from flask import jsonify as _jsonify, request as _request
from functools import wraps as _wraps


def app_key_required(fn):
    """Dual auth for the mobile/Flutter app: accept a valid OpenAlgo API key
    (X-API-KEY header or ?apikey= query) OR a normal browser session.

    Plugin services self-authenticate to the broker via the instance's stored
    API key, so a valid app key is a sufficient and equivalent credential.
    """
    @_wraps(fn)
    def wrapper(*args, **kwargs):
        key = _request.headers.get("X-API-KEY") or _request.args.get("apikey")
        if key:
            try:
                from database.auth_db import get_auth_token_broker
                auth, _, _ = get_auth_token_broker(key)
                if auth:
                    return fn(*args, **kwargs)
            except Exception:
                pass
            return _jsonify({"status": "error", "message": "Invalid or revoked API key"}), 401
        if is_session_valid():
            return fn(*args, **kwargs)
        return _jsonify({"status": "error", "message": "Authentication required"}), 401
    return wrapper
'''

src = src.replace(anchor, decorator_code, 1)

# 2. Swap every route's @check_session_validity for @app_key_required
count = src.count("@check_session_validity\n")
src = src.replace("@check_session_validity\n", "@app_key_required\n")
print(f"routes_patched={count}")

open(PATH, "w").write(src)
ast.parse(src)
print("SYNTAX_OK")
