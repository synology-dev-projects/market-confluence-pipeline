import logging
from datetime import date
from typing import Dict, Any, List, Tuple

logger = logging.getLogger("quant.pipeline.confluence.scorer")


def score_and_classify_record(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates cross-metric alignment between Options Flow and GEX/DEX microstructure.
    Assigns an objective confluence_status, a 0.0-100.0 score, and a 1-sentence rationale.
    """
    res = dict(item)

    if not item.get("gex_available"):
        res["confluence_status"] = "FLOW_ONLY"
        res["confluence_score"] = 40.0
        res["confluence_rationale"] = (
            f"{item['ticker']}: Flow exhibits {item['flow_bias']} bias ({item['call_premium_pct']}% Calls), "
            f"but GEX/DEX dealer inventory is currently unavailable."
        )
        return res

    call_pct = float(item.get("call_premium_pct", 0.0))
    put_pct = float(item.get("put_premium_pct", 0.0))
    flow_bias = str(item.get("flow_bias", "NEUTRAL"))
    spot = item.get("spot_price")
    flip = item.get("zero_gamma_flip")
    net_gex = float(item.get("net_gex") or 0.0)
    net_dex = float(item.get("net_dex") or 0.0)
    c_wall = float(item.get("call_wall") or 0.0)
    p_wall = float(item.get("put_wall") or 0.0)
    whale_count = int(item.get("whale_prints_count", 0))

    spot_above_flip = (spot >= flip) if (spot is not None and flip is not None) else False
    is_positive_gamma = (net_gex > 0)

    # 1. Structural Hedge Recognition
    # Spot is high & positive gamma, but institutional flow is heavily sweeping puts
    if spot_above_flip and is_positive_gamma and flow_bias == "BEARISH":
        status = "STRUCTURAL_HEDGE"
        score = min(85.0, 65.0 + (whale_count * 5.0))
        gex_billions = net_gex / 1_000_000_000.0
        rationale = (
            f"{item['ticker']}: Put sweeps ({put_pct:.1f}% Puts, {whale_count} 🐳) against positive gamma "
            f"(+${gex_billions:.1f}B) reflect institutional portfolio hedging rather than directional breakdown."
        )

    # 2. Confirmed Bullish Confluence
    elif flow_bias == "BULLISH" and (spot_above_flip or is_positive_gamma or net_dex > 0):
        status = "CONFIRMED_BULL"
        score = 60.0
        if call_pct >= 65.0:
            score += 10.0
        if spot_above_flip:
            score += 10.0
        if net_dex > 0:
            score += 5.0
        score += min(15.0, whale_count * 5.0)
        score = min(98.0, score)
        flip_str = f"${flip:.2f}" if flip is not None else "N/A"
        rationale = (
            f"{item['ticker']}: Spot sits above Zero Flip ({flip_str}) in supportive gamma regime, "
            f"aligned with {call_pct:.1f}% Call flow and {whale_count} whale sweeps."
        )

    # 3. Confirmed Bearish Acceleration
    elif flow_bias == "BEARISH" and (not spot_above_flip or not is_positive_gamma or net_dex < 0):
        status = "CONFIRMED_BEAR"
        score = 60.0
        if put_pct >= 65.0:
            score += 10.0
        if not spot_above_flip:
            score += 10.0
        if net_dex < 0:
            score += 5.0
        score += min(15.0, whale_count * 5.0)
        score = min(98.0, score)
        flip_str = f"${flip:.2f}" if flip is not None else "N/A"
        rationale = (
            f"{item['ticker']}: Spot trading below Zero Flip ({flip_str}) in negative gamma, "
            f"reinforcing dealer selling acceleration with {put_pct:.1f}% Put flow."
        )

    # 4. Volatility Pin / Range Suppression
    elif is_positive_gamma and flow_bias == "NEUTRAL" and p_wall <= (spot or 0.0) <= c_wall:
        status = "VOL_PIN"
        score = 75.0 if net_gex >= 50_000_000_000.0 else 65.0
        gex_billions = net_gex / 1_000_000_000.0
        rationale = (
            f"{item['ticker']}: Positive gamma (+${gex_billions:.1f}B) locks spot between "
            f"${p_wall:.2f} Put Wall and ${c_wall:.2f} Call Wall under balanced institutional flow."
        )

    # 5. Divergent Setup
    else:
        status = "DIVERGENT"
        score = 50.0
        rationale = (
            f"{item['ticker']}: Institutional flow ({flow_bias}, {call_pct:.1f}% Calls) diverges "
            f"from dealer gamma microstructure dynamics."
        )

    res["confluence_status"] = status
    res["confluence_score"] = round(score, 1)
    res["confluence_rationale"] = rationale
    return res


def score_all_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Scores and classifies all enriched candidates and sorts by score descending."""
    scored = [score_and_classify_record(c) for c in candidates]
    scored.sort(key=lambda x: (x.get("confluence_score", 0.0), x.get("total_flow_premium", 0.0)), reverse=True)
    return scored


def build_daily_summary(scan_date: date, scored_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generates the aggregate summary record for daily_confluence_summary."""
    total_count = len(scored_records)
    bull_count = sum(1 for r in scored_records if r.get("confluence_status") == "CONFIRMED_BULL")
    bear_count = sum(1 for r in scored_records if r.get("confluence_status") == "CONFIRMED_BEAR")
    pin_count = sum(1 for r in scored_records if r.get("confluence_status") == "VOL_PIN")
    div_count = sum(1 for r in scored_records if r.get("confluence_status") in ("DIVERGENT", "STRUCTURAL_HEDGE", "FLOW_ONLY"))

    top_whale_sym = "N/A"
    top_whale_prem = 0.0
    top_whale_fmt = "$0.00"

    if scored_records:
        whale_sorted = sorted(scored_records, key=lambda x: x.get("top_whale_premium", 0.0), reverse=True)
        top_w = whale_sorted[0]
        if top_w.get("top_whale_premium", 0.0) > 0:
            top_whale_sym = top_w.get("ticker", "N/A")
            top_whale_prem = float(top_w.get("top_whale_premium", 0.0))
            top_whale_fmt = top_w.get("formatted_top_whale_premium", "$0.00")

    if bull_count > bear_count:
        regime_summary = "BULLISH FLOW CONFLUENCE"
    elif bear_count > bull_count:
        regime_summary = "BEARISH ACCELERATION BIAS"
    else:
        regime_summary = "VOLATILITY SUPPRESSION / BALANCED"

    session_label = f"Post-Market EOD Scan ({scan_date})"

    return {
        "scan_date": scan_date,
        "session_label": session_label,
        "total_scanned_count": total_count,
        "confirmed_bull_count": bull_count,
        "confirmed_bear_count": bear_count,
        "vol_pin_count": pin_count,
        "divergent_count": div_count,
        "top_whale_ticker": top_whale_sym,
        "top_whale_premium": top_whale_prem,
        "formatted_top_whale_premium": top_whale_fmt,
        "market_regime_summary": regime_summary
    }
