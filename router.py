"""Fast rule-based model router for Pyth Send (no local LLM call).
Classify → news (Orbio/sonar) or NEAR (deep/quick); fall back local if NEAR fails,
NEAR if news/Orbio fails. Config: ~/pyth/routing.json (defaults if absent)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
_REPO = Path(__file__).resolve().parent
CONFIG_PATH = Path(__import__("os").environ.get("PYTH_ROUTING_JSON") or (_REPO / "routing.json"))
# Also accept config beside this file (box tests / first install copy)
CONFIG_FALLBACK = ROOT / "routing.json"
USAGE_CSV = Path(__import__("os").environ.get("PITH_USAGE_CSV") or (_REPO / "logs" / "pith_usage.csv"))
USAGE_HEADER = ["timestamp_et", "model", "provider", "input_tokens", "output_tokens",
                "cost_credit", "latency_s", "query_chars", "source"]
ET = ZoneInfo("America/New_York")

DEFAULTS = {
    "news": {"provider": "orbio", "model": "perplexity/sonar", "label": "news"},
    "near_deep": {"provider": "near", "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                  "label": "NEAR", "max_tokens": 4000},
    "near_quick": {"provider": "near", "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                   "label": "NEAR", "max_tokens": 1500},
    "local": {"provider": "ollama", "model": "llama3.1:8b", "label": "local"},
    "fallback": {"near_fail": "local", "news_fail": "near"},
    "deep_char_threshold": 120,
}

# Word/phrase cues for current-info / news / local live info (case-insensitive).
# Note: "local:" prefix (with colon) still forces the Ollama agent — checked before these.
NEWS_PATTERNS = [
    r"\btoday\b", r"\blatest\b", r"\bnews\b", r"\bcurrent\b", r"\bprice\b",
    r"\bprices\b", r"\bthis week\b", r"\brecent\b", r"\b2026\b",
    r"\bwho won\b", r"\bheadline[s]?\b", r"\bbreaking\b", r"\bright now\b",
    r"\bas of (?:today|now|this)\b", r"\bstock price\b",
    r"\bwhat.?s the (?:btc|eth|bitcoin|ethereum|sol|near)\b",
    # local / live place & time (r20): farms, weather, hours, season, sentiment, …
    r"\bnear me\b", r"\blocal\b", r"\bin season\b", r"\bout of season\b",
    r"\bopen\b", r"\bhours\b", r"\bthis morning\b", r"\btonight\b",
    r"\bweather\b", r"\btides?\b", r"\bhappening\b", r"\bsentiment\b",
    r"\bare there (?:any\s+)?(?:\w+[\s,]+){0,8}(?:farms?|restaurants?|events?)\b",
    r"\bis it\b.{0,48}\bseason\b",
]
NEWS_RE = re.compile("|".join(NEWS_PATTERNS), re.I)

# Deep / thoughtful cues (why/explain/compare/code/philosophy/soul) → near_deep label.
DEEP_PATTERNS = [
    r"\bwhy\b", r"\bexplain\b", r"\bcompare\b", r"\bcomparison\b",
    r"\bcode\b", r"\bimplement\b", r"\brefactor\b", r"\bdebug\b",
    r"\bphilosoph\w*\b", r"\banalyz[e]\b", r"\banalysis\b",
    r"\bwalk me through\b", r"\bstep by step\b", r"\bdifference between\b",
    # philosophy follow-ups (r20)
    r"\bsoul\b", r"\bdeath\b", r"\bconsciousness\b", r"\bmeaning\b",
    r"\bexistence\b", r"\bessence\b", r"\bgod\b", r"\bspirit\b",
    r"\bmetaphysic\w*\b", r"\balong those lines\b",
    r"\bwhere does\b", r"\bwhat happens when\b",
]
DEEP_RE = re.compile("|".join(DEEP_PATTERNS), re.I)

LOCAL_PREFIX_RE = re.compile(r"^\s*local\s*:\s*", re.I)

# Personal / user-local data — MUST run before news so "my portfolio today" never hits Sonar.
# Topics mirror local tools: portfolio.py, team_bridge (email/inbox), memory_tool, doc_index,
# stakes desks, uploads/files. Do NOT match bare "my" (keeps "my favorite philosophy…" on NEAR).
PERSONAL_PATTERNS = [
    r"\bmy\s+(portfolio|holdings?|bags?|balances?|wallets?|stakes?|positions?)\b",
    r"\bmy\s+(e-?mails?|inbox|mail|notes?|memory|files?|docs?|documents?|records?)\b",
    r"\bmy\s+(calendar|schedule|reminders?)\b",
    r"\b(how'?s|how\s+is)\s+my\s+portfolio\b",
    r"\bcheck\s+(my\s+|the\s+|your\s+)?(e-?mails?|inbox|mail)\b",
    r"\b(what|when|where)\s+did\s+i\b",
    r"\bdo\s+i\s+have\b",
    r"\bremind\s+me\b",
    r"\bmark\s+the\s+book\b",
    r"\b(in|from)\s+my\s+(files?|docs?|documents?|memory|records?|notes?)\b",
    r"\bteam\s+inbox\b",
    r"\bload\s+(my\s+)?portfolio\b",
    r"\b(update|rebuild)\s+(my\s+)?portfolio\b",
]
# General outbound email (compose / confirm / cancel / edit) — local only, never news/NEAR.
# Misheard voice "send" must hit the local confirm gate, not a remote model that might "helpfully" send.
EMAIL_SEND_PATTERNS = [
    r"\b(?:send|draft|write|compose)\s+(?:an?\s+)?e-?mails?\b",
    r"\be-?mail\s+\S+@",
    r"\be-?mail\s+\w[\w.\- ]{0,40}?\s+(?:about|re|regarding|saying)\b",
    r"\bsend\s+the\s+(?:pending\s+)?(?:email|draft)\b",
    r"\bchange\s+(?:the\s+)?(?:subject|body|to|recipient|address)\b",
    r"\bcancel\s+(?:the\s+)?(?:email|draft|message)\b",
    r"^\s*send(?:\s+it)?\s*[.!?]*\s*$",
    r"^\s*cancel\s*[.!?]*\s*$",
]
PERSONAL_RE = re.compile("|".join(PERSONAL_PATTERNS + EMAIL_SEND_PATTERNS), re.I)


@dataclass
class Decision:
    category: str          # news | near_deep | near_quick | local
    provider: str          # orbio | near | ollama
    model: str
    label: str             # short UI label (news / NEAR / local)
    reason: str            # human phrase for footer, e.g. "deep question"
    message: str           # possibly stripped of local: prefix
    forced: bool = False


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for path in (CONFIG_PATH, CONFIG_FALLBACK):
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg.update(raw)
                break
        except Exception as e:
            print(f"[router config] {type(e).__name__}: {path}")
    # Ensure nested defaults for known categories
    for k, v in DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            merged = dict(v)
            merged.update(cfg[k])
            cfg[k] = merged
    return cfg


def classify(message: str, cfg: dict | None = None) -> Decision:
    """Instant rule-based classify. Never calls an LLM."""
    cfg = cfg or load_config()
    raw = (message or "").strip()
    if not raw:
        entry = cfg["near_quick"]
        return Decision("near_quick", entry["provider"], entry["model"], entry["label"],
                        "empty", "", False)

    if LOCAL_PREFIX_RE.match(raw):
        msg = LOCAL_PREFIX_RE.sub("", raw, count=1).strip() or raw
        entry = cfg["local"]
        return Decision("local", entry["provider"], entry["model"], entry["label"],
                        "local: prefix", msg, True)

    # Personal data / email compose+confirm → local only (never Orbio/NEAR).
    if PERSONAL_RE.search(raw):
        entry = cfg["local"]
        return Decision("local", entry["provider"], entry["model"], entry["label"],
                        "your data", raw, False)

    if NEWS_RE.search(raw):
        entry = cfg["news"]
        return Decision("news", entry["provider"], entry["model"], entry["label"],
                        "news/current info", raw, False)

    threshold = int(cfg.get("deep_char_threshold") or 200)
    if DEEP_RE.search(raw) or len(raw) >= threshold:
        entry = cfg["near_deep"]
        reason = "deep question" if DEEP_RE.search(raw) else "long question"
        return Decision("near_deep", entry["provider"], entry["model"], entry["label"],
                        reason, raw, False)

    entry = cfg["near_quick"]
    return Decision("near_quick", entry["provider"], entry["model"], entry["label"],
                    "quick", raw, False)


def _failed(info: dict | None) -> bool:
    if not info:
        return True
    st = str(info.get("status") or "")
    if st in ("ok",):
        return False
    # cap / no_key / timeout / error / empty / http_* are failures for fallback
    return True


def _auto_tag(label: str, reason: str, via_fallback: str | None = None) -> str:
    if via_fallback:
        return f"**[auto → {label} ({reason}); fell back from {via_fallback}]**\n\n"
    return f"**[auto → {label} ({reason})]**\n\n"


def _log_local(model: str, secs: float, qchars: int, source: str = "local"):
    """Append a local-row to pith_usage.csv without changing the header."""
    import csv
    try:
        USAGE_CSV.parent.mkdir(parents=True, exist_ok=True)
        new = not USAGE_CSV.exists() or USAGE_CSV.stat().st_size == 0
        ts = datetime.now(ET).isoformat(timespec="seconds")
        with USAGE_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(USAGE_HEADER)
            # tokens/cost unknown for local Ollama path
            w.writerow([ts, model, "ollama", 0, 0, "", f"{secs:.2f}", int(qchars), source])
    except Exception as e:
        print(f"[router local usage] {type(e).__name__}")


def _ask_near(message, history, source="near", max_tokens=None):
    from near_remote import ask_near
    return ask_near(message, history, max_tokens=max_tokens, source=source)


def _ask_news(message, source="news"):
    from orbio_remote import ask_news
    return ask_news(message, source=source)


def route_message(message: str, history=None, local_runner=None, force_local: bool = False):
    """Pick provider and call it. local_runner(msg) -> reply str for Ollama/agent path.
    Returns (reply_markdown, info_dict). Never raises."""
    cfg = load_config()
    if force_local:
        entry = cfg["local"]
        dec = Decision("local", entry["provider"], entry["model"], entry["label"],
                       "LOCAL button", (message or "").strip(), True)
    else:
        dec = classify(message, cfg)

    info = {"category": dec.category, "reason": dec.reason, "provider": dec.provider,
            "model": dec.model, "label": dec.label, "routed": True}

    # --- local ---
    if dec.category == "local":
        return _run_local(dec, local_runner, info)

    # --- news ---
    if dec.category == "news":
        reply, ninfo = _ask_news(dec.message, source="news")
        info["remote"] = ninfo
        if not _failed(ninfo):
            return _auto_tag(dec.label, dec.reason) + reply, info
        # fallback → NEAR
        fb = (cfg.get("fallback") or {}).get("news_fail") or "near"
        if fb == "near":
            mt = (cfg.get("near_deep") or {}).get("max_tokens")
            reply2, ninfo2 = _ask_near(dec.message, history, source="near", max_tokens=mt)
            info["fallback"] = "near"
            info["remote"] = ninfo2
            if not _failed(ninfo2):
                return _auto_tag("NEAR", dec.reason, "news") + reply2, info
            # NEAR also failed → local
            info["fallback"] = "local"
            dec2 = Decision("local", cfg["local"]["provider"], cfg["local"]["model"],
                            cfg["local"]["label"], "news then NEAR failed", dec.message, False)
            return _run_local(dec2, local_runner, info, via="news→NEAR")
        return _auto_tag(dec.label, dec.reason) + reply, info

    # --- NEAR (deep or quick) ---
    if dec.provider == "near" or dec.category in ("near_deep", "near_quick"):
        mt = (cfg.get(dec.category) or {}).get("max_tokens")
        reply, ninfo = _ask_near(dec.message, history, source="near", max_tokens=mt)
        info["remote"] = ninfo
        if not _failed(ninfo):
            return _auto_tag(dec.label, dec.reason) + reply, info
        # fallback → local
        info["fallback"] = "local"
        dec2 = Decision("local", cfg["local"]["provider"], cfg["local"]["model"],
                        cfg["local"]["label"], f"{dec.reason}; NEAR unavailable",
                        dec.message, False)
        return _run_local(dec2, local_runner, info, via="NEAR")

    # Unknown category → local
    return _run_local(dec, local_runner, info)


def _run_local(dec: Decision, local_runner, info: dict, via: str | None = None):
    if local_runner is None:
        tag = _auto_tag("local", dec.reason, via)
        return tag + "**Local model unavailable** (no runner wired).", {**info, "status": "no_runner"}
    t0 = time.time()
    try:
        reply = local_runner(dec.message)
        secs = time.time() - t0
        _log_local(dec.model, secs, len(dec.message), source="local")
        tag = _auto_tag("local", dec.reason, via)
        # Local replies have no remote footer; add a light one.
        footer = f"\n\n---\n*via local `{dec.model}` · {secs:.1f}s*"
        info["status"] = "ok"
        info["provider"] = "ollama"
        return tag + (reply or "") + footer, info
    except Exception as e:
        secs = time.time() - t0
        _log_local(dec.model, secs, len(dec.message), source="local:error")
        info["status"] = "error"
        return (_auto_tag("local", dec.reason, via)
                + f"**Local model error:** {type(e).__name__}.", info)
