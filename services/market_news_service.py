"""
Market News service — ported from fno-trader-pro onto OpenAlgo.

Two surfaces:
  - fetch_news: Indian + global RSS feeds, parsed with stdlib ElementTree
    (no feedparser dependency), deduped and newest-first, cached 2 min.
  - fetch_symbol_news: TradingView's news-headlines API for a symbol
    (indices, MCX commodities, equities), merged with keyword-matched RSS
    items, with provider resolution and a source breakdown.

The summarize() AI path is dropped: OpenAlgo has no LLM credentials wired in,
and keyword sentiment still gives the panel a usable Bullish/Bearish tag.
"""

import logging
import re
import time
from html import unescape
from xml.etree import ElementTree as ET

import requests

log = logging.getLogger("services.market_news")

FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Mint", "https://www.livemint.com/rss/markets"),
    ("NDTV Profit", "https://www.ndtv.com/business/rss"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Yahoo Finance India", "https://finance.yahoo.com/news/rssindex"),
    ("CNBC Markets", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_CACHE = {"ts": 0, "data": []}
_CACHE_TTL = 120
_SYMBOL_CACHE = {}


def _strip_html(raw: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", raw or ""))).strip()


def _parse_feed(name: str, url: str) -> list:
    """Minimal RSS/Atom parser on stdlib ElementTree."""
    out = []
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=8)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    # RSS 2.0: channel/item ; Atom: feed/entry
    nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for e in nodes[:30]:
        def _t(tag):
            el = e.find(tag) or e.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
            return (el.text or "").strip() if el is not None and el.text else ""
        title = unescape(_t("title")).strip()
        if not title:
            continue
        link = _t("link") or ""
        if not link:
            lnk = e.find("{http://www.w3.org/2005/Atom}link")
            if lnk is not None:
                link = lnk.get("href") or ""
        summary = _strip_html(_t("summary") or _t("description"))
        pub = _t("published") or _t("pubDate") or _t("updated")
        ts = 0
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                from datetime import datetime
                ts = int(datetime.strptime(pub.replace(" GMT", " +0000"), fmt).timestamp())
                break
            except (ValueError, TypeError):
                continue
        out.append({
            "source": name,
            "title": title,
            "link": link,
            "summary": summary[:600],
            "published": ts or int(time.time()),
        })
    return out


def fetch_news(limit=60, refresh=False) -> dict:
    """All feeds merged, deduped, newest-first (cached 2 min)."""
    if not refresh and _CACHE["data"] and time.time() - _CACHE["ts"] < _CACHE_TTL:
        return {"source": "rss", "articles": _CACHE["data"][:limit]}
    articles = []
    seen = set()
    for name, url in FEEDS:
        try:
            for a in _parse_feed(name, url):
                if a["title"].lower() in seen:
                    continue
                seen.add(a["title"].lower())
                articles.append(a)
        except Exception as ex:
            log.warning("news feed %s failed: %s", name, ex)
    articles.sort(key=lambda a: a.get("published", 0), reverse=True)
    _CACHE.update(ts=time.time(), data=articles)
    return {"source": "rss", "articles": articles[:limit]}


BULLISH_KEYWORDS = ["gain", "rise", "jump", "rally", "profit", "growth", "high", "up", "surge",
                    "record", "beat", "buy", "bull", "expansion", "positive"]
BEARISH_KEYWORDS = ["drop", "fall", "decline", "loss", "plunge", "down", "slump", "cut",
                    "sell", "bear", "miss", "negative", "warn", "risk", "debt"]


def sentiment_of(title: str, summary: str) -> tuple:
    text = (title + ". " + (summary or "")).lower()
    bull = sum(1 for k in BULLISH_KEYWORDS if k in text)
    bear = sum(1 for k in BEARISH_KEYWORDS if k in text)
    if bull > bear:
        return "Bullish"
    if bear > bull:
        return "Bearish"
    return "Neutral"


SYMBOL_EXACT_MAP = {
    "NIFTY": ["nifty", "sensex", "dalal street", "indian shares", "benchmark indices", "nse", "bse"],
    "BANKNIFTY": ["bank nifty", "banknifty", "nifty bank", "banking stocks", "banking index", "private banks", "psu banks"],
    "FINNIFTY": ["finnifty", "financial services", "financial index", "nifty financial"],
    "MIDCPNIFTY": ["midcpnifty", "midcap", "midcap nifty", "nifty midcap"],
    "SENSEX": ["sensex", "bse", "dalal street", "bse sensex"],
    "BANKEX": ["bankex", "bse bankex"],
    "CRUDEOIL": ["crude", "oil", "brent", "wti", "opec", "petroleum", "energy", "hormuz", "saudi", "barrel"],
    "NATURALGAS": ["natural gas", "natgas", "gas", "lng"],
    "GOLD": ["gold", "xau", "bullion", "precious metal", "yellow metal"],
    "SILVER": ["silver", "xag", "white metal", "bullion"],
    "COPPER": ["copper", "base metal"],
}

COMMODITY_TICKERS = {
    "CRUDEOIL": ["NYMEX:CL1!", "TVC:USOIL", "MCX:CRUDEOIL"],
    "GOLD": ["FOREXCOM:XAUUSD", "COMEX:GC1!", "MCX:GOLD"],
    "SILVER": ["FOREXCOM:XAGUSD", "COMEX:SI1!", "MCX:SILVER"],
    "NATURALGAS": ["NYMEX:NG1!", "MCX:NATURALGAS"],
    "COPPER": ["COMEX:HG1!", "MCX:COPPER"],
    "ZINC": ["MCX:ZINC"],
    "LEAD": ["MCX:LEAD"],
    "ALUMINIUM": ["MCX:ALUMINIUM"],
}

PROVIDER_MAP = {
    "reuters": ("Reuters", "dowjones_reuters"),
    "dow_jones": ("Dow Jones", "dowjones_reuters"),
    "dowjones": ("Dow Jones", "dowjones_reuters"),
    "barchart": ("Barchart", "barchart_ideas"),
    "barchart_ideas": ("Barchart Ideas", "barchart_ideas"),
    "tradingview": ("TradingView", "barchart_ideas"),
    "the_block": ("The Block", "barchart_ideas"),
    "business_standard": ("Business Standard", "indian"),
    "moneycontrol": ("Moneycontrol", "indian"),
    "economictimes": ("Economic Times", "indian"),
    "economic_times": ("Economic Times", "indian"),
    "mint": ("Mint", "indian"),
    "livemint": ("Mint", "indian"),
    "ndtvprofit": ("NDTV Profit", "indian"),
    "ndtv_profit": ("NDTV Profit", "indian"),
    "cnbc": ("CNBC", "indian"),
    "yahoo": ("Yahoo Finance", "tradingeconomics"),
    "bloomberg": ("Bloomberg", "dowjones_reuters"),
}


def _resolve_provider(it: dict) -> tuple:
    raw = (it.get("provider") or it.get("source") or "").lower().strip()
    if not raw and it.get("id"):
        id_prefix = it["id"].split(":")[0].lower()
        if id_prefix in PROVIDER_MAP:
            return PROVIDER_MAP[id_prefix]
    if raw in PROVIDER_MAP:
        return PROVIDER_MAP[raw]
    for k, v in PROVIDER_MAP.items():
        if k in raw:
            return v
    if not raw:
        return ("TradingView", "barchart_ideas")
    return (raw.capitalize(), "barchart_ideas")


def _tv_symbol_for(sym_body: str, exchange: str) -> list:
    """TradingView tickers whose headline feed covers this OpenAlgo symbol."""
    if sym_body in COMMODITY_TICKERS:
        return COMMODITY_TICKERS[sym_body]
    if "BANKNIFTY" in sym_body or "NIFTYBANK" in sym_body:
        return ["NSE:BANKNIFTY"]
    if "FINNIFTY" in sym_body:
        return ["NSE:FINNIFTY"]
    if "NIFTY" in sym_body:
        return ["NSE:NIFTY", "NSE:NIFTY50"]
    if "SENSEX" in sym_body:
        return ["BSE:SENSEX"]
    if "BANKEX" in sym_body:
        return ["BSE:BANKEX"]
    return [f"{exchange}:{sym_body}", f"NSE:{sym_body}", sym_body]


def fetch_symbol_news(symbol: str, limit: int = 50) -> dict:
    """Symbol news: TradingView headlines + keyword-matched RSS, source breakdown."""
    empty = {"symbol": "", "count": 0, "sources": [{"name": "All", "count": 0}], "items": []}
    if not symbol:
        return empty

    s_clean = symbol.strip().upper()
    exchange, sym_body = s_clean.split(":", 1) if ":" in s_clean else ("NSE", s_clean)
    sym_body = re.sub(r"-(EQ|FUT|OPT|CE|PE|INDEX)$", "", sym_body)
    sym_body = re.sub(r"\d{2}[A-Z]{3}.*$", "", sym_body)
    sym_body = re.sub(r"\d+.*$", "", sym_body)
    if not sym_body:
        sym_body = "NIFTY"

    ck = (sym_body, exchange)
    cached = _SYMBOL_CACHE.get(ck)
    if cached and time.time() - cached["ts"] < 60:
        return cached["data"]

    tv_tickers = _tv_symbol_for(sym_body, exchange)
    keywords = SYMBOL_EXACT_MAP.get(sym_body, [sym_body.lower()])

    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.tradingview.com/",
    }
    raw_items = []
    seen = set()
    for ticker in tv_tickers:
        url = f"https://news-headlines.tradingview.com/v2/headlines?client=web&symbol={ticker}&lang=en"
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                items = r.json().get("items", [])
                raw_items.extend(items)
                if len(raw_items) >= 50:
                    break
        except Exception as e:
            log.warning("TradingView news fetch error for %s: %s", ticker, e)

    filtered = []
    for it in raw_items:
        title = it.get("title", "").strip()
        if not title or title.lower() in seen:
            continue
        title_lower = title.lower()
        story_lower = it.get("storyPath", "").lower()
        rel_syms = [s.get("symbol", "").upper() for s in it.get("relatedSymbols", []) if s.get("symbol")]
        if not (any(sym_body in s for s in rel_syms)
                or any(kw in title_lower or kw in story_lower for kw in keywords)):
            continue
        seen.add(title.lower())
        pub_ts = int(it.get("published", 0) or 0)
        link = it.get("link") or (f"https://www.tradingview.com{it['storyPath']}" if it.get("storyPath") else "")
        provider_name, provider_cat = _resolve_provider(it)
        filtered.append({
            "id": it.get("id") or str(pub_ts),
            "title": title,
            "source": provider_name,
            "provider_key": re.sub(r"[^a-z0-9]", "", provider_name.lower()),
            "category": provider_cat,
            "published": pub_ts,
            "link": link or "https://www.tradingview.com/news/",
            "urgency": it.get("urgency", 2),
            "symbols": rel_syms or [tv_tickers[0]],
            "sentiment": sentiment_of(title, it.get("storyPath", "")),
        })

    # merge keyword-matched RSS items (Indian + global feeds)
    try:
        for a in fetch_news(limit=60).get("articles", []):
            a_title = a.get("title", "").strip()
            if not a_title or a_title.lower() in seen:
                continue
            full_text = f"{a_title} {a.get('summary', '')}".lower()
            if any(kw in full_text for kw in keywords):
                seen.add(a_title.lower())
                src_name = a.get("source", "Market News")
                filtered.append({
                    "id": str(a.get("published", int(time.time()))),
                    "title": a_title,
                    "source": src_name,
                    "provider_key": re.sub(r"[^a-z0-9]", "", src_name.lower()),
                    "category": "indian",
                    "published": int(a.get("published", time.time())),
                    "link": a.get("link", ""),
                    "summary": a.get("summary", "")[:500],
                    "urgency": 2,
                    "symbols": [tv_tickers[0]],
                    "sentiment": sentiment_of(a_title, a.get("summary", "")),
                })
    except Exception as e:
        log.debug("symbol RSS scan failed: %s", e)

    filtered.sort(key=lambda x: x.get("published", 0), reverse=True)
    filtered = filtered[:limit]

    source_counts = {}
    for item in filtered:
        s_name = item.get("source") or "TradingView"
        source_counts[s_name] = source_counts.get(s_name, 0) + 1
    sources_list = [{"name": "All", "count": len(filtered)}]
    sources_list += [{"name": n, "count": c} for n, c in
                     sorted(source_counts.items(), key=lambda x: -x[1])[:8]]

    data = {"symbol": symbol, "count": len(filtered), "sources": sources_list, "items": filtered}
    _SYMBOL_CACHE[ck] = {"ts": time.time(), "data": data}
    return data
