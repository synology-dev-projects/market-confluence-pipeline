import logging
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd

logger = logging.getLogger("quant.pipeline.confluence.scorer")


def _calculate_dte(exp_val: Any, scan_date: Optional[date]) -> Tuple[Optional[str], Optional[int]]:
    """Calculates days to expiration from expiration value and scan_date."""
    if not exp_val:
        return None, None
    exp_str = str(exp_val)[:10]
    try:
        exp_dt = pd.to_datetime(exp_str).date()
        if scan_date:
            dte = max(0, (exp_dt - scan_date).days)
        else:
            dte = max(0, (exp_dt - date.today()).days)
        return exp_str, dte
    except Exception:
        return exp_str, None


def score_and_classify_record(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates candidate for Asymmetric Options Play:
    1. BULL_SPRING: Spot below >= 80% Gamma/Delta exposure (gex_above_pct or dex_above_pct >= 80%).
    2. BEAR_EXHAUSTION: Spot above >= 80% Gamma/Delta exposure (gex_below_pct or dex_below_pct >= 80%).
    Calculates 4-factor viability score (0.0 to 100.0).
    If thresholds not met, play_type is None and candidate does not qualify.
    """
    res = dict(item)

    # Defaults
    res["play_type"] = None
    res["rank"] = None
    res["exposure_imbalance_pct"] = None
    res["imbalance_type"] = None
    res["pin_wall_strike"] = None
    res["pin_wall_type"] = None
    res["pin_expiration"] = None
    res["pin_dte"] = None
    res["pin_dist_pct"] = None
    res["viability_score"] = None
    res["confluence_status"] = "NON_QUALIFYING"
    res["confluence_score"] = 0.0
    res["confluence_rationale"] = "Does not meet the strict >= 80% exposure imbalance threshold."

    if not item.get("gex_available"):
        res["confluence_status"] = "FLOW_ONLY"
        res["confluence_rationale"] = f"{item.get('ticker', 'SYM')}: Options flow observed, but GEX/DEX dealer inventory unavailable."
        return res

    spot = float(item.get("spot_price") or 0.0)
    gex_above = item.get("gex_above_pct")
    dex_above = item.get("dex_above_pct")
    call_wall = float(item.get("call_wall") or 0.0)
    put_wall = float(item.get("put_wall") or 0.0)
    key_gamma = float(item.get("key_gamma_strike") or 0.0)
    dominant_exp = item.get("dominant_expiration")
    scan_dt = item.get("scan_date")
    if isinstance(scan_dt, str):
        try:
            scan_dt = pd.to_datetime(scan_dt).date()
        except Exception:
            scan_dt = None

    if gex_above is None and dex_above is None:
        res["confluence_status"] = "NO_EXPOSURE_DATA"
        return res

    g_above = float(gex_above) if gex_above is not None else 0.0
    d_above = float(dex_above) if dex_above is not None else 0.0
    g_below = 100.0 - g_above if gex_above is not None else 0.0
    d_below = 100.0 - d_above if dex_above is not None else 0.0

    max_spring_imb = max(g_above, d_above)
    max_exhaust_imb = max(g_below, d_below)

    is_bull_spring = max_spring_imb >= 80.0
    is_bear_exhaustion = max_exhaust_imb >= 80.0

    if not is_bull_spring and not is_bear_exhaustion:
        return res

    # Resolve play type if both triggered (choose highest imbalance)
    if is_bull_spring and is_bear_exhaustion:
        if max_spring_imb >= max_exhaust_imb:
            is_bear_exhaustion = False
        else:
            is_bull_spring = False

    # 1. Exposure Imbalance Score (S_exposure, 50% Weight)
    if is_bull_spring:
        play_type = "BULL_SPRING"
        exposure_imb = max_spring_imb
        imbalance_type = "GEX" if g_above >= d_above else "DEX"
    else:
        play_type = "BEAR_EXHAUSTION"
        exposure_imb = max_exhaust_imb
        imbalance_type = "GEX" if g_below >= d_below else "DEX"

    # Normalized score: 80% -> 70.0, scaling linearly to 100% -> 100.0
    s_exposure = 70.0 + (min(exposure_imb, 100.0) - 80.0) / 20.0 * 30.0

    # 2. Near-Term Pinning Expiry Score (S_pin, 25% Weight)
    if play_type == "BULL_SPRING":
        # Resistance ceiling holding price down
        if call_wall > 0:
            pin_strike = call_wall
            pin_type = "CALL_WALL"
        elif key_gamma > 0:
            pin_strike = key_gamma
            pin_type = "KEY_GAMMA"
        else:
            pin_strike = put_wall
            pin_type = "PUT_WALL"
    else:
        # Support floor propping price up
        if put_wall > 0:
            pin_strike = put_wall
            pin_type = "PUT_WALL"
        elif key_gamma > 0:
            pin_strike = key_gamma
            pin_type = "KEY_GAMMA"
        else:
            pin_strike = call_wall
            pin_type = "CALL_WALL"

    pin_dist_pct = round(abs(pin_strike - spot) / spot * 100.0, 1) if spot > 0 and pin_strike > 0 else 0.0

    pin_exp_str, pin_dte = _calculate_dte(dominant_exp, scan_dt)
    if pin_dte is None:
        pin_dte = 14

    # Base pin score by DTE
    if pin_dte <= 7:
        base_pin = 100.0
    elif pin_dte <= 14:
        base_pin = 85.0
    else:
        base_pin = 40.0

    # Proximity bonus / penalty
    if pin_dist_pct <= 2.5:
        s_pin = min(100.0, base_pin + 15.0)
    elif pin_dist_pct <= 5.0:
        s_pin = base_pin
    else:
        s_pin = max(20.0, base_pin - 15.0)

    # 3. Flow Directional Ratio Score (S_flow_ratio, 15% Weight)
    call_prem = float(item.get("call_premium") or 0.0)
    put_prem = float(item.get("put_premium") or 0.0)

    if play_type == "BULL_SPRING":
        flow_ratio = round(call_prem / max(put_prem, 1.0), 2)
    else:
        flow_ratio = round(put_prem / max(call_prem, 1.0), 2)

    if flow_ratio >= 3.0:
        s_flow = 100.0
    elif flow_ratio >= 2.0:
        s_flow = 85.0
    elif flow_ratio >= 1.2:
        s_flow = 70.0
    else:
        s_flow = 40.0

    # 4. Flow DB Hit Frequency Score (S_hit_count, 10% Weight)
    hits_count = int(item.get("flow_hits_count") or item.get("whale_prints_count") or 1)

    if hits_count >= 15:
        s_hits = 100.0
    elif hits_count >= 8:
        s_hits = 85.0
    elif hits_count >= 3:
        s_hits = 70.0
    else:
        s_hits = 50.0

    # Total Viability Score
    v_score = round(
        (s_exposure * 0.50) +
        (s_pin * 0.25) +
        (s_flow * 0.15) +
        (s_hits * 0.10),
        1
    )

    ratio_label = "Call/Put" if play_type == "BULL_SPRING" else "Put/Call"
    pin_type_fmt = pin_type.replace("_", " ").title()
    pos_desc = "Above" if play_type == "BULL_SPRING" else "Below"
    ticker_str = item.get("ticker", "SYM")
    rationale = (
        f"{ticker_str}: {play_type.replace('_', ' ').title()} setup with {exposure_imb:.1f}% {imbalance_type} {pos_desc}, "
        f"pinned near ${pin_strike:.2f} {pin_type_fmt} ({pin_dte} DTE, {pin_dist_pct}% away) with {flow_ratio:.1f}x {ratio_label} flow ({hits_count} prints)."
    )

    res["play_type"] = play_type
    res["exposure_imbalance_pct"] = round(exposure_imb, 1)
    res["imbalance_type"] = imbalance_type
    res["pin_wall_strike"] = pin_strike
    res["pin_wall_type"] = pin_type
    res["pin_expiration"] = pin_exp_str
    res["pin_dte"] = pin_dte
    res["pin_dist_pct"] = pin_dist_pct
    res["flow_hits_count"] = hits_count
    res["flow_call_put_ratio"] = flow_ratio
    res["viability_score"] = v_score

    # Backwards-compatible fields
    res["confluence_status"] = play_type
    res["confluence_score"] = v_score
    res["confluence_rationale"] = rationale

    return res


def score_all_candidates(candidates: List[Dict[str, Any]], max_plays: int = 10) -> List[Dict[str, Any]]:
    """
    Scores candidates and returns strictly qualifying plays (capped at max_plays),
    ranked by viability_score descending.
    """
    scored = [score_and_classify_record(c) for c in candidates]
    qualifying = [r for r in scored if r.get("play_type") is not None]

    qualifying.sort(
        key=lambda x: (
            x.get("viability_score", 0.0),
            x.get("exposure_imbalance_pct", 0.0),
            x.get("total_flow_premium", 0.0)
        ),
        reverse=True
    )

    top_plays = qualifying[:max_plays]
    for idx, play in enumerate(top_plays):
        play["rank"] = idx + 1

    return top_plays


def build_daily_summary(
    scan_date: date,
    scored_records: List[Dict[str, Any]],
    total_watchlist_count: int = 0
) -> Dict[str, Any]:
    """
    Generates the aggregate summary record for daily_confluence_summary.
    """
    total_scanned = len(scored_records)
    bull_count = sum(1 for r in scored_records if r.get("play_type") == "BULL_SPRING")
    bear_count = sum(1 for r in scored_records if r.get("play_type") == "BEAR_EXHAUSTION")

    top_whale_sym = "N/A"
    top_whale_prem = 0.0
    top_whale_fmt = "$0.00"

    if scored_records:
        whale_sorted = sorted(scored_records, key=lambda x: x.get("top_whale_premium", 0.0) or 0.0, reverse=True)
        top_w = whale_sorted[0]
        if (top_w.get("top_whale_premium") or 0.0) > 0:
            top_whale_sym = top_w.get("ticker", "N/A")
            top_whale_prem = float(top_w.get("top_whale_premium", 0.0))
            top_whale_fmt = top_w.get("formatted_top_whale_premium", "$0.00")

    # Top Catalyst
    top_catalyst_sym = "None"
    top_catalyst_expiry = "N/A"
    if scored_records:
        top_play = scored_records[0]
        top_catalyst_sym = top_play.get("ticker", "None")
        pin_strike = top_play.get("pin_wall_strike")
        pin_wall_type = (top_play.get("pin_wall_type") or "Wall").replace("_", " ").title()
        pin_dte = top_play.get("pin_dte", "N/A")
        if pin_strike:
            top_catalyst_expiry = f"${pin_strike:.0f} {pin_wall_type} • {pin_dte} DTE"
        else:
            top_catalyst_expiry = f"{pin_dte} DTE"

    if bull_count > bear_count:
        regime_summary = "BULL SPRING CONFLUENCE"
    elif bear_count > bull_count:
        regime_summary = "BEAR EXHAUSTION BIAS"
    elif total_scanned > 0:
        regime_summary = "BALANCED ASYMMETRIC BIAS"
    else:
        regime_summary = "NO QUALIFYING PLAYS"

    session_label = f"Post-Market EOD Scan ({scan_date})"

    return {
        "scan_date": scan_date,
        "session_label": session_label,
        "total_scanned_count": total_scanned,
        "confirmed_bull_count": bull_count,
        "confirmed_bear_count": bear_count,
        "vol_pin_count": 0,
        "divergent_count": 0,
        "top_whale_ticker": top_whale_sym,
        "top_whale_premium": top_whale_prem,
        "formatted_top_whale_premium": top_whale_fmt,
        "market_regime_summary": regime_summary,
        "total_watchlist_count": total_watchlist_count or max(50, total_scanned),
        "qualifying_bull_spring_count": bull_count,
        "qualifying_bear_exhaustion_count": bear_count,
        "top_catalyst_ticker": top_catalyst_sym,
        "top_catalyst_expiry": top_catalyst_expiry
    }
