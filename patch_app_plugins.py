#!/usr/bin/env python3
"""Wire /plugins dual-auth into app.py."""
import ast

PATH = "/home/ubuntu/openalgo/app.py"
src = open(PATH).read()
changed = False

# 1. before_request session hook: skip /plugins/ (auth enforced per-view)
old = '            or request.path.startswith("/assets/")  # React frontend assets'
new = old + '\n            or request.path.startswith("/plugins/")  # plugin API: per-view dual auth (API key or session)'
if 'request.path.startswith("/plugins/")' not in src:
    assert old in src, "before_request anchor not found"
    src = src.replace(old, new, 1)
    changed = True

# 2. CSRF exemption for the plugin blueprint views (next to the mobile block)
old2 = '''    except Exception as e:  # noqa: BLE001 — CSRF wiring must never break boot
        logger.warning(f"mobile CSRF exemption failed: {e}")
'''
new2 = old2 + '''    # Plugin API (scalper advisor / orderflow / brief) is called by the Flutter
    # app with an API key and by the browser with a session — the per-view
    # app_key_required decorator enforces both, so CSRF tokens are waived here.
    try:
        import blueprints.scalper_orderflow as _plugin_mod

        _plugin_views = {
            name
            for name, vf in app.view_functions.items()
            if getattr(vf, "__module__", "") == _plugin_mod.__name__
        }
        for name in _plugin_views:
            app.csrf.exempt(app.view_functions[name])
    except Exception as e:  # noqa: BLE001 — CSRF wiring must never break boot
        logger.warning(f"plugin CSRF exemption failed: {e}")
'''
if "plugin CSRF exemption" not in src:
    assert old2 in src, "csrf anchor not found"
    src = src.replace(old2, new2, 1)
    changed = True

open(PATH, "w").write(src)
ast.parse(src)
print("APP_PATCH_OK" if changed else "NO_CHANGE")
