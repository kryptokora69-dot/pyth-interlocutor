"""Send one message (plus a little recent context) to Orbio's OpenAI-compatible API.
Key: ORBIO_API_KEY from env or ~/.config/pith/orbio.env (never logged).
Log: ~/.local/share/pith/orbio_calls.csv (time, model, tokens, cost, latency, status; no prompt text).
Usage log: ~/pyth/logs/pith_usage.csv (spec: box /workspace/feeds/pith_usage_README.md; no prompt text, no key).
ask_external -> qwen (reasoning off); ask_news -> perplexity/sonar (web search)."""
import csv, os, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

BASE_URL = "https://api.orbio.so/api/v1"
BALANCE_URL = "https://www.orbio.so/api/v1/key"   # footer balance, read after each reply
DEFAULT_MODEL = "qwen/qwen3.7-flash"     # ~$0.03 in / $0.13 out per 1M tokens
NEWS_MODEL = "perplexity/sonar"          # web search; ~0.007 CREDIT per query
PRICES = {"qwen/qwen3.7-flash": (0.03, 0.13), "perplexity/sonar": (1.0, 1.0)}  # USD per 1M tokens, footer fallback only if usage.cost absent
TIMEOUT = 60
MAX_TOKENS_DEFAULT = 4000  # override with ORBIO_MAX_TOKENS
NEWS_MAX_TOKENS = 1500
DAILY_CAP_DEFAULT = 0.25  # USD per day (ET); override with ORBIO_DAILY_CAP_USD
REASONING = {"enabled": False}  # qwen: no thinking tokens; ORBIO_REASONING_EFFORT=low|medium|high to cap instead
_REPO = Path(__file__).resolve().parent
ENV_FILE = Path(__import__("os").environ.get("ORBIO_ENV_FILE") or (Path.home() / ".config" / "pith" / "orbio.env"))
LOG_FILE = Path(__import__("os").environ.get("ORBIO_LOG_FILE") or (_REPO / "logs" / "orbio_calls.csv"))
USAGE_CSV = Path(__import__("os").environ.get("PITH_USAGE_CSV") or (_REPO / "logs" / "pith_usage.csv"))
USAGE_HEADER = ["timestamp_et", "model", "provider", "input_tokens", "output_tokens", "cost_credit", "latency_s", "query_chars", "source"]
ET = ZoneInfo("America/New_York")
SYSTEM = "You are a strong external model helping PYTH, a local assistant. Answer clearly and concisely. You cannot browse; use only the context given."
NEWS_SYSTEM = "You are a news researcher helping PYTH, a local assistant. Search the web and answer with the latest relevant news: concise bullet points, each with its date and source. Say so if nothing recent is found."


def _config():
    cfg = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    key = os.environ.get("ORBIO_API_KEY") or cfg.get("ORBIO_API_KEY", "")
    model = os.environ.get("ORBIO_MODEL") or cfg.get("ORBIO_MODEL") or DEFAULT_MODEL
    news_model = os.environ.get("ORBIO_NEWS_MODEL") or cfg.get("ORBIO_NEWS_MODEL") or NEWS_MODEL
    return key, model, news_model


def _log(model, tin, tout, cost, secs, status):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        new = not LOG_FILE.exists()
        with LOG_FILE.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "model", "tokens_in", "tokens_out", "cost_usd", "seconds", "status"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), model, tin, tout, f"{cost:.6f}", f"{secs:.1f}", status])
    except Exception as e:
        print(f"[orbio log error] {type(e).__name__}")


def _num(x):
    """Plain decimal string, no exponent, no trailing zeros."""
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _usage(ts, model, provider, tin, tout, cost, secs, qchars, source):
    """Append one row to ~/pyth/logs/pith_usage.csv. cost=None -> blank (unknown, not estimated)."""
    try:
        USAGE_CSV.parent.mkdir(parents=True, exist_ok=True)
        new = not USAGE_CSV.exists() or USAGE_CSV.stat().st_size == 0
        with USAGE_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(USAGE_HEADER)
            w.writerow([ts, model, provider or "orbio", int(tin), int(tout),
                        "" if cost is None else _num(cost), f"{secs:.2f}", int(qchars), source])
    except Exception as e:
        print(f"[pith usage log error] {type(e).__name__}")


def _balance(resp, key):
    """Spendable CREDIT after the reply: GET /api/v1/key -> balance.available. None if unavailable.
    (X-Orbio-Balance header is the pre-request figure and can include other jobs' holds, so it is not used.)"""
    try:
        r = requests.get(BALANCE_URL, timeout=10, headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            b = (r.json() or {}).get("balance")
            b = b.get("available") if isinstance(b, dict) else b
            if b is not None:
                return float(b)
    except Exception as e:
        print(f"[orbio balance error] {type(e).__name__}")
    return None


def _exact_cost(d, resp):
    """What Orbio charged (CREDIT): usage.cost, else body cost.credit, else X-Orbio-Cost header. None if unknown."""
    for v in ((d.get("usage") or {}).get("cost"),
              (d.get("cost") or {}).get("credit") if isinstance(d.get("cost"), dict) else None,
              resp.headers.get("X-Orbio-Cost")):
        try:
            if v not in (None, ""):
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _daily_cap():
    try:
        return float(os.environ.get("ORBIO_DAILY_CAP_USD") or DAILY_CAP_DEFAULT)
    except ValueError:
        return DAILY_CAP_DEFAULT


def spent_today():
    """Sum cost_usd for today's rows (box/PC local clock, ET)."""
    today = datetime.now().date().isoformat()
    total = 0.0
    try:
        with LOG_FILE.open(newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("time") or "").startswith(today):
                    try:
                        total += float(row.get("cost_usd") or 0)
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return total


def _reasoning():
    eff = (os.environ.get("ORBIO_REASONING_EFFORT") or "").strip().lower()
    return {"effort": eff} if eff else dict(REASONING)


def ask_external(message, history=None, max_tokens=None, source="external"):
    """qwen via Orbio, reasoning off. Returns (reply_markdown, info_dict). Never raises."""
    return _ask(message, history, max_tokens, source, news=False)


def ask_news(message, history=None, max_tokens=None, source="news"):
    """perplexity/sonar (web search) via Orbio. No chat context. Returns (reply_markdown, info_dict). Never raises."""
    return _ask(message, None, max_tokens or NEWS_MAX_TOKENS, source, news=True)


def _ask(message, history, max_tokens, source, news):
    kind = "News model" if news else "External model"
    cap, spent = _daily_cap(), spent_today()
    if spent >= cap:
        return (f"**Daily external budget reached** (${spent:.4f} of ${cap:.2f} today). Orbio was not called; ask PYTH locally, or raise ORBIO_DAILY_CAP_USD.", {"status": "cap"})
    key, model, news_model = _config()
    if news:
        model = news_model
    if not max_tokens:
        try:
            max_tokens = int(os.environ.get("ORBIO_MAX_TOKENS") or MAX_TOKENS_DEFAULT)
        except ValueError:
            max_tokens = MAX_TOKENS_DEFAULT
    if not key:
        return (f"**{kind} unavailable:** no ORBIO_API_KEY found (set it in ~/.config/pith/orbio.env).", {"status": "no_key"})
    ctx = [m for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")][-6:]
    msgs = [{"role": "system", "content": NEWS_SYSTEM if news else SYSTEM}] + [{"role": m["role"], "content": str(m["content"])[:4000]} for m in ctx]
    msgs.append({"role": "user", "content": message})
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens}
    if not news:
        body["reasoning"] = _reasoning()
    ts, qchars = datetime.now(ET).isoformat(timespec="seconds"), len(message)
    t0 = time.time()
    try:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        r = requests.post(f"{BASE_URL}/chat/completions", timeout=TIMEOUT, headers=hdrs, json=body)
        if r.status_code == 400 and "reasoning" in body and "reasoning" in r.text.lower():
            print("[orbio] reasoning param rejected; retrying without it")
            body.pop("reasoning")
            r = requests.post(f"{BASE_URL}/chat/completions", timeout=TIMEOUT, headers=hdrs, json=body)
        secs = time.time() - t0
        if r.status_code != 200:
            _log(model, 0, 0, 0.0, secs, f"http_{r.status_code}")
            _usage(ts, model, "orbio", 0, 0, 0, secs, qchars, f"{source}:error-{r.status_code}")
            return (f"**{kind} error** (HTTP {r.status_code}). Nothing was changed; try again or ask PYTH locally.", {"status": f"http_{r.status_code}"})
        d = r.json()
        choice = (d.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        cut = choice.get("finish_reason") == "length"
        u = d.get("usage") or {}
        tin, tout = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        exact = _exact_cost(d, r)
        pin, pout = PRICES.get(model, (0.0, 0.0))
        cost = exact if exact is not None else (tin * pin + tout * pout) / 1e6
        provider = d.get("provider") or "orbio"
        _usage(ts, model, provider, tin, tout, exact, secs, qchars, source if text.strip() else f"{source}:empty")
        if not text.strip():
            _log(model, tin, tout, cost, secs, "empty")
            return (f"**{kind} returned an empty answer.** Try again or rephrase.", {"status": "empty"})
        _log(model, tin, tout, cost, secs, "ok")
        if cut:
            text = text.rstrip() + "\n\n*[reply cut off at token limit]*"
        if news:
            ann = [(a.get("url_citation") or {}).get("url") for a in ((choice.get("message") or {}).get("annotations") or []) if isinstance(a, dict)]
            cites = (d.get("citations") or [s.get("url") for s in (d.get("search_results") or []) if isinstance(s, dict) and s.get("url")]
                     or [u for u in ann if u])
            if cites:
                text = text.rstrip() + "\n\n**Sources:**\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(cites[:10], 1))
        bal = _balance(r, key)
        bal_txt = f" · Balance: {bal:.4f} CREDIT (shared account)" if bal is not None else " · Balance: n/a"
        cost_txt = f"{_num(cost)} CREDIT" if exact is not None else f"~${cost:.5f}"
        footer = f"\n\n---\n*via {'news' if news else 'external'} model `{model}` (Orbio) · {tin}+{tout} tokens · {cost_txt} · {secs:.1f}s{bal_txt}*"
        head = "**[News reply]**" if news else "**[External model reply]**"
        return (head + "\n\n" + text.strip() + footer,
                {"status": "ok", "model": model, "tokens_in": tin, "tokens_out": tout, "cost_usd": cost, "balance": bal})
    except requests.Timeout:
        _log(model, 0, 0, 0.0, time.time() - t0, "timeout")
        _usage(ts, model, "orbio", 0, 0, 0, time.time() - t0, qchars, f"{source}:error-timeout")
        return (f"**{kind} timed out** after {TIMEOUT}s. Try again or ask PYTH locally.", {"status": "timeout"})
    except Exception as e:
        _log(model, 0, 0, 0.0, time.time() - t0, "error")
        _usage(ts, model, "orbio", 0, 0, 0, time.time() - t0, qchars, f"{source}:error-{type(e).__name__}")
        return (f"**{kind} error:** {type(e).__name__}. Try again or ask PYTH locally.", {"status": "error"})
