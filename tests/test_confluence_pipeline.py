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


def test_confluence_scoring_confirmed_bull():
    cand = {
        "ticker": "NVDA",
        "flow_bias": "BULLISH",
        "call_premium_pct": 72.5,
        "put_premium_pct": 27.5,
        "total_flow_premium": 25_000_000.0,
        "whale_prints_count": 5,
        "spot_price": 228.45,
        "zero_gamma_flip": 211.20,
        "net_gex": 309_000_000_000.0,
        "net_dex": 15_000_000_000.0,
        "call_wall": 235.0,
        "put_wall": 220.0,
        "gex_available": True
    }
    res = score_and_classify_record(cand)
    assert res["confluence_status"] == "CONFIRMED_BULL"
    assert res["confluence_score"] >= 80.0
    assert "above Zero Flip" in res["confluence_rationale"]
    assert "NVDA" in res["confluence_rationale"]


def test_confluence_scoring_confirmed_bear():
    cand = {
        "ticker": "TSLA",
        "flow_bias": "BEARISH",
        "call_premium_pct": 25.0,
        "put_premium_pct": 75.0,
        "total_flow_premium": 35_000_000.0,
        "whale_prints_count": 4,
        "spot_price": 205.00,
        "zero_gamma_flip": 215.00,
        "net_gex": -120_000_000_000.0,
        "net_dex": -25_000_000_000.0,
        "call_wall": 225.0,
        "put_wall": 195.0,
        "gex_available": True
    }
    res = score_and_classify_record(cand)
    assert res["confluence_status"] == "CONFIRMED_BEAR"
    assert res["confluence_score"] >= 80.0
    assert "below Zero Flip" in res["confluence_rationale"]


def test_confluence_scoring_structural_hedge():
    cand = {
        "ticker": "SPY",
        "flow_bias": "BEARISH",
        "call_premium_pct": 30.0,
        "put_premium_pct": 70.0,
        "total_flow_premium": 150_000_000.0,
        "whale_prints_count": 8,
        "spot_price": 769.25,
        "zero_gamma_flip": 745.00,
        "net_gex": 850_000_000_000.0,
        "net_dex": 50_000_000_000.0,
        "call_wall": 785.0,
        "put_wall": 750.0,
        "gex_available": True
    }
    res = score_and_classify_record(cand)
    assert res["confluence_status"] == "STRUCTURAL_HEDGE"
    assert "portfolio hedging" in res["confluence_rationale"]


def test_confluence_scoring_vol_pin():
    cand = {
        "ticker": "AAPL",
        "flow_bias": "NEUTRAL",
        "call_premium_pct": 51.0,
        "put_premium_pct": 49.0,
        "total_flow_premium": 10_000_000.0,
        "whale_prints_count": 0,
        "spot_price": 225.00,
        "zero_gamma_flip": 220.00,
        "net_gex": 120_000_000_000.0,
        "net_dex": 0.0,
        "call_wall": 230.0,
        "put_wall": 220.0,
        "gex_available": True
    }
    res = score_and_classify_record(cand)
    assert res["confluence_status"] == "VOL_PIN"
    assert "locks spot" in res["confluence_rationale"]


def test_confluence_scoring_flow_only_fallback():
    cand = {
        "ticker": "XYZ",
        "flow_bias": "BULLISH",
        "call_premium_pct": 80.0,
        "put_premium_pct": 20.0,
        "total_flow_premium": 5_000_000.0,
        "whale_prints_count": 1,
        "gex_available": False
    }
    res = score_and_classify_record(cand)
    assert res["confluence_status"] == "FLOW_ONLY"
    assert res["confluence_score"] == 40.0
    assert "currently unavailable" in res["confluence_rationale"]


def test_build_daily_summary():
    scan_dt = date(2026, 9, 4)
    scored = [
        {"confluence_status": "CONFIRMED_BULL", "top_whale_premium": 5_000_000.0, "formatted_top_whale_premium": "$5.00M", "ticker": "NVDA"},
        {"confluence_status": "CONFIRMED_BULL", "top_whale_premium": 2_000_000.0, "formatted_top_whale_premium": "$2.00M", "ticker": "AMD"},
        {"confluence_status": "CONFIRMED_BEAR", "top_whale_premium": 10_000_000.0, "formatted_top_whale_premium": "$10.00M", "ticker": "TSLA"},
        {"confluence_status": "VOL_PIN", "top_whale_premium": 0.0, "formatted_top_whale_premium": "$0.00", "ticker": "AAPL"},
    ]
    summary = build_daily_summary(scan_dt, scored)
    assert summary["scan_date"] == scan_dt
    assert summary["total_scanned_count"] == 4
    assert summary["confirmed_bull_count"] == 2
    assert summary["confirmed_bear_count"] == 1
    assert summary["vol_pin_count"] == 1
    assert summary["top_whale_ticker"] == "TSLA"
    assert summary["formatted_top_whale_premium"] == "$10.00M"
    assert summary["market_regime_summary"] == "BULLISH FLOW CONFLUENCE"
    assert "2026-09-04" in summary["session_label"]


def test_schema_table_definitions():
    assert "scan_date" in DAILY_CONFLUENCE_SCANS.c
    assert "ticker" in DAILY_CONFLUENCE_SCANS.c
    assert "confluence_score" in DAILY_CONFLUENCE_SCANS.c
    assert "wall_spread_range" in DAILY_CONFLUENCE_SCANS.c
    assert "confluence_rationale" in DAILY_CONFLUENCE_SCANS.c

    pk_cols = [col.name for col in DAILY_CONFLUENCE_SCANS.primary_key.columns]
    assert pk_cols == ["scan_date", "ticker"]

    assert "scan_date" in DAILY_CONFLUENCE_SUMMARY.c
    assert "session_label" in DAILY_CONFLUENCE_SUMMARY.c
    assert "market_regime_summary" in DAILY_CONFLUENCE_SUMMARY.c
