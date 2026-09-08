import os
import logging
import requests
from typing import Dict, Any, Optional, List

logger = logging.getLogger("quant.pipeline.confluence.gexdex")

GEXDEX_API_URL = os.getenv("GEXDEX_API_URL", "http://192.168.1.68:8095")
GEXDEX_API_KEY = os.getenv("GEXDEX_API_KEY", "YOUR_SECRET_API_KEY_HERE")


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


def _get_metric_val(item: Any, key: str, default: Any = None) -> Any:
    """Helper to extract value from either a dictionary or a Pydantic/object instance."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def match_candidates_gexdex(
    candidates: List[Dict[str, Any]],
    max_workers: int = 5
) -> List[Dict[str, Any]]:
    """
    Enriches flow candidate dictionaries with GEX/DEX microstructure data.
    First attempts querying the GEX/DEX HTTP microservice, then falls back to in-process service if available.
    Includes per-ticker fault isolation: if data is missing or times out, safely falls back to FLOW_ONLY.
    """
    if not candidates:
        return []

    tickers = [c["ticker"] for c in candidates]
    metrics_map = {}

    # 1. Attempt GEX/DEX Microservice HTTP API with chunking (10 tickers per request)
    chunk_size = 10
    timeout_sec = 60
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            url = f"{GEXDEX_API_URL.rstrip('/')}/api/v1/gexdex"
            headers = {"X-API-Key": GEXDEX_API_KEY}
            params = {"tickers": ",".join(chunk)}
            resp = requests.get(url, headers=headers, params=params, timeout=timeout_sec)
            if resp.ok:
                api_data = resp.json()
                chunk_data = api_data.get("batch_data", api_data)
                metrics_map.update(chunk_data)
                logger.info(f"Retrieved GEX/DEX API data for chunk {i//chunk_size + 1} ({len(chunk_data)} tickers).")
            else:
                logger.warning(f"GEX/DEX API chunk {i//chunk_size + 1} returned HTTP {resp.status_code}: {resp.text}")
        except Exception as api_err:
            logger.warning(f"GEX/DEX HTTP query chunk {i//chunk_size + 1} failed: {api_err}")

    logger.info(f"Retrieved GEX/DEX API data for {len(metrics_map)} / {len(tickers)} total tickers.")

    # 2. Fallback to in-process engine if API failed or returned empty
    if not metrics_map:
        try:
            from app.engine.service import get_gexdex_data
            logger.info(f"Querying in-process GEX/DEX engine for {len(tickers)} tickers...")
            metrics_map = get_gexdex_data(tickers)
            logger.info(f"Retrieved in-process GEX/DEX data for {len(metrics_map)} / {len(tickers)} tickers.")
        except Exception as ex:
            logger.warning(f"GEX/DEX in-process engine fallback encountered error: {ex}")
            metrics_map = {}

    enriched: List[Dict[str, Any]] = []
    for cand in candidates:
        sym = cand["ticker"].upper().strip()
        metrics = metrics_map.get(sym)

        if metrics:
            spot = float(_get_metric_val(metrics, "spot_price", 0.0) or 0.0)
            flip = float(_get_metric_val(metrics, "zero_gex_level", 0.0) or 0.0)
            c_wall = float(_get_metric_val(metrics, "call_wall", 0.0) or 0.0)
            p_wall = float(_get_metric_val(metrics, "put_wall", 0.0) or 0.0)
            net_gex = float(_get_metric_val(metrics, "net_gex", 0.0) or 0.0)
            net_dex = float(_get_metric_val(metrics, "net_dex", 0.0) or 0.0)
            regime = str(_get_metric_val(metrics, "gamma_regime", "DATA_UNAVAILABLE") or "DATA_UNAVAILABLE").upper()

            gex_above_raw = _get_metric_val(metrics, "gex_above_pct", None)
            dex_above_raw = _get_metric_val(metrics, "dex_above_pct", None)
            gex_above = float(gex_above_raw) if gex_above_raw is not None else None
            dex_above = float(dex_above_raw) if dex_above_raw is not None else None

            key_gamma = float(_get_metric_val(metrics, "key_gamma_strike", 0.0) or 0.0)
            dominant_exp = str(_get_metric_val(metrics, "dominant_expiration", "") or "")
            front_week_gex = float(_get_metric_val(metrics, "front_week_gex_pct", 0.0) or 0.0)

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
                "gex_above_pct": gex_above,
                "dex_above_pct": dex_above,
                "key_gamma_strike": key_gamma,
                "dominant_expiration": dominant_exp,
                "front_week_gex_pct": front_week_gex,
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
                "gex_above_pct": None,
                "dex_above_pct": None,
                "key_gamma_strike": None,
                "dominant_expiration": None,
                "front_week_gex_pct": None,
                "gex_available": False
            }

        merged = {**cand, **gex_data}
        enriched.append(merged)

    # Re-sort to maintain total flow premium ordering
    enriched.sort(key=lambda x: x.get("total_flow_premium", 0.0), reverse=True)
    return enriched
