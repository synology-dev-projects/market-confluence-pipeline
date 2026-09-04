import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger("quant.pipeline.confluence.gexdex")


def format_signed_dollar(val: Any) -> str:
    """Formats numeric exposures into signed compact dollar strings (+$309.20B, -$12.50M, $0.00)."""
    if val is None:
        return "$0.00"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return str(val)

    if v == 0:
        return "$0.00"

    sign = "+" if v > 0 else "-"
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"{sign}${abs_v / 1_000_000_000:.2f}B"
    elif abs_v >= 1_000_000:
        return f"{sign}${abs_v / 1_000_000:.2f}M"
    elif abs_v >= 1_000:
        return f"{sign}${abs_v / 1_000:.2f}K"
    return f"{sign}${abs_v:.2f}"


def match_candidates_gexdex(
    candidates: List[Dict[str, Any]],
    max_workers: int = 5
) -> List[Dict[str, Any]]:
    """
    Enriches flow candidate dictionaries with GEX/DEX microstructure data using get_gexdex_data.
    Includes per-ticker fault isolation: if data is missing or times out, safely falls back to FLOW_ONLY.
    """
    if not candidates:
        return []

    tickers = [c["ticker"] for c in candidates]
    metrics_map = {}

    try:
        from app.engine.service import get_gexdex_data
        logger.info(f"Querying GEX/DEX engine for {len(tickers)} prospective tickers...")
        metrics_map = get_gexdex_data(tickers)
        logger.info(f"Successfully retrieved GEX/DEX data for {len(metrics_map)} / {len(tickers)} tickers.")
    except Exception as ex:
        logger.warning(f"GEX/DEX batch resolution encountered error: {ex}")
        metrics_map = {}

    enriched: List[Dict[str, Any]] = []
    for cand in candidates:
        sym = cand["ticker"].upper().strip()
        metrics = metrics_map.get(sym)

        if metrics:
            spot = float(metrics.spot_price)
            flip = float(metrics.zero_gex_level)
            c_wall = float(metrics.call_wall)
            p_wall = float(metrics.put_wall)
            net_gex = float(metrics.net_gex)
            net_dex = float(metrics.net_dex)
            regime = str(metrics.gamma_regime).upper()

            spot_vs_flip = round(((spot - flip) / flip * 100.0), 1) if flip > 0 else 0.0
            wall_spread = f"${p_wall:.2f} - ${c_wall:.2f}"

            gex_data = {
                "spot_price": spot,
                "formatted_spot_price": f"${spot:.2f}",
                "gamma_regime": regime,
                "net_gex": net_gex,
                "formatted_net_gex": format_signed_dollar(net_gex),
                "net_dex": net_dex,
                "formatted_net_dex": format_signed_dollar(net_dex),
                "zero_gamma_flip": flip,
                "spot_vs_flip_pct": spot_vs_flip,
                "call_wall": c_wall,
                "put_wall": p_wall,
                "wall_spread_range": wall_spread,
                "gex_available": True
            }
        else:
            gex_data = {
                "spot_price": None,
                "formatted_spot_price": "N/A",
                "gamma_regime": "DATA_UNAVAILABLE",
                "net_gex": None,
                "formatted_net_gex": "N/A",
                "net_dex": None,
                "formatted_net_dex": "N/A",
                "zero_gamma_flip": None,
                "spot_vs_flip_pct": None,
                "call_wall": None,
                "put_wall": None,
                "wall_spread_range": "N/A",
                "gex_available": False
            }

        merged = {**cand, **gex_data}
        enriched.append(merged)

    # Re-sort to maintain total flow premium ordering
    enriched.sort(key=lambda x: x.get("total_flow_premium", 0.0), reverse=True)
    return enriched
