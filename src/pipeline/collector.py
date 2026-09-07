import logging
from datetime import date, datetime
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import sqlalchemy as sa

logger = logging.getLogger("quant.pipeline.confluence.collector")


def format_dollar_amount(val: Any) -> str:
    """Formats numeric values into compact, readable dollar strings ($14.20M, $780.00K, $1.85B, $0.00)."""
    if val is None:
        return "$0.00"
    if isinstance(val, str):
        if val.startswith("$") or val.startswith("+$") or val.startswith("-$"):
            return val
        try:
            val = float(val)
        except ValueError:
            return val
    try:
        v = float(val)
    except (TypeError, ValueError):
        return str(val)

    if v == 0:
        return "$0.00"

    sign = "-" if v < 0 else ""
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"{sign}${abs_v / 1_000_000_000:.2f}B"
    elif abs_v >= 1_000_000:
        return f"{sign}${abs_v / 1_000_000:.2f}M"
    elif abs_v >= 1_000:
        return f"{sign}${abs_v / 1_000:.2f}K"
    return f"{sign}${abs_v:.2f}"


def get_latest_flow_date(engine: sa.Engine) -> Optional[date]:
    """Retrieves the latest non-null TRADE_DATE available in unusual_option_flow_te."""
    query = sa.text("SELECT MAX(trade_date) FROM unusual_option_flow_te WHERE trade_date IS NOT NULL")
    with engine.connect() as conn:
        res = conn.execute(query).scalar()
        if res is not None:
            if isinstance(res, (datetime, pd.Timestamp)):
                return res.date()
            elif isinstance(res, date):
                return res
            elif isinstance(res, str):
                return pd.to_datetime(res).date()
    return None


EXCLUDED_BROAD_INDICES = {
    "VIX", "SPX", "NDX", "RUT", "DJX", "XSP", "MRUT", "VXX", "UVXY"
}


def collect_flow_candidates(
    engine: sa.Engine,
    target_date: Optional[date] = None,
    min_symbol_premium: float = 1_000_000.0,
    top_n: int = 50
) -> Tuple[Optional[date], List[Dict[str, Any]]]:
    """
    Collects and aggregates institutional options flow grouped by symbol for the target_date.
    Returns the Top N tickers (default 50) by total institutional premium as the primary watchlist.
    Applies staleness checking: if target_date is not specified, uses the latest session date.
    Returns: (resolved_scan_date, list of aggregated candidate dictionaries)
    """
    resolved_date = target_date or get_latest_flow_date(engine)
    if not resolved_date:
        logger.warning("Staleness Breaker: No valid trading dates found in unusual_option_flow_te.")
        return None, []

    sql_query = sa.text("""
        SELECT 
            trade_date,
            symbol,
            order_type,
            strike_price,
            strike_otm_pct,
            expiration_date,
            open_interest,
            is_unusual_oi,
            premium,
            net_score
        FROM unusual_option_flow_te
        WHERE trade_date = :t_date
    """)

    with engine.connect() as conn:
        df = pd.read_sql_query(sql_query, conn, params={"t_date": str(resolved_date)})

    if df.empty:
        logger.warning(f"No options flow records found for trade_date={resolved_date}")
        return resolved_date, []

    df.columns = df.columns.str.upper()

    candidates: List[Dict[str, Any]] = []
    for sym, group in df.groupby("SYMBOL"):
        sym_str = str(sym).upper().strip()
        if sym_str in EXCLUDED_BROAD_INDICES:
            logger.info(f"Excluding broad market/volatility index symbol: {sym_str}")
            continue

        # Filter out artifact prints (strike <= 0 or missing values)
        valid_mask = group["STRIKE_PRICE"].fillna(0) > 0
        valid_group = group[valid_mask]
        if valid_group.empty:
            continue

        total_prem = float(valid_group["PREMIUM"].fillna(0).sum())

        if total_prem < min_symbol_premium:
            continue

        call_mask = valid_group["ORDER_TYPE"].astype(str).str.contains("CALL", case=False, na=False)
        put_mask = valid_group["ORDER_TYPE"].astype(str).str.contains("PUT", case=False, na=False)

        call_prem = float(valid_group.loc[call_mask, "PREMIUM"].fillna(0).sum())
        put_prem = float(valid_group.loc[put_mask, "PREMIUM"].fillna(0).sum())

        call_pct = round((call_prem / total_prem * 100.0), 1) if total_prem > 0 else 0.0
        put_pct = round((put_prem / total_prem * 100.0), 1) if total_prem > 0 else 0.0

        if call_pct >= 55.0:
            bias = "BULLISH"
        elif put_pct >= 55.0:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        hits_count = len(group)
        call_put_ratio = min(round(call_prem / max(put_prem, 1.0), 2), 999.99)

        whale_prints = group[group["PREMIUM"] >= 1_000_000.0]
        whale_count = len(whale_prints)

        top_whale_prem = 0.0
        top_whale_action = "None"
        if not group.empty:
            sorted_by_prem = group.sort_values(by="PREMIUM", ascending=False)
            top_row = sorted_by_prem.iloc[0]
            top_whale_prem = float(top_row.get("PREMIUM", 0.0) or 0.0)
            action_type = str(top_row.get("ORDER_TYPE", "FLOW")).replace("_", " ").upper()
            strike_val = top_row.get("STRIKE_PRICE", "")
            exp_val = str(top_row.get("EXPIRATION_DATE", ""))[:10]
            try:
                strike_str = f"${float(strike_val):.2f}"
            except Exception:
                strike_str = f"${strike_val}"
            top_whale_action = f"{action_type} {strike_str} {exp_val}".strip()

        net_score_series = group["NET_SCORE"].dropna()
        net_sentiment = round(float(net_score_series.mean()), 2) if not net_score_series.empty else 0.0

        candidates.append({
            "scan_date": resolved_date,
            "ticker": sym_str,
            "total_flow_premium": total_prem,
            "formatted_flow_premium": format_dollar_amount(total_prem),
            "call_premium": call_prem,
            "put_premium": put_prem,
            "call_premium_pct": call_pct,
            "put_premium_pct": put_pct,
            "flow_bias": bias,
            "flow_hits_count": hits_count,
            "flow_call_put_ratio": call_put_ratio,
            "whale_prints_count": whale_count,
            "top_whale_premium": top_whale_prem,
            "formatted_top_whale_premium": format_dollar_amount(top_whale_prem) if top_whale_prem > 0 else "$0.00",
            "top_whale_action": top_whale_action,
            "net_sentiment_score": net_sentiment
        })

    # Sort descending by total flow premium and cap at top_n
    candidates.sort(key=lambda x: x["total_flow_premium"], reverse=True)
    if top_n and len(candidates) > top_n:
        candidates = candidates[:top_n]

    logger.info(f"Collected Top {len(candidates)} prospective flow candidates for {resolved_date}.")
    return resolved_date, candidates
