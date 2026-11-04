"""Send one message (plus recent chat context) to NEAR AI Cloud (OpenAI-compatible).
Key: NEARAI_API_KEY from env or ~/.config/pith/nearai.env (never logged or printed).
Also accepts NEAR_AI_API_KEY env (box alias). File mode should be 600.
Usage log: ~/pyth/logs/pith_usage.csv (same header as Orbio; source=near).
Daily spend cap: independent of Orbio (default $0.25/day ET, NEARAI_DAILY_CAP).
ask_near -> deepseek-ai/DeepSeek-V4.1-Flash-1017 (Private TEE) by default.
Estimated org credit left: ~/pyth/near_credits.json + pith_usage.csv (no API key use)."""
import csv, os, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

BASE_URL = "https://cloud-api.near.ai/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"  # Private TEE; $0.30 in / $1.20 out per 1M
PRICES = {
    "deepseek-ai/DeepSeek-V4.1-Flash-1017": (0.30, 1.20),
}  # USD per 1M tokens; used when usage.cost absent
TIMEOUT = 60
MAX_TOKENS_DEFAULT = 1500  # override with NEARAI_MAX_TOKENS
DAILY_CAP_DEFAULT = 0.25  # USD per day (ET); override with NEARAI_DAILY_CAP
_REPO = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("NEARAI_ENV_FILE") or (Path.home() / ".config" / "pith" / "nearai.env"))
LOG_FILE = Path(os.environ.get("NEARAI_LOG_FILE") or (_REPO / "logs" / "near_calls.csv"))
USAGE_CSV = Path(os.environ.get("PITH_USAGE_CSV") or (_REPO / "logs" / "pith_usage.csv"))
USAGE_HEADER = ["timestamp_et", "model", "provider", "input_tokens", "output_tokens",
                "cost_credit", "latency_s", "query_chars", "source"]
ET = ZoneInfo("America/New_York")


def _system_prompt():
    """System prompt with today's date (America/New_York) so the model does not guess the date."""
    today = datetime.now(ET).strftime("%A, %B %d, %Y")
    return (
        "You are a strong external model on NEAR AI Cloud helping Pyth, a local assistant. "
        f"Today's date is {today} (America/New_York). "
        "Answer clearly and concisely. You cannot browse; use only the context given."
    )


def _config():
    cfg = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    # Prefer NEARAI_API_KEY (PC file); NEAR_AI_API_KEY is a box/env alias.
    key = (os.environ.get("NEARAI_API_KEY")
           or os.environ.get("NEAR_AI_API_KEY")
           or cfg.get("NEARAI_API_KEY", "")
           or cfg.get("NEAR_AI_API_KEY", ""))
    model = os.environ.get("NEARAI_MODEL") or cfg.get("NEARAI_MODEL") or DEFAULT_MODEL
    return key, model


def _log(model, tin, tout, cost, secs, status):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        new = not LOG_FILE.exists()
        with LOG_FILE.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "model", "tokens_in", "tokens_out", "cost_usd", "seconds", "status"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), model, tin, tout,
                        f"{cost:.6f}", f"{secs:.1f}", status])
    except Exception as e:
        print(f"[near log error] {type(e).__name__}")


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
            w.writerow([ts, model, provider or "near", int(tin), int(tout),
                        "" if cost is None else _num(cost), f"{secs:.2f}", int(qchars), source])
    except Exception as e:
        print(f"[pith usage log error] {type(e).__name__}")


def _exact_cost(d, resp):
    """NEAR may return usage.cost (USD). Else None (caller estimates from PRICES)."""
    for v in ((d.get("usage") or {}).get("cost"),
              (d.get("usage") or {}).get("total_cost"),
              (d.get("cost") if not isinstance(d.get("cost"), dict) else None)):
        try:
            if v not in (None, ""):
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _daily_cap():
    try:
        return float(os.environ.get("NEARAI_DAILY_CAP") or DAILY_CAP_DEFAULT)
    except ValueError:
        return DAILY_CAP_DEFAULT


def spent_today():
    """Sum cost_usd for today's NEAR rows (local clock; file times are local/ET)."""
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


def ask_near(message, history=None, max_tokens=None, source="near"):
    """NEAR AI Cloud chat completion. Returns (reply_markdown, info_dict). Never raises.
    Local Pyth history is not mutated by the caller; we only read a copy of recent turns."""
    kind = "NEAR AI"
    # Mock / local fallback for demos and CI without a real NEARAI_API_KEY.
    if (os.environ.get("PYTH_MOCK") or os.environ.get("NEARAI_MOCK") or "").strip().lower() in ("1", "true", "yes", "on"):
        model = os.environ.get("NEARAI_MODEL") or DEFAULT_MODEL
        reply = (
            f"**[NEAR AI mock reply]**\n\n"
            f"Mock NEAR AI Cloud response (no API key used).\n\n"
            f"**You asked:** {str(message)[:500]}\n\n"
            f"Set `NEARAI_API_KEY` and unset `PYTH_MOCK` for a live Private TEE call "
            f"to `{model}` via cloud-api.near.ai.\n\n"
            f"---\n*via NEAR AI mock · 0+0 tokens · $0 · 0.0s*"
        )
        return reply, {"status": "ok", "model": f"mock:{model}", "tokens_in": 0, "tokens_out": 0,
                       "cost_usd": 0.0, "exact_cost": True, "mock": True}
    cap, spent = _daily_cap(), spent_today()
    if spent >= cap:
        return (f"**Daily NEAR budget reached** (${spent:.4f} of ${cap:.2f} today). "
                f"NEAR AI was not called; ask Pyth locally, or raise NEARAI_DAILY_CAP.",
                {"status": "cap"})
    key, model = _config()
    if max_tokens is None:
        try:
            max_tokens = int(os.environ.get("NEARAI_MAX_TOKENS") or MAX_TOKENS_DEFAULT)
        except ValueError:
            max_tokens = MAX_TOKENS_DEFAULT
    else:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = MAX_TOKENS_DEFAULT
    if not key:
        return (f"**{kind} unavailable:** no NEARAI_API_KEY found "
                f"(set it in ~/.config/pith/nearai.env).", {"status": "no_key"})
    ctx = [m for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")][-6:]
    msgs = [{"role": "system", "content": _system_prompt()}] + [
        {"role": m["role"], "content": str(m["content"])[:4000]} for m in ctx
    ]
    msgs.append({"role": "user", "content": message})
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens}
    ts, qchars = datetime.now(ET).isoformat(timespec="seconds"), len(message)
    t0 = time.time()
    try:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        r = requests.post(f"{BASE_URL}/chat/completions", timeout=TIMEOUT, headers=hdrs, json=body)
        secs = time.time() - t0
        if r.status_code != 200:
            _log(model, 0, 0, 0.0, secs, f"http_{r.status_code}")
            _usage(ts, model, "near", 0, 0, 0, secs, qchars, f"{source}:error-{r.status_code}")
            return (f"**{kind} error** (HTTP {r.status_code}). Nothing was changed; "
                    f"try again or ask Pyth locally.",
                    {"status": f"http_{r.status_code}"})
        d = r.json()
        choice = (d.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        cut = choice.get("finish_reason") == "length"
        u = d.get("usage") or {}
        tin = int(u.get("prompt_tokens") or 0)
        tout = int(u.get("completion_tokens") or 0)
        exact = _exact_cost(d, r)
        pin, pout = PRICES.get(model, (0.30, 1.20))  # default Flash prices if model not in table
        cost = exact if exact is not None else (tin * pin + tout * pout) / 1e6
        provider = d.get("provider") or "near"
        _usage(ts, model, provider, tin, tout, exact if exact is not None else cost, secs, qchars,
               source if text.strip() else f"{source}:empty")
        if not text.strip():
            _log(model, tin, tout, cost, secs, "empty")
            return (f"**{kind} returned an empty answer.** Try again or rephrase.", {"status": "empty"})
        _log(model, tin, tout, cost, secs, "ok")
        if cut:
            text = text.rstrip() + "\n\n*[reply cut off at token limit]*"
        # Live org balance API needs a session JWT (API key alone → 401). Instead, append
        # a local estimate from ~/pyth/near_credits.json + pith_usage.csv (never logs keys).
        cost_txt = f"${_num(cost)}" if exact is not None else f"~${cost:.5f}"
        credit_txt = ""
        try:
            from near_credits import footer_credit_text
            credit_txt = footer_credit_text()
        except Exception as e:
            print(f"[near credits estimate error] {type(e).__name__}")
        credit_bit = f" · {credit_txt}" if credit_txt else ""
        footer = (f"\n\n---\n*via NEAR AI `{model}` · {tin}+{tout} tokens · "
                  f"{cost_txt} · {secs:.1f}s{credit_bit}*")
        if credit_txt and "low" in credit_txt:
            footer += "\n*Low NEAR credit — top up at cloud.near.ai or wait for staking accrual.*"
        head = "**[NEAR AI reply]**"
        return (head + "\n\n" + text.strip() + footer,
                {"status": "ok", "model": model, "tokens_in": tin, "tokens_out": tout,
                 "cost_usd": cost, "exact_cost": exact is not None})
    except requests.Timeout:
        _log(model, 0, 0, 0.0, time.time() - t0, "timeout")
        _usage(ts, model, "near", 0, 0, 0, time.time() - t0, qchars, f"{source}:error-timeout")
        return (f"**{kind} timed out** after {TIMEOUT}s. Try again or ask Pyth locally.",
                {"status": "timeout"})
    except Exception as e:
        _log(model, 0, 0, 0.0, time.time() - t0, "error")
        _usage(ts, model, "near", 0, 0, 0, time.time() - t0, qchars,
               f"{source}:error-{type(e).__name__}")
        return (f"**{kind} error:** {type(e).__name__}. Try again or ask Pyth locally.",
                {"status": "error"})
