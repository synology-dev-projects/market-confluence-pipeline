import os
import sys
from datetime import date
import pytest

# Add parent directories to sys.path
test_dir = os.path.dirname(os.path.abspath(__file__))
pipeline_root = os.path.dirname(test_dir)
sys.path.insert(0, pipeline_root)
sys.path.insert(0, os.path.join(pipeline_root, "src"))

from src.pipeline.collector import format_dollar_amount
from src.pipeline.gexdex_matcher import format_signed_dollar
from src.pipeline.confluence_scorer import (
    score_and_classify_record,
    score_all_candidates,
    build_daily_summary
)
from src.database.schema import DAILY_CONFLUENCE_SCANS, DAILY_CONFLUENCE_SUMMARY


def test_format_dollar_amount():
    assert format_dollar_amount(1_500_000_000) == "$1.50B"
    assert format_dollar_amount(22_900_000) == "$22.90M"
    assert format_dollar_amount(750_000) == "$750.00K"
    assert format_dollar_amount(120.50) == "$120.50"
    assert format_dollar_amount(0) == "$0.00"
    assert format_dollar_amount(None) == "$0.00"


def test_format_signed_dollar():
    assert format_signed_dollar(309_200_000_000) == "+$309.20B"
    assert format_signed_dollar(-12_500_000) == "-$12.50M"
    assert format_signed_dollar(0) == "$0.00"


def test_confluence_scoring_bull_spring():
    cand = {
        "ticker": "NVDA",
        "scan_date": date(2026, 9, 4),
        "spot_price": 350.0,
        "gex_above_pct": 85.0,
        "dex_above_pct": 88.0,
        "call_wall": 355.0,
        "put_wall": 330.0,
        "key_gamma_strike": 355.0,
        "dominant_expiration": "2026-09-18",
        "call_premium": 15_000_000.0,
        "put_premium": 3_000_000.0,
        "total_flow_premium": 18_000_000.0,
        "flow_hits_count": 16,
        "gex_available": True
    }
    res = score_and_classify_record(cand)
    assert res["play_type"] == "BULL_SPRING"
    assert res["exposure_imbalance_pct"] == 88.0
    assert res["imbalance_type"] == "DEX"
    assert res["pin_wall_strike"] == 355.0
    assert res["pin_wall_type"] == "CALL_WALL"
    assert res["pin_dte"] == 14
    assert res["flow_call_put_ratio"] == 5.0
    assert res["viability_score"] >= 85.0
    assert "Bull Spring" in res["confluence_rationale"]


def test_confluence_scoring_bear_exhaustion():
    cand = {
        "ticker": "TSLA",
        "scan_date": date(2026, 9, 4),
        "spot_price": 200.0,
        "gex_above_pct": 15.0,  # 85.0% below
        "dex_above_pct": 20.0,  # 80.0% below
        "call_wall": 220.0,
        "put_wall": 198.0,
        "key_gamma_strike": 200.0,
        "dominant_expiration": "2026-09-11",
        "call_premium": 2_000_000.0,
        "put_premium": 8_000_000.0,
        "total_flow_premium": 10_000_000.0,
        "flow_hits_count": 10,
        "gex_available": True
    }
    res = score_and_classify_record(cand)
    assert res["play_type"] == "BEAR_EXHAUSTION"
    assert res["exposure_imbalance_pct"] == 85.0
    assert res["imbalance_type"] == "GEX"
    assert res["pin_wall_strike"] == 198.0
    assert res["pin_wall_type"] == "PUT_WALL"
    assert res["pin_dte"] == 7
    assert res["flow_call_put_ratio"] == 4.0
    assert res["viability_score"] >= 85.0
    assert "Bear Exhaustion" in res["confluence_rationale"]


def test_confluence_scoring_non_qualifying():
    cand = {
        "ticker": "AAPL",
        "scan_date": date(2026, 9, 4),
        "spot_price": 225.0,
        "gex_above_pct": 55.0,
        "dex_above_pct": 50.0,
        "call_wall": 230.0,
        "put_wall": 220.0,
        "dominant_expiration": "2026-09-18",
        "call_premium": 5_000_000.0,
        "put_premium": 5_000_000.0,
        "total_flow_premium": 10_000_000.0,
        "flow_hits_count": 5,
        "gex_available": True
    }
    res = score_and_classify_record(cand)
    assert res["play_type"] is None
    assert res["viability_score"] is None
    assert res["confluence_status"] == "NON_QUALIFYING"


def test_confluence_scoring_flow_only_fallback():
    cand = {
        "ticker": "XYZ",
        "call_premium": 5_000_000.0,
        "put_premium": 1_000_000.0,
        "total_flow_premium": 6_000_000.0,
        "flow_hits_count": 2,
        "gex_available": False
    }
    res = score_and_classify_record(cand)
    assert res["play_type"] is None
    assert res["confluence_status"] == "FLOW_ONLY"
    assert "unavailable" in res["confluence_rationale"]


def test_score_all_candidates_top_10_cap_and_rank():
    candidates = []
    # Generate 15 qualifying candidates with descending exposure imbalance
    for i in range(15):
        candidates.append({
            "ticker": f"SYM{i:02d}",
            "scan_date": date(2026, 9, 4),
            "spot_price": 100.0 + i,
            "gex_above_pct": 80.0 + i,  # 80.0 to 94.0
            "dex_above_pct": 75.0,
            "call_wall": 102.0 + i,
            "put_wall": 95.0 + i,
            "dominant_expiration": "2026-09-18",
            "call_premium": 10_000_000.0 + (i * 1_000_000),
            "put_premium": 1_000_000.0,
            "total_flow_premium": 11_000_000.0 + (i * 1_000_000),
            "flow_hits_count": 10,
            "gex_available": True
        })

    ranked = score_all_candidates(candidates, max_plays=10)
    assert len(ranked) == 10
    # Verify rank 1 to 10
    for idx, play in enumerate(ranked):
        assert play["rank"] == idx + 1
        assert play["play_type"] == "BULL_SPRING"

    # Verify strictly sorted descending by viability_score
    scores = [p["viability_score"] for p in ranked]
    assert scores == sorted(scores, reverse=True)


def test_build_daily_summary():
    scan_dt = date(2026, 9, 4)
    scored = [
        {"play_type": "BULL_SPRING", "ticker": "NVDA", "top_whale_premium": 5_000_000.0, "formatted_top_whale_premium": "$5.00M", "pin_wall_strike": 355.0, "pin_wall_type": "CALL_WALL", "pin_dte": 14},
        {"play_type": "BULL_SPRING", "ticker": "AMD", "top_whale_premium": 2_000_000.0, "formatted_top_whale_premium": "$2.00M", "pin_wall_strike": 160.0, "pin_wall_type": "CALL_WALL", "pin_dte": 7},
        {"play_type": "BEAR_EXHAUSTION", "ticker": "TSLA", "top_whale_premium": 10_000_000.0, "formatted_top_whale_premium": "$10.00M", "pin_wall_strike": 198.0, "pin_wall_type": "PUT_WALL", "pin_dte": 7},
    ]
    summary = build_daily_summary(scan_dt, scored, total_watchlist_count=50)
    assert summary["scan_date"] == scan_dt
    assert summary["total_scanned_count"] == 3
    assert summary["total_watchlist_count"] == 50
    assert summary["qualifying_bull_spring_count"] == 2
    assert summary["qualifying_bear_exhaustion_count"] == 1
    assert summary["top_whale_ticker"] == "TSLA"
    assert summary["formatted_top_whale_premium"] == "$10.00M"
    assert summary["top_catalyst_ticker"] == "NVDA"
    assert "$355 Call Wall • 14 DTE" in summary["top_catalyst_expiry"]
    assert summary["market_regime_summary"] == "BULL SPRING CONFLUENCE"
    assert "2026-09-04" in summary["session_label"]


def test_schema_table_definitions():
    assert "scan_date" in DAILY_CONFLUENCE_SCANS.c
    assert "ticker" in DAILY_CONFLUENCE_SCANS.c
    assert "play_type" in DAILY_CONFLUENCE_SCANS.c
    assert "rank" in DAILY_CONFLUENCE_SCANS.c
    assert "exposure_imbalance_pct" in DAILY_CONFLUENCE_SCANS.c
    assert "pin_wall_strike" in DAILY_CONFLUENCE_SCANS.c
    assert "viability_score" in DAILY_CONFLUENCE_SCANS.c

    pk_cols = [col.name for col in DAILY_CONFLUENCE_SCANS.primary_key.columns]
    assert pk_cols == ["scan_date", "ticker"]

    assert "scan_date" in DAILY_CONFLUENCE_SUMMARY.c
    assert "total_watchlist_count" in DAILY_CONFLUENCE_SUMMARY.c
    assert "qualifying_bull_spring_count" in DAILY_CONFLUENCE_SUMMARY.c
    assert "qualifying_bear_exhaustion_count" in DAILY_CONFLUENCE_SUMMARY.c
    assert "top_catalyst_ticker" in DAILY_CONFLUENCE_SUMMARY.c
    assert "top_catalyst_expiry" in DAILY_CONFLUENCE_SUMMARY.c


def test_index_exclusion_and_spot_validity():
    from src.pipeline.confluence_scorer import score_and_classify_record

    # 1. Broad index exclusion
    vix_cand = {
        "ticker": "VIX",
        "spot_price": 14.53,
        "gex_above_pct": 90.0,
        "dex_above_pct": 99.0,
        "call_wall": 14.5,
        "dominant_expiration": "2026-09-18",
        "call_premium": 5_000_000.0,
        "put_premium": 0.0,
        "flow_hits_count": 1,
        "gex_available": True
    }
    res = score_and_classify_record(vix_cand)
    assert res["play_type"] is None
    assert res["confluence_status"] == "INDEX_EXCLUDED"

    # 2. Invalid spot price
    bad_spot = {
        "ticker": "AAPL",
        "spot_price": 0.0,
        "gex_above_pct": 90.0,
        "dex_above_pct": 90.0,
        "call_wall": 230.0,
        "gex_available": True
    }
    res_bad = score_and_classify_record(bad_spot)
    assert res_bad["play_type"] is None
    assert res_bad["confluence_status"] == "INVALID_SPOT"
