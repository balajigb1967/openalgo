"""
Shared rate limiting and 429-retry helpers for all Fyers API calls.

Fyers enforces a single global cap per API key across every REST endpoint --
order, data, quotes, depth, history, funds: 10 requests/second, 200/minute,
100000/day (see fyers-api-docs/FYERS_API_v3.md -> "Rate Limits"). Unlike Dhan,
which has independent per-endpoint-class limits (charts vs marketfeed), Fyers'
10 req/sec budget is shared process-wide, so pacing state MUST live in one
place that every module importing it sees -- not per BrokerData instance.

Services create a fresh BrokerData(auth_token) per request (see
services/option_chain_service.py, services/oi_tracker_service.py, etc.), so
any rate-limit state kept on `self` is reset away on every call and never
actually paces anything against concurrent requests. That was the root cause
of option-chain/depth bursts (many individual /data/depth calls for OI)
routinely exceeding the real 10 req/sec cap and getting HTTP 429'd.

2026-09: a second failure mode surfaced -- the per-second pacer alone still
allows 8 req/sec *sustained*, i.e. 480/min against a documented 200/min cap.
Continuous pollers (scalper depth every 3s, option-chain refreshes, mobile
quote polling) together sat above the minute budget and the account spent the
whole session in 429 storms with retry amplification (2000+ 429s logged in a
day; the retry storms queued every request thread and made the whole server
unresponsive). The limiter therefore now enforces BOTH windows: per-second
pacing AND a rolling per-minute budget. When the minute budget is exhausted
the caller sleeps until the oldest request in the window ages out -- a
self-throttling server beats a 429-retry storm.
"""

import threading
import time
from collections import deque

_lock = threading.Lock()
_last_call_time = 0.0

# Documented cap is 10 req/sec; pace at ~8 req/sec (0.125s) to leave headroom
# for clock jitter and for order/fund/margin calls sharing the same quota
# from other modules running concurrently in the same process.
MIN_INTERVAL = 0.125

# Documented cap is 200 req/min; budget 180/min (a 10% headroom). The window
# is a rolling deque of timestamps -- every caller registered here blocks
# until the oldest timestamp in the window is older than 60s.
MINUTE_BUDGET = 180
MINUTE_WINDOW = 60.0
_minute_calls: deque[float] = deque()

MAX_RETRIES = 3
BASE_BACKOFF = 1.0  # seconds; exponential fallback when no Retry-After header: 1, 2, 4

# Load-shedding caps. When the minute budget is exhausted, waiting does not
# create capacity -- it stacks request threads (each holding per-thread DB
# sessions, i.e. SQLite file handles) until the process hits its descriptor
# limit and every DB open starts failing ("unable to open database file"),
# which takes down auth and quotes for the whole app. So:
#   - market-data callers shed after SHED_AFTER seconds (raise RateLimitShed;
#     the API gateway turns it into a normal error response),
#   - order-critical callers never shed before CRITICAL_WAIT_CAP seconds --
#     a dropped order is worse than a delayed one.
SHED_AFTER = 2.0
CRITICAL_WAIT_CAP = 15.0


class RateLimitShed(Exception):
    """Raised when a non-critical caller is shed due to an exhausted budget."""

    def __init__(self, wait: float, budget: int):
        self.wait = wait
        self.budget = budget
        super().__init__(
            f"Fyers rate budget ({budget}/min) exhausted; "
            f"call shed after {wait:.1f}s wait"
        )


def retry_delay_from_headers(headers, attempt):
    """Compute how long to wait before retrying a 429.

    Fyers documents both `Retry-After` (seconds) and `X-Retry-After-Ms`
    (milliseconds) response headers on rate-limited requests -- prefer those
    over a blind exponential guess when present.
    """
    retry_after_ms = headers.get("X-Retry-After-Ms") or headers.get("x-retry-after-ms")
    if retry_after_ms:
        try:
            return max(float(retry_after_ms) / 1000.0, 0.05)
        except (TypeError, ValueError):
            pass
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return max(float(retry_after), 0.05)
        except (TypeError, ValueError):
            pass
    return BASE_BACKOFF * (2**attempt)


def apply_rate_limit(critical: bool = False):
    """Block the calling thread until it is safe to make another Fyers API call.

    Shared process-wide (module-level lock + timestamp) so every caller
    across broker.fyers.api paces against the same clock, regardless of how
    many separate BrokerData/order_api calls are in flight at once.

    Two windows are enforced:
      1. Per-second spacing (MIN_INTERVAL between any two calls).
      2. A rolling per-minute budget (MINUTE_BUDGET within any
         MINUTE_WINDOW). The sleep happens outside the lock, so a caller
         waiting on the minute budget does not serialise callers that are
         inside the budget.

    When the minute budget is exhausted the caller's required wait is capped:
    market-data callers (critical=False) raise RateLimitShed after SHED_AFTER
    seconds; order-critical callers (critical=True) wait up to
    CRITICAL_WAIT_CAP seconds, then proceed anyway -- an order is never failed
    locally, the broker's own 429/Retry-After handling remains the backstop.
    Raises must happen before the slot is reserved so shed callers consume no
    budget.
    """
    global _last_call_time

    minute_wait = 0.0
    with _lock:
        now = time.time()

        # --- rolling minute budget ---
        while _minute_calls and now - _minute_calls[0] >= MINUTE_WINDOW:
            _minute_calls.popleft()
        if len(_minute_calls) >= MINUTE_BUDGET:
            # Oldest call in the window must age past MINUTE_WINDOW before
            # budget frees up for this one.
            raw_wait = MINUTE_WINDOW - (now - _minute_calls[0])
            raw_wait = max(raw_wait, 0.05)
            if not critical and raw_wait > SHED_AFTER:
                raise RateLimitShed(raw_wait, MINUTE_BUDGET)
            # Critical callers are clamped, never shed: beyond the cap they
            # proceed over budget rather than fail an order locally.
            minute_wait = min(raw_wait, CRITICAL_WAIT_CAP)

        # --- per-second spacing (computed as of the post-minute-wait time) ---
        effective_now = now + minute_wait
        elapsed = effective_now - _last_call_time
        sleep_time = MIN_INTERVAL - elapsed if elapsed < MIN_INTERVAL else 0
        _last_call_time = effective_now + sleep_time

        # Reserve the slot now (inside the lock) so concurrent callers queue
        # behind it in order rather than all fitting "at once" later.
        _minute_calls.append(effective_now + sleep_time + minute_wait)

    if minute_wait > 0:
        # Jitter so a burst of waiters does not release in a thundering herd.
        time.sleep(minute_wait * (0.9 + 0.2 * (time.monotonic() % 1)))
    if sleep_time > 0:
        time.sleep(sleep_time)
