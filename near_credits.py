"""Estimate remaining NEAR AI Cloud credit balance from local config + pith_usage.csv.

Does NOT call cloud APIs or touch keys. Balance =
  sum(purchases.usd) + accrued_staking_credits − sum(NEAR-source costs since cost_since).

Staking accrues only when staking.start_date is set (YYYY-MM-DD). Until then accrued = 0.
Rate: (near / 100) * usd_per_100_per_month, prorated by elapsed days / 30.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_REPO = Path(__file__).resolve().parent
DEFAULT_CFG = Path(__import__("os").environ.get("NEAR_CREDITS_JSON") or (_REPO / "near_credits.json"))
DEFAULT_USAGE = Path(__import__("os").environ.get("PITH_USAGE_CSV") or (_REPO / "logs" / "pith_usage.csv"))
DAYS_PER_MONTH = 30.0


def _load_cfg(path: Path | None = None) -> dict:
    p = path or DEFAULT_CFG
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def _parse_day(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def purchases_total(cfg: dict) -> float:
    total = 0.0
    for row in cfg.get("purchases") or []:
        try:
            total += float(row.get("usd") or 0)
        except (TypeError, ValueError):
            pass
    return total


def staking_accrued(cfg: dict, now: datetime | None = None) -> float:
    """Prorated monthly staking credits since start_date. 0 if start_date is null."""
    st = cfg.get("staking") or {}
    start = _parse_day(st.get("start_date"))
    if start is None:
        return 0.0
    try:
        near = float(st.get("near") or 0)
        rate = float(st.get("usd_per_100_per_month") or 0)
    except (TypeError, ValueError):
        return 0.0
    monthly = (near / 100.0) * rate
    now = now or datetime.now(ET)
    # Use ET calendar date for day count; partial day counts as fraction of 24h.
    start_dt = datetime(start.year, start.month, start.day, tzinfo=ET)
    elapsed_days = max(0.0, (now - start_dt).total_seconds() / 86400.0)
    return monthly * (elapsed_days / DAYS_PER_MONTH)


def _is_near_source(src: str) -> bool:
    src = (src or "").strip()
    return src == "near" or src.startswith("near:")


def near_costs_since(usage_csv: Path, since: str) -> float:
    """Sum cost_credit for rows with source near/near:* and timestamp date >= since."""
    since_day = _parse_day(since) or date(2026, 10, 30)
    total = 0.0
    try:
        with usage_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not _is_near_source(row.get("source") or ""):
                    continue
                ts = (row.get("timestamp_et") or "")[:10]
                day = _parse_day(ts)
                if day is None or day < since_day:
                    continue
                try:
                    total += float(row.get("cost_credit") or 0)
                except (TypeError, ValueError):
                    pass
    except FileNotFoundError:
        pass
    return total


def estimate_balance(
    cfg_path: Path | None = None,
    usage_csv: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Return dict with purchases, accrued, spent, balance, low warning flag, footer bits."""
    cfg = _load_cfg(cfg_path)
    usage = usage_csv or DEFAULT_USAGE
    since = str(cfg.get("cost_since") or "2026-10-30")
    purch = purchases_total(cfg)
    accrued = staking_accrued(cfg, now=now)
    spent = near_costs_since(usage, since)
    bal = purch + accrued - spent
    try:
        low_thresh = float(cfg.get("low_balance_usd") or 1.0)
    except (TypeError, ValueError):
        low_thresh = 1.0
    low = bal < low_thresh
    return {
        "purchases": purch,
        "accrued": accrued,
        "spent": spent,
        "balance": bal,
        "low": low,
        "low_thresh": low_thresh,
        "cost_since": since,
        "staking_start": (cfg.get("staking") or {}).get("start_date"),
    }


def footer_credit_text(
    cfg_path: Path | None = None,
    usage_csv: Path | None = None,
    now: datetime | None = None,
) -> str:
    """One-line estimated credit for the NEAR reply footer. Empty string on failure."""
    try:
        info = estimate_balance(cfg_path=cfg_path, usage_csv=usage_csv, now=now)
    except Exception:
        return ""
    bal = info["balance"]
    line = f"≈ ${bal:.2f} NEAR credit left"
    if info["low"]:
        line += f" ⚠ low (under ${info['low_thresh']:.0f})"
    return line
