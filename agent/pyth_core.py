import os
import json
import re
import sys
import datetime
import logging
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.gecko_mcp import get_simple_prices, get_market_snapshot
from tools.coinmarketcap_tool import get_cmc_prices
from tools.file_loader import load_document
from tools.web_search_tool import web_search
from tools.page_fetch import fetch_pages
from tools.memory_tool import get_personal_memory, remember_fact
from tools.mcp_tool import mcp_call, mcp_list_tools
from tools.orbio_watch import orbio_watch
from memory.pyth_memory_store import append_turn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PYTH_CORE")

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "llama3.1:8b"
FALLBACK_MODEL = "mistral7b:latest"


def _load_watchlist():
    path = ROOT / "data" / "watchlist.json"
    fallback_ids = {"btc": "bitcoin", "sol": "solana", "sgl": "x402-singularity-layer"}
    fallback_book = ["bitcoin", "ethereum", "solana"]
    extra = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tickers = raw.get("tickers") or {}
        ids = {}
        for k, v in tickers.items():
            if not isinstance(v, dict):
                continue
            if v.get("coingecko"):
                ids[k.lower()] = v["coingecko"]
                extra[v["coingecko"]] = {
                    "mint": v.get("mint"),
                    "note": v.get("note"),
                    "search": v.get("search"),
                }
            elif v.get("search"):
                extra["search:" + k.lower()] = {
                    "search": v["search"],
                    "note": v.get("note"),
                }
        book = raw.get("default_book") or fallback_book
        extra["_desks"] = raw.get("desks") or {}
        extra["_tickers"] = tickers
        return ids, book, extra
    except Exception:
        return fallback_ids, fallback_book, extra


WATCH_IDS, DEFAULT_BOOK, WATCH_EXTRA = _load_watchlist()


def _price_rows(price_blob):
    if not isinstance(price_blob, dict):
        return []
    data = (
        price_blob.get("prices")
        or price_blob.get("data")
        or price_blob.get("quotes")
        or price_blob
    )
    if not isinstance(data, dict):
        return []
    tickers = WATCH_EXTRA.get("_tickers") or {}
    cg_to_sym, cg_to_qty = {}, {}
    for row in tickers.values():
        cid = row.get("coingecko")
        if cid and row.get("symbol"):
            cg_to_sym[cid] = row["symbol"]
        if cid and row.get("qty") is not None:
            cg_to_qty[cid] = row.get("qty")
    out = []
    for cid, row in data.items():
        if not isinstance(row, dict) or "usd" not in row:
            continue
        out.append(
            {
                "cid": cid,
                "sym": cg_to_sym.get(cid, cid),
                "px": row.get("usd"),
                "chg": row.get("usd_24h_change"),
                "qty": cg_to_qty.get(cid),
            }
        )
    return out


def _price_table(price_blob):
    rows = _price_rows(price_blob)
    if not rows:
        return ""
    lines = [
        "| Asset | Qty | Price USD | 24h % | Bag USD |",
        "| ----- | --- | --------- | ----- | ------- |",
    ]
    bags = 0.0
    have_bag = False
    for r in rows:
        px, chg, qty, sym = r["px"], r["chg"], r["qty"], r["sym"]
        chg_s = f"{chg:.2f}%" if isinstance(chg, (int, float)) else "—"
        px_s = f"{px:,.6f}" if isinstance(px, float) and px < 1 else f"{px:,.2f}"
        if isinstance(qty, (int, float)) and isinstance(px, (int, float)):
            bag = qty * float(px)
            bags += bag
            have_bag = True
            bag_s = f"{bag:,.2f}"
            qty_s = (
                f"{qty:,.0f}"
                if float(qty) >= 1000 and float(qty).is_integer()
                else f"{qty:g}"
            )
        else:
            bag_s, qty_s = "—", "—"
        lines.append(f"| {sym} | {qty_s} | {px_s} | {chg_s} | {bag_s} |")
    if have_bag:
        lines.append(f"| TOTAL |  |  |  | {bags:,.2f} |")
    return "\n".join(lines)


def _price_blurb(price_blob):
    rows = _price_rows(price_blob)
    if not rows:
        return ""
    bits = []
    for r in rows:
        px, chg, sym = r["px"], r["chg"], r["sym"]
        if not isinstance(px, (int, float)):
            continue
        px_s = f"${px:,.6f}" if px < 1 else f"${px:,.2f}"
        if isinstance(chg, (int, float)):
            sign = "+" if chg >= 0 else ""
            direction = "up" if chg >= 0 else "down"
            syn = (
                f"{sym} printed {px_s} on CoinGecko MCP, {direction} "
                f"{abs(chg):.2f}% over the last 24 hours. "
                f"No other cause is in the tape."
            )
            bits.append(
                f"**{sym}**\n* Price: {px_s}\n* 24h Change: {sign}{chg:.2f}%\n* {syn}"
            )
        else:
            bits.append(f"**{sym}**\n* Price: {px_s}\n* 24h Change: —")
    return "Last 24h from CoinGecko MCP only (not news)\n\n" + "\n\n".join(bits)


def _get_detailed_tool_manifest():
    return {
        "get_simple_prices": {"ids": "list of coingecko IDs"},
        "get_market_snapshot": {},
        "get_cmc_prices": {"tokens": "list of symbols"},
        "load_document": {"path": "string"},
        "web_search": {"query": "string"},
        "fetch_pages": {"urls": "list of URLs"},
        "get_personal_memory": {"query": "string"},
        "remember_fact": {"key": "label", "value": "fact"},
        "get_time": {"zones": "optional map label->IANA zone"},
        "mcp_list_tools": {},
        "mcp_call": {"name": "tool name", "arguments": "dict"},
        "orbio_watch": {},
    }


SYSTEM_PROMPT = f"""
You are PYTH, Kora's agentic Crypto CTO.
Never invent prices, quantities, or news.
SGL is the Singularity Layer token (CoinGecko id x402-singularity-layer), not Solana or Solanium. x402 itself is Coinbase's open HTTP-402 payment protocol, a separate thing that SGL uses; never say x402 is SGL. AERO is Aerodrome. NOS is Nosana.
If TOOL_RESULT has numbers, use those numbers only.
TOOLS: {json.dumps(_get_detailed_tool_manifest())}
"""


def ask_model(messages, model=None):
    payload = {
        "model": model or MODEL_NAME,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=90)
        r.raise_for_status()
        return r.json()["message"]["content"].strip()
    except Exception as e:
        logger.error("Ollama error: %s", e)
        if (model or MODEL_NAME) != FALLBACK_MODEL:
            return ask_model(messages, FALLBACK_MODEL)
        return f"[Ollama Error: {e}]"


def execute_tool(name, args):
    args = args or {}
    try:
        if name == "get_cmc_prices":
            return get_cmc_prices(args.get("tokens") or [])
        if name == "get_simple_prices":
            return get_simple_prices(args.get("ids") or DEFAULT_BOOK)
        if name == "get_market_snapshot":
            return get_market_snapshot()
        if name == "load_document":
            return load_document(args.get("path", ""))
        if name == "web_search":
            return web_search(args.get("query", ""), num_results=int(args.get("num_results") or 5))
        if name == "fetch_pages":
            return fetch_pages(args.get("urls") or args.get("url") or [])
        if name == "get_personal_memory":
            return get_personal_memory(args.get("query", "all"))
        if name == "remember_fact":
            return remember_fact(args.get("key", "note"), args.get("value", ""))
        if name == "get_time":
            now = datetime.datetime.now().astimezone()
            utc = datetime.datetime.now(datetime.timezone.utc)
            out = {
                "utc_time": utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "local_time": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "timezone": str(now.tzinfo),
            }
            try:
                from zoneinfo import ZoneInfo
            except Exception:
                ZoneInfo = None
            if ZoneInfo:
                for label, zname in (args.get("zones") or {}).items():
                    try:
                        t = datetime.datetime.now(ZoneInfo(zname))
                        out[label] = t.strftime("%Y-%m-%d %H:%M:%S %Z")
                    except Exception as e:
                        out[label] = str(e)
            return out
        if name == "mcp_list_tools":
            return mcp_list_tools()
        if name == "mcp_call":
            return mcp_call(args.get("name") or "", args.get("arguments") or {})
        if name == "orbio_watch":
            return orbio_watch()
        return {"error": f"Tool '{name}' is not recognized."}
    except Exception as e:
        return {"error": str(e)}


def _ids_from_text(text: str):
    ids = []
    lowered = text.lower()
    ambig = {"extra", "if", "near", "pump", "cards", "ai", "on", "all", "not"}
    for key, cid in WATCH_IDS.items():
        if key in ambig and not re.search(
            rf"(\${key}\b|{key} token|{key} protocol)",
            lowered,
        ):
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", lowered):
            if cid not in ids:
                ids.append(cid)
    return ids


def _desk_ids():
    desks = WATCH_EXTRA.get("_desks") or {}
    tickers = WATCH_EXTRA.get("_tickers") or {}
    want = list(desks.get("ira") or []) + list(desks.get("stakes") or [])
    pids = []
    for sym in want:
        cid = (tickers.get(sym) or {}).get("coingecko")
        if cid and cid not in pids:
            pids.append(cid)
    return pids or list(DEFAULT_BOOK)


def plan_forced_tools(user_input: str):
    text = user_input.lower()
    planned = []

    if re.search(r"\b(orbio watch|orbio status|credit book)\b", text):
        planned.append(("orbio_watch", {}))
    if re.search(r"\b(mcp tools|list mcp)\b", text):
        planned.append(("mcp_list_tools", {}))
    if re.search(r"\b(browse)\b.+\bmcp_workspace\b", text):
        planned.append(("mcp_call", {"name": "mcp_list_dir", "arguments": {"path": "mcp_workspace"}}))

    urls = re.findall(r"https?://[^\s\]\>\)\"']+", user_input)
    if urls:
        planned.append(("fetch_pages", {"urls": urls[:3]}))

    if re.search(r"\b(tell me more|what is orbio|about orbio)\b", text):
        planned.append(("get_simple_prices", {"ids": ["orbio-so"]}))
        planned.append(("fetch_pages", {"urls": ["https://www.orbio.so/"]}))

    if re.search(r"\b(what time|what'?s the time|current time|utc time|what date)\b", text):
        zones = {}
        if re.search(r"\b(mumbai|bombay|india|ist)\b", text):
            zones["mumbai"] = "Asia/Kolkata"
        if re.search(r"\b(florida|edt|eastern)\b", text):
            zones["florida"] = "America/New_York"
        planned.append(("get_time", {"zones": zones}))

    wants_prices = bool(re.search(r"\b(price of|prices of|how much is|quote for|spot price)\b", text))
    ids = _ids_from_text(user_input)
    if wants_prices or (ids and len(user_input) < 180):
        planned.append(("get_simple_prices", {"ids": ids or DEFAULT_BOOK}))

    wants_mark = bool(
        re.search(
            r"\b((how'?s|how is)\s+my\s+portfolio|portfolio\s+(update|looking|tonight|this evening)|my\s+holdings|mark the book)\b",
            text,
        )
    )
    wants_tape = bool(
        re.search(
            r"\b(price action of each|each (portfolio )?asset|watchlist (action|tape)|24h (on|for) each)\b",
            text,
        )
    )
    if wants_mark or wants_tape:
        planned.append(("get_time", {}))
        planned.append(("get_simple_prices", {"ids": _desk_ids()}))

    if re.search(r"\b(search|look up|research|headlines)\b", text):
        planned.append(("web_search", {"query": user_input[:200], "num_results": 5}))

    return planned


def _format_mcp_reply(bundle: dict) -> str:
    listed = bundle.get("mcp_list_tools") or {}
    parts = []
    if listed.get("tools"):
        parts.append("MCP tools (local pyth-fs):")
        for t in listed["tools"]:
            name = t.get("name") if isinstance(t, dict) else str(t)
            parts.append(f"- `{name}`")
    return "\n".join(parts) if parts else "MCP returned empty."


def _stamp_reply(user_input, reply, history):
    history.append({"role": "user", "content": user_input})
    history.append({"role": "assistant", "content": reply})
    try:
        append_turn(user_input, reply)
    except Exception:
        pass
    return reply, history


def _legacy_run_agent(user_input: str, history=None):
    if history is None:
        history = []
    current_messages = (
        [{"role": "system", "content": _system_prompt()}] + _recent(history)
    )
    current_messages.append({"role": "user", "content": user_input})

    forced = plan_forced_tools(user_input)
    if forced:
        bundle = {}
        for name, args in forced:
            bundle[name] = execute_tool(name, args)
        table = _price_table(bundle.get("get_simple_prices"))
        clock = bundle.get("get_time") or {}
        stamp = ""
        if isinstance(clock, dict):
            stamp = clock.get("local_time") or clock.get("utc_time") or ""
        reportish = bool(
            re.search(
                r"\b((how'?s|how is)\s+my\s+portfolio|portfolio\s+(update|looking|tonight|this evening)|my\s+holdings|mark the book)\b",
                user_input,
                re.I,
            )
        )
        blurb_ask = bool(
            re.search(
                r"\b(price action of each|each (portfolio )?asset|watchlist (action|tape)|24h (on|for) each)\b",
                user_input,
                re.I,
            )
        )
        if table and reportish and not blurb_ask:
            return _stamp_reply(user_input, f"Portfolio update ({stamp})\n\n{table}", history)
        if set(bundle.keys()) <= {"get_time"} and bundle.get("get_time"):
            lines = [f"Local {clock.get('local_time')}", f"UTC {clock.get('utc_time')}"]
            for k, v in clock.items():
                if k not in ("local_time", "utc_time", "timezone"):
                    lines.append(f"{k} {v}")
            return _stamp_reply(user_input, "\n".join(lines), history)
        if blurb_ask and bundle.get("get_simple_prices"):
            blurb = _price_blurb(bundle.get("get_simple_prices"))
            if blurb:
                return _stamp_reply(user_input, blurb, history)
        if set(bundle) <= {"mcp_list_tools", "mcp_call"}:
            return _stamp_reply(user_input, _format_mcp_reply(bundle), history)
        extra = ("\n\nCOPY THIS TABLE VERBATIM.\n" + table) if table else ""
        current_messages.append(
            {"role": "user", "content": "TOOL_RESULT: " + json.dumps(bundle)[:12000] + extra}
        )

    reply = ask_model(current_messages)
    return _stamp_reply(user_input, reply, history)


# ---------------------------------------------------------------------------
# Compound routing layer (added 2026-10-15): weather, coin lookup, market,
# self-description, multi-part questions, no-tool price guard.
# ---------------------------------------------------------------------------
from tools.weather_tool import get_weather, format_weather

CG_REST = "https://api.coingecko.com/api/v3"


def _system_prompt():
    now = datetime.datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M %Z")
    return (SYSTEM_PROMPT + f"\nCurrent date/time: {now}.\n"
            "Never state prices, market caps or % changes without a TOOL_RESULT from this turn. "
            "If you have no live data from this turn, say plainly you'd need to check live data instead of guessing. Never mention TOOL_RESULT or internal tool names to the user.\n")


def search_coin(query):
    """Resolve a name/symbol to a CoinGecko id via /search (keyless public API)."""
    q = query.strip().lower()
    try:
        r = requests.get(f"{CG_REST}/search", params={"query": q}, timeout=15)
        r.raise_for_status()
        coins = r.json().get("coins") or []
    except Exception as e:
        return {"error": str(e)}
    if not coins:
        return None
    def rank(c):
        exact = (c.get("symbol") or "").lower() == q or (c.get("name") or "").lower() == q
        mr = c.get("market_cap_rank") or 10**9
        return (0 if exact else 1, mr)
    best = sorted(coins, key=rank)[0]
    return {"id": best["id"], "symbol": (best.get("symbol") or "").upper(), "name": best.get("name"),
            "rank": best.get("market_cap_rank")}


def lookup_coin_price(query):
    q = query.strip().lower().lstrip("$")
    cid = WATCH_IDS.get(q)
    meta = {"id": cid, "symbol": q.upper(), "name": cid} if cid else search_coin(q)
    if not meta or meta.get("error"):
        return {"error": (meta or {}).get("error") or f"no CoinGecko match for '{query}'"}
    px = get_simple_prices([meta["id"]])
    row = ((px.get("prices") or {}).get(meta["id"]) or {}) if isinstance(px, dict) else {}
    if "usd" not in row:
        try:
            r = requests.get(f"{CG_REST}/simple/price", params={"ids": meta["id"], "vs_currencies": "usd",
                             "include_24hr_change": "true", "include_market_cap": "true"}, timeout=15)
            row = r.json().get(meta["id"]) or {}
        except Exception as e:
            return {"error": str(e)}
    return {"source": "coingecko", **meta, "usd": row.get("usd"), "change_24h": row.get("usd_24h_change"),
            "mcap": row.get("usd_market_cap")}


def _fmt_px(px):
    if not isinstance(px, (int, float)):
        return "—"
    return f"${px:,.6f}" if px < 1 else f"${px:,.2f}"


def _fmt_chg(c):
    return f"{'+' if c >= 0 else ''}{c:.2f}%" if isinstance(c, (int, float)) else "—"


def format_coin(c):
    if c.get("error"):
        return f"Price lookup failed: {c['error']}"
    mc = f", mcap ${c['mcap']/1e6:,.0f}M" if isinstance(c.get("mcap"), (int, float)) else ""
    return f"**{c.get('name') or c['id']} ({c.get('symbol')})**: {_fmt_px(c.get('usd'))}, 24h {_fmt_chg(c.get('change_24h'))}{mc} (CoinGecko id `{c['id']}`)"


def market_overview():
    out = {"snapshot": get_market_snapshot(per_page=50)}
    try:
        out["global"] = requests.get(f"{CG_REST}/global", timeout=15).json().get("data") or {}
    except Exception as e:
        out["global"] = {"error": str(e)}
    try:
        out["fng"] = (requests.get("https://api.alternative.me/fng/", timeout=15).json().get("data") or [{}])[0]
    except Exception as e:
        out["fng"] = {"error": str(e)}
    return out


def format_market(m):
    lines = ["**Crypto market** (CoinGecko + alternative.me)"]
    g = m.get("global") or {}
    if g.get("total_market_cap"):
        lines.append(f"* Total mcap: ${g['total_market_cap']['usd']/1e12:,.2f}T, 24h {_fmt_chg(g.get('market_cap_change_percentage_24h_usd'))}; "
                     f"BTC dominance {g['market_cap_percentage']['btc']:.1f}%")
    rows = [r for r in ((m.get("snapshot") or {}).get("markets") or []) if isinstance(r, dict)]
    for sym in ("btc", "eth", "sol"):
        r = next((x for x in rows if x.get("symbol") == sym), None)
        if r:
            lines.append(f"* {sym.upper()}: {_fmt_px(r.get('usd'))} ({_fmt_chg(r.get('change_24h'))})")
    movers = sorted([r for r in rows if isinstance(r.get("change_24h"), (int, float))], key=lambda r: r["change_24h"])
    if movers:
        lines.append("* Top-50 best: " + ", ".join(f"{r['symbol'].upper()} {_fmt_chg(r['change_24h'])}" for r in movers[::-1][:3]))
        lines.append("* Top-50 worst: " + ", ".join(f"{r['symbol'].upper()} {_fmt_chg(r['change_24h'])}" for r in movers[:3]))
    f = m.get("fng") or {}
    if f.get("value"):
        lines.append(f"* Fear & Greed: {f['value']} ({f.get('value_classification')})")
    return "\n".join(lines) if len(lines) > 1 else "Market data unavailable right now."


def self_description():
    tools = _get_detailed_tool_manifest()
    tools.update({"get_weather": {"location": "place, default New Smyrna Beach"},
                  "lookup_coin_price": {"query": "any coin name/symbol via CoinGecko search"},
                  "market_overview": {}})
    return ("**About me:** I'm PYTH, Kora's local crypto agent. I run on Ollama "
            f"({MODEL_NAME}, fallback {FALLBACK_MODEL}) via agent/pyth_core.py: a regex router splits your "
            "message into parts, runs the matching tools, and only uses the model for parts no tool covers. "
            "Prices only come from tool results.\nTools: " + ", ".join(f"`{k}`" for k in tools))


_WEATHER_RE = re.compile(r"\b(weather|forecast|temperature|rain(ing|y)?|humid(ity)?|wind(y)?|sunny|storm)\b", re.I)
_MARKET_RE = re.compile(r"\b(crypto market|the market|markets?\s+(looking|doing|today|this)|market (sentiment|overview)|sentiment|fear (and|&) greed)\b", re.I)
_PORT_RE = re.compile(r"\b((how'?s|how is)\s+my\s+portfolio|portfolio|my\s+holdings|my bags|mark the book)\b", re.I)
_SELF_RE = re.compile(r"\b(about yourself|who are you|your structure|what tools|what can you do|your tools)\b", re.I)
_GREET_RE = re.compile(r"^\s*(hi|hello|hey|yo|gm|good (morning|evening|afternoon))\b[\s,!.]*(pyth)?[\s!.]*$", re.I)
_COIN_STOP = {"the", "a", "current", "of", "token", "coin", "today", "now", "right", "is", "my", "crypto", "market", "doing", "looking"}


def _coin_query(part):
    p = part.lower()
    m = (re.search(r"\b(?:price|prices|quote)\s+(?:of\s+|for\s+)?(?:the\s+)?(?:current\s+)?([$a-z0-9][a-z0-9\-. ]{0,30}?)(?:\s+(?:token|coin))?(?:\s*\(([a-z0-9$]+)\))?\s*(?:today|now|\?|$|,|\.)", p)
         or re.search(r"\b(?:how is|how'?s)\s+([$a-z0-9\-]+)(?:\s+(?:token|coin))?\s+doing\b", p)
         or re.search(r"\b([$a-z0-9\-]+)(?:\s+(?:token|coin))?\s+price\b", p))
    if not m:
        return None
    if m.lastindex and m.lastindex >= 2 and m.group(2):
        return m.group(2).lstrip("$")
    words = [w for w in m.group(1).lstrip("$").split() if w not in _COIN_STOP]
    if not words or words[0] in ("my", "portfolio"):
        return None
    return words[0] if len(words) == 1 else " ".join(words)


def _split_parts(text):
    parts = re.split(r"\?|\.\.+|(?<=[a-z\)])\.\s|,\s*(?:and\s+)?|\s+and\s+(?=(?:what|how|is|tell|give|also|price)\b)|\balso\b", text, flags=re.I)
    return [p.strip(" .,") for p in parts if p and len(p.strip(" .,")) > 2]


def _classify(part):
    if _SELF_RE.search(part):
        return ("self", None)
    if _WEATHER_RE.search(part):
        m = re.search(r"\b(?:in|for|at)\s+([A-Z][A-Za-z .]+?(?:,\s*[A-Z]{2})?)(?:\s+(?:today|tomorrow|now|this)\b|$)", part)
        return ("weather", m.group(1) if m else None)
    if _PORT_RE.search(part):
        return ("portfolio", None)
    if _MARKET_RE.search(part):
        return ("market", None)
    q = _coin_query(part)
    if q:
        return ("coin", q)
    return ("other", part)


_PRICEY = re.compile(r"\$\s?\d|\d+(\.\d+)?\s?%|\b\d[\d,]*\.\d+\s?(usd|dollars)\b", re.I)


def _guard(reply, tools_ran):
    if not tools_ran and _PRICEY.search(reply or ""):
        return ("I won't guess at prices or market numbers without live data, and no tool ran for that. "
                "Ask me e.g. 'price BTC', 'how's the crypto market looking', or 'how is my portfolio' and I'll pull real numbers.")
    return reply


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    if _GREET_RE.match(user_input):
        msgs = ([{"role": "system", "content": _system_prompt()}] + _recent(history))
        msgs.append({"role": "user", "content": user_input})
        return _stamp_reply(user_input, _guard(ask_model(msgs), False), history)
    plan = [(_classify(p), p) for p in _split_parts(user_input)] or [(("other", user_input), user_input)]
    kinds = {k for (k, _), _ in plan}
    if kinds <= {"other"}:
        legacy_tools = bool(plan_forced_tools(user_input))
        reply, history = _legacy_run_agent(user_input, history)
        guarded = _guard(reply, legacy_tools)
        if guarded != reply:
            history[-1]["content"] = guarded
        return guarded, history
    sections, done, others = [], set(), []
    for (kind, arg), part in plan:
        key = (kind, (arg or "").lower() if kind != "other" else part)
        if key in done:
            continue
        done.add(key)
        if kind == "self":
            sections.append(self_description())
        elif kind == "weather":
            sections.append(format_weather(get_weather(arg)))
        elif kind == "portfolio":
            stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            table = _price_table(execute_tool("get_simple_prices", {"ids": _desk_ids()}))
            sections.append(f"**Portfolio update ({stamp})**\n\n{table}" if table else "Portfolio prices unavailable.")
        elif kind == "market":
            sections.append(format_market(market_overview()))
        elif kind == "coin":
            sections.append(format_coin(lookup_coin_price(arg)))
        else:
            others.append(part)
    if others:
        msgs = [{"role": "system", "content": _system_prompt()},
                {"role": "user", "content": "Answer briefly, no prices or numbers: " + "; ".join(others)}]
        sections.append(_guard(ask_model(msgs), False))
    return _stamp_reply(user_input, "\n\n".join(sections), history)


# ---------------------------------------------------------------------------
# Round 2 overrides (2026-10-15): web knowledge route, narrower price guard,
# casual self-intro, fake-tool / fake-search text stripping.
# ---------------------------------------------------------------------------
_TECH_SELF_RE = re.compile(r"\b(your structure|what tools|your tools|how (do )?you work|architecture|tool list)\b", re.I)
_CASUAL_SELF_RE = re.compile(r"\b(about yourself|who are you|what can you do|introduce yourself)\b", re.I)
_KNOW_RE = re.compile(r"\b(what do you know about|who is|who are the|latest|news|headlines|what'?s new with)\b", re.I)
_PROPER_RE = re.compile(r"\b(what is|what are|tell me about|what can you tell me about)\s+(?:the\s+)?([A-Z][\w.\-]*|[a-z]+[A-Z]\w*)")
_ASSET_WORDS = r"(btc|bitcoin|eth|ether(eum)?|sol(ana)?|link|chainlink|pyth|jup|aero|near|nos|sgl|orbio|npro|extra|token|coin|crypto|price|market cap|mcap|24h|24-hour|usd|trading at)"
_ASSET_PRICE_RE = re.compile(rf"({_ASSET_WORDS}[^.\n]{{0,50}}(\$\s?\d|\d+(\.\d+)?\s?%))|((\$\s?\d[\d,.]*|\d+(\.\d+)?\s?%)[^.\n]{{0,50}}{_ASSET_WORDS})", re.I)
_FAKE_TOOL_RE = re.compile(r"\(?\s*(using\s+)?TOOL\s*:[^\n)]*\)?\)?|[^.\n]*\blet me (check|search|look)[^.\n]*\.{0,3}", re.I)
_FAKE_SEARCH_RE = re.compile(r"\b(here'?s what I found|I searched( the web)?|based on (my|the) (search|results)|according to (my|the) search( results)?|my search (shows|found))\b[:,]?", re.I)


def _clean(reply, tools_ran):
    reply = _FAKE_TOOL_RE.sub("", reply or "")
    if not tools_ran:
        reply = _FAKE_SEARCH_RE.sub("From my built-in knowledge (no live search was run):", reply)
    return re.sub(r"\n{3,}", "\n\n", reply).strip()


def _guard(reply, tools_ran):
    reply = _clean(reply, tools_ran)
    if not tools_ran and _ASSET_PRICE_RE.search(reply):
        return ("I won't guess at crypto prices or market numbers without live data, and no tool ran for that. "
                "Ask me e.g. 'price BTC', 'how's the crypto market looking', or 'how is my portfolio' and I'll pull real numbers.")
    return reply


def casual_intro():
    return ("I'm PYTH, Kora's crypto sidekick, running locally on this machine. I can pull live coin prices, "
            "mark your portfolio, give you a read on the crypto market, check the weather, look things up on the web, "
            "read files you point me to, and remember notes for later. What do you want to dig into?")


def web_answer(question):
    q = re.sub(r"^(what do you know about|who is|tell me about|what can you tell me about)\s+", "", question.strip(" ?"), flags=re.I)
    res = web_search(q, num_results=5)
    hits = [r for r in (res.get("results") or []) if r.get("url")]
    if not hits:
        return f"I tried a web search for '{q}' but got nothing back ({res.get('error', 'no results')}), so I won't guess."
    ctx = "\n".join(f"[{i+1}] {h['title']} — {h['snippet']} ({h['url']})" for i, h in enumerate(hits))
    pages = fetch_pages([hits[0]["url"]])
    ctx += "\n\nTOP PAGE: " + json.dumps(pages)[:3000]
    msgs = [{"role": "system", "content": _system_prompt()},
            {"role": "user", "content": f"Question: {question}\nWEB_RESULTS:\n{ctx}\n\nAnswer in 3-6 sentences using ONLY these results, "
             "cite them as [n]. If they don't answer the question, say so."}]
    ans = _clean(ask_model(msgs), True)
    return ans + "\n\nSources:\n" + "\n".join(f"[{i+1}] {h['title']} — {h['url']}" for i, h in enumerate(hits))


_classify_r1 = _classify


def _classify(part):
    if _TECH_SELF_RE.search(part):
        return ("self", None)
    if _CASUAL_SELF_RE.search(part):
        return ("intro", None)
    if _GREET_RE.match(part):
        return ("greet", part)
    k = _classify_r1(part)
    if k[0] != "other":
        return k
    if _KNOW_RE.search(part) or _PROPER_RE.search(part):
        return ("web", part)
    return k


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    if _GREET_RE.match(user_input):
        msgs = ([{"role": "system", "content": _system_prompt()}] + _recent(history))
        msgs.append({"role": "user", "content": user_input})
        return _stamp_reply(user_input, _guard(ask_model(msgs), False), history)
    plan = [(_classify(p), p) for p in _split_parts(user_input)] or [(("other", user_input), user_input)]
    kinds = {k for (k, _), _ in plan}
    if kinds & {"self", "intro"} and "intro" in kinds and "self" in kinds:
        plan = [x for x in plan if x[0][0] != "intro"]
    if kinds <= {"other", "greet"}:
        legacy_tools = bool(plan_forced_tools(user_input))
        if legacy_tools:
            reply, history = _legacy_run_agent(user_input, history)
        else:
            msgs = ([{"role": "system", "content": _system_prompt() +
                      ("Only if a question refers to something you cannot identify (e.g. 'the data plans' with no context), ask one short clarifying question "
                      "without guessing what it might be. Philosophical questions, koans and chit-chat are NOT ambiguous: just answer them thoughtfully and briefly.")}] + _recent(history))
            msgs.append({"role": "user", "content": user_input})
            reply = ask_model(msgs)
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": reply})
        guarded = _guard(reply, legacy_tools)
        history[-1]["content"] = guarded
        return guarded, history
    sections, done, others = [], set(), []
    for (kind, arg), part in plan:
        key = (kind, (arg or "").lower() if kind not in ("other", "web") else part)
        if key in done or kind == "greet":
            continue
        done.add(key)
        if kind == "self":
            sections.append(self_description())
        elif kind == "intro":
            sections.append(casual_intro())
        elif kind == "web":
            sections.append(web_answer(part))
        elif kind == "weather":
            sections.append(format_weather(get_weather(arg)))
        elif kind == "portfolio":
            stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            table = _price_table(execute_tool("get_simple_prices", {"ids": _desk_ids()}))
            sections.append(f"**Portfolio update ({stamp})**\n\n{table}" if table else "Portfolio prices unavailable.")
        elif kind == "market":
            sections.append(format_market(market_overview()))
        elif kind == "coin":
            sections.append(format_coin(lookup_coin_price(arg)))
        else:
            others.append(part)
    if others:
        msgs = [{"role": "system", "content": _system_prompt()},
                {"role": "user", "content": "Answer briefly, no prices or numbers: " + "; ".join(others)}]
        sections.append(_guard(ask_model(msgs), False))
    return _stamp_reply(user_input, "\n\n".join(sections), history)


# ---------------------------------------------------------------------------
# Round 3 (2026-10-15): file reading with hard no-hallucination rule,
# local document index (FTS5) and personal/document question routing.
# ---------------------------------------------------------------------------
from tools.file_loader import is_unreadable
from tools.doc_index import search_documents, list_documents, update_index

_manifest_r2 = _get_detailed_tool_manifest


def _get_detailed_tool_manifest():
    m = _manifest_r2()
    m.update({"search_documents": {"query": "search the user's indexed files in memory/ and data/uploads/"},
              "list_documents": {}})
    return m


_execute_tool_r2 = execute_tool


def execute_tool(name, args):
    if name == "search_documents":
        return search_documents((args or {}).get("query", ""))
    if name == "list_documents":
        return list_documents()
    return _execute_tool_r2(name, args)


_FILE_EXT = r"(pdf|jpe?g|png|webp|tiff?|docx|txt|md|json|csv|tsv)"
_FILE_RE = re.compile(rf"((?:/[^\s]+)|(?:[\w\-().]+(?: \(\d\))?\.{_FILE_EXT}))(?=\s|$|[?,!])", re.I)
_DOC_RE = re.compile(r"\b(when did i|did i|have i|was i|where did i|what did i|my (records?|letters?|reports?|notes|certificates?|statements?|forms?|contracts?|history|papers)|in my (files|docs|documents|memory|records)|my (files|docs|documents)|list (my )?(documents|files)|what does my)\b", re.I)


def _resolve_file(ref):
    p = Path(ref)
    if p.is_absolute() and p.exists():
        return p
    for d in (ROOT / "data" / "uploads", ROOT / "memory"):
        for cand in (d / ref, d / ref.replace(" ", "_"), d / ref.replace("_", " ")):
            if cand.exists():
                return cand
    return None


def _indexed_text(fp):
    """Index just this file (dedup by sha), then reuse the indexed text so a file is OCR'd at most once.
    Previously: load_document() OCR'd it, then update_index() rescanned every file and OCR'd it again."""
    from tools.doc_index import _db, index_file, file_text, embed_missing
    con = _db()
    status = index_file(con, fp)
    row = con.execute("SELECT status FROM files WHERE path=?", (str(fp),)).fetchone()
    st = (row or [""])[0] or status
    m = re.match(r"duplicate of (.+)$", st)
    src = str(fp)
    if m:
        r2 = con.execute("SELECT path FROM files WHERE path LIKE ? AND status='ok'", (f"%/{m.group(1)}",)).fetchone()
        src = r2[0] if r2 else src
    text = file_text(src) or ""
    try:
        embed_missing(con)
    except Exception:
        pass
    return text


def file_answer(user_input, fp, ref):
    try:
        text = _indexed_text(fp)
    except Exception:
        text = ""
    if not text.strip():
        text = load_document(str(fp))
    bad = is_unreadable(text)
    if bad:
        return f"I couldn't read text from {fp.name} ({bad}), so I won't summarize or guess at its contents."
    instr = user_input.replace(ref, "").replace(str(fp), "").strip() or "Summarize this file."
    msgs = [{"role": "system", "content": _system_prompt()},
            {"role": "user", "content": (
                f"FILE: {fp.name}\nEXTRACTED_TEXT (may contain OCR errors):\n<<<\n{text[:14000]}\n>>>\n\n"
                f"Request: {instr}\n\nRules: use ONLY the extracted text. Quote key facts (names, dates, places, awards) in quotes. "
                "Do not invent placeholders like [Date] or [Unit]; if a fact isn't in the text, say it isn't stated. "
                "Do not describe anyone as deceased and do not write an obituary or eulogy unless the text explicitly says the person died. "
                "Keep it to a short summary with bullet points.")}]
    ans = _clean(ask_model(msgs), True)
    note = " (text truncated to the first 14k characters)" if len(text) > 14000 else ""
    return f"{ans}\n\nSource: {fp.name}{note}"


def doc_answer(question):
    if re.search(r"\blist (my )?(documents|files)\b", question, re.I):
        docs = list_documents()
        return "Indexed documents:\n" + "\n".join(f"- {d['file']} ({d['status']})" for d in docs)
    hits = search_documents(question).get("results") or []
    if not hits:
        return "I didn't find anything relevant to that in your indexed files (memory/ and data/uploads/)."
    ctx = "\n\n".join(f"[{h['file']}]\n{h['text']}" for h in hits)
    msgs = [{"role": "system", "content": "You are PYTH, the user's local assistant, answering from his personal documents. Be accurate and brief."},
            {"role": "user", "content": (
                f"The user is the owner of this local Pyth instance; 'I'/'my' refers to them. Question: {question}\n\n"
                f"DOCUMENT EXCERPTS (OCR text, may have errors):\n{ctx[:12000]}\n\n"
                "Answer only the question asked (do not invent extra questions), in at most 5 sentences. Answer every part of the question. For dates: if no exact date is stated, give the closest time context the excerpts do state (year, deployment, operation) and say the exact date isn't in the files. "
                "Answer ONLY from these excerpts, briefly, quoting the key phrase and citing the filename in [brackets]. "
                "If the excerpts don't contain the answer, reply exactly: NOT_FOUND")}]
    ans = _clean(ask_model(msgs), True)
    if "NOT_FOUND" in ans:
        return ("I didn't find that in your indexed files. Closest files checked: "
                + ", ".join(sorted({h['file'] for h in hits})) + ".")
    return ans + "\n\nSearched: " + ", ".join(dict.fromkeys(h["file"] for h in hits))


_run_agent_r2 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    for m in _FILE_RE.finditer(user_input):
        ref = m.group(1)
        fp = _resolve_file(ref)
        if fp and fp.is_file():
            return _stamp_reply(user_input, file_answer(user_input, fp, ref), history)
        if re.search(rf"\.{_FILE_EXT}$", ref, re.I) and not ref.startswith("http"):
            return _stamp_reply(user_input, f"I couldn't find a file named {ref} in data/uploads/ or memory/.", history)
    if _DOC_RE.search(user_input) and not (_PORT_RE.search(user_input) or _WEATHER_RE.search(user_input)):
        return _stamp_reply(user_input, doc_answer(user_input), history)
    return _run_agent_r2(user_input, history)


# ---------------------------------------------------------------------------
# Round 3b: KryptoKora team briefing routing (memory/kryptokora_team.md).
# ---------------------------------------------------------------------------
TEAM_FILE = ROOT / "memory" / "kryptokora_team.md"
_TEAM_RE = re.compile(r"\b(kryptokora|krypto kora|my team|the team|grok ?bots?|henry|conrad|butch|alfred|riley|gladise|flo|ira|prime|kacey|sigel|orbi|zero|coder)\b", re.I)
_system_prompt_r3 = _system_prompt


def _system_prompt():
    return _system_prompt_r3() + ("PYTH is part of the user's KryptoKora Grok Bot team; for questions about the team or its bots, "
                                  "consult memory/kryptokora_team.md.\n")


def team_answer(question):
    text = load_document(str(TEAM_FILE)) if TEAM_FILE.exists() else ""
    if is_unreadable(text):
        return doc_answer(question)
    msgs = [{"role": "system", "content": "You are PYTH, a member of the user's KryptoKora Grok Bot team. Be accurate and brief."},
            {"role": "user", "content": f"TEAM BRIEFING (kryptokora_team.md):\n<<<\n{text[:14000]}\n>>>\n\nQuestion: {question}\n"
             "Answer ONLY from the briefing in 1-4 plain sentences: for a bot, state its role and what it does, then quote the key phrase. Only state who a bot reports to if that bot's own line literally says 'reports to' or 'reporting to'; otherwise say the briefing doesn't state its reporting line (don't infer from other lines). If it isn't in the briefing, say so."}]
    ans = _clean(ask_model(msgs), True)
    stated = {m.group(1).lower(): m.group(2) for m in re.finditer(r"^- (\w+):.*?report(?:s|ing) to (\w+)", text, re.M)}
    def _fix(m):
        bot, boss = m.group(1), m.group(2)
        if stated.get(bot.lower(), "").lower() == boss.lower():
            return m.group(0)
        if bot.lower() in stated:
            return f"{bot} reports to {stated[bot.lower()]}"
        return f"The briefing doesn't state who {bot} reports to"
    ans = re.sub(r"\b([A-Z][a-z]+) reports (?:directly )?to ([A-Z][a-z]+)", _fix, ans)
    return ans + "\n\nSource: kryptokora_team.md"


_run_agent_r3 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    if _TEAM_RE.search(user_input) and not _FILE_RE.search(user_input) and not (
            _PORT_RE.search(user_input) or _WEATHER_RE.search(user_input) or re.search(r"\bprice\b", user_input, re.I)):
        return _stamp_reply(user_input, team_answer(user_input), history)
    return _run_agent_r3(user_input, history)



# ---------------------------------------------------------------------------
# Round 4 (2026-10-15): award/document alias map, per-file grounding,
# follow-up context, conversation memory, phrase cleanup.
# ---------------------------------------------------------------------------
from tools.doc_index import file_meta

ALIASES = {
    # Example aliases: map a short topic to filename/text phrases in your own documents.
    "certificate": ["certificate", "certification"],
    "evaluation": ["evaluation report", "performance review"],
    "service record": ["service record", "certificate of release"],
    "housing": ["housing statement", "lease"],
    "reference letter": ["reference letter", "letter of recommendation", "lor"],
}


def _recent(history, turns=6):
    msgs = [{"role": m["role"], "content": m["content"]} for m in (history or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]
    return msgs[-turns * 2:]


def _norm(x):
    return re.sub(r"[^a-z0-9 ]+", " ", x.lower().replace("_", " "))


def _alias_keys(text):
    t = " " + _norm(text) + " "
    keys = []
    for k, terms in ALIASES.items():
        if any(re.search(rf"\b{re.escape(_norm(term).strip())}\b", t) for term in terms):
            keys.append(k)
    return keys


def dedicated_files(key):
    terms = [_norm(x).strip() for x in ALIASES[key]]
    meta = file_meta()
    by_name = [m for m in meta if any(tm.replace(" ", "") in _norm(m["file"]).replace(" ", "") for tm in terms)]
    if by_name:
        return by_name
    return [m for m in meta if m["chars"] < 30000 and any(tm in _norm(m["head"]) for tm in terms)]


def _clean_r4(ans):
    ans = re.sub(r"[^.\n]*\bI need to check live data[^.\n]*\.?", "", ans or "", flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", ans).strip()


def grounded_answer(question, files, history=None, key=None):
    """Answer from specific files only; each labelled as a separate document."""
    blocks, used = [], []
    for m in files[:3]:
        text = load_document(m["path"])
        if is_unreadable(text):
            continue
        if key and not any(_norm(t).strip() in _norm(text) for t in ALIASES[key]):
            continue  # file doesn't actually mention the award/document
        used.append(m)
        blocks.append(f"=== SOURCE FILE: {m['file']} (separate document) ===\n{text[:9000]}")
    if not used:
        return None, []
    ctx = "\n\n".join(blocks)
    convo = "\n".join(f"{x['role']}: {x['content'][:400]}" for x in _recent(history, 3))
    msgs = [{"role": "system", "content": "You are PYTH, a local AI interlocutor, answering from the owner's documents. Be accurate and brief."},
            {"role": "user", "content": (
                (f"RECENT CONVERSATION:\n{convo}\n\n" if convo else "") +
                f"{ctx[:14000]}\n\nQuestion: {question}\n\n"
                "Rules: answer ONLY from the source file text above, in at most 5 sentences, quoting the key phrase and citing the "
                "file name in [brackets]. Each SOURCE FILE is a separate document, possibly about a different award: never merge facts "
                "across documents. If the answer isn't in the text, say it isn't stated in that file.")}]
    ans = _clean_r4(_clean(ask_model(msgs), True))
    return ans + "\n\nSource: " + ", ".join(m["file"] for m in used), [m["path"] for m in used]


def doc_answer_r4(question, history=None):
    keys = _alias_keys(question)
    for key in keys:
        files = dedicated_files(key)
        if files:
            ans, used = grounded_answer(question, files, history, key)
            if ans:
                return ans, used
            return f"I found files named for {key.upper()} but none of them actually mention it, so I won't guess.", []
    hits = search_documents(question).get("results") or []
    if keys:  # a named award/doc with no dedicated file: only keep chunks that mention it
        terms = [_norm(t).strip() for k in keys for t in ALIASES[k]]
        hits = [h for h in hits if any(tm in _norm(h["text"]) for tm in terms)]
        if not hits:
            return f"I couldn't find a document about {', '.join(k.upper() for k in keys)} in your indexed files.", []
    if not hits:
        return "I didn't find anything relevant to that in your indexed files (memory/ and data/uploads/).", []
    order = list(dict.fromkeys(h["path"] for h in hits))[:3]
    files = [{"file": Path(p).name, "path": p} for p in order]
    ans, used = grounded_answer(question, files, history)
    return (ans or "I didn't find that in your indexed files."), used


_FOLLOW_RE = re.compile(r"^\s*(read|only|just|use|no\b|nope|wrong|that'?s (wrong|not right|incorrect)|incorrect|not that|that one|"
                        r"what about|where was i|when was (that|it)|and (where|when|why|what)|why|how so|which)|"
                        r"\b(that one|that file|only that|this file|that citation|that award|that document|for that|for it|same file)\b", re.I)


def _last_files(history):
    for m in reversed(history or []):
        if m.get("role") == "assistant" and m.get("files"):
            return m["files"]
    return []


def _stamp_files(user_input, reply, history, files):
    reply, history = _stamp_reply(user_input, reply, history)
    if files:
        history[-1]["files"] = files
    return reply, history


_run_agent_r3b = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    last = _last_files(history)
    keys = _alias_keys(user_input)
    # 'read X only' / corrections / follow-ups re-use or narrow to the last files
    if _FOLLOW_RE.search(user_input) and (last or keys) and not _FILE_RE.search(user_input):
        files = []
        for k in keys:
            files = dedicated_files(k)
            if files:
                break
        if not files:
            files = [{"file": Path(p).name, "path": p} for p in last]
        ans, used = grounded_answer(user_input, files, history, keys[0] if keys and files and not last else None) if files else (None, [])
        if ans:
            return _stamp_files(user_input, ans, history, used)
    if (_DOC_RE.search(user_input) or keys and re.search(r"\b(i|my|me)\b", user_input, re.I)) and not (
            _PORT_RE.search(user_input) or _WEATHER_RE.search(user_input) or _TEAM_RE.search(user_input)):
        ans, used = doc_answer_r4(user_input, history)
        return _stamp_files(user_input, ans, history, used)
    reply, history = _run_agent_r3b(user_input, history)
    if history and history[-1].get("role") == "assistant":
        history[-1]["content"] = _clean_r4(history[-1]["content"]) if "Source:" in history[-1]["content"] else history[-1]["content"]
        reply = history[-1]["content"]
    return reply, history


# ---------------------------------------------------------------------------
# Round 5 (2026-10-15): hybrid RAG (BM25 + nomic-embed-text, RRF), whole-doc
# chunk retrieval, conversation memory, better follow-ups.
# ---------------------------------------------------------------------------
import time as _time
from tools.doc_index import hybrid_search, file_text, add_conversation, conversation_since, LAST_TIMING


def grounded_answer(question, files, history=None, key=None):
    """Answer from specific files only, using the most relevant chunks from anywhere in each file."""
    blocks, used = [], []
    convo_q = " ".join(m["content"] for m in _recent(history, 1) if m["role"] == "user")
    q = question if len(question.split()) >= 8 else f"{convo_q} {question}"
    for m in files[:3]:
        full = file_text(m["path"])
        if not full:  # not indexed (yet): read directly
            full = load_document(m["path"]) or ""
            if is_unreadable(full):
                continue
        if key and not any(_norm(t).strip() in _norm(full) for t in ALIASES[key]):
            continue
        if len(full) <= 9000:
            body = full
        else:
            hits = hybrid_search(q, k=7, paths=[m["path"]], per_file=7)
            body = "\n...\n".join(h["text"] for h in sorted(hits, key=lambda h: h["id"]))
        used.append(m)
        blocks.append(f"=== SOURCE FILE: {m['file']} (separate document) ===\n{body[:9000]}")
    if not used:
        return None, []
    convo = "\n".join(f"{x['role']}: {x['content'][:400]}" for x in _recent(history, 3))
    msgs = [{"role": "system", "content": "You are PYTH, a local AI interlocutor, answering from the owner's documents. Be accurate and brief."},
            {"role": "user", "content": (
                (f"RECENT CONVERSATION:\n{convo}\n\n" if convo else "") +
                f"{chr(10).join(blocks)[:14000]}\n\nQuestion: {question}\n\n"
                "Rules: answer ONLY from the source file text above, in at most 5 sentences, quoting the key phrase and citing the "
                "file name in [brackets]. Each SOURCE FILE is a separate document, possibly about a different award: never merge facts "
                "across documents. If the answer isn't in the text, say it isn't stated in that file.")}]
    ans = _clean_r4(_clean(ask_model(msgs), True))
    ans = _verify_years(ans, "\n".join(blocks))
    return ans + "\n\nSource: " + ", ".join(m["file"] for m in used), [m["path"] for m in used]


def _verify_years(ans, source):
    """Drop sentences that cite a year not present in the source text (model-invented dates)."""
    src_years = set(re.findall(r"\b(?:19|20)\d\d\b", source))
    kept, dropped = [], False
    for sent in re.split(r"(?<=[.!?])\s+", ans):
        yrs = set(re.findall(r"\b(?:19|20)\d\d\b", sent))
        if yrs - src_years:
            dropped = True
            continue
        kept.append(sent)
    out = " ".join(kept).strip()
    if dropped:
        out += "\n\n(Note: I removed a date that isn't in the source text. The file doesn't state those specific dates.)"
    return out


_RECALL_RE = re.compile(r"\b(what did (i|we) (ask|talk|discuss|say)|did i (ask|mention|tell)|earlier today|last time we|we (talked|discussed|chatted)|our (last|previous|earlier) (chat|conversation)|remember when i)\b", re.I)


def recall_answer(question):
    today = datetime.datetime.now().astimezone().date().isoformat()
    if re.search(r"\b(today|earlier|this morning)\b", question, re.I):
        rows = conversation_since(today)[-40:]
        label = "today's"
    else:
        rows = [h["text"] for h in hybrid_search(question, k=10, source="conversation", per_file=1)]
        label = "matching"
    if not rows:
        return "I don't have any past conversation logged for that yet."
    if label == "today's":  # deterministic list, no model
        items = []
        for r in rows:
            m = re.match(r"\[(\S+)\] User asked: (.*)", r)
            if m and not _RECALL_RE.search(m.group(2)):
                items.append(f"- {m.group(1)[11:16]} ET — {m.group(2).strip()}")
        return "Earlier today you asked me (past conversation log):\n" + "\n".join(items[-25:])
    ctx = "\n\n".join(rows)[-9000:]
    msgs = [{"role": "system", "content": "You are PYTH. Summarize the user's PAST CONVERSATION log accurately. These are past chats, not documents."},
            {"role": "user", "content": f"PAST CONVERSATION ({label} turns, oldest first):\n{ctx}\n\nQuestion: {question}\n"
             "Answer briefly as a short bullet list of the topics/questions the user asked, with times. Don't invent any."}]
    return _clean_r4(_clean(ask_model(msgs), True)) + "\n\n(Source: past conversation log)"


_SHORT_FU_RE = re.compile(r"^\s*(and\b|what about|when\b|where\b|dates?\b|which\b|why\b|how long|who\b)", re.I)


def _is_followup(text, history):
    if not _last_files(history):
        return False
    if _GREET_RE.match(text) or _FILE_RE.search(text) or _TEAM_RE.search(text) or _RECALL_RE.search(text):
        return False
    if _classify(text)[0] not in ("other", "web"):
        return False
    if _alias_keys(text):
        return False  # names a (possibly new) award/doc: let doc routing pick its file
    if len(text.split()) < 8:
        return True
    return bool(_SHORT_FU_RE.match(text)) and bool(re.search(r"\b(it|that one|this one|those|then|there|them|the dates?|same)\b", text, re.I))


_run_agent_r4 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    t0 = _time.time()
    LAST_TIMING.clear()
    if _RECALL_RE.search(user_input):
        reply, history = _stamp_reply(user_input, recall_answer(user_input), history)
    elif _is_followup(user_input, history):
        files = [{"file": Path(p).name, "path": p} for p in _last_files(history)]
        ans, used = grounded_answer(user_input, files, history)
        if ans and not re.search(r"\b(not (mentioned|stated|provided|found)|isn'?t (stated|mentioned)|does not (provide|mention|contain))\b", ans, re.I):
            reply, history = _stamp_files(user_input, ans, history, used)
        else:
            reply, history = _run_agent_r4(user_input, history)
    else:
        reply, history = _run_agent_r4(user_input, history)
    try:
        add_conversation(user_input, reply)
    except Exception as e:
        logger.warning("conversation log failed: %s", e)
    logger.info("turn %.1fs retrieval=%s", _time.time() - t0, dict(LAST_TIMING))
    if history and history[-1].get("role") == "assistant":
        history[-1]["timing"] = {"total_s": round(_time.time() - t0, 1), **LAST_TIMING}
    return reply, history


# ---------------------------------------------------------------------------
# Round 6 (2026-10-15): live-tool priority over doc follow-ups, tides,
# weather periods, identity file, awards roll-up, SOURCE FILE tag stripping.
# ---------------------------------------------------------------------------
import subprocess as _sp
from tools.tide_tool import get_tides, format_tides
from tools.weather_tool import get_weather_period, format_weather_period

IDENTITY_FILE = ROOT / "memory" / "pyth_identity.md"
_TIDE_RE = re.compile(r"\b(tides?|high tide|low tide)\b", re.I)
_TIME_RE = re.compile(r"\b(what time|what'?s the time|current time|what date|today'?s date)\b", re.I)
_IDENT_RE = re.compile(r"\b(who you are|who are you|why you exist|why do you exist|(your |primary |main )(role|purpose)|role and purpose|about yourself|"
                       r"your structure|what tools|your tools|how (do )?you work|what can you do|introduce yourself)\b", re.I)
_AWARDS_RE = re.compile(r"\b(my|all my|list my|summari[sz]e my)\b[^.?]*\b(awards|medals|decorations)\b", re.I)
_FORMAT_ONLY_RE = re.compile(r"^(give me|write|i want|please)?\s*(an?\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten)?[- ]?(paragraphs?|verbose|detailed|long)\s*(summary|answer|response|essay)?\s*$", re.I)
_NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _length_spec(text):
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ]paragraphs?\b", text, re.I)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMW[m.group(1).lower()]
        return f"exactly {n} paragraphs, each 4-6 sentences"
    if re.search(r"\b(verbose|detailed|in depth|long)\b", text, re.I):
        return "4-6 detailed paragraphs"
    return None


def _strip_tags(s):
    s = re.sub(r"=+\s*SOURCE FILE:[^=\n]*=+", "", s or "")
    s = re.sub(r"\[\s*SOURCE FILE:\s*([^\]]+?)(\s*\(separate document\))?\s*\]", "", s)
    s = re.sub(r"\(separate document\)", "", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def identity_answer(question, length=None, technical=False):
    ident = IDENTITY_FILE.read_text(encoding="utf-8") if IDENTITY_FILE.exists() else ""
    tools = list(_get_detailed_tool_manifest()) + ["get_weather", "get_tides", "lookup_coin_price", "market_overview"]
    msgs = [{"role": "system", "content": "You are PYTH describing yourself. Use ONLY the IDENTITY notes and TOOL LIST. "
             "You are NOT the user; never describe the user's military career, awards or life as your own."},
            {"role": "user", "content": f"IDENTITY:\n{ident}\n\nTOOL LIST: {', '.join(tools)}\n\nRequest: {question}\n\n"
             f"Write in first person, plain friendly prose. Length: {length or ('2-3 short paragraphs' if technical else '3-4 sentences')}."
             + (" Include how you are structured and the tools you use." if technical else "")}]
    return _clean_r4(_clean(ask_model(msgs), True))


def _legacy_award_pages():
    """Layout text of the legacy PDF pages holding the decorations table."""
    path = ROOT / "memory" / os.environ.get("PYTH_AWARDS_PDF", "awards.pdf")
    if not path.exists():
        return ""
    try:
        txt = _sp.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""
    pages = txt.split("\f")
    hits = [i for i, p in enumerate(pages) if "Citing Authority" in p or "Badges and Tabs" in p]
    return "\n".join(pages[i] for i in sorted(set(hits)))[:7000]


def awards_answer(question):
    blocks, sources = [], []
    table = _legacy_award_pages()
    if table:
        blocks.append(f"DECORATIONS TABLE ({os.environ.get('PYTH_AWARDS_PDF', 'awards.pdf')}):\n" + table)
        sources.append(f"{os.environ.get('PYTH_AWARDS_PDF', 'awards.pdf')} (decorations)")
    for key in ("certificate", "evaluation", "service record"):
        for m in dedicated_files(key)[:1]:
            t = file_text(m["path"]) or ""
            if key == "service record":
                i = t.upper().find("DECORATIONS")
                t = t[max(0, i - 100): i + 1200] if i >= 0 else ""
            else:
                t = t[:1500]
            if t.strip() and m["file"] not in [s.split(" ")[0] for s in sources]:
                blocks.append(f"CITATION/RECORD ({m['file']}):\n{t}")
                sources.append(m["file"])
    if not blocks:
        return "I couldn't find award records in your indexed files."
    msgs = [{"role": "system", "content": "You are PYTH, the user's local assistant. Be accurate; use only the provided records."},
            {"role": "user", "content": "\n\n".join(blocks)[:13000] + f"\n\nRequest: {question}\n\n"
             "Make ONE deduplicated list of the user's military awards, decorations and badges in chronological order (oldest first). "
             "Format each line as: '- <date or period> — <award> — <short reason/where, if stated> (source: <file name>)'. "
             "Use dates exactly as written in the records; if no date is given write 'date not stated' and put those at the end. "
             "Do not invent awards, dates or reasons."}]
    ans = _strip_tags(_clean_r4(_clean(ask_model(msgs), True)))
    ans = _verify_years(ans, "\n".join(blocks))
    seen, lines = set(), []
    for ln in ans.splitlines():
        m = re.match(r"\s*-\s*(?:[^—]*—\s*)?([^—(]+)", ln)
        name = re.sub(r"\W+", " ", (m.group(1) if m else "")).strip().lower()
        if ln.strip().startswith("-") and name:
            if name in seen:
                continue
            seen.add(name)
        lines.append(ln)
    ans = "\n".join(l for l in lines if not re.search(r"date .?not stated.? is not included|no awards with unstated", l, re.I))
    return "**Your military awards (chronological)**\n" + ans + "\n\nSources: " + "; ".join(sources)


def _split_r6(text):
    parts = _split_parts(re.sub(r"\b(then|and then)\b", ",", text, flags=re.I))
    out = []
    for p in parts:
        if _FORMAT_ONLY_RE.match(p) or re.fullmatch(r"(and|also|so|ok)", p.strip(), re.I):
            continue
        out.append(p)
    return out


def _classify_r6(part):
    if _TIDE_RE.search(part):
        return "tide"
    if _AWARDS_RE.search(part):
        return "awards"
    if _IDENT_RE.search(part):
        return "tech" if _TECH_SELF_RE.search(part) else "ident"
    if _TIME_RE.search(part):
        return "time"
    k = _classify(part)[0]
    return k


_LIVE = {"weather", "tide", "coin", "market", "portfolio", "time", "ident", "tech", "awards"}


def _weather_period(text):
    t = text.lower()
    for p in ("tonight", "tomorrow", "afternoon"):
        if p in t:
            return p
    if re.search(r"\b(this evening|later today)\b", t):
        return "tonight" if "evening" in t else "afternoon"
    return None


_run_agent_r5 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    parts = _split_r6(user_input) or [user_input]
    kinds = [_classify_r6(p) for p in parts]
    if not (set(kinds) & _LIVE) or _GREET_RE.match(user_input) or _FILE_RE.search(user_input):
        reply, history = _run_agent_r5(user_input, history)
        reply = _strip_tags(reply)
        if history and history[-1].get("role") == "assistant":
            history[-1]["content"] = reply
        return reply, history
    t0 = _time.time()
    length = _length_spec(user_input)
    sections, files_used, done = [], [], set()
    ident_parts = [p for p, k in zip(parts, kinds) if k in ("ident", "tech")]
    for part, kind in zip(parts, kinds):
        if kind in ("ident", "tech"):
            if "ident" in done:
                continue
            done.add("ident")
            sections.append(identity_answer("; ".join(ident_parts), length, technical="tech" in kinds))
        elif kind == "awards":
            sections.append(awards_answer(part))
        elif kind == "tide":
            if "tide" in done:
                continue
            done.add("tide")
            sections.append(format_tides(get_tides(), "tomorrow" if "tomorrow" in part.lower() else None))
        elif kind == "weather":
            arg = _classify(part)[1]
            per = _weather_period(part)
            sections.append(format_weather_period(get_weather_period(arg, per)) if per else format_weather(get_weather(arg)))
        elif kind == "time":
            c = execute_tool("get_time", {})
            sections.append(f"Local time: {c.get('local_time')}")
        elif kind == "market":
            if "market" in done:
                continue
            done.add("market")
            sections.append(format_market(market_overview()))
        elif kind in ("coin", "portfolio"):
            r, _h = _run_agent_r2(part, [])
            sections.append(r)
        elif _DOC_RE.search(part) or _alias_keys(part):
            ans, used = doc_answer_r4(part, history)
            sections.append(ans); files_used += used
        elif kind == "web":
            sections.append(web_answer(part))
        elif kind == "greet":
            continue
        else:
            msgs = [{"role": "system", "content": _system_prompt()}] + _recent(history) + [{"role": "user", "content": part}]
            sections.append(_guard(ask_model(msgs), False))
    reply = _strip_tags("\n\n".join(s for s in sections if s))
    reply, history = _stamp_reply(user_input, reply, history)
    history[-1]["tool_context"] = reply[:6000]
    if files_used:
        history[-1]["files"] = files_used
    try:
        add_conversation(user_input, reply)
    except Exception:
        pass
    logger.info("r6 turn %.1fs kinds=%s", _time.time() - t0, kinds)
    return reply, history



# ---------------------------------------------------------------------------
# Round 7 (2026-10-15): opinion follow-ups on tool results, market routing,
# fake tool-text stripping, team-bot contact stub, identity-based prompt.
# ---------------------------------------------------------------------------
_IDENT_SHORT = ("You are PYTH, the user's (operator 'Kora') locally owned, bespoke AI interlocutor and advisor, running privately "
                "on his machine via Ollama, the independent local advisor on his KryptoKora team. Mottos: 'Ex nihilo' and "
                "'Slow is smooth, smooth is fast.' You are not the user.")
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("You are PYTH, Kora's agentic Crypto CTO.", _IDENT_SHORT)

_MARKET_R7 = re.compile(r"\b(market update|crypto update|how'?s the market|how is the market|market (today|now|overview|summary)|"
                        r"happening in (the )?(crypto )?market|state of the market)\b|^\s*(the )?(crypto )?markets?\s*[?.!]*\s*$", re.I)
_OPINION_RE = re.compile(r"\b(buy the dip|good time to (buy|sell)|should i (buy|sell|hold|add|trim)|bullish|bearish|what do you think|"
                         r"your (take|opinion|thoughts)|thoughts\?|eh\?|is (it|this) a good|worth buying|time to buy)\b", re.I)
_BOTS = r"(henry|conrad|butch|alfred|riley|gladise|flo|ira|prime|kacey|sigel|orbi|zero|coder)"
_CONTACT_RE = re.compile(rf"\b(contact|message|email|e-mail|text|ping|reach( out to)?|get in touch with|ask|tell|have)\s+{_BOTS}\b|"
                         rf"\b(have you|want you to|can you|could you|please)\b[^.?]*\b(contact|message|email|ping|reach|ask)\b[^.?]*\b{_BOTS}\b", re.I)
_FAKE_TOOLTEXT_RE = re.compile(r"(?ims)^\s*TOOL_RESULT\s*:.*?(?:\n\s*\n|\Z)|^.*\blet me (run|use|call) (some |a |the )?tools?\b.*$|"
                               r"^\s*(TOOL|CALL|ACTION)\s*:\s*\w+.*$|\{\s*\"(tool|name)\"\s*:\s*\"[a-z_]+\".*?\}")


def contact_team_bot(bot, message):
    """Stub for the PYTH <-> KryptoKora bridge (planned: email to Riley). Not configured yet."""
    return {"ok": False, "status": "not_configured", "bot": bot,
            "detail": "The PYTH-to-team bridge isn't set up yet; Coder is building an email bridge."}


def _strip_fake_tools(s):
    s = _FAKE_TOOLTEXT_RE.sub("", s or "")
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _last_tool_context(history):
    for m in reversed(history or []):
        if m.get("role") == "assistant":
            return m.get("tool_context")
    return None


def _nums(s):
    return set(re.findall(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?", s or ""))


def opinion_answer(question, ctx, history):
    msgs = [{"role": "system", "content": _system_prompt()},
            *_recent(history, 3),
            {"role": "user", "content": f"LIVE DATA FROM YOUR LAST TOOL RUN (this conversation):\n{ctx}\n\nUser asks: {question}\n\n"
             "Give a balanced, honest take in 4-6 sentences that references specific numbers from the live data above "
             "(only those numbers): if present, cover the total market cap 24h change, the Fear & Greed reading, and BTC/ETH moves, then weigh bullish vs bearish signals and say plainly whether it looks like a dip worth buying or a wait-and-see. Don't call any tools or print TOOL_RESULT. "
             "End with one short line: 'Not financial advice.'"}]
    ans = _strip_fake_tools(_clean(ask_model(msgs), True))
    bad = [n for n in _nums(ans) if re.search(r"[$%]", n) and n.lstrip("+-") not in {x.lstrip("+-") for x in _nums(ctx)}]
    if bad:  # numbers not in the tool data: drop those sentences
        ans = " ".join(x for x in re.split(r"(?<=[.!?])\s+", ans) if not any(b in x for b in bad))
    if "financial advice" not in ans.lower():
        ans += "\n\nNot financial advice."
    return ans


_classify_r6_base = _classify_r6


def _classify_r6(part):
    if _MARKET_R7.search(part):
        return "market"
    return _classify_r6_base(part)


_run_agent_r6 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    m = _CONTACT_RE.search(user_input)
    if m:
        bot = next(b for b in m.groups()[::-1] if b and re.fullmatch(_BOTS, b, re.I)).title()
        res = contact_team_bot(bot, user_input)
        reply = (f"I can't reach {bot} directly yet; Coder is building an email bridge between me and the team. "
                 f"Until then, you could ask {bot} yourself, and I'm happy to draft the message for you."
                 if not res.get("ok") else f"Sent to {bot}.")
        reply, history = _stamp_reply(user_input, reply, history)
        try:
            add_conversation(user_input, reply)
        except Exception:
            pass
        return reply, history
    ctx = _last_tool_context(history)
    if ctx and _OPINION_RE.search(user_input) and _classify_r6(user_input) not in _LIVE:
        reply, history = _stamp_reply(user_input, opinion_answer(user_input, ctx, history), history)
        history[-1]["tool_context"] = ctx
        try:
            add_conversation(user_input, reply)
        except Exception:
            pass
        return reply, history
    reply, history = _run_agent_r6(user_input, history)
    cleaned = _strip_fake_tools(reply)
    if cleaned != reply and history and history[-1].get("role") == "assistant":
        history[-1]["content"] = cleaned
    return cleaned or "I don't have a real answer for that without running a tool.", history


# ---------------------------------------------------------------------------
# Round 8 (2026-10-15): presentation fixes, tide answer-first, portfolio vs
# market, broader assessment follow-ups, token deep dive, cleaner prefixes.
# ---------------------------------------------------------------------------
_CUR = {"input": ""}


def _fix_tables(text):
    """Re-split markdown tables collapsed onto one line ('| a | b | | --- | --- | | x | y |'), using the separator's column count."""
    out = []
    for line in (text or "").split("\n"):
        m = re.search(r"\|\s*\|\s*:?-{3,}", line)
        if not (m and line.count("|") >= 9):
            out.append(line); continue
        pre, tab = "", line
        k = line.find("|")
        pre, tab = line[:k].strip(), line[k:].strip()
        toks = [t.strip() for t in tab.strip("|").split("|")]
        try:
            n = len([t for t in toks if re.fullmatch(r":?-{3,}:?", t)])
            first_sep = next(i for i, t in enumerate(toks) if re.fullmatch(r":?-{3,}:?", t))
        except StopIteration:
            out.append(line); continue
        if first_sep != n + 1 or n < 2:
            out.append(line); continue
        rows, i = [], 0
        while i < len(toks):
            rows.append("| " + " | ".join(toks[i:i + n]) + " |")
            i += n + 1
        if pre:
            out += [pre, ""]
        out.extend(rows)
    return "\n".join(out)


def _clean(reply, tools_ran):
    reply = _FAKE_TOOL_RE.sub("", reply or "")
    if not tools_ran and _FAKE_SEARCH_RE.search(reply):
        reply = _FAKE_SEARCH_RE.sub("", reply)
        reply = "(From my built-in knowledge; no live search was run.)\n\n" + reply.lstrip(" :,")
    return re.sub(r"\n{3,}", "\n\n", reply).strip()


def format_tides(t, when=None):
    if t.get("error") or not t.get("tides"):
        return f"Tide data unavailable ({t.get('error', 'no predictions')})."
    now, q = t["now"], _CUR["input"].lower()
    f = lambda x: f"{x['type'].title()} tide {x['time'].strftime('%a %-I:%M %p')} ET ({x['ft']:.1f} ft)"
    if when == "tomorrow":
        day = (now + datetime.timedelta(days=1)).date()
        rows = [x for x in t["tides"] if x["time"].date() == day]
    else:
        rows = [x for x in t["tides"] if x["time"] > now][:4]
    want = "low" if re.search(r"\blow\b", q) else "high" if re.search(r"\bhigh\b", q) else None
    first = next((x for x in rows if x["type"] == want), None) if want else (rows[0] if rows else None)
    lead = ""
    if first:
        lead = (f"**{'Next ' if when != 'tomorrow' else 'Tomorrow, '}{first['type']} tide at {t['station'].split(' (')[0]}: "
                f"{first['time'].strftime('%A %-I:%M %p')} ET ({first['ft']:.1f} ft).**\n\n")
    return lead + "Upcoming tides:\n" + "\n".join("* " + f(x) for x in rows) + f"\n\n({t['station'].split('(')[-1].rstrip(')')} predictions, MLLW datum)"


_PLEASANTRY_RE = re.compile(r"^\s*(thanks?( you)?|thx|ok(ay)?|great|cool|nice|awesome|perfect|got it|sounds good|yes|yep)\s*[!.]*\s*$", re.I)
_split_r6_base = _split_r6


def _split_r6(text):
    return [p for p in _split_r6_base(text) if not _PLEASANTRY_RE.match(p)]


_PVM_RE = re.compile(r"\bportfolio\b.*\b(against|vs\.?|versus|compared?|relative|broader|the market|benchmark|outperform|underperform)\b", re.I)


def portfolio_vs_market():
    stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    blob = execute_tool("get_simple_prices", {"ids": _desk_ids()})
    rows = [r for r in _price_rows(blob) if isinstance(r["px"], (int, float)) and isinstance(r["qty"], (int, float))]
    if not rows:
        return "Portfolio prices unavailable right now."
    table = _price_table(blob)
    now_v = sum(r["qty"] * r["px"] for r in rows)
    contrib = []
    prev_v = 0.0
    for r in rows:
        v = r["qty"] * r["px"]
        c = r["chg"] if isinstance(r["chg"], (int, float)) else 0.0
        pv = v / (1 + c / 100)
        prev_v += pv
        contrib.append((r["sym"], v - pv, c))
    bag_chg = (now_v - prev_v) / prev_v * 100 if prev_v else 0.0
    m = market_overview()
    g = m.get("global") or {}
    mcap_chg = g.get("market_cap_change_percentage_24h_usd")
    mk = [x for x in ((m.get("snapshot") or {}).get("markets") or []) if isinstance(x, dict)]
    btc = next((x.get("change_24h") for x in mk if x.get("symbol") == "btc"), None)
    contrib.sort(key=lambda x: x[1])
    top = ", ".join(f"{s} {'+' if d >= 0 else '-'}${abs(d):,.0f} ({c:+.2f}%)" for s, d, c in contrib[::-1][:3])
    bot = ", ".join(f"{s} {'+' if d >= 0 else '-'}${abs(d):,.0f} ({c:+.2f}%)" for s, d, c in contrib[:3])
    lines = [f"**Portfolio vs market ({stamp})**", "", table, "",
             f"* **Your bag:** ${now_v:,.2f}, {bag_chg:+.2f}% over 24h ({'+' if now_v >= prev_v else '-'}${abs(now_v - prev_v):,.2f})"]
    if isinstance(btc, (int, float)):
        lines.append(f"* **BTC 24h:** {btc:+.2f}%")
    if isinstance(mcap_chg, (int, float)):
        lines.append(f"* **Total crypto market cap 24h:** {mcap_chg:+.2f}%")
    lines += [f"* **Top contributors:** {top}", f"* **Biggest drags:** {bot}", ""]
    take = []
    if isinstance(mcap_chg, (int, float)):
        diff = bag_chg - mcap_chg
        take.append(f"Your portfolio is {'outperforming' if diff >= 0 else 'underperforming'} the broader market by "
                    f"{abs(diff):.2f} percentage points today ({bag_chg:+.2f}% vs {mcap_chg:+.2f}%).")
    if isinstance(btc, (int, float)):
        take.append(f"Against BTC alone you're {'ahead' if bag_chg >= btc else 'behind'} ({bag_chg:+.2f}% vs {btc:+.2f}%).")
    take.append(f"The move is mostly driven by {contrib[::-1][0][0]} on the upside and {contrib[0][0]} on the downside.")
    lines.append(" ".join(take))
    return "\n".join(lines)


_ASSESS_RE = re.compile(r"\b(assess(ment)?|your own words|general feeling|gut feel|interpret|what do you make of|your take|take on|"
                        r"read on|summari[sz]e (that|this|it|the (data|research|numbers))|make sense of|what does (that|this|it) (mean|tell)|"
                        r"buy the dip|good time to (buy|sell)|should i (buy|sell|hold|add|trim)|bullish|bearish|what do you think|"
                        r"your (opinion|thoughts)|thoughts\?|eh\?|is (it|this) a good|worth buying)\b", re.I)


def assessment_answer(question, ctx, history):
    msgs = [{"role": "system", "content": _system_prompt()},
            {"role": "user", "content": f"LIVE DATA FROM YOUR LAST TOOL RUN:\n{ctx}\n\nUser asks: {question}\n\n"
             "Write 1-2 short paragraphs of narrative in your own words: what the data says about the market mood today, "
             "citing a few key numbers from the live data only (e.g. total market cap change, Fear & Greed, BTC/ETH, notable movers). "
             "Do NOT repeat the table or list the data again, do not call tools, do not print TOOL_RESULT. "
             "If it's a buy/sell question, weigh both sides. End with: 'Not financial advice.'"}]
    ans = _strip_fake_tools(_clean(ask_model(msgs), True))
    ctxn = {x.lstrip("+-") for x in _nums(ctx)}
    bad = [n for n in _nums(ans) if re.search(r"[$%]", n) and n.lstrip("+-") not in ctxn]
    if bad:
        ans = " ".join(x for x in re.split(r"(?<=[.!?])\s+", ans) if not any(b in x for b in bad))
    if "financial advice" not in ans.lower():
        ans += "\n\nNot financial advice."
    return ans


_DIVE_RE = re.compile(r"\b(deep dive|deep-dive|dig into|full rundown|everything about|research)\b", re.I)
_DEEPER_RE = re.compile(r"^\s*(yes|yeah|yep|sure|ok(ay)?|please)?[,!. ]*(do that|go (ahead|deeper)|dig deeper|more|yes please|do it|go for it)?\s*[.!]*\s*$", re.I)


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def coin_deep_dive(query, chain=None):
    q = query.strip().lower()
    cid = WATCH_IDS.get(q)
    meta = {"id": cid} if cid else search_coin(q)
    if not meta or meta.get("error"):
        return f"I couldn't find a CoinGecko listing for '{query}'.", None
    try:
        r = requests.get(f"{CG_REST}/coins/{meta['id']}", params={"localization": "false", "tickers": "false",
                         "community_data": "false", "developer_data": "false", "sparkline": "false"}, timeout=20)
        r.raise_for_status(); d = r.json()
    except Exception as e:
        return f"CoinGecko coin lookup failed: {e}", None
    md = d.get("market_data") or {}
    usd = lambda k: (md.get(k) or {}).get("usd")
    name, sym = d.get("name"), (d.get("symbol") or "").upper()
    plats = {k: v for k, v in (d.get("platforms") or {}).items() if k}
    L = [f"## {name} ({sym}) — deep dive", f"*CoinGecko id `{d['id']}`, market-cap rank {d.get('market_cap_rank') or '—'}*", ""]
    if chain:
        ck = re.sub(r"[^a-z0-9]", "", chain.lower())
        hit = {k: v for k, v in plats.items() if ck in re.sub(r"[^a-z0-9]", "", k.lower())}
        if hit:
            k, v = next(iter(hit.items()))
            L.append(f"**Chain check:** yes, {name} is listed on **{k}** — contract `{v}` (per CoinGecko).")
        else:
            L.append(f"**Chain check:** CoinGecko does **not** list {name} on '{chain}'. Listed platforms: "
                     + (", ".join(plats) or "none"))
        L.append("")
    L.append("**Market data**")
    for label, val in [("Price", usd("current_price")), ("Market cap", usd("market_cap")), ("FDV", usd("fully_diluted_valuation")),
                       ("24h volume", usd("total_volume"))]:
        if isinstance(val, (int, float)):
            L.append(f"* {label}: {_fmt_px(val) if label == 'Price' else '$' + format(val, ',.0f')}")
    for label, k in [("24h", "price_change_percentage_24h"), ("7d", "price_change_percentage_7d"), ("30d", "price_change_percentage_30d")]:
        if isinstance(md.get(k), (int, float)):
            L.append(f"* Change {label}: {md[k]:+.2f}%")
    for label, k in [("Circulating supply", "circulating_supply"), ("Total supply", "total_supply"), ("Max supply", "max_supply")]:
        v = md.get(k)
        L.append(f"* {label}: {format(v, ',.0f') if isinstance(v, (int, float)) else 'not stated'}")
    if usd("ath"):
        L.append(f"* ATH: {_fmt_px(usd('ath'))} ({((md.get('ath_date') or {}).get('usd') or '')[:10]}), "
                 f"ATL: {_fmt_px(usd('atl'))} ({((md.get('atl_date') or {}).get('usd') or '')[:10]})")
    L += ["", "**Profile**", f"* Categories: {', '.join(c for c in d.get('categories') or [] if c) or 'none listed'}",
          f"* Genesis date: {d.get('genesis_date') or 'not stated'}"]
    if plats:
        L.append("* Contracts: " + "; ".join(f"{k}: `{v}`" for k, v in plats.items()))
    desc = _strip_html((d.get("description") or {}).get("en"))
    if desc:
        L += ["", "**Description (CoinGecko):** " + (desc[:700] + ("…" if len(desc) > 700 else ""))]
    links = d.get("links") or {}
    lk = [u for u in (links.get("homepage") or []) if u]
    if links.get("twitter_screen_name"):
        lk.append(f"https://x.com/{links['twitter_screen_name']}")
    lk += [u for u in ((links.get("repos_url") or {}).get("github") or []) if u]
    lk += [u for u in (links.get("blockchain_site") or []) if u][:2]
    if lk:
        L += ["", "**Links (from CoinGecko):**"] + [f"* {u}" for u in lk]
    news = web_search(f"{name} {sym} crypto token news", num_results=5)
    hits = [h for h in (news.get("results") or []) if h.get("url")]
    if hits:
        L += ["", "**Recent web results:**"] + [f"* [{h['title']}]({h['url']}) — {(h.get('snippet') or '')[:140]}" for h in hits]
    L += ["", "Want me to go deeper — read the top news pages and the project site and summarize them with sources?"]
    return "\n".join(L), {"id": d["id"], "name": name, "symbol": sym, "home": lk[:1], "news": [h["url"] for h in hits[:3]]}


def deeper_dive(dd):
    urls = list(dict.fromkeys((dd.get("home") or []) + (dd.get("news") or [])))[:4]
    res = web_search(f"{dd['name']} {dd['symbol']} token 2026", num_results=5)
    hits = [h for h in (res.get("results") or []) if h.get("url")]
    for h in hits:
        if h["url"] not in urls and len(urls) < 5:
            urls.append(h["url"])
    if not urls:
        return "I couldn't find any pages to dig into."
    pages = fetch_pages(urls)
    srcs = "\n".join(f"[{i+1}] {u}" for i, u in enumerate(urls))
    ctx = json.dumps(pages)[:9000] + "\n\nSEARCH SNIPPETS:\n" + "\n".join(f"- {h['title']}: {h.get('snippet')}" for h in hits)
    msgs = [{"role": "system", "content": _system_prompt()},
            {"role": "user", "content": f"Token: {dd['name']} ({dd['symbol']}). SOURCES:\n{srcs}\n\nPAGE TEXT:\n{ctx}\n\n"
             "Summarize what these sources say about the project (what it is, recent news/developments, risks) in 2-3 short paragraphs, "
             "citing sources as [n]. Use only the text above; say if the pages had little useful content. No prices unless in the text."}]
    ans = _strip_fake_tools(_clean(ask_model(msgs), True))
    return f"**{dd['name']} — deeper look**\n\n{ans}\n\nSources:\n{srcs}"


def _last_meta(history, key):
    for m in reversed(history or []):
        if m.get("role") == "assistant":
            return m.get(key)
    return None


_run_agent_r7 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    _CUR["input"] = user_input
    extra, reply = {}, None
    dd_last = _last_meta(history, "deep_dive")
    ctx = _last_tool_context(history)
    if dd_last and _DEEPER_RE.match(user_input) and user_input.strip():
        reply = deeper_dive(dd_last)
        extra = {"tool_context": reply[:6000]}
    elif _DIVE_RE.search(user_input) and re.search(r"\b(token|coin)\b", user_input, re.I):
        m = (re.search(r"\b(?:the\s+)?\$?([A-Za-z0-9][\w\-]*)\s+(?:token|coin)\b", user_input))
        chain = re.search(r"\bon\s+(?:the\s+)?([A-Za-z0-9][\w ]*?)\s+(?:chain|network|blockchain)\b", user_input, re.I)
        if m:
            reply, dd = coin_deep_dive(m.group(1), chain.group(1) if chain else None)
            extra = {"tool_context": reply[:6000], "deep_dive": dd} if dd else {}
    elif _PVM_RE.search(user_input):
        reply = portfolio_vs_market()
        extra = {"tool_context": reply[:6000]}
    elif ctx and _ASSESS_RE.search(user_input) and not re.search(r"\b(update|latest|refresh|now|again)\b", user_input, re.I):
        reply = assessment_answer(user_input, ctx, history)
        extra = {"tool_context": ctx}
    if reply is not None:
        reply = _fix_tables(_strip_tags(reply))
        reply, history = _stamp_reply(user_input, reply, history)
        history[-1].update(extra)
        try:
            add_conversation(user_input, reply)
        except Exception:
            pass
        return reply, history
    reply, history = _run_agent_r7(user_input, history)
    fixed = _fix_tables(reply)
    if history and history[-1].get("role") == "assistant":
        history[-1]["content"] = fixed
    return fixed, history


# ---------------------------------------------------------------------------
# Round 9 (2026-10-15): pending offers + affirmatives, pasted-article path,
# conversation trace / rationale, token-pinned web search, route metadata.
# ---------------------------------------------------------------------------
import functools as _ft
from tools import doc_index as _di

_TRACE = {"tools": []}


def _traced(name, fn):
    @_ft.wraps(fn)
    def w(*a, **k):
        arg = a[0] if a else (k or "")
        _TRACE["tools"].append(f"{name}({str(arg)[:60]})")
        return fn(*a, **k)
    return w


for _n in ("get_weather", "get_weather_period", "get_tides", "market_overview", "lookup_coin_price", "fetch_pages",
           "search_documents", "hybrid_search", "coin_deep_dive", "deeper_dive", "portfolio_vs_market", "load_document",
           "web_answer", "team_answer", "awards_answer", "identity_answer"):
    if _n in globals():
        globals()[_n] = _traced(_n, globals()[_n])

_raw_web_search = web_search
_TOKEN_CTX = {}


def web_search(query, num_results=5):
    """Token-pinned web search: adds token context and drops results that don't mention the token."""
    _TRACE["tools"].append(f"web_search({query[:60]})")
    tok = _TOKEN_CTX.get("tok")
    if tok and tok["name"].split(".")[0].lower() in query.lower():
        q = f"{tok['name']} {tok['symbol']} crypto token" + (f" {tok['domain']}" if tok.get("domain") else "")
        res = _raw_web_search(q, num_results=max(num_results, 8))
        keep = []
        for h in res.get("results") or []:
            blob = f"{h.get('title')} {h.get('snippet')} {h.get('url')}"
            if (re.search(rf"\b{re.escape(tok['symbol'])}\b", blob) or (tok.get("domain") and tok["domain"] in blob.lower())
                    or re.search(r"\$" + re.escape(tok["symbol"]), blob, re.I)
                    or (re.search(re.escape(tok["name"].split(".")[0]), blob, re.I) and re.search(r"\b(token|crypto|coin|defi|chain)\b", blob, re.I))):
                keep.append(h)
        res = {**res, "query": q, "results": keep[:num_results], "filtered_out": len(res.get("results") or []) - len(keep)}
        return res
    return _raw_web_search(query, num_results=num_results)


_coin_deep_dive_r8 = coin_deep_dive


def coin_deep_dive(query, chain=None):
    cid = WATCH_IDS.get(query.strip().lower())
    meta = {"id": cid} if cid else (search_coin(query) or {})
    if meta.get("id"):
        try:
            d = requests.get(f"{CG_REST}/coins/{meta['id']}", params={"localization": "false", "tickers": "false",
                             "market_data": "false", "community_data": "false", "developer_data": "false"}, timeout=15).json()
            home = ((d.get("links") or {}).get("homepage") or [""])[0]
            dom = re.sub(r"^https?://(www\.)?", "", home).strip("/").lower()
            _TOKEN_CTX["tok"] = {"name": d.get("name") or query, "symbol": (d.get("symbol") or query).upper(), "domain": dom}
        except Exception:
            pass
    return _coin_deep_dive_r8(query, chain)


_AFFIRM_RE = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|please|absolutely|definitely|do it|do that|go ahead|go for it|sounds good|y)\b", re.I)
_PASTE_LEAD_RE = re.compile(r"^\s*(summari[sz]e|review|read this|tl;?dr|note any alpha|analy[sz]e this|what do you make of this)", re.I)
_META_RE = re.compile(r"\b(read back|re-?read|look back|go back) (through|over)? ?(our|the) (conversation|chat|messages)|explain (the|your) (rationale|reasoning)|"
                      r"why did you|step by step (what|how) you|what happened there|walk me through (what|how) you", re.I)


def _set_offer(history, kind, args):
    if history and history[-1].get("role") == "assistant":
        history[-1]["pending_offer"] = {"type": kind, "args": args}


def _pending(history):
    for m in reversed(history or []):
        if m.get("role") == "assistant":
            return m.get("pending_offer")
    return None


def _holdings_note(text):
    tickers = WATCH_EXTRA.get("_tickers") or {}
    notes = []
    for k, v in tickers.items():
        sym = (v.get("symbol") or "").upper()
        if sym and sym not in [n[0] for n in notes] and re.search(rf"(\${sym}\b|\b{sym}\b)", text) and v.get("qty"):
            notes.append((sym, v["qty"], v.get("desk")))
    return notes


def index_paste(title, body):
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    path = f"pasted://{ts}"
    con = _di._db()
    for c in _di._chunks(body):
        cur = con.execute("INSERT INTO chunks(path, name, text) VALUES (?,?,?)", (path, f"pasted article: {title[:80]}", c))
        try:
            v = _di.embed([c])[0]
            con.execute("INSERT OR REPLACE INTO vecs(id, path, vec) VALUES (?,?,?)", (cur.lastrowid, path, v.tobytes()))
        except Exception:
            pass
    con.commit()
    return path


def paste_summary(text):
    m = re.match(r"^\s*([^:\n]{0,160}?)[:\n]\s*(.+)$", text, re.S)
    instr, body = (m.group(1), m.group(2)) if m and len(m.group(2)) > 200 else ("Summarize this", text)
    title = re.split(r"[.\n]", body.strip(), 1)[0][:80]
    notes = _holdings_note(body)
    hold = "; ".join(f"you hold {q:,} {s} ({d} desk)" for s, q, d in notes) or "none of the user's watchlist tokens are mentioned"
    msgs = [{"role": "system", "content": "You are PYTH, the user's analyst. Work ONLY from the pasted text; do not add outside facts or prices."},
            {"role": "user", "content": f"PASTED TEXT:\n<<<\n{body[:12000]}\n>>>\n\nRequest: {instr}\nthe user's holdings relevant to this: {hold}\n\n"
             "Write these sections in markdown:\n**TL;DR** (2 sentences)\n**Key developments** (bullets, with the dates given in the text)\n"
             "**Bull points**\n**Bear / risk points**\n**Alpha / what to watch** (concrete, specific items from the text)\n"
             "**What it means for your holdings** (1-2 sentences addressed to the user as 'you', using the holdings line above; no price targets)\n"
             "Only use facts in the pasted text."}]
    ans = _strip_fake_tools(_clean(ask_model(msgs), True))
    ans = _verify_years(ans, body)
    try:
        index_paste(title, body)
        saved = "\n\n_(Saved to my document index as a pasted article, so you can ask about it later.)_"
    except Exception:
        saved = ""
    return ans + saved


_ROUTE_WHY = {
    "greeting/chat": "No tool intent, so the local model answered conversationally.",
    "live tools": "The message asked for live data, so I ran the matching tools and formatted their results.",
    "portfolio vs market": "You compared the portfolio with the market, so I priced the book and pulled market-wide data.",
    "assessment": "You asked for an interpretation, so I reused the previous turn's tool results instead of re-running tools.",
    "deep dive": "You asked for a token deep dive, so I pulled CoinGecko's coin record plus a web search.",
    "deeper dive": "You accepted my offer to go deeper, so I read the project site and top news pages.",
    "paste summary": "You pasted text to review, so I summarized only that text without routing on words inside it.",
    "documents": "The question was about your own records, so I searched your indexed documents.",
    "team": "The question was about the KryptoKora team, so I read the team briefing.",
    "trace": "You asked me to explain my steps, so I rebuilt them from the conversation's routing log.",
}


def conversation_trace(history, n=10):
    turns, pend_user = [], None
    for m in history:
        if m.get("role") == "user":
            pend_user = m["content"]
        elif m.get("role") == "assistant" and pend_user is not None:
            turns.append((pend_user, m)); pend_user = None
    if not turns:
        return "This chat session has no earlier turns yet (the web UI history may have been cleared)."
    out = [f"**Step-by-step trace of the last {min(n, len(turns))} turns**", ""]
    for i, (u, a) in enumerate(turns[-n:], 1):
        route = a.get("route", "unknown (before route logging)")
        tools = ", ".join(a.get("tools") or []) or "none"
        out.append(f"{i}. **You:** \"{u[:110]}{'…' if len(u) > 110 else ''}\"")
        out.append(f"   * Route: {route}; tools: {tools}")
        out.append(f"   * Why: {_ROUTE_WHY.get(route, 'Routed by keyword rules.')}")
        if a.get("misroute"):
            out.append(f"   * ⚠ Misroute: {a['misroute']}")
    return "\n".join(out)


def _infer_route(user_input, h, tools):
    if h.get("deep_dive"):
        return "deep dive"
    t = " ".join(tools)
    if "team_answer" in t:
        return "team"
    if h.get("files") or "search_documents" in t or "hybrid_search" in t:
        return "documents"
    if h.get("tool_context") or tools:
        return "live tools"
    return "greeting/chat"


_run_agent_r8 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    _CUR["input"] = user_input
    _TRACE["tools"] = []
    reply, route, extra = None, None, {}
    pend = _pending(history)
    is_paste = len(user_input) > 600 or (_PASTE_LEAD_RE.match(user_input) and len(user_input) > 250)
    if pend and _AFFIRM_RE.match(user_input) and len(user_input) < 80:
        if pend["type"] == "deeper_dive":
            reply, route = deeper_dive(pend["args"]), "deeper dive"
            extra = {"tool_context": reply[:6000]}
    elif is_paste:
        reply, route = paste_summary(user_input), "paste summary"
    elif _META_RE.search(user_input):
        reply, route = conversation_trace(history), "trace"
        if re.search(r"\btry (that|it) again\b", user_input, re.I):
            last_paste = next((m["content"] for m in reversed(history) if m.get("role") == "user" and len(m["content"]) > 600), None)
            if last_paste:
                reply += "\n\n---\n**Retrying your pasted article:**\n\n" + paste_summary(last_paste)
    if reply is not None:
        reply = _fix_tables(_strip_tags(reply))
        reply, history = _stamp_reply(user_input, reply, history)
        history[-1].update(extra)
        try:
            add_conversation(user_input[:1500], reply)
        except Exception:
            pass
    else:
        reply, history = _run_agent_r8(user_input, history)
        route = None
    h = history[-1]
    h["tools"] = list(dict.fromkeys(_TRACE["tools"]))[:8]
    h["route"] = route or _infer_route(user_input, h, h["tools"])
    if len(user_input) > 600 and h["route"] not in ("paste summary",):
        h["misroute"] = "a long pasted text was routed by keywords instead of being summarized"
    if h.get("deep_dive") and "Want me to go deeper" in reply:
        h["pending_offer"] = {"type": "deeper_dive", "args": h["deep_dive"]}
    return reply, history


# Round 9b: line-preserving year check, trace execute_tool, second-person holdings.
def _verify_years(ans, source):
    src_years = set(re.findall(r"\b(?:19|20)\d\d\b", source))
    out_lines, dropped = [], False
    for line in (ans or "").split("\n"):
        sents = re.split(r"(?<=[.!?])\s+", line)
        kept = [s for s in sents if not (set(re.findall(r"\b(?:19|20)\d\d\b", s)) - src_years)]
        if len(kept) < len(sents):
            dropped = True
        if kept or not line.strip():
            out_lines.append(" ".join(kept))
    out = "\n".join(out_lines).strip()
    if dropped:
        out += "\n\n(Note: I removed a date that isn't in the source text.)"
    return out


execute_tool = _traced("execute_tool", execute_tool)


# ---------------------------------------------------------------------------
# Round 10 (2026-10-15): live email bridge to the KryptoKora team (Riley first).
# ---------------------------------------------------------------------------
from tools.team_bridge import send_to_team, check_team_replies, CONNECTED as _BRIDGE_BOTS, canon_bot as _canon_bot

_BOTS10 = r"(henry|henre|conrad|butch|alfred|riley|gladise|gladys|flo|ira|prime|kacey|sigel|orbi|zero|coder|kooter)"
_CONTACT10_RE = re.compile(rf"\b(ask|message|email|e-mail|text|ping|tell|have|contact|reach( out to)?|get in touch with|send)\s+{_BOTS10}\b|"
                           rf"\b(have you|want you to|can you|could you|please)\b[^.?]*\b(contact|message|email|ping|reach|ask|send)\b[^.?]*\b{_BOTS10}\b", re.I)
_REPLIES_RE = re.compile(r"\b(any (new )?(replies|responses|answers)|check (the )?team inbox|team inbox|did \w+ (answer|reply|respond|get back)|"
                         rf"(replies|reply|response) from (the team|{_BOTS10})|heard back)\b", re.I)
_LAST_POLL = {"t": 0.0}


def contact_team_bot(bot, message):
    b = bot.lower()
    if b in ("kooter",):
        b = "coder"
    b = _canon_bot(b)
    if b not in _BRIDGE_BOTS:
        return {"ok": False, "status": "not_connected", "bot": b}
    e = send_to_team(b, message)
    return {"ok": e["status"] == "sent", **e}


def _extract_question(text, bot):
    q = re.sub(rf"^.*?\b(ask|message|email|e-mail|text|ping|tell|have|contact|send)\s+(a message to\s+)?{bot}\b\s*(to\s+|that\s+|whether\s+|if\s+)?[:,\-]?\s*", "", text, count=1, flags=re.I | re.S)
    q = re.sub(rf"\b(on our kryptokora team|from the team)\b", "", q, flags=re.I).strip()
    # embedded question -> direct question: "when the next team meeting is" -> "when is the next team meeting"
    m = re.match(r"^(when|where|what|who|how|why|which)\s+(.+?)\s+(is|are|was|were|will be)\s*[?.]?$", q, re.I)
    if m:
        q = f"{m.group(1)} {m.group(3)} {m.group(2)}"
    return (q[0].upper() + q[1:]) if q else text.strip()


def _index_reply(r):
    try:
        path = f"team://{r['bot']}/{r.get('id') or r['uid']}"
        con = _di._db()
        txt = f"[{r['date']}] Reply from {r['bot'].title()} ({r.get('id')}): {r['subject']}\n{r['body']}"
        for c in _di._chunks(txt):
            cur = con.execute("INSERT INTO chunks(path, name, text) VALUES (?,?,?)", (path, f"team reply: {r['bot']}", c))
            try:
                con.execute("INSERT OR REPLACE INTO vecs(id, path, vec) VALUES (?,?,?)", (cur.lastrowid, path, _di.embed([c])[0].tobytes()))
            except Exception:
                pass
        con.commit()
    except Exception as e:
        logger.warning("index reply failed: %s", e)


def format_replies(res):
    if res.get("error"):
        return f"I couldn't check the team inbox ({res['error']})."
    rs = res.get("replies") or []
    for r in rs:
        _index_reply(r)
    if not rs:
        return "No new replies from the team yet."
    return "\n\n".join(f"**Reply from {r['bot'].title()}** ({r.get('id') or 'no ref'}, {r['date']})\n> " +
                       "\n> ".join((r["body"] or "(empty)").splitlines()[:25]) for r in rs)


def team_reply_notice(min_interval=120):
    """For the web UI: at most once per interval, check for new replies and return a short notice (or '')."""
    import time as _t
    if _t.time() - _LAST_POLL["t"] < min_interval:
        return ""
    _LAST_POLL["t"] = _t.time()
    try:
        res = check_team_replies()
    except Exception:
        return ""
    rs = res.get("replies") or []
    for r in rs:
        _index_reply(r)
    return "\n".join(f"📬 New reply from {r['bot'].title()} ({r.get('id') or 'no ref'}): {re.sub(r'\s+', ' ', r['body'])[:200]}" for r in rs)


_m10 = _get_detailed_tool_manifest


def _get_detailed_tool_manifest():
    m = _m10()
    m.update({"send_to_team": {"bot": "riley (only Riley connected so far)", "message": "question"},
              "check_team_replies": {}})
    return m


_run_agent_r9 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    reply = None
    if _REPLIES_RE.search(user_input) and len(user_input) < 200:
        _LAST_POLL["t"] = _time.time()
        reply, route = format_replies(check_team_replies()), "team inbox check"
    else:
        m = _CONTACT10_RE.search(user_input)
        if m and len(user_input) < 1500:
            said = next(g for g in m.groups()[::-1] if g and re.fullmatch(_BOTS10, g, re.I)).lower()
            bot = _canon_bot(said)
            question = _extract_question(user_input, said)
            res = contact_team_bot(bot, question)
            route = "team message"
            names = ", ".join(b.title() for b in sorted(_BRIDGE_BOTS))
            if res.get("status") == "not_connected":
                reply = (f"My email bridge reaches only {names} so far, so I didn't send anything to {bot.title()}. "
                         f"Want me to ask one of them instead, or you can message {bot.title()} directly?")
            elif res.get("ok"):
                reply = (f"Sent to {bot.title()} via the team inbox ({res['id']}). Replies usually take a few minutes; "
                         f"ask \"any replies from the team?\" to check.\n\n> {question}")
            else:
                reply = f"I tried to email {bot.title()} but it failed ({res.get('status')}). Nothing was sent."
    if reply is None:
        return _run_agent_r9(user_input, history)
    reply, history = _stamp_reply(user_input, reply, history)
    history[-1].update({"route": route, "tools": ["send_to_team" if route == "team message" else "check_team_replies"]})
    try:
        add_conversation(user_input, reply)
    except Exception:
        pass
    return reply, history


# ---------------------------------------------------------------------------
# Round 11 (2026-10-15): inbox phrasing -> check_team_replies (with stored fallback),
# Riley holdings recall, narrative/interpretation grounded in market data,
# guard suppressed when tool data is in context, duplicate guard removal.
# ---------------------------------------------------------------------------
from tools.team_bridge import stored_replies as _stored_replies

_GUARD_MSG_RE = re.compile(r"I won't guess at (crypto )?prices or market numbers without live data[^\n]*(\n[^\n]*I'll pull real numbers\.)?", re.I)
_R11 = {"ctx": False}
_INBOX11_RE = re.compile(
    r"\b(check (my |the |your )?(e-?mails?|inbox|mail)|check on (the )?(e-?mails?|messages?)|"
    r"any (new )?(e-?mails?|messages?|mail|replies|responses|word|news) (from|by)|"
    r"(e-?mails?|messages?|update|reply|replies|response) from (riley|kryptokora|the team)|"
    rf"let me know when (she|he|they|{_BOTS10}) (replies|responds|answers|gets back|writes)|"
    rf"(has|did) {_BOTS10} (replied|reply|responded|respond|written|answered|sent)|"
    r"she should'?ve given you|heard (back )?from riley)\b|^\s*(check )?(e-?mails?|inbox)\s*[?.!]*\s*$", re.I)
_HOLD11_RE = re.compile(r"\b(portfolio|holdings|holding table|table)\b.*\b(riley|sent|team|email)\b|\b(riley|team)'?s? (portfolio|holdings)\b", re.I)
_NARR11_RE = re.compile(r"\b(narrative|interpretation|interpret|glean|in (your|plain) words|what does (that|this|it) (mean|tell|say)|"
                        r"don'?t just give me (the )?numbers|your (own )?(read|view|assessment|interpretation)|make of (that|this|it))\b", re.I)
_MKT11_RE = re.compile(r"\b(crypto )?market(s)?\b.*\b(look|doing|today|now)|\bhow'?s (the )?crypto|\bmarket news\b", re.I)

_guard_r10 = _guard


def _guard(reply, tools_ran):
    if _R11["ctx"] or _TRACE["tools"]:
        return _clean(reply, True)
    return _guard_r10(reply, tools_ran)


def _dedupe_guard(text):
    seen, out = False, []
    for para in re.split(r"\n\s*\n", text or ""):
        if _GUARD_MSG_RE.search(para):
            if seen:
                continue
            seen = True
        out.append(para)
    return "\n\n".join(out)


def _index_reply(r):  # replaces round 10: de-duplicated by path
    try:
        path = f"team://{r['bot']}/{r.get('id') or r['uid']}/{r['uid']}"
        con = _di._db()
        con.execute("DELETE FROM chunks WHERE path=?", (path,))
        try:
            con.execute("DELETE FROM vecs WHERE path=?", (path,))
        except Exception:
            pass
        kind = "holdings table" if r["body"].count("|") > 20 else "reply"
        txt = f"[{r['date']}] {kind.title()} from {r['bot'].title()} ({r.get('id')}): {r['subject']}\n{r['body']}"
        for c in _di._chunks(txt):
            cur = con.execute("INSERT INTO chunks(path, name, text) VALUES (?,?,?)", (path, f"team {kind}: {r['bot']}", c))
            try:
                con.execute("INSERT OR REPLACE INTO vecs(id, path, vec) VALUES (?,?,?)", (cur.lastrowid, path, _di.embed([c])[0].tobytes()))
            except Exception:
                pass
        con.commit()
    except Exception as e:
        logger.warning("index reply failed: %s", e)


def _render_reply(r):
    body = re.sub(r"^From:[^\n]*\n+", "", (r.get("body") or "(empty)").strip())
    body = re.sub(r"^PYTH-BRIDGE REPLY #P\d{12}:\s*", "", body)
    body = re.sub(r"\n+Ref: #P\d{12}\s*$", "", body)
    body = re.sub(r"\n(\|)", r"\n\1", body)
    # make sure a table starts on its own paragraph so markdown renders it
    body = re.sub(r"([^\n|])\n(\|[^\n]*\|\n\|\s*:?-{3,})", r"\1\n\n\2", body)
    return f"**Reply from {r['bot'].title()}** ({r.get('id') or 'no ref'}, {r['date']})\n\n{_fix_tables(body)}"


def format_replies(res, notify=False):
    head = "I'll check every time you send me a message; when a reply lands, a 📬 notice shows up under my next answer.\n\n" if notify else ""
    if res.get("error"):
        return head + f"I couldn't check the team inbox ({res['error']})."
    rs = res.get("replies") or []
    for r in rs:
        _index_reply(r)
    if rs:
        return head + f"📬 {len(rs)} new repl{'y' if len(rs) == 1 else 'ies'} in the team inbox:\n\n" + "\n\n---\n\n".join(_render_reply(r) for r in rs)
    old = _stored_replies(3)
    if old:
        return (head + "No new replies since my last check. Here are the latest ones I already have:\n\n"
                + "\n\n---\n\n".join(_render_reply(r) for r in old))
    return head + "No replies from the team yet."


def holdings_from_riley():
    """Show the newest real holdings table (not org-chart pipe trees)."""
    try:
        from tools.portfolio_autoload import find_latest_holdings, find_latest_from_stored, is_holdings_table
        r = None
        try:
            r = find_latest_holdings()
        except Exception:
            r = None
        r = r or find_latest_from_stored()
        if r:
            as_of = r.get("holdings_date") or ""
            extra = f" (as of {as_of})" if as_of else ""
            return _render_reply(r) + f"\n\n_(Holdings table{extra}; saved in my document index.)_"
    except Exception:
        pass
    # Strict fallback: require Asset/Amount/Value headers, not mere pipe count (Oct 26 bug)
    def _ok(body):
        try:
            from tools.portfolio_autoload import is_holdings_table
            return is_holdings_table(body)[0]
        except Exception:
            low = body.lower()
            return ("| asset |" in low or "|asset|" in low.replace(" ", "")) and "amount" in low and "value" in low and body.count("|") > 20
    rs = [r for r in _stored_replies(50) if _ok(r.get("body") or "")]
    if not rs:
        res = check_team_replies()
        for r in res.get("replies") or []:
            _index_reply(r)
        rs = [r for r in _stored_replies(50) if _ok(r.get("body") or "")]
    if not rs:
        return "I don't have a holdings table from Riley yet. Looking for subject/body 'Holdings YYYY-MM-DD' with Asset/Amount/Value columns."
    return _render_reply(rs[-1]) + "\n\n_(Saved in my document index as a team holdings table.)_"


def market_narrative(question, ctx=None):
    data = ctx
    if not data:
        m = market_overview()
        data = format_market(m)
    news = web_search("crypto market news today", num_results=5)
    hits = news.get("results") or []
    if hits:
        nctx = "\n".join(f"- {h.get('title')}: {(h.get('snippet') or '')[:200]}" for h in hits)
        foot = "\n\n**Headlines checked:**\n" + "\n".join(f"* [{h.get('title')}]({h.get('url')})" for h in hits[:4])
    else:
        nctx = "(web search returned no results)"
        foot = f"\n\n_(Web news search came back empty{': ' + news['error'] if news.get('error') else ''}, so this read is from the market data only.)_"
    full = f"{data}\n\nNEWS HEADLINES (web search):\n{nctx}"
    ans = assessment_answer(question, full, None)
    show = "" if ctx else data + "\n\n"
    return show + "**My read:**\n\n" + ans + foot, full


_run_agent_r10 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    _TRACE["tools"] = []
    recent_ctx = next((m.get("tool_context") for m in reversed(history[-6:]) if m.get("role") == "assistant" and m.get("tool_context")), None)
    _R11["ctx"] = bool(recent_ctx)
    reply, route, extra, tools = None, None, {}, []
    short = len(user_input) < 300
    try:
        if short and _HOLD11_RE.search(user_input) and re.search(r"\b(riley|sent|team)\b", user_input, re.I):
            reply, route, tools = holdings_from_riley(), "team holdings", ["stored_replies"]
        elif short and (_INBOX11_RE.search(user_input) or _REPLIES_RE.search(user_input)):
            _LAST_POLL["t"] = _time.time()
            notify = bool(re.search(r"\blet me know when\b", user_input, re.I))
            reply, route, tools = format_replies(check_team_replies(), notify), "team inbox check", ["check_team_replies"]
        elif short and _NARR11_RE.search(user_input) and (recent_ctx or _MKT11_RE.search(user_input)):
            fresh = bool(_MKT11_RE.search(user_input)) or not recent_ctx
            reply, full = market_narrative(user_input, None if fresh else recent_ctx)
            route, tools, extra = "market narrative", ["market_overview", "web_search"] if fresh else ["web_search"], {"tool_context": full[:6000]}
            _R11["ctx"] = True
        if reply is not None:
            reply = _dedupe_guard(reply)
            reply, history = _stamp_reply(user_input, reply, history)
            history[-1].update({"route": route, "tools": tools, **extra})
            try:
                add_conversation(user_input[:1500], reply)
            except Exception:
                pass
            return reply, history
        reply, history = _run_agent_r10(user_input, history)
        fixed = _dedupe_guard(reply)
        if _GUARD_MSG_RE.search(fixed) and (recent_ctx or history[-1].get("tool_context")):
            fixed, full = market_narrative(user_input, recent_ctx or history[-1].get("tool_context"))
            history[-1]["tool_context"] = full[:6000]
            history[-1]["route"] = "market narrative (guard override)"
        if history and history[-1].get("role") == "assistant":
            history[-1]["content"] = fixed
        return fixed, history
    finally:
        _R11["ctx"] = False


# ---------------------------------------------------------------------------
# Round 12 (2026-10-15): Riley's holdings table is the default portfolio
# (data/portfolio.json, priced live), 'update portfolio from Riley', stricter
# narrative number check (bare figures and ratios).
# ---------------------------------------------------------------------------
from tools import portfolio as _pf

_PORT12_RE = re.compile(r"\b((how'?s|how is)\s+(my|our|the)\s+(portfolio|book|bag)|^\s*(my |our |the )?(portfolio|holdings)\s*[?.!]*\s*$|"
                        r"(show|price|value|mark)\s+(me\s+)?(my|our|the)\s+(portfolio|holdings|book)|my holdings|our holdings|portfolio (update|looking|total|value))\b", re.I)
_UPD12_RE = re.compile(r"\b(update|refresh|replace|reload)\s+(the\s+|my\s+|our\s+)?(portfolio|holdings)(\s+(from|with|using)\s+(riley|the (new|latest) (table|reply)))?\b", re.I)


def _latest_riley_table():
    """Newest Holdings email/table. Prefers IMAP All Mail subject 'Holdings YYYY-MM-DD'
    (self-sends may skip INBOX); falls back to stored_replies with header guards."""
    try:
        from tools.portfolio_autoload import find_latest_holdings, find_latest_from_stored
        try:
            r = find_latest_holdings()
            if r:
                return r
        except Exception:
            pass
        r = find_latest_from_stored()
        if r:
            return r
    except Exception:
        pass
    def _ok(body):
        try:
            from tools.portfolio_autoload import is_holdings_table
            return is_holdings_table(body)[0]
        except Exception:
            low = (body or "").lower()
            return "asset" in low and "amount" in low and "value" in low and (body or "").count("|") > 20
    rs = [r for r in _stored_replies(50) if _ok(r.get("body") or "")]
    return rs[-1] if rs else None


def update_portfolio_from_riley():
    r = _latest_riley_table()
    if not r:
        return ("I don't have a holdings table from Riley to load. "
                "Expect subject/body 'Holdings YYYY-MM-DD' in All Mail (or say nothing and wait for the 7:15 AM autoload).")
    try:
        from tools.portfolio_autoload import apply_table, holdings_date_from_body
        body = r.get("body") or ""
        hdate = r.get("holdings_date") or holdings_date_from_body(body)
        result = apply_table(
            body,
            source=f"team holdings {r.get('id')} uid {r.get('uid')} ({r.get('date')})",
            meta={"source_uid": r.get("uid"), "holdings_date": hdate, "as_of": hdate},
            dry_run=False,
        )
        if result.get("status") == "skip":
            return f"Portfolio already up to date ({result.get('reason')}).\n\n" + _pf.format_portfolio()[0]
        if result.get("status") == "refuse":
            return f"I refused to replace the portfolio: {result.get('reason')}"
        if result.get("status") != "updated":
            return f"Portfolio update did not apply ({result.get('status')}: {result.get('reason')})."
        d = _pf.load()
        flagged = [p["asset"] for p in d["positions"] if p["flag"]]
        as_of = d.get("holdings_date") or hdate or r.get("date")
        return (f"Portfolio replaced with holdings as of {as_of} ({len(d['positions'])} positions, "
                f"{len(flagged)} flagged: {', '.join(flagged)}).\n\n" + _pf.format_portfolio(d)[0])
    except Exception:
        body = re.sub(r"^From:[^\n]*\n+", "", r["body"])
        d = _pf.build(body, source=f"team reply {r.get('id')} uid {r['uid']} ({r['date']})",
                      meta={"source_uid": r["uid"], "holdings_date": r.get("holdings_date"), "as_of": r.get("holdings_date")})
        flagged = [p["asset"] for p in d["positions"] if p["flag"]]
        as_of = d.get("holdings_date") or r.get("holdings_date") or r["date"]
        return (f"Portfolio replaced with holdings as of {as_of} ({len(d['positions'])} positions, "
                f"{len(flagged)} flagged: {', '.join(flagged)}).\n\n" + _pf.format_portfolio(d)[0])


def portfolio_answer():
    if _pf.load():
        return _pf.format_portfolio()[0]
    return _run_agent_r11("how is my portfolio", [])[0] + "\n\n_(data/portfolio.json missing; used watchlist.json.)_"


def _portfolio_offer(reply):
    """After an inbox check: offer to replace the portfolio if Riley's newest table isn't the loaded one."""
    r, d = _latest_riley_table(), _pf.load() or {}
    if r and d.get("source_uid") != r["uid"] and "Reply from" in reply:
        return reply + ("\n\n📋 Riley's reply includes a holdings table that isn't my current portfolio. "
                        "Say **'update portfolio from Riley'** (or 'yes') to replace it."), True
    return reply, False


# stricter number check for narrative/assessment answers
_assess_r11 = assessment_answer
_NUM12_RE = re.compile(r"\d+(?:\.\d+)?\s*[:/]\s*\d+(?:\.\d+)?|[-+]?\$?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmMbBtT]\b|x\b)?")


def _norm12(n):
    n = n.strip()
    if re.search(r"\d\s*[:/]\s*\d", n):
        return re.sub(r"\s", "", n)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", n)
    return m.group(0).replace(",", "") if m else ""


def assessment_answer(question, ctx, history):
    ans = _assess_r11(question, ctx, history)
    ctxn = {_norm12(x).rstrip("%") for x in _NUM12_RE.findall(ctx or "")}
    ctx_flat = re.sub(r"[\s,$]", "", ctx or "")
    keep, dropped = [], 0
    for para in ans.split("\n"):
        sents = re.split(r"(?<=[.!?])\s+", para)
        ok = []
        for s in sents:
            bad = False
            for n in _NUM12_RE.findall(s):
                k = _norm12(n).rstrip("%")
                if re.search(r"[:/]", k):  # ratios must appear verbatim in the data
                    bad = k not in ctx_flat
                elif k and k not in ctxn and not re.fullmatch(r"\d", k) and k not in ("24h", "24", "50"):
                    bad = True
                if bad:
                    break
            if bad:
                dropped += 1
            else:
                ok.append(s)
        if ok or not para.strip():
            keep.append(" ".join(ok))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    if dropped:
        out = out.replace("Not financial advice.", "").rstrip() + \
              f"\n\n_(Removed {dropped} sentence{'s' if dropped > 1 else ''} citing figures not in the data.)_\n\nNot financial advice."
    return out


_run_agent_r11 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    reply, route, tools, extra = None, None, [], {}
    pend = _pending(history)
    short = len(user_input) < 200
    if pend and pend.get("type") == "portfolio_update" and _AFFIRM_RE.match(user_input) and len(user_input) < 80:
        reply, route, tools = update_portfolio_from_riley(), "portfolio update", ["build_portfolio"]
    elif short and _UPD12_RE.search(user_input):
        reply, route, tools = update_portfolio_from_riley(), "portfolio update", ["build_portfolio"]
    elif short and _PORT12_RE.search(user_input) and not _PVM_RE.search(user_input) and not _HOLD11_RE.search(user_input):
        _TRACE["tools"] = []
        reply, route, tools = portfolio_answer(), "portfolio", ["portfolio.json", "coingecko simple/price"]
    if reply is not None:
        reply, history = _stamp_reply(user_input, reply, history)
        history[-1].update({"route": route, "tools": tools, "tool_context": reply[:6000]})
        try:
            add_conversation(user_input[:1500], reply)
        except Exception:
            pass
        return reply, history
    reply, history = _run_agent_r11(user_input, history)
    if history and history[-1].get("route") in ("team inbox check", "team holdings"):
        reply, offered = _portfolio_offer(reply)
        if offered:
            history[-1]["content"] = reply
            history[-1]["pending_offer"] = {"type": "portfolio_update", "args": {}}
    return reply, history


# ---------------------------------------------------------------------------
# Round 13 (2026-10-15): live CoinGecko data overrides web figures in deep /
# deeper dives; numbered, verified citations in the deeper look.
# ---------------------------------------------------------------------------
_FIG13_RE = re.compile(r"[-+]?\$\s?\d[\d,]*(?:\.\d+)?\s*(?:[kKmMbB]\b|million|billion|thousand)?|[-+]?\d[\d,]*(?:\.\d+)?\s*%|"
                       r"\b\d[\d,]*(?:\.\d+)?\s*(?:million|billion|[mMbB])\b(?=[^a-z]|$)", re.I)


def _fig_val(f):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", f)
    v = float(m.group(0).replace(",", ""))
    if re.search(r"\b(b|billion)\b", f, re.I) or re.search(r"\db\b", f, re.I):
        v *= 1e9
    elif re.search(r"(million|\dm\b|\sm\b)", f, re.I):
        v *= 1e6
    elif re.search(r"(thousand|\dk\b)", f, re.I):
        v *= 1e3
    return v, "%" in f


def _live_coin(cid):
    try:
        r = requests.get(f"{CG_REST}/coins/markets", params={"vs_currency": "usd", "ids": cid,
                         "price_change_percentage": "24h,7d,30d"}, timeout=15)
        r.raise_for_status(); x = (r.json() or [{}])[0]
    except Exception:
        return None
    return {k: x.get(k) for k in ("current_price", "market_cap", "fully_diluted_valuation", "total_volume",
                                    "price_change_percentage_24h_in_currency", "price_change_percentage_7d_in_currency",
                                    "price_change_percentage_30d_in_currency", "circulating_supply", "ath", "atl")}


def _live_block(live):
    if not live:
        return ""
    f = lambda v, p=False: "n/a" if not isinstance(v, (int, float)) else (f"{v:+.2f}%" if p else (_fmt_px(v) if v < 1000 else f"${v:,.0f}"))
    return (f"Price {f(live['current_price'])}; market cap {f(live['market_cap'])}; FDV {f(live['fully_diluted_valuation'])}; "
            f"24h volume {f(live['total_volume'])}; change 24h {f(live['price_change_percentage_24h_in_currency'], True)}, "
            f"7d {f(live['price_change_percentage_7d_in_currency'], True)}, 30d {f(live['price_change_percentage_30d_in_currency'], True)}")


def _live_ok(fig, live):
    """True if a figure matches one of the live numbers (within 1.5%)."""
    v, pct = _fig_val(fig)
    sign = -1 if fig.strip().startswith("-") else 1
    for k, x in (live or {}).items():
        if not isinstance(x, (int, float)):
            continue
        if pct != ("percentage" in k):
            continue
        tgt = abs(x) if pct else x
        if tgt and abs(v - tgt) / abs(tgt) <= 0.015 and (not pct or sign * x >= 0 or not fig.strip()[0] in "+-"):
            return True
    return False


def _scrub_figures(text, live):
    """Drop sentences with price / market-cap / % figures that don't match the live CoinGecko data."""
    out, dropped = [], 0
    for para in (text or "").split("\n"):
        keep = []
        for s in re.split(r"(?<=[.!?])\s+", para):
            figs = _FIG13_RE.findall(s)
            if figs and not all(_live_ok(f, live) for f in figs):
                dropped += 1
                continue
            keep.append(s)
        if keep or not para.strip():
            out.append(" ".join(keep))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), dropped


def deeper_dive(dd):
    _TRACE["tools"].append(f"deeper_dive({dd.get('name')})")
    live = (_live_coin(dd["id"]) if dd.get("id") else None) or dd.get("live")
    urls = list(dict.fromkeys((dd.get("home") or []) + (dd.get("news") or [])))[:4]
    res = web_search(f"{dd['name']} {dd['symbol']} token 2026", num_results=5)
    hits = [h for h in (res.get("results") or []) if h.get("url")]
    for h in hits:
        if h["url"] not in urls and len(urls) < 5:
            urls.append(h["url"])
    if not urls:
        return "I couldn't find any pages to dig into."
    pages = (fetch_pages(urls) or {}).get("pages") or []
    by_url = {u: next((p for p in pages if p.get("url") in (u, u.rstrip("/"))), None) for u in urls}
    for i, u in enumerate(urls):  # fall back to positional match (redirects change r.url)
        if by_url[u] is None and i < len(pages):
            by_url[u] = pages[i]
    snip = {h["url"]: f"{h.get('title')}: {h.get('snippet') or ''}" for h in hits}
    blocks, usable = [], set()
    for i, u in enumerate(urls, 1):
        p = by_url.get(u) or {}
        txt = (p.get("text") or "")[:1800]
        if not txt and u in snip:
            txt = "(page not readable; search snippet) " + snip[u]
        if txt:
            usable.add(i)
        blocks.append(f"=== SOURCE [{i}] {u} ===\n{txt or '(no readable text)'}")
    srcs = "\n".join(f"[{i}] {u}" for i, u in enumerate(urls, 1))
    lb = _live_block(live)
    msgs = [{"role": "system", "content": _system_prompt()},
            {"role": "user", "content": f"Token: {dd['name']} ({dd['symbol']}). The tools already ran this turn; their TOOL_RESULTs are below. Write the summary now; do not ask to run tools.\n\n"
             f"TOOL_RESULT coingecko (LIVE, authoritative):\n{lb or 'unavailable'}\n\nTOOL_RESULT fetch_pages:\n"
             + "\n\n".join(blocks) + "\n\n"
             "Summarize what these sources say about the project (what it is, recent news/developments, risks) in 2-3 short paragraphs. "
             "Cite a source as [n] ONLY when that exact fact appears inside the SOURCE [n] block. "
             "Do NOT quote any price, market cap, volume or % change from the web sources; they are often stale. "
             "If you mention market numbers, use only the coingecko TOOL_RESULT line and cite it as [live]. "
             "Say if the pages had little useful content."}]
    ans = _strip_fake_tools(_clean(ask_model(msgs), True))
    ans, dropped = _scrub_figures(ans, live)
    # citations: drop markers pointing at missing / unreadable sources
    bad_cites = sorted({int(n) for n in re.findall(r"\[(\d+)\]", ans) if int(n) not in usable})
    ans = re.sub(r"\s?\[(\d+)\]", lambda m: "" if int(m.group(1)) not in usable else m.group(0), ans)
    notes = []
    if lb:
        notes.append(f"**Live market data (CoinGecko, now):** {lb}")
    if dropped:
        notes.append(f"_(Removed {dropped} sentence{'s' if dropped > 1 else ''} with web price/market-cap/% figures that conflict with the live CoinGecko data.)_")
    if bad_cites:
        notes.append(f"_(Removed citation(s) {', '.join(f'[{n}]' for n in bad_cites)}: those sources had no readable text.)_")
    return f"**{dd['name']} — deeper look**\n\n{ans}\n\n" + ("\n\n".join(notes) + "\n\n" if notes else "") + f"Sources:\n{srcs}"


_coin_deep_dive_r12 = coin_deep_dive


def coin_deep_dive(query, chain=None):
    """Round 13: strip conflicting price/mcap/% figures out of the web-result snippets in the deep dive."""
    text, dd = _coin_deep_dive_r12(query, chain)
    if not dd:
        return text, dd
    live = _live_coin(dd["id"])
    if not live:
        return text, dd
    dd["live"] = live
    out, n = [], 0
    in_web = False
    for ln in text.split("\n"):
        if ln.startswith("**Recent web results"):
            in_web = True
        elif in_web and ln.startswith("* [") and " — " in ln:
            head, sn = ln.split(" — ", 1)
            clean, d = _scrub_figures(sn, live)
            if d:
                n += d
                ln = head + " — " + (clean or "(snippet figures removed: stale vs live data)")
        out.append(ln)
    text = "\n".join(out)
    if n:
        text = text.replace("\nWant me to go deeper", f"\n_(Removed {n} stale web figure(s) from snippets; the market data above is live.)_\n\nWant me to go deeper")
    return text, dd


# ---------------------------------------------------------------------------
# Round 14 (2026-10-15): safe math eval, auto web search for "what is X"
# definitions, no user-facing TOOL_RESULT jargon.
# ---------------------------------------------------------------------------
import ast as _ast
import math as _math
import operator as _op

_MATH_OPS = {_ast.Add: _op.add, _ast.Sub: _op.sub, _ast.Mult: _op.mul, _ast.Div: _op.truediv, _ast.Pow: _op.pow,
             _ast.Mod: _op.mod, _ast.FloorDiv: _op.floordiv, _ast.USub: _op.neg, _ast.UAdd: _op.pos}
_MATH_FN = {"sqrt": _math.sqrt, "cbrt": lambda x: math_cbrt(x), "abs": abs, "round": round, "log": _math.log, "log10": _math.log10,
            "ln": _math.log, "exp": _math.exp, "sin": _math.sin, "cos": _math.cos, "tan": _math.tan, "factorial": _math.factorial}
_MATH_CONST = {"pi": _math.pi, "e": _math.e}


def math_cbrt(x):
    return -((-x) ** (1 / 3)) if x < 0 else x ** (1 / 3)


def safe_math_eval(expr):
    """Evaluate an arithmetic expression with no names, attributes or calls beyond a math whitelist."""
    def ev(n):
        if isinstance(n, _ast.Expression):
            return ev(n.body)
        if isinstance(n, _ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, _ast.BinOp) and type(n.op) in _MATH_OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, _ast.Pow) and abs(b) > 1000:
                raise ValueError("exponent too large")
            return _MATH_OPS[type(n.op)](a, b)
        if isinstance(n, _ast.UnaryOp) and type(n.op) in _MATH_OPS:
            return _MATH_OPS[type(n.op)](ev(n.operand))
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id in _MATH_FN and not n.keywords:
            args = [ev(a) for a in n.args]
            if n.func.id == "factorial" and args and args[0] > 500:
                raise ValueError("too large")
            return _MATH_FN[n.func.id](*args)
        if isinstance(n, _ast.Name) and n.id in _MATH_CONST:
            return _MATH_CONST[n.id]
        raise ValueError("unsupported expression")
    return ev(_ast.parse(expr, mode="eval"))


_MATH_LEAD = re.compile(r"^\s*(what('?s| is| are)|calculate|compute|evaluate|solve|how much is)\s+", re.I)


def _math_expr(text):
    t = text.strip().lower().rstrip("?.! ")
    t = _MATH_LEAD.sub("", t)
    t = re.sub(r"^(the\s+)?", "", t)
    t = re.sub(r"(\d),(?=\d{3}\b)", r"\1", t)
    t = re.sub(r"(square root|sqrt) of\s+", "sqrt ", t)
    t = re.sub(r"(cube root) of\s+", "cbrt ", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*% of\s+", r"\1/100*", t)
    t = re.sub(r"factorial of\s+", "factorial ", t)
    reps = [(r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\b(times|multiplied by|x)\b", "*"), (r"\bdivided by\b", "/"), (r"\bover\b", "/"),
            (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"), (r"\bto the power of\b", "**"), (r"\^", "**"), (r"\bmod(ulo)?\b", "%"), (r"[×]", "*"), (r"[÷]", "/")]
    for a, b in reps:
        t = re.sub(a, b, t)
    t = re.sub(r"\b(sqrt|cbrt|factorial|log10|log|ln|exp|sin|cos|tan|abs)\s+(\d+(?:\.\d+)?|\([^()]*\))", r"\1(\2)", t)
    if not re.fullmatch(r"[\d\s.+\-*/%()]*(?:(?:sqrt|cbrt|factorial|log10|log|ln|exp|sin|cos|tan|abs|round|pi)[\d\s.+\-*/%()]*)*", t):
        return None
    if not re.search(r"\d", t) or not (re.search(r"[+\-*/%]", t) or re.search(r"[a-z]\(", t)):
        return None
    return t


def math_answer(text):
    expr = _math_expr(text)
    if not expr:
        return None
    try:
        v = safe_math_eval(expr)
    except Exception:
        return None
    if isinstance(v, float):
        v = int(round(v)) if abs(v - round(v)) < 1e-9 and abs(v) < 1e15 else float(f"{v:.10g}")
    _TRACE["tools"].append(f"safe_math_eval({expr})")
    return f"**{v:,}**" if isinstance(v, int) and abs(v) >= 10000 else f"**{v}**", expr


_DEF14_RE = re.compile(r"^\s*(?:what(?:'s| is| are)|whats|define|explain what|tell me what)\s+(?:an?\s+|the\s+)?(.{1,60}?)(?:\s+is)?\s*\??\s*$|"
                       r"^\s*what does\s+(.{1,60}?)\s+(?:mean|stand for|do)\s*\??\s*$", re.I)
_DEF14_SKIP = re.compile(r"\b(price|market|portfolio|holdings|weather|tide|time|date|today|tomorrow|my|your|you|our|this|that|it|"
                         r"going on|happening|up|new|the latest|news|plan|name|square root|pyth)\b|\d+\s*[+\-*/x]", re.I)


def _def_term(text):
    m = _DEF14_RE.match(text)
    if not m:
        return None
    term = (m.group(1) or m.group(2) or "").strip(" \"'`")
    if not term or _DEF14_SKIP.search(term) or len(term.split()) > 5:
        return None
    if term.lower() in WATCH_IDS or re.fullmatch(_BOTS10, term, re.I):  # known coin or team bot -> existing routes
        return None
    return term


def define_answer(term, question):
    _TRACE["tools"].append(f"web_search(what is {term})")
    res = _raw_web_search(f"what is {term}", num_results=6)
    hits = [h for h in (res.get("results") or []) if h.get("url")][:6]
    mem = []
    try:
        for h in hybrid_search(term, k=6):
            # only use memory chunks that actually define the term
            if re.search(rf"\b{re.escape(term)}\b\s*(\(|is\b|=|refers to|means|:)", h.get("text") or "", re.I):
                mem.append(h)
    except Exception:
        pass
    if not hits and not mem:
        why = f" ({res['error']})" if res.get("error") else ""
        return f"I don't have a definition of **{term}** in my notes, and the web search came back empty{why}, so I won't guess."
    srcs = "\n".join(f"[{i}] {h['title']}: {(h.get('snippet') or '')[:350]} ({h['url']})" for i, h in enumerate(hits, 1))
    memlines = []
    for m in mem[:3]:
        for ln in (m.get("text") or "").splitlines():
            if re.search(rf"\b{re.escape(term)}\b", ln, re.I) and ln.strip() not in memlines:
                memlines.append(ln.strip()[:300])
    memtxt = "\n".join(f"- ({m['file']}) {ln}" for m in mem[:1] for ln in memlines[:3])
    msgs = [{"role": "system", "content": "You are PYTH, the user's analyst. Answer only from the sources given. Never mention internal tool names."},
            {"role": "user", "content": f"Question: {question}\n\nWEB SOURCES:\n{srcs or '(none)'}\n\n"
             + (f"THE USER'S NOTES THAT MENTION IT:\n{memtxt}\n\n" if memtxt else "")
             + f"Define '{term}' in 2-4 sentences from the web sources, citing [n]. If the sources describe different things with the same name, "
               "say so briefly. Keep related projects separate from the term itself (don't say the term IS a project that merely uses it). "
               "If the user's notes relate to it, add one short line starting 'In your notes:'. Don't add prices."}]
    ans = _strip_fake_tools(_clean(ask_model(msgs), True))
    ans = re.sub(r"\s?\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(hits) else "", ans)
    out = f"**{term}**\n\n{ans}"
    if res.get("error"):
        out += f"\n\n_(The web search didn't return results: {res['error']}. This answer uses only your notes.)_"
    if hits:
        out += "\n\nSources (web search):\n" + "\n".join(f"[{i}] [{h['title']}]({h['url']})" for i, h in enumerate(hits[:5], 1))
    return out


_JARGON_RE = re.compile(r"TOOL_RESULTS?|tool[_ ]result", re.I)


def _scrub_jargon(text):
    if not _JARGON_RE.search(text or ""):
        return text
    out = []
    for para in text.split("\n"):
        sents = re.split(r"(?<=[.!?])\s+", para)
        kept = [s for s in sents if not _JARGON_RE.search(s)]
        if kept or not para.strip():
            out.append(" ".join(kept))
    t = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return t or "I don't have live data for that yet. Ask me to check it (e.g. 'price SGL' or 'how's the market') and I'll pull real numbers."


_run_agent_r13 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    reply, route, tools = None, None, []
    if len(user_input) < 200:
        _TRACE["tools"] = []
        ma = math_answer(user_input)
        if ma:
            reply, route, tools = ma[0], "math", [f"safe_math_eval({ma[1]})"]
        else:
            term = _def_term(user_input)
            if term:
                reply, route, tools = define_answer(term, user_input), "definition (web search)", list(_TRACE["tools"])
    if reply is not None:
        reply = _scrub_jargon(reply)
        reply, history = _stamp_reply(user_input, reply, history)
        history[-1].update({"route": route, "tools": tools})
        try:
            add_conversation(user_input[:1500], reply)
        except Exception:
            pass
        return reply, history
    reply, history = _run_agent_r13(user_input, history)
    clean = _scrub_jargon(reply)
    if clean != reply and history and history[-1].get("role") == "assistant":
        history[-1]["content"] = clean
    return clean, history


# ---------------------------------------------------------------------------
# Round 15 (2026-10-15): short previews for stored replies, word-safe 📬 notice.
# ---------------------------------------------------------------------------
def _preview(body, n=150):
    b = re.sub(r"^From:[^\n]*\n+", "", (body or "").strip())
    b = re.sub(r"^PYTH-BRIDGE REPLY #P\d{12}:\s*", "", b)
    b = re.sub(r"\n+Ref: #P\d{12}\s*$", "", b)
    b = re.sub(r"(?m)^\s*\|.*\|\s*$", " ", b)          # never inline table rows
    b = re.sub(r"\s+", " ", b).strip()
    if len(b) <= n:
        return b
    cut = b[:n].rsplit(" ", 1)[0].rstrip(",;:-–")
    return cut + "…"


def _has_table(body):
    return (body or "").count("|") > 20


def _preview_line(r):
    tail = " _(includes a holdings table; say **'show the portfolio Riley sent'** to view it)_" if _has_table(r["body"]) else ""
    return f"* **{r['bot'].title()}** · {r['date']} · {r.get('id') or 'no ref'}: {_preview(r['body'])}{tail}"


_format_replies_r11 = format_replies


def format_replies(res, notify=False):
    if res.get("error") or res.get("replies"):
        return _format_replies_r11(res, notify)
    head = "I'll check every time you send me a message; when a reply lands, a 📬 notice shows up under my next answer.\n\n" if notify else ""
    old = _stored_replies(5)
    if not old:
        return head + "No replies from the team yet."
    return head + "No new replies since my last check. Latest ones I have:\n\n" + "\n".join(_preview_line(r) for r in old[::-1])


def team_reply_notice(min_interval=120):
    import time as _t
    if _t.time() - _LAST_POLL["t"] < min_interval:
        return ""
    _LAST_POLL["t"] = _t.time()
    try:
        res = check_team_replies()
    except Exception:
        return ""
    rs = res.get("replies") or []
    for r in rs:
        _index_reply(r)
    return "\n".join(f"📬 New reply from {r['bot'].title()} ({r.get('id') or 'no ref'}): {_preview(r['body'], 200)}"
                     + (" (includes a holdings table; say 'show the portfolio Riley sent')" if _has_table(r["body"]) else "") for r in rs)


# ---------------------------------------------------------------------------
# Round 17 (2026-10-15): wider team-contact verbs, pending team questions,
# hard anti-fabrication guard for "I contacted Henry" claims, honest challenges.
# ---------------------------------------------------------------------------
_BN17 = r"(riley|henry|henre|gladise|gladys)"
_VERB17_RE = re.compile(r"\b(check with|reach out( to)?|contact|find out( from)?|see (if|when|whether|what|who|where|how)|query|ask|tell|"
                        r"email|e-mail|message|ping|get in touch with|have|send)\b", re.I)
_FOLLOW17_RE = re.compile(r"^\s*(ok(ay)?,?\s*)?(please\s+)?(reach out and ask|go ahead( and (ask|send)( it)?)?|ask (him|her|them)|"
                          r"yes,?\s*(please,?\s*)?(send it|do it|ask)?|yep|yeah|sure|send it|do it|do that|please do|ok send( it)?|"
                          r"reach out|ask away)\s*(please)?[.!]*\s*$", re.I)
_CHALLENGE17_RE = re.compile(r"\b(not true|isn'?t true|don'?t think (that|what you( just)? said|this) is (true|right|correct)|"
                             r"that'?s (wrong|false|made up|a lie|not right)|you made (that|it) up|did you (really|actually)|"
                             r"you didn'?t (send|ask|contact|reach))\b", re.I)
_FAB17_RE = re.compile(rf"\b(I'?ve|I have|I just|I)\s+(already\s+)?(reached out|contacted|messaged|emailed|pinged|asked|spoken|spoke|checked|heard back|heard)\b[^.\n]{{0,40}}\b{_BN17}\b|"
                       rf"\b{_BN17}\s*:\s*[\"“']|\b{_BN17}\s+(says|said|replied|responded|confirmed|told me|is responding|wrote back)\b|"
                       r"\b(he|she)'?s responding\b|\b(he|she) (says|said|replied|responded|confirmed)\b", re.I)

SYSTEM_PROMPT += ("\nTEAM CONTACT RULE: You cannot contact team bots yourself. Never claim you reached out to, asked, emailed or heard from "
                  "Riley, Henry, Gladise or any bot, and never invent or quote their replies. Only the email bridge sends messages; "
                  "if the user wants something asked, offer to send it.\n")


def _canon17(name):
    return _canon_bot(name.lower())


def _question17(text):
    t0 = text.strip()
    # a direct question placed before the bot name: "When's the meeting? Henry would know"
    pre = re.split(rf"\b{_BN17}\b", t0, maxsplit=1, flags=re.I)[0]
    qm = re.search(r"([^.?!]*\?)", pre)
    if qm and len(qm.group(1).split()) >= 3 and not _VERB17_RE.search(qm.group(1)):
        q = qm.group(1).strip()
        q = re.sub(r"^when'?s\b", "When is", q, flags=re.I)
        return q[0].upper() + q[1:]
    # bot as subject: "see if Henry is free Friday" -> "Are you free Friday?"
    sm = re.search(rf"\b{_BN17}\s+(is|has|can|will|would|could)\s+(.+)$", t0, re.I)
    if sm and re.search(rf"\b(if|whether|when|see)\s+{_BN17}\b", t0, re.I):
        v = {"is": "Are", "has": "Have"}.get(sm.group(2).lower(), sm.group(2).title())
        return f"{v} you {sm.group(3).strip(' .?!')}?"
    q = text.strip()
    q = re.sub(r"\b(on|from|in)\s+(our|the)\s+(kryptokora|krypto kora)\s+team\b|\bon (our|the) team\b|\bfrom the team\b", "", q, flags=re.I)
    # drop everything up to and including the "verb ... bot" clause
    q = re.sub(rf"^.*?\b{_BN17}\b[\s,:-]*", "", q, count=1, flags=re.I | re.S) if re.search(rf"\b{_BN17}\b", q, re.I) else q
    q = re.sub(r"^(and\s+)?(please\s+)?(see|find out|ask|check|query|tell (him|her|them))\s*(if|whether)?\s*", r"\5 ", q, flags=re.I).strip()
    q = re.sub(r"^(to|that|about|for)\s+", lambda m: "" if m.group(1).lower() in ("to", "that") else m.group(0), q, flags=re.I).strip()
    q = re.sub(r"\s+", " ", q).strip(" .,!")
    if not q:
        return ""
    m = re.match(r"^(when|where|what|who|how|why|which)\s+(.+?)\s+(is|are|was|were|will be)\s*\??$", q, re.I)
    if m:
        q = f"{m.group(1)} {m.group(3)} {m.group(2)}"
    keep = {"riley", "henry", "gladise", "pyth", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    words = q.split(" ")
    words = [w if (i == 0 or not re.fullmatch(r"[A-Z][a-z]+", w) or w.lower() in keep) else w.lower() for i, w in enumerate(words)]
    q = " ".join(words)
    q = q[0].upper() + q[1:]
    if re.match(r"^(when|where|what|who|how|why|which|is|are|do|does|did|can|could|will|would|should|has|have)\b", q, re.I) and not q.endswith("?"):
        q = q.rstrip(".") + "?"
    if re.match(r"^(if|whether)\s+", q, re.I):
        q = "Can you tell me whether " + re.sub(r"^(if|whether)\s+", "", q, flags=re.I).rstrip("?") + "?"
    return q


def _is_inbox17(t):
    return bool(_INBOX11_RE.search(t) or _REPLIES_RE.search(t) or _HOLD11_RE.search(t))


def _send17(bot, question, history, user_input):
    res = contact_team_bot(bot, question)
    if res.get("ok"):
        reply = (f"Sent to {bot.title()} via the team inbox ({res['id']}). Replies usually take a few minutes; "
                 f"ask \"any replies from the team?\" to check.\n\n> {question}")
        meta = {"route": "team message", "tools": ["send_to_team"], "team_sent": {"bot": bot, "question": question, "id": res.get("id")}}
    else:
        reply = f"I tried to email {bot.title()} but it failed ({res.get('status')}). Nothing was sent."
        meta = {"route": "team message", "tools": ["send_to_team"], "pending_offer": {"type": "team_send", "bot": bot, "question": question}}
    return reply, meta


def _recent_team_ask(history):
    """Last bot + question mentioned by the user in the recent turns."""
    for m in reversed((history or [])[-8:]):
        if m.get("role") == "assistant" and (m.get("pending_offer") or {}).get("type") == "team_send":
            return m["pending_offer"]["bot"], m["pending_offer"]["question"]
        if m.get("role") == "user":
            b = re.search(rf"\b{_BN17}\b", m["content"], re.I)
            if b:
                q = _question17(m["content"])
                if q:
                    return _canon17(b.group(1)), q
    return None


def _offer17(bot, q):
    return f"Want me to send \"{q}\" to {bot.title()}?" if q else f"What should I ask {bot.title()}?"


_run_agent_r16 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    t = user_input.strip()
    reply, meta = None, {}
    last = next((m for m in reversed(history) if m.get("role") == "assistant"), {}) or {}
    pend = last.get("pending_offer") if (last.get("pending_offer") or {}).get("type") == "team_send" else None
    bot_m = re.search(rf"\b{_BN17}\b", t, re.I)
    if len(t) < 600 and bot_m and _VERB17_RE.search(t) and not _is_inbox17(t):
        bot, q = _canon17(bot_m.group(1)), _question17(t)
        if q:
            reply, meta = _send17(bot, q, history, t)
        else:
            reply = f"Sure. {_offer17(bot, '')}"
            meta = {"route": "team offer", "pending_offer": {"type": "team_send", "bot": bot, "question": ""}}
    elif len(t) < 200 and (_FOLLOW17_RE.match(t) or _CHALLENGE17_RE.search(t)) and last.get("team_sent"):
        ts = last["team_sent"]
        reply = (f"That part is real: I did send \"{ts['question']}\" to {ts['bot'].title()} via the team inbox ({ts.get('id')}). "
                 f"There's no reply yet; I'll show a 📬 notice when one lands, or ask \"any replies from the team?\".")
        meta = {"route": "team sent recap", "team_sent": ts}
    elif len(t) < 80 and _FOLLOW17_RE.match(t) and (pend or _recent_team_ask(history)):
        bot, q = (pend["bot"], pend["question"]) if pend and pend.get("question") else (_recent_team_ask(history) or (None, None))
        if bot and q:
            reply, meta = _send17(bot, q, history, t)
    elif len(t) < 200 and _CHALLENGE17_RE.search(t) and (last.get("fabricated") or _FAB17_RE.search(last.get("content") or "")):
        ra = _recent_team_ask(history)
        bot, q = ra if ra else ("the team", "")
        sent_any = any(m.get("team_sent") for m in history[-10:] if m.get("role") == "assistant")
        reply = ("You're right, and I'm sorry. That wasn't true: I never contacted "
                 f"{bot.title() if bot != 'the team' else 'anyone on the team'}, "
                 + ("and nothing was sent in that exchange" if not sent_any else "and the earlier message is the only one actually sent")
                 + ", so there is no reply. Any quote I gave was made up. "
                 + (_offer17(bot, q) if ra else "Tell me who to ask and what, and I'll send it for real."))
        meta = {"route": "honest correction"}
        if ra:
            meta["pending_offer"] = {"type": "team_send", "bot": bot, "question": q}
    if reply is not None:
        reply, history = _stamp_reply(user_input, reply, history)
        history[-1].update(meta)
        try:
            add_conversation(user_input[:1500], reply)
        except Exception:
            pass
        return reply, history
    reply, history = _run_agent_r16(user_input, history)
    h = history[-1] if history else {}
    real = h.get("route") in ("team message", "team inbox check", "team holdings") or "send_to_team" in (h.get("tools") or [])
    sent_recent = next((m.get("team_sent") for m in reversed(history[:-1][-8:]) if m.get("role") == "assistant" and m.get("team_sent")), None)
    if not real and sent_recent and (re.search(r"\b(didn'?t|did not|never|haven'?t)\s+(actually\s+|really\s+)?(send|sent|contact|reach|ask)|nothing was sent|made a mistake|claiming I already", reply or "", re.I)
                                    or re.search(rf"\b{sent_recent['bot']}\b", reply or "", re.I)):
        ts = sent_recent
        reply = (f"Ha. For the record, the message is real: I sent \"{ts['question']}\" to {ts['bot'].title()} ({ts.get('id')}). "
                 "No reply yet; a 📬 notice will show up when it lands.")
        h.update({"content": reply, "route": "team sent recap"})
        return reply, history
    if not real and _FAB17_RE.search(reply or ""):
        ra = _recent_team_ask(history[:-1])
        bm = re.search(rf"\b{_BN17}\b", reply, re.I)
        bot = ra[0] if ra else (_canon17(bm.group(1)) if bm else "the team")
        q = ra[1] if ra else ""
        reply = (f"I haven't contacted {bot.title() if bot != 'the team' else 'anyone on the team'}. Nothing was sent, and I don't have a reply from them, "
                 f"so I can't tell you what they said. {_offer17(bot, q) if bot != 'the team' else 'Tell me who to ask and I will send it.'}")
        h.update({"content": reply, "fabricated": True, "route": "fabrication guard",
                  "pending_offer": {"type": "team_send", "bot": bot, "question": q} if bot != "the team" else None})
    return reply, history


# ---------------------------------------------------------------------------
# Round 18 (2026-10-15): philosophical / common-word "what is" questions go to
# the LLM; only unknown proper nouns or terms trigger a web definition search.
# ---------------------------------------------------------------------------
_PHILO18_RE = re.compile(r"\b(meaning|purpose|point|nature|essence|secret) of\b|\b(life|love|happiness|death|god|consciousness|"
                         r"the universe|existence|reality|truth|beauty|justice|freedom|time itself|the soul|morality|success|wisdom)\b", re.I)
try:
    _DICT18 = {w.strip().lower() for w in open("/usr/share/dict/words", encoding="utf-8", errors="ignore") if w.strip().isalpha()}
except Exception:
    _DICT18 = set()
_def_term_r14 = _def_term


def _def_term(text):
    term = _def_term_r14(text)
    if not term:
        return None
    if _PHILO18_RE.search(term):
        return None
    words = term.split()
    looks_term = bool(re.search(r"[A-Z0-9\-_.]", term)) or any(w.lower() not in _DICT18 for w in words if w.isalpha()) \
        or any(not w.isalpha() for w in words)
    if not looks_term:  # plain lowercase dictionary words ("what is entropy") -> LLM answers directly
        return None
    return term


# ---------------------------------------------------------------------------
# Round 19 (2026-10-15): market summary never silently drops lines (REST
# fallback + explicit "unavailable" notes); portfolio vs market uses
# data/portfolio.json aggregated by asset.
# ---------------------------------------------------------------------------
def _rest_markets(n=50):
    r = requests.get(f"{CG_REST}/coins/markets", params={"vs_currency": "usd", "per_page": n, "page": 1,
                     "price_change_percentage": "24h"}, timeout=15)
    r.raise_for_status()
    return [{"id": x["id"], "symbol": x["symbol"], "usd": x.get("current_price"), "change_24h": x.get("price_change_percentage_24h"),
             "mcap": x.get("market_cap")} for x in r.json()]


def market_overview():
    out = {"problems": []}
    snap = None
    try:
        snap = get_market_snapshot(per_page=50)
    except Exception as e:
        snap = {"error": str(e)}
    rows = [r for r in ((snap or {}).get("markets") or []) if isinstance(r, dict)]
    if not rows:
        why = (snap or {}).get("error") or "empty response"
        try:
            rows = _rest_markets(50)
            out["problems"].append(f"CoinGecko MCP markets call failed ({str(why)[:80]}); used CoinGecko REST instead")
        except Exception as e:
            out["problems"].append(f"coin prices and top-50 movers unavailable (MCP: {str(why)[:80]}; REST: {str(e)[:80]})")
    out["snapshot"] = {"markets": rows}
    try:
        out["global"] = requests.get(f"{CG_REST}/global", timeout=15).json().get("data") or {}
    except Exception as e:
        out["global"] = {"error": str(e)}
    try:
        out["fng"] = (requests.get("https://api.alternative.me/fng/", timeout=15).json().get("data") or [{}])[0]
    except Exception as e:
        out["fng"] = {"error": str(e)}
    return out


market_overview = _traced("market_overview", market_overview)
_format_market_r5 = format_market


def format_market(m):
    txt = _format_market_r5(m)
    notes = list(m.get("problems") or [])
    g = m.get("global") or {}
    if not g.get("total_market_cap"):
        notes.append(f"total market cap unavailable ({str(g.get('error') or 'CoinGecko /global returned no data')[:80]})")
    rows = (m.get("snapshot") or {}).get("markets") or []
    if rows:
        for sym in ("btc", "eth", "sol"):
            if not any(r.get("symbol") == sym for r in rows):
                notes.append(f"{sym.upper()} missing from the CoinGecko response")
    if not (m.get("fng") or {}).get("value"):
        notes.append("Fear & Greed unavailable")
    if notes:
        txt += "\n\n_Data gaps: " + "; ".join(dict.fromkeys(notes)) + "._"
    return txt


_portfolio_vs_market_r8 = portfolio_vs_market


def portfolio_vs_market():
    d = _pf.load()
    if not d:
        return _portfolio_vs_market_watchlist() + "\n\n_(data/portfolio.json missing; used watchlist.json.)_"
    stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    ids = [p["coingecko"] for p in d["positions"] if p.get("coingecko")]
    px, err = _pf.live_prices(ids)
    agg, flags, floor_v = {}, [], 0.0
    for p in d["positions"]:
        if p["flag"] == "floor":
            floor_v += p.get("table_value") or 0
            continue
        if p["flag"] == "unvalued":
            flags.append(f"{p['asset']} ({p['holder']}): not priced, not counted")
            continue
        sym = p["symbol"]
        a = agg.setdefault(sym, {"qty": 0.0, "px": None, "chg": None, "stale": False, "value": 0.0})
        a["qty"] += p.get("amount") or 0
        live = px.get(p.get("coingecko") or "", {})
        if live.get("usd") is not None:
            a["px"], a["chg"] = live["usd"], live.get("usd_24h_change")
            a["value"] += (p.get("amount") or 0) * live["usd"]
        else:
            a["stale"] = True
            a["px"] = a["px"] or p.get("table_price")
            a["value"] += p.get("table_value") if p.get("table_value") is not None else (p.get("amount") or 0) * (p.get("table_price") or 0)
    for s, a in agg.items():
        if a["stale"]:
            flags.append(f"{s}: no live price (no CoinGecko id), Riley's table price used, 24h change unknown and treated as 0%")
    if floor_v:
        flags.append(f"NFT/gaming floor estimates (~${floor_v:,.2f}) are included in the total but have no 24h change")
    rows = sorted(agg.items(), key=lambda kv: -kv[1]["value"])
    now_v = sum(a["value"] for _, a in rows) + floor_v
    prev_v, contrib = floor_v, []
    for s, a in rows:
        c = a["chg"] if isinstance(a["chg"], (int, float)) else 0.0
        pv = a["value"] / (1 + c / 100)
        prev_v += pv
        contrib.append((s, a["value"] - pv, c))
    bag_chg = (now_v - prev_v) / prev_v * 100 if prev_v else 0.0
    L = [f"**Portfolio vs market ({stamp})** — Riley's holdings table, all holders combined", "",
         "| Asset | Qty | Price USD | 24h % | Value USD |", "|---|---|---|---|---|"]
    for s, a in rows:
        chg = f"{a['chg']:+.2f}%" if isinstance(a["chg"], (int, float)) else "n/a"
        L.append(f"| {s}{' ⚠️' if a['stale'] else ''} | {a['qty']:,.6g} | {_pf._px(a['px'])} | {chg} | ${a['value']:,.2f} |")
    if floor_v:
        L.append(f"| NFTs/gaming (floor) | — | floor | n/a | ~${floor_v:,.2f} |")
    L.append(f"| **TOTAL** | | | | **${now_v:,.2f}** |")
    m = market_overview()
    g = m.get("global") or {}
    mcap_chg = g.get("market_cap_change_percentage_24h_usd")
    btc = next((r.get("change_24h") for r in (m.get("snapshot") or {}).get("markets") or [] if r.get("symbol") == "btc"), None)
    if btc is None and isinstance((px.get("bitcoin") or {}).get("usd_24h_change"), (int, float)):
        btc = px["bitcoin"]["usd_24h_change"]
    contrib.sort(key=lambda x: x[1])
    top = ", ".join(f"{s} {'+' if v >= 0 else '-'}${abs(v):,.0f} ({c:+.2f}%)" for s, v, c in contrib[::-1][:3] if v > 0) or "none"
    bot = ", ".join(f"{s} -${abs(v):,.0f} ({c:+.2f}%)" for s, v, c in contrib[:3] if v < 0) or "none"
    L += ["", f"* **Your bag:** ${now_v:,.2f}, {bag_chg:+.2f}% over 24h ({'+' if now_v >= prev_v else '-'}${abs(now_v - prev_v):,.2f})"]
    L.append(f"* **BTC 24h:** {btc:+.2f}%" if isinstance(btc, (int, float)) else "* **BTC 24h:** unavailable")
    L.append(f"* **Total crypto market cap 24h:** {mcap_chg:+.2f}%" if isinstance(mcap_chg, (int, float)) else
             f"* **Total crypto market cap 24h:** unavailable ({str(g.get('error') or 'no data')[:60]})")
    L += [f"* **Top contributors:** {top}", f"* **Biggest drags:** {bot}", ""]
    take = []
    if isinstance(mcap_chg, (int, float)):
        diff = bag_chg - mcap_chg
        take.append(f"Your portfolio is {'outperforming' if diff >= 0 else 'underperforming'} the broader market by "
                    f"{abs(diff):.2f} percentage points today ({bag_chg:+.2f}% vs {mcap_chg:+.2f}%).")
    if isinstance(btc, (int, float)):
        take.append(f"Against BTC alone you're {'ahead' if bag_chg >= btc else 'behind'} ({bag_chg:+.2f}% vs {btc:+.2f}%).")
    if contrib:
        take.append(f"The move is mostly driven by {contrib[::-1][0][0]} on the upside and {contrib[0][0]} on the downside.")
    L.append(" ".join(take))
    if err:
        flags.append(f"live pricing failed ({err}); table prices used")
    if flags:
        L += ["", "**Flags:**"] + [f"* {f}" for f in dict.fromkeys(flags)]
    return "\n".join(L)


_portfolio_vs_market_watchlist = globals().get("_portfolio_vs_market_r8") or (lambda: "Portfolio prices unavailable right now.")
portfolio_vs_market = _traced("portfolio_vs_market", portfolio_vs_market)


# ---------------------------------------------------------------------------
# Round 20 (2026-10-15): named-document honesty (fuzzy file match or "upload
# it?"), per-file grounding, correction checks against the document text.
# ---------------------------------------------------------------------------
import difflib as _dl
from tools.doc_index import list_documents as _list_docs, index_file as _index_file, _db as _di_db

_DOCWORD20 = r"(letter|lor|document|doc|file|certificate|cert|citation|award|report|evaluation|write-?up|reference|appraisal|plan|pdf|jpg|scan|medal|transcript|record|orders|service.?record)"
_DOCQ20_RE = re.compile(rf"\b(read|what does|what do|summar\w*|indicate|say|says|tell me about|show me|open|look at|explain|go over|review)\b.*\b{_DOCWORD20}\b|"
                        rf"\b{_DOCWORD20}\b.*\b(read it|say|says|indicate|summar\w*|mean)\b", re.I)
_GENERIC20 = set("read what does do say says indicate tell me about show open look at explain the a an my his her this that it please summarize "
                 "summary summarise go over review letter lor document doc file certificate cert citation award report evaluation write up "
                 "writeup reference ref appraisal plan pdf jpg scan medal transcript record orders copy from of for in on to and is was "
                 "mean means".split())


def _file_tokens(name):
    stem = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I)
    stem = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    stem = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", stem)
    return [t.lower() for t in re.split(r"[\W_]+", stem) if t]


def _docs20():
    seen, out = set(), []
    for d in _list_docs():
        n = d["file"]
        if n.lower() in seen or d["path"].startswith(("team://", "pasted://", "conv://")):
            continue
        seen.add(n.lower())
        out.append(d)
    return out


def match_document(text):
    """Fuzzy-match a named document in the user's message. Returns (doc|None, name_tokens)."""
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", text.lower()) if t not in _GENERIC20 and len(t) >= 3]
    if not toks:
        return None, []
    best, best_s = None, 0.0
    for d in _docs20():
        ft = _file_tokens(d["file"])
        s = 0.0
        for q in toks:
            r = max((_dl.SequenceMatcher(None, q, f).ratio() for f in ft), default=0)
            if r >= 0.8:
                s += r
        if s > best_s:
            best, best_s = d, s
    return (best if best_s >= 0.8 else None), toks


def _doc_text20(d):
    from tools.doc_index import file_text
    txt = file_text(d["path"])
    if not txt:
        con = _di_db()
        row = con.execute("SELECT status FROM files WHERE path=?", (d["path"],)).fetchone()
        m = re.match(r"duplicate of (.+)$", (row or [""])[0] or "")
        if m:
            p = con.execute("SELECT path FROM files WHERE path LIKE ? AND status='ok'", (f"%/{m.group(1)}",)).fetchone()
            if p:
                txt = file_text(p[0])
    return txt


def named_doc_answer(question, d):
    text = _doc_text20(d)
    if not text.strip():
        return f"I have **{d['file']}** on file, but no readable text from it, so I won't describe it. Want me to re-run OCR on it?", None
    msgs = [{"role": "system", "content": "You are PYTH, the user's analyst. You describe ONLY the document text given. Never add facts."},
            {"role": "user", "content": f"FILE: {d['file']}\nTEXT (OCR, may have small errors):\n<<<\n{text[:12000]}\n>>>\n\nUser asks: {question}\n\n"
             "Answer from this text only. Say who wrote/signed it, to whom, the date, and the key points, quoting short phrases. "
             "If the file name contains a different person's surname than the signer (e.g. file 'Smith_LOR' signed by Jones), point that out; otherwise don't comment on the file name. If something truly isn't in the text, "
             "say so plainly; never say 'not explicitly stated' about anything that appears in the text."}]
    ans = _strip_fake_tools(_clean(ask_model(msgs), True))
    ans = _verify_years(ans, text)
    return f"{ans}\n\nSource: {d['file']}", text


_CORR20_RE = re.compile(r"^\s*(no[,.!]?\s+|nope[,.!]?\s+|actually[,.]?\s+|wrong[,.!]?\s+|that'?s wrong[,.!]?\s+|you'?re wrong[,.!]?\s+)?"
                        r"(it (was|is|says|said)|the (operation|op|name|date|unit|award) (was|is)|it'?s actually|actually it'?s|it should be)\s+(?P<claim>.+?)\s*[.!]*$", re.I)


def correction_check(claim, d, text):
    c = claim.strip(" \"'“”")
    words = [w for w in re.findall(r"[A-Za-z0-9]+", c) if len(w) > 2]
    low = text.lower()
    found = [w for w in words if w.lower() in low]
    if words and len(found) == len(words) and c.lower() in re.sub(r"\s+", " ", low):
        i = re.sub(r"\s+", " ", low).find(c.lower())
        snip = re.sub(r"\s+", " ", text)[max(0, i - 60): i + len(c) + 60]
        return f"You're right, the document supports that: it says \"…{snip.strip()}…\" ({d['file']})."
    if found:
        return (f"Partly: I can find {', '.join(found)} in {d['file']}, but not \"{c}\" as a phrase. "
                "The text may differ or the OCR may have garbled it; I'm not going to change my reading without the document showing it.")
    return (f"I can't confirm that from the document. {d['file']} doesn't contain \"{c}\" anywhere in its text"
            + (" (the OCR of that part may be incomplete)" if len(text) < 2000 else "") + ". I'll go with what the document shows unless you have another source.")


_run_agent_r19 = run_agent


def run_agent(user_input: str, history=None):
    if history is None:
        history = []
    t = user_input.strip()
    last = next((m for m in reversed(history) if m.get("role") == "assistant"), {}) or {}
    reply, meta = None, {}
    is_path = bool(re.search(r"(/home/|data/uploads/|\.(pdf|jpe?g|png|docx|txt)\b)", t, re.I) and re.search(r"/", t))
    if not is_path and len(t) < 300 and last.get("doc_file") and _CORR20_RE.match(t):
        d = {"file": last["doc_file"], "path": last.get("doc_path")}
        text = _doc_text20(d) or ""
        claim = _CORR20_RE.match(t).group("claim")
        reply, meta = correction_check(claim, d, text), {"route": "document correction check", "doc_file": d["file"], "doc_path": d["path"]}
    elif not is_path and len(t) < 300 and _DOCQ20_RE.search(t) and not _is_inbox17(t) and not re.search(rf"\b{_BN17}\b", t, re.I):
        d, toks = match_document(t)
        if d:
            reply, _txt = named_doc_answer(t, d)
            meta = {"route": "named document", "doc_file": d["file"], "doc_path": d["path"], "tools": [f"file_text({d['file']})"]}
        elif last.get("doc_file") and not toks or (last.get("doc_file") and re.search(r"\b(it|that|this)\b", t, re.I)):
            d = {"file": last["doc_file"], "path": last.get("doc_path")}
            reply, _txt = named_doc_answer(t, d)
            meta = {"route": "named document (previous)", "doc_file": d["file"], "doc_path": d["path"]}
        elif toks or not last.get("doc_file"):
            names = ", ".join(sorted({x['file'] for x in _docs20() if re.search(r'\.(pdf|jpe?g|png|docx)$', x['file'], re.I)})[:12])
            reply = (f"I don't have that document - upload it? I couldn't match \"{' '.join(toks) or 'that'}\" to anything in my index. "
                     f"Files I do have include: {names}.")
            meta = {"route": "document not found"}
    if reply is not None:
        reply, history = _stamp_reply(user_input, reply, history)
        history[-1].update(meta)
        try:
            add_conversation(user_input[:1500], reply)
        except Exception:
            pass
        return reply, history
    reply, history = _run_agent_r19(user_input, history)
    # remember which single file an upload / file answer used, for follow-up corrections
    m = re.search(r"Source:\s*([^\s(]+\.(?:pdf|jpe?g|png|docx|txt))", reply or "", re.I)
    if m and history:
        d = next((x for x in _docs20() if x["file"] == m.group(1)), None)
        if d:
            history[-1].update({"doc_file": d["file"], "doc_path": d["path"]})
    return reply, history


# ---------------------------------------------------------------------------
# Round 21 (2026-10-26): honest "add that file to memory". Checks the real index
# instead of letting the model claim an action it never took.
# ---------------------------------------------------------------------------
_MEMADD21_RE = re.compile(r"\b(add|save|put|store|keep)\b.{0,30}\b(file|doc|document|it|that|this)\b.{0,30}\b(memory|index|recall)\b|\bremember (that|this) (file|doc|document)\b", re.I)
_CLAIM21_RE = re.compile(r"\bI(?:'ve| have)? (?:just )?(?:added|saved)\b[^.\n]{0,80}?\bto (?:my )?memory\b", re.I)
_MEMTOOLS21 = ("remember_fact", "index_file", "add_fact")
_run_agent_r20 = run_agent


def _last_file21(history):
    for m in reversed(history or []):
        if m.get("doc_path"):
            return m["doc_path"]
        hit = re.search(r"(/home/\S+\.(?:pdf|jpe?g|png|docx|txt))", m.get("content") or "", re.I)
        if hit:
            return hit.group(1)
    try:
        from tools.doc_index import _db
        r = _db().execute("SELECT path FROM files WHERE path LIKE '%/data/uploads/%' ORDER BY mtime DESC LIMIT 1").fetchone()
        return r[0] if r else None
    except Exception:
        return None


def run_agent(user_input: str, history=None):
    history = history if history is not None else []
    t = user_input.strip()
    if len(t) < 200 and _MEMADD21_RE.search(t):
        p = _last_file21(history)
        if not p:
            reply = "I'm not sure which file you mean. Drop it on the screen again and I'll index it."
        else:
            from tools.doc_index import _db, index_file
            con = _db()
            fp = Path(p)
            status = index_file(con, fp) if fp.exists() else "missing"
            row = con.execute("SELECT status, chars FROM files WHERE path=?", (p,)).fetchone()
            st = row[0] if row else status
            if row and (st == "ok" or st.startswith("duplicate of")):
                chars = row[1]
                if st.startswith("duplicate of"):
                    src = con.execute("SELECT chars FROM files WHERE path LIKE ? AND status='ok'", (f"%/{st[13:]}",)).fetchone()
                    chars = src[0] if src else chars
                    st = f"same content as {st[13:]}"
                reply = (f"Done. **{fp.name}** is in my document index ({st}, {chars} chars of text), "
                         "so I can recall it in later questions.")
            else:
                reply = f"I could not add **{fp.name}** to memory: index status is '{st}'."
        reply, history = _stamp_reply(user_input, reply, history)
        history[-1].update({"route": "memory add (index check)", "tools": ["index_file"]})
        return reply, history
    reply, history = _run_agent_r20(user_input, history)
    tools = (history[-1].get("tools") if history else None) or []
    mem_ran = any(any(k in str(x) for k in _MEMTOOLS21) for x in tools)
    if reply and _CLAIM21_RE.search(reply) and not mem_ran:
        reply = re.sub(r"[^.!?\n]*" + _CLAIM21_RE.pattern + r"[^.!?\n]*[.!?]?", "(Note: nothing was saved to memory; no save step ran. Say \"add that file to memory\" to index a file.)", reply, count=1, flags=re.I)
        if history:
            history[-1]["content"] = reply
    return reply, history


# ---------------------------------------------------------------------------
# Round 22 (2026-10-31): general outbound email with mandatory confirmation.
# Compose → pending draft (To/Subject/Body) → send only on 'send'/'send it'/
# SEND EMAIL button. cancel discards; change subject/body/to edits. One draft,
# 30 min TTL. Team bots stay on team_bridge. Router keeps these local (never news).
# ---------------------------------------------------------------------------
from tools import email_send as _em22

_run_agent_r21 = run_agent

# Capture "the address is X" / "email them at X" after a need-address ask.
_ADDR_FOLLOW22_RE = re.compile(
    r"^\s*(?:(?:the\s+)?(?:address|email|e-mail)(?:\s+is|\s+at)?\s+|it's\s+|use\s+)?"
    r"(?P<addr>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*[.!]*\s*$",
    re.I,
)


def _email22_handle(user_input: str, history):
    """Return (reply, meta) if handled, else (None, None). Never sends without confirm."""
    t = (user_input or "").strip()
    last = next((m for m in reversed(history or []) if m.get("role") == "assistant"), {}) or {}
    draft = _em22.load_draft()

    # Confirm / cancel / edit when a draft is pending (or just expired → honest message).
    if draft and _em22.is_confirm(t):
        res = _em22.send_draft(draft)
        if res.get("status") == "sent":
            _em22.clear_draft()
            reply = (f"Sent to **{res['to']}**.\n"
                     f"Subject: {res['subject']}\n\n"
                     "_(Logged in logs/email_sent.csv — time/to/subject only.)_")
            return reply, {"route": "email send", "tools": ["email_send.send"]}
        return (f"SMTP send failed ({res.get('error')}). Draft kept — fix and say **send** again, or **cancel**.",
                {"route": "email send failed", "tools": ["email_send.send"]})

    if _em22.is_cancel(t):
        if draft:
            _em22.clear_draft()
            return "Pending email draft discarded. Nothing was sent.", {"route": "email cancel"}
        # No draft: let other handlers (team follow-ups, etc.) see "cancel" only if we don't claim it.
        # Bare cancel with no draft → short ack so we don't fall through to LLM inventing a send.
        if re.match(r"^\s*cancel\s*[.!?]*\s*$", t, re.I):
            return "No pending email draft to cancel.", {"route": "email cancel noop"}
        return None, None

    if draft:
        edited, err = _em22.apply_edit(draft, t)
        if err:
            return err, {"route": "email edit need address"}
        if edited is not None:
            # keep original created_et so TTL doesn't reset forever on edits
            edited["created_et"] = draft.get("created_et") or edited.get("created_et")
            saved = _em22.save_draft(edited)
            return _em22.format_pending(saved), {"route": "email edit", "tools": ["email_send.draft"]}

    # Confirm with no draft (voice mishear / button with nothing pending) — never send.
    if _em22.is_confirm(t):
        return ("There's no pending email draft to send. "
                "Say e.g. `email bob@x.com about …` or `send an email to bob saying …` first.",
                {"route": "email send noop"})

    # Follow-up address after "I don't have an email for X"
    need = last.get("email_need") if isinstance(last.get("email_need"), dict) else None
    if need and len(t) < 200:
        am = _ADDR_FOLLOW22_RE.match(t)
        if am:
            addr = am.group("addr")
            topic = need.get("topic") or ""
            subj, body = _em22.default_subject_body(topic)
            if topic:
                polished = _em22.polish_with_near(topic, addr)
                source = "local"
                if polished:
                    subj, body = polished
                    source = "near"
            else:
                source = "local"
            saved = _em22.save_draft({"to": addr, "subject": subj, "body": body, "source": source})
            return _em22.format_pending(saved), {"route": "email draft", "tools": ["email_send.draft"]}

    # Compose (deterministic parse; NEAR optional for wording only).
    # Skip if this looks like a team-bot contact (Riley/Henry/Gladise) — bridge owns those.
    if re.search(rf"\b{_BN17}\b", t, re.I) and _VERB17_RE.search(t) and "@" not in t:
        return None, None
    if _em22.COMPOSE_RE.match(t) or re.match(
        r"^\s*(?:please\s+)?(?:send|draft|write|compose)\s+(?:an?\s+)?e-?mails?\b", t, re.I
    ) or re.match(r"^\s*(?:please\s+)?e-?mail\s+\S+", t, re.I):
        result = _em22.try_compose_and_draft(t, use_near=True)
        if not result.get("handled"):
            return None, None
        meta = {"route": result.get("route") or "email draft", "tools": result.get("tools") or []}
        if result.get("pending_topic") is not None or result.get("ask"):
            meta["email_need"] = {
                "topic": result.get("pending_topic") or "",
                "raw_to": result.get("pending_raw_to") or "",
            }
        return result["reply"], meta
    return None, None


def run_agent(user_input: str, history=None):
    history = history if history is not None else []
    try:
        reply, meta = _email22_handle(user_input, history)
    except Exception as e:
        logger.warning("email_send handler error: %s", e)
        reply, meta = None, None
    if reply is not None:
        reply, history = _stamp_reply(user_input, reply, history)
        history[-1].update(meta or {})
        try:
            add_conversation(user_input[:1500], reply)
        except Exception:
            pass
        return reply, history
    return _run_agent_r21(user_input, history)
