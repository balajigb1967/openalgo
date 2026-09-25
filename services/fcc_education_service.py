# FCC Academy — live education service for OpenAlgo.
#
# Teaches market concepts with the operator's OWN live data: every lesson is
# grounded in the same services the terminal trusts (quotes, history, option
# chains, depth), so "what is RSI" is answered with TODAY's RSI on the
# operator's active symbol rather than a textbook abstraction.
#
# Surfaces (blueprints/fcc_ai.py):
# - curriculum   the lesson index with quiz banks
# - lesson       explanation + live data snapshot for one lesson
# - quiz         graded attempt (records progress for signed-in users)
# - ask          grounded tutor turn through the FCC proxy
# - progress     per-user completion/score state
#
# Education only: lessons explain, they never advise. The tutor prompt and
# every explanation repeat that rule — no buy/sell calls, ever.

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EDU_STATE_PATH = os.path.join(PROJECT_ROOT, ".fcc_education.json")

_IST = timezone(timedelta(hours=5, minutes=30))

_progress_lock = threading.Lock()

# Default teaching instrument: the index everyone knows, with the richest
# option chain. Clients override with their active chart symbol.
_DEFAULT_SYMBOL = "NIFTY"


# ----------------------------------------------------------------- curriculum

def _q(question: str, options: list[str], answer: int, why: str) -> dict:
    return {"question": question, "options": options, "answer": answer, "why": why}


CURRICULUM: list[dict] = [
    {
        "id": "reading-a-quote",
        "title": "Reading a Quote",
        "subtitle": "What LTP, open, high, low and previous close actually tell you",
        "minutes": 4,
        "live": True,
        "explanation": [
            "Every trading decision starts with a quote, but a quote is not one "
            "number — it is a small story. The **LTP (Last Traded Price)** is the "
            "price of the most recent trade: it is the only number that changes "
            "tick by tick. **Open** is the first traded price of the day and often "
            "anchors how traders judge the session: above it feels strong, below "
            "it feels weak. **High** and **Low** mark the day's extreme prices — "
            "the boundaries the market has tested and respected so far. "
            "**Previous close** is yesterday's final price; the day's % change is "
            "always measured against it, not against today's open.",
            "Watch the gap: if today's open is far above the previous close, "
            "overnight news or global markets pushed it there, and the gap often "
            "acts as a magnet or a floor for the rest of the day. Volume tells "
            "you how much participation sits behind the move — a price rise on "
            "rising volume is a crowd; the same rise on thin volume is a few "
            "shouts in an empty hall.",
        ],
        "quiz": [
            _q("A stock's % change on your screen is calculated against…",
               ["Today's opening price", "Yesterday's closing price",
                "The day's low", "The last trade you saw"], 1,
               "Change is always measured against the previous close — that is "
               "what makes it comparable across days."),
            _q("LTP means…",
               ["The average price of the day", "The price of the most recent trade",
                "The best ask price", "The closing auction price"], 1,
               "Last Traded Price = the price at which the most recent trade "
                "executed. It updates on every trade."),
            _q("A rally on unusually LOW volume usually suggests…",
               ["Strong institutional buying", "Weak participation behind the move",
                "An exchange circuit breaker", "Nothing at all"], 1,
               "Volume is participation. Big moves need crowds; low-volume moves "
               "tend to fade."),
        ],
    },
    {
        "id": "candle-anatomy",
        "title": "Candle Anatomy",
        "subtitle": "Open, high, low, close — and what the body and wicks whisper",
        "minutes": 5,
        "live": True,
        "explanation": [
            "One candle compresses a time window (a day, an hour, five minutes) "
            "into four prices: **open**, **high**, **low**, **close**. The "
            "**body** spans open→close: green when close is above open (buyers "
            "won the window), red when close is below open (sellers won). The "
            "**wicks** (shadows) stretch to the high and low — the prices the "
            "market probed but abandoned.",
            "Long wicks mean rejection: a long lower wick says sellers pushed "
            "price down and buyers slammed it back — demand appeared at the lows. "
            "A long upper wick is the mirror image: buyers tried, sellers "
            "slammed the door. A small body with wicks on both sides (a doji) "
            "means balance — neither side could win. Body-to-wick ratio is the "
            "sentence structure of the tape: big body, tiny wicks = conviction; "
            "tiny body, long wicks = indecision.",
        ],
        "quiz": [
            _q("A candle closes at 100 after opening at 95. The body is…",
               ["Red, spanning 95→100", "Green, spanning 95→100",
                "Green, spanning 95→the high", "Red, spanning 95→the low"], 1,
               "Close above open = green body spanning exactly open→close "
               "(95→100). Wicks are separate."),
            _q("A very long lower wick with a small body near the top means…",
               ["Sellers dominated the whole window",
                "Price fell but buyers rejected the lows",
                "The exchange halted trading", "Volume was zero"], 1,
               "Long lower wick = the low was rejected. Someone bought "
               "aggressively down there."),
            _q("A doji (open ≈ close, long wicks both sides) signals…",
               ["Guaranteed crash", "Balance / indecision between buyers and sellers",
                "A trading halts", "That the candle is invalid"], 1,
               "Neither side won the window. In isolation it means balance — "
               "context decides what balance breaks into."),
        ],
    },
    {
        "id": "trend-and-ma",
        "title": "Trend & Moving Averages",
        "subtitle": "SMA, EMA and reading the slope instead of predicting it",
        "minutes": 6,
        "live": True,
        "explanation": [
            "A **moving average (MA)** is the mean close of the last N candles, "
            "rolled forward one candle at a time. It smooths noise so you can "
            "see the direction of travel. The **SMA** weights every day equally; "
            "the **EMA** (exponential) weights recent days more, so it turns "
            "faster. Traders watch 20 (short-term), 50 (medium) and 200 (the "
            "famous long-term line).",
            "Trend is a *description*, not a prophecy: price holding above a "
            "rising 20/50 EMA with the 20 above the 50 is an uptrend *so far*. "
            "Crosses are events, not instructions — a 'golden cross' (50 above "
            "200) arrives long after the turn has happened, because averages "
            "lag price by design. The honest reading: above a rising MA, dips "
            "toward it tend to find buyers; below a falling MA, rallies toward "
            "it tend to find sellers. That is all an MA is: a moving reference "
            "the crowd already watches.",
        ],
        "quiz": [
            _q("An EMA differs from an SMA because it…",
               ["Uses only closing prices", "Weights recent prices more heavily",
                "Ignores volume", "Only works on indexes"], 1,
               "EMA = exponential weighting: recent candles matter more, so it "
               "reacts faster (and whipsaws more) than the SMA."),
            _q("Price is far BELOW its falling 200-DMA. The disciplined reading is…",
               ["It must bounce now", "The long-term trend is down; rallies face "
                "supply until proven otherwise", "The 200-DMA is invalid",
                "Buy immediately"], 1,
               "A falling 200-DMA below price is a headwind, not a signal to "
                "buy — and never a guarantee in either direction."),
            _q("Why do moving averages 'lag' price?",
               ["They use future data", "They average past prices, so turns "
                "appear after the turn", "Brokers delay them", "They only update daily"], 1,
               "An average of the past necessarily reacts after the newest "
               "candle moves. That lag is the price of smoothness."),
        ],
    },
    {
        "id": "momentum-rsi",
        "title": "Momentum & RSI",
        "subtitle": "Measuring the speed of the move — and its limits",
        "minutes": 5,
        "live": True,
        "explanation": [
            "**RSI (Relative Strength Index)** compares the size of recent up "
            "moves to recent down moves over 14 candles and squeezes the result "
            "into 0–100. Above 70 is conventionally 'overbought', below 30 "
            "'oversold' — the move has been unusually one-sided.",
            "The classic beginner mistake is selling just because RSI crossed "
            "70. In strong trends RSI can ride above 70 for weeks while the "
            "instrument keeps rising — overbought means *strong*, not 'sell'. "
            "The signal with better odds is **divergence**: price makes a new "
            "high but RSI makes a lower high — the new high was pushed with "
            "less momentum than the last one, and momentum usually leads price "
            "at turns. Treat RSI as a speedometer: it tells you how hard the "
            "pedal is pressed, not where the road bends.",
        ],
        "quiz": [
            _q("RSI at 78 in a strong uptrend most precisely means…",
               ["Sell immediately", "The move has been unusually one-sided recently",
                "The trend has reversed", "RSI is broken"], 1,
               "Overbought = stretched, one-sided strength. Strong trends hold "
                "high RSI for long stretches."),
            _q("Price makes a new high, RSI makes a lower high. This is…",
               ["Bullish continuation", "Bearish divergence — momentum fading",
                "A data error", "Impossible"], 1,
               "New price high on weaker momentum = bearish divergence; the "
                "push is losing force."),
            _q("The standard default RSI lookback is…",
               ["7 candles", "14 candles", "50 candles", "200 candles"], 1,
               "Wilder's default is 14 — short enough to react, long enough "
               "to smooth noise."),
        ],
    },
    {
        "id": "volatility-bollinger",
        "title": "Volatility & Bollinger Bands",
        "subtitle": "Squeeze, expansion and why bands are not buy/sell lines",
        "minutes": 5,
        "live": True,
        "explanation": [
            "**Bollinger Bands** draw a 20-period SMA and place bands 2 standard "
            "deviations above and below it. Standard deviation is volatility, so "
            "when markets go quiet the bands **squeeze** together; when a move "
            "arrives they **expand** violently. The width of the bands is a "
            "direct read of how much the market is moving right now.",
            "Two rules beginners miss. First, touching a band is *not* a "
            "reversal signal — in trends, price can 'walk the band' for days, "
            "riding the upper line higher. Second, a squeeze precedes expansion "
            "but does not reveal direction: it says a big move is being loaded, "
            "not which way it fires. Bands describe the market's breathing — "
            "calm, then exertion — they do not cast spells on it.",
        ],
        "quiz": [
            _q("Bollinger Bands are built from…",
               ["A 14-period RSI and fixed % lines", "A 20-period SMA ± 2 standard deviations",
                "Yesterday's high and low", "Option open interest"], 1,
               "Middle = 20-SMA; bands = ±2 standard deviations, i.e. a "
               "statistical volatility envelope."),
            _q("A very tight squeeze between the bands suggests…",
               ["A big move is likely near, direction unknown",
                "Buy the upper band", "Sell the lower band", "The market will sleep forever"], 1,
               "Low volatility clusters before high volatility. The squeeze "
                "loads the spring — it does not aim it."),
            _q("In a strong uptrend, price repeatedly tagging the upper band means…",
               ["Instant reversal", "Trend strength — 'walking the band'",
                "The bands are mispriced", "Volume must be zero"], 1,
               "Band touches in trends are common and often continue. Bands "
                "are context, not signals."),
        ],
    },
    {
        "id": "option-basics",
        "title": "Options: Calls, Puts & Premium",
        "subtitle": "What you actually buy when you buy an option — live ATM prices",
        "minutes": 7,
        "live": True,
        "explanation": [
            "A **call option** is the right (not obligation) to BUY at the "
            "**strike** price; a **put** is the right to SELL at the strike. "
            "For that right you pay a **premium** — and the premium is where "
            "the real trading happens. Premium = **intrinsic value** (how far "
            "the option is in-the-money right now) + **time value** (the market's "
            "priced guess about how much more it could move before expiry, "
            "driven by volatility and days left).",
            "This is why options feel unfair to beginners: the underlying moves "
            "your way and the premium still falls — time value decayed faster "
            "than intrinsic value grew. ATM (at-the-money) options carry the "
            "most time value and the fastest decay; deep ITM options behave "
            "almost like the underlying; OTM options are lottery tickets that "
            "usually expire worthless. On the live chain, compare the ATM call "
            "and put premiums: the side with the richer premium is where the "
            "crowd is paying for protection or speculation.",
        ],
        "quiz": [
            _q("Buying a call option gives you…",
               ["The obligation to buy", "The right, without obligation, to buy at the strike",
                "Ownership of shares immediately", "Dividends"], 1,
               "Options are rights, not obligations. Calls = right to buy; "
                "puts = right to sell."),
            _q("Option premium = intrinsic value + …",
               ["Brokerage", "Time value", "STT", "Margin"], 1,
               "Premium splits into intrinsic (in-the-money amount now) and "
                "time value (priced possibility before expiry)."),
            _q("Which option decays fastest as expiry approaches?",
               ["Deep ITM", "ATM (at-the-money)", "Deep OTM already at ₹0.05", "All equally"], 1,
               "ATM options are pure time value — all theta, no intrinsic — "
                "so their decay is steepest into expiry."),
        ],
    },
    {
        "id": "oi-buildup",
        "title": "Open Interest & Buildup",
        "subtitle": "Reading fresh money entering and leaving positions",
        "minutes": 6,
        "live": True,
        "explanation": [
            "**Open interest (OI)** counts contracts that exist — positions "
            "opened and not yet closed. Volume counts today's trades; OI counts "
            "the stock of open positions. When price rises AND OI rises, new "
            "longs are being opened: a **long buildup**. Price falls with OI "
            "rising: **short buildup** — fresh shorts. Price rises while OI "
            "falls: **short covering** — shorts buying back, not new buyers. "
            "Price falls with OI falling: **long unwinding** — longs giving up.",
            "The option chain's biggest OI strikes act like walls: heavy call "
            "OI above price is often read as resistance (call writers betting "
            "price stays below), heavy put OI below as support. **PCR** (put-call "
            "OI ratio) above 1 means more puts than calls are open — often a "
            "contrarian tell after extremes. **Max pain** is the strike where "
            "the most option buyers lose at expiry — a gravity well writers "
            "defend into expiry week. None of these are laws; they are the "
            "footprints of where the crowd's money sits.",
        ],
        "quiz": [
            _q("Price rises 2% while OI also rises. This is…",
               ["Short covering", "Long buildup (fresh longs)", "Long unwinding", "Neutral"], 1,
               "Rising price + rising OI = new positions being opened on the "
               "long side."),
            _q("Heavy call OI at a strike above spot is conventionally read as…",
               ["Support", "Resistance (call writers defend it)", "A buy signal", "Volume"], 1,
               "Big call OI above price = writers betting price stays below "
               "it — the classic resistance read."),
            _q("PCR (OI) far above 1 after a long fall often suggests…",
               ["Bearishness is unanimous and stretched",
                "Bulls have won", "Nothing", "Exchange error"], 1,
               "Extreme put writing after a fall is crowd behaviour; extremes "
               "in sentiment often precede reversals — carefully, not surely."),
        ],
    },
    {
        "id": "risk-position-sizing",
        "title": "Risk & Position Sizing",
        "subtitle": "The one lesson that decides whether you survive to learn the rest",
        "minutes": 6,
        "live": False,
        "explanation": [
            "Every other lesson here improves your *odds*; this one decides "
            "whether you get to keep playing. **Position sizing** is choosing "
            "how much to risk on one idea. The standard method: fix a risk "
            "budget per trade (beginners: **0.5–1% of capital**), define your "
            "invalidation — the price where the idea is simply wrong — and "
            "size so that hitting it loses only the budget: "
            "**quantity = risk budget ÷ (entry − stop)**.",
            "Example: ₹5,00,000 capital, 1% = ₹5,000 risk. You buy at 1,000, "
            "stop at 980 → ₹20 per share risk → 250 shares maximum. Not 250 "
            "*because you feel confident* — 250 because that is what survives "
            "the stop. The arithmetic that ends accounts is compounding losses "
            "on oversized positions: five consecutive 1% losses are a bruise; "
            "five consecutive 20% losses are a funeral. Leverage multiplies "
            "both directions — in F&O, sizing is the difference between a "
            "strategy and a coin flip with fees.",
        ],
        "quiz": [
            _q("Capital ₹2,00,000, risk 1% per trade, stop is ₹50 away. Max loss on the stop…",
               ["₹50", "₹2,000", "₹20,000", "Whatever feels right"], 1,
               "1% of ₹2,00,000 = ₹2,000 budget. Size = 2000 ÷ 50 = 40 units; "
               "max loss = the budget, always."),
            _q("The main purpose of a stop-loss is…",
               ["Predicting the future", "Defining invalidation and capping the loss",
                "Avoiding brokerage", "Guaranteeing profit"], 1,
               "A stop is where your idea is proven wrong and the loss stops "
               "growing. It is a definition, not a prediction."),
            _q("After 5 straight losses at 1% risk each, your capital is down about…",
               ["1%", "5%", "25%", "95%"], 1,
               "5 × 1% ≈ 5% — survivable. The same trades at 20% risk each "
               "would be ruin. That is the whole point."),
        ],
    },
]

_CURRICULUM_BY_ID = {l["id"]: l for l in CURRICULUM}


# ---------------------------------------------------------------- data helpers

def _resolve_symbol(symbol: str | None, exchange: str | None) -> tuple[str, str]:
    """Normalize the teaching instrument. Anything without an explicit
    exchange falls back to the F&O exchange mapping used everywhere else."""
    from services.fcc_ai_service import _fo_exchange
    sym = (symbol or _DEFAULT_SYMBOL).strip().upper()
    exch = (exchange or "").strip().upper() or _fo_exchange(sym)
    return sym, exch


def _live_quote(symbol: str, exchange: str, api_key: str | None) -> dict:
    try:
        from services.quotes_service import get_quotes
        ok, resp, _ = get_quotes(symbol=symbol, exchange=exchange, api_key=api_key or "")
        data = resp.get("data") if ok and isinstance(resp, dict) else None
        if isinstance(data, dict) and data.get("ltp") is not None:
            return {
                "ltp": float(data.get("ltp") or 0),
                "change": float(data.get("change") or data.get("ch") or 0),
                "change_pct": float(data.get("pchg") or data.get("chp") or 0),
                "open": float(data.get("open") or 0),
                "high": float(data.get("high") or 0),
                "low": float(data.get("low") or 0),
                "prev_close": float(data.get("prev_close") or data.get("previous_close") or 0),
                "volume": data.get("volume"),
                "ts": datetime.now(_IST).strftime("%H:%M:%S IST"),
            }
    except Exception as e:
        logger.debug("education quote %s: %s", symbol, e)
    return {}


def _daily_closes(symbol: str, exchange: str, api_key: str | None, days: int = 120) -> list[float]:
    try:
        from services.history_service import get_history
        now = datetime.now()
        ok, resp, _ = get_history(
            symbol=symbol, exchange=exchange, interval="D",
            start_date=(now - timedelta(days=days + 20)).strftime("%Y-%m-%d"),
            end_date=now.strftime("%Y-%m-%d"), api_key=api_key or "")
        data = resp.get("data") if ok and isinstance(resp, dict) else None
        rows = data if isinstance(data, list) else []
        out = []
        for r in rows:
            try:
                out.append(float(r.get("close")))
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        logger.debug("education history %s: %s", symbol, e)
    return []


def _recent_candles(symbol: str, exchange: str, api_key: str | None, count: int = 8) -> list[dict]:
    try:
        from services.history_service import get_history
        now = datetime.now()
        ok, resp, _ = get_history(
            symbol=symbol, exchange=exchange, interval="D",
            start_date=(now - timedelta(days=count * 2 + 20)).strftime("%Y-%m-%d"),
            end_date=now.strftime("%Y-%m-%d"), api_key=api_key or "")
        data = resp.get("data") if ok and isinstance(resp, dict) else None
        rows = data if isinstance(data, list) else []
        out = []
        for r in rows[-count:]:
            try:
                ts = int(r.get("timestamp") or 0)
                out.append({
                    "date": datetime.fromtimestamp(ts, tz=_IST).strftime("%d %b"),
                    "open": float(r.get("open") or 0), "high": float(r.get("high") or 0),
                    "low": float(r.get("low") or 0), "close": float(r.get("close") or 0),
                })
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        logger.debug("education candles %s: %s", symbol, e)
    return []


def _chain_snapshot(symbol: str, exchange: str, ltp: float, api_key: str | None) -> dict:
    """Compact chain read for the options/OI lessons: ATM premiums, PCR,
    max pain, the top call/put OI walls and their buildup classification."""
    from services.fcc_ai_service import _classify_buildup
    out: dict = {"has_options": False}
    if ltp <= 0:
        return out
    try:
        from services.option_chain_service import get_option_chain
        from services.expiry_service import get_expiry_dates
        fo = {"NSE_INDEX": "NFO", "BSE_INDEX": "BFO"}.get(exchange, exchange)
        try:
            ok, resp, _ = get_expiry_dates(symbol=symbol, exchange=fo,
                                           instrumenttype="options", api_key=api_key or "")
            data = resp.get("data") if ok and isinstance(resp, dict) else None
            exps = (data or {}).get("expiry") or (data or {}).get("dates") or []
            exp = exps[0] if exps else None
        except Exception:
            exp = None
        if not exp:
            return out
        ok, resp, _ = get_option_chain(underlying=symbol, exchange=exchange,
                                       expiry_date=exp, strike_count=10,
                                       api_key=api_key or "")
        if not ok:
            return out
        data = resp.get("data") if isinstance(resp, dict) else resp
        chain = (data or {}).get("chain") or []
        if not chain:
            return out
        # Real payload shape: {strike, ce: {oi, oi_chg, ltp, chp...}, pe: {...}}
        rows = []
        tot_ce = tot_pe = 0.0
        for row in chain:
            ce, pe = row.get("ce") or {}, row.get("pe") or {}
            r = {
                "strike": row.get("strike"), "ce": ce, "pe": pe,
                "ce_oi": float(ce.get("oi") or 0),
                "pe_oi": float(pe.get("oi") or 0),
                "ce_chg": float(ce.get("oi_chg") or ce.get("changein_oi") or 0),
                "pe_chg": float(pe.get("oi_chg") or pe.get("changein_oi") or 0),
            }
            rows.append(r)
            tot_ce += r["ce_oi"]
            tot_pe += r["pe_oi"]
        if not rows or (tot_ce + tot_pe) <= 0:
            return out
        atm = min(rows, key=lambda r: abs(float(r["strike"] or 0) - ltp))
        out.update({
            "has_options": True,
            "expiry": str(exp),
            "atm_strike": atm["strike"],
            "atm_call_ltp": float(atm["ce"].get("ltp") or 0) or None,
            "atm_put_ltp": float(atm["pe"].get("ltp") or 0) or None,
            "pcr": round(tot_pe / tot_ce, 2) if tot_ce > 0 else None,
        })
        # Max pain: the strike where option writers lose the least.
        try:
            def pain(st: float) -> float:
                return sum(
                    r["ce_oi"] * max(0.0, st - float(r["strike"] or 0))
                    + r["pe_oi"] * max(0.0, float(r["strike"] or 0) - st)
                    for r in rows)
            strikes = [float(r["strike"] or 0) for r in rows]
            out["max_pain"] = min(strikes, key=pain)
        except Exception:
            out["max_pain"] = None

        # Top OI walls (largest standing OI) with buildup classification —
        # the same helper the terminal's own chain analytics trust.
        def _wall(side: str) -> dict | None:
            top = max(rows, key=lambda r: r[f"{side}_oi"], default=None)
            if not top or top[f"{side}_oi"] <= 0:
                return None
            leg = top[side]
            price_chg = float(leg.get("chp") or leg.get("pchg") or 0)
            return {
                "strike": top["strike"], "oi": top[f"{side}_oi"],
                "oi_chg": top[f"{side}_chg"],
                "buildup": _classify_buildup(top[f"{side}_chg"], price_chg),
            }
        out["call_wall"] = _wall("ce")
        out["put_wall"] = _wall("pe")
    except Exception as e:
        logger.debug("education chain %s: %s", symbol, e)
    return out


def _indicator_snapshot(symbol: str, exchange: str, api_key: str | None) -> dict:
    from services.fcc_ai_service import _bollinger_bands, _macd, _moving_averages, _rsi
    closes = _daily_closes(symbol, exchange, api_key)
    if not closes:
        return {}
    out: dict = {}
    out.update(_moving_averages(closes, closes[-1]))
    rsi = _rsi(closes)
    if rsi is not None:
        out["rsi14"] = round(rsi, 1)
    macd = _macd(closes)
    if macd:
        out["macd"] = macd
    bb = _bollinger_bands(closes)
    if bb:
        out["bollinger"] = bb
    return out


def _depth_snapshot(symbol: str, exchange: str, api_key: str | None) -> dict:
    try:
        from services.depth_service import get_depth
        ok, resp, _ = get_depth(symbol=symbol, exchange=exchange, api_key=api_key or "")
        d = resp.get("data") if ok and isinstance(resp, dict) else None
        if isinstance(d, dict) and (d.get("bids") or d.get("asks")):
            return {
                "top_bid": (d.get("bids") or [{}])[0].get("price"),
                "top_ask": (d.get("asks") or [{}])[0].get("price"),
                "total_buy_qty": d.get("totalbuyqty"),
                "total_sell_qty": d.get("totalsellqty"),
            }
    except Exception as e:
        logger.debug("education depth %s: %s", symbol, e)
    return {}


# Which live blocks each lesson needs. Risk lesson is arithmetic — no feed.
_LESSON_DATA = {
    "reading-a-quote": ("quote", "depth"),
    "candle-anatomy": ("quote", "candles"),
    "trend-and-ma": ("quote", "indicators"),
    "momentum-rsi": ("quote", "indicators"),
    "volatility-bollinger": ("quote", "indicators"),
    "option-basics": ("quote", "chain"),
    "oi-buildup": ("quote", "chain"),
    "risk-position-sizing": (),
}


# ------------------------------------------------------------------- surfaces

def get_curriculum() -> dict:
    return {"lessons": [
        {k: l[k] for k in ("id", "title", "subtitle", "minutes", "live")}
        for l in CURRICULUM
    ]}


def get_lesson(lesson_id: str, symbol: str | None = None,
               exchange: str | None = None, api_key: str | None = None) -> dict:
    lesson = _CURRICULUM_BY_ID.get(lesson_id)
    if not lesson:
        raise KeyError(lesson_id)
    sym, exch = _resolve_symbol(symbol, exchange)
    want = _LESSON_DATA.get(lesson_id, ())
    quote = _live_quote(sym, exch, api_key) if "quote" in want else {}
    ltp = float(quote.get("ltp") or 0)

    data: dict = {
        "symbol": sym, "exchange": exch,
        "fetched_at": datetime.now(_IST).strftime("%d %b %H:%M IST"),
    }
    if quote:
        data["quote"] = quote
    if "candles" in want:
        data["candles"] = _recent_candles(sym, exch, api_key)
    if "indicators" in want:
        data["indicators"] = _indicator_snapshot(sym, exch, api_key)
    if "chain" in want:
        data["chain"] = _chain_snapshot(sym, exch, ltp, api_key)
    if lesson_id == "reading-a-quote":
        data["depth"] = _depth_snapshot(sym, exch, api_key)

    return {
        "lesson": {k: lesson[k] for k in ("id", "title", "subtitle", "minutes", "live")},
        "data": data,
        "explanation": lesson["explanation"],
        "quiz": [{"question": q["question"], "options": q["options"]}
                 for q in lesson["quiz"]],
    }


def grade_quiz(lesson_id: str, answers: dict, user_id: str | None = None) -> dict:
    lesson = _CURRICULUM_BY_ID.get(lesson_id)
    if not lesson:
        raise KeyError(lesson_id)
    results = []
    correct = 0
    for i, q in enumerate(lesson["quiz"]):
        given = answers.get(str(i), answers.get(i))
        try:
            given_i = int(given) if given is not None else None
        except (TypeError, ValueError):
            given_i = None
        right = given_i == q["answer"]
        correct += 1 if right else 0
        results.append({
            "index": i, "question": q["question"], "given": given_i,
            "answer": q["answer"], "correct": right, "why": q["why"],
        })
    score = round(correct * 100 / len(lesson["quiz"]))
    record_progress(user_id, lesson_id, score)
    return {
        "score": score, "correct": correct, "total": len(lesson["quiz"]),
        "passed": score >= 67, "results": results,
    }


# -------------------------------------------------------------------- progress

def _load_progress() -> dict:
    try:
        if os.path.exists(_EDU_STATE_PATH):
            with open(_EDU_STATE_PATH, "r") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def record_progress(user_id: str | None, lesson_id: str, score: int) -> None:
    """Persist completion for signed-in users; API-key callers stay ephemeral
    (their key identifies the broker account, not a person)."""
    if not user_id:
        return
    with _progress_lock:
        state = _load_progress()
        user = state.setdefault(user_id, {})
        prev = user.get(lesson_id) or {}
        user[lesson_id] = {
            "completed": True,
            "best_score": max(int(score), int(prev.get("best_score") or 0)),
            "attempts": int(prev.get("attempts") or 0) + 1,
            "last_ts": time.time(),
        }
        try:
            with open(_EDU_STATE_PATH, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.debug("education progress persist failed: %s", e)


def get_progress(user_id: str | None) -> dict:
    if not user_id:
        return {"ephemeral": True, "lessons": {}, "completed": 0}
    with _progress_lock:
        state = _load_progress()
    user = state.get(user_id) or {}
    return {"ephemeral": False, "lessons": user,
            "completed": sum(1 for v in user.values() if v.get("completed"))}


# ---------------------------------------------------------------- tutor (LLM)

def _edu_llm_turn(system: str, user_text: str) -> tuple[str, str]:
    """One tutor turn through the FCC proxy: Anthropic-style first,
    OpenAI-compatible fallback — the same dual path as fcc_ai_service.chat."""
    from services.fcc_ai_service import FCC_AGENTS, _default_model, _fcc_request
    model = _default_model() or "claude-haiku-4-20250514"
    try:
        raw = _fcc_request("POST", "/v1/messages", body={
            "model": model, "max_tokens": 900, "stream": False,
            "system": system, "messages": [{"role": "user", "content": user_text}],
        })
        content = "".join(b.get("text", "") for b in (raw.get("content") or [])
                          if b.get("type") == "text")
        return content, raw.get("model") or model
    except RuntimeError:
        raw = _fcc_request("POST", "/v1/chat/completions", body={
            "model": model, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_text}],
        })
        content = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return content, raw.get("model") or model


def ask_tutor(question: str, lesson_id: str | None = None,
              symbol: str | None = None, exchange: str | None = None,
              api_key: str | None = None) -> dict:
    """Grounded tutor: teaches the question with the lesson's live snapshot
    in context. Education-only guardrails live in the system prompt."""
    from services.fcc_ai_service import FCC_ENABLED
    if not FCC_ENABLED:
        raise RuntimeError("FCC integration is disabled (FCC_ENABLED=false)")
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")

    lesson = _CURRICULUM_BY_ID.get(lesson_id) if lesson_id else None
    sym, exch = _resolve_symbol(symbol, exchange)
    blocks = [(
        "You are FCC Academy, the patient trading tutor inside OpenAlgo. You "
        "teach market concepts using the operator's OWN live data, supplied "
        "below. Hard rules: you EDUCATE, you never advise — no buy/sell "
        "suggestions, no targets, no stop-loss recommendations for the "
        "operator's positions. Use ₹ and Indian number conventions ( lakh, "
        "crore). Keep answers under 220 words, plain text, and end with one "
        "short follow-up question that checks understanding."
    )]
    if lesson:
        blocks.append(f"LESSON — {lesson['title']}:\n" + "\n".join(lesson["explanation"]))
    try:
        snap = get_lesson(lesson_id or "reading-a-quote", sym, exch, api_key)
        blocks.append("LIVE DATA (this is today's real data for "
                      f"{snap['data']['symbol']} [{snap['data']['exchange']}], "
                      f"fetched {snap['data'].get('fetched_at', '')}):\n"
                      + json.dumps(snap["data"], default=str)[:2500])
    except Exception as e:
        logger.debug("education tutor snapshot failed: %s", e)
    blocks.append(f"OPERATOR'S QUESTION: {question}")

    content, model = _edu_llm_turn("\n\n".join(blocks), question)
    return {"answer": content, "model": model,
            "lesson": lesson_id, "symbol": sym, "exchange": exch}
