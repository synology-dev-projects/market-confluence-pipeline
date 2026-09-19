import os
import sys
import argparse
import logging
from datetime import datetime, date
import pandas as pd
import sqlalchemy as sa

# Setup candidate import paths
script_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.dirname(script_dir)
pipeline_dir = os.path.dirname(src_dir)
quant_system_dir = os.path.dirname(pipeline_dir)

for p in [
    pipeline_dir,
    src_dir,
    os.path.join(quant_system_dir, "common-lib"),
    os.path.join(quant_system_dir, "common_lib"),
    os.path.join(quant_system_dir, "quant-pwa", "gateway"),
    "/app",
    "/volume2/homes/rachardv/git-repos/master/common-lib"
]:
    if os.path.exists(p) and p not in sys.path:
        sys.path.insert(0, p)

from src.database.schema import ensure_tables, upsert_scans, upsert_summary
from src.pipeline.collector import collect_flow_candidates
from src.pipeline.gexdex_matcher import match_candidates_gexdex
from src.pipeline.confluence_scorer import score_all_candidates, build_daily_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("quant.pipeline.confluence.daily")


def run_pipeline(target_date_str: str = None, min_premium: float = 1_000_000.0) -> int:
    """Executes the daily market confluence scan batch."""
    logger.info("======================================================================")
    logger.info("   STARTING MARKET CONFLUENCE DAILY INCREMENTAL PIPELINE")
    logger.info("======================================================================")

    config = None
    try:
        # 1. Resolve Engine via common_lib
        from common_lib.config.main_config import load_config
        from common_lib.connectors.postgres import get_postgres_engine
        config = load_config()
        engine = get_postgres_engine(config)

        # 2. Ensure Target Tables Exist
        ensure_tables(engine)

        # 3. Parse Target Date
        target_dt = None
        if target_date_str:
            target_dt = pd.to_datetime(target_date_str).date()

        # 4. Step 1: Collect Flow Candidates
        resolved_date, flow_candidates = collect_flow_candidates(
            engine,
            target_date=target_dt,
            min_symbol_premium=min_premium
        )

        if not flow_candidates:
            logger.info(f"No flow records found or staleness circuit breaker engaged for date={resolved_date}. Exiting cleanly.")
            return 0

        logger.info(f"Identified {len(flow_candidates)} prospective symbols from unusual flow for {resolved_date}.")

        # 5. Step 2: Match with GEX/DEX
        matched_candidates = match_candidates_gexdex(flow_candidates)

        # 6. Step 3: Confluence Scoring & Classification
        scored_records = score_all_candidates(matched_candidates)
        summary_record = build_daily_summary(resolved_date, scored_records, total_watchlist_count=len(flow_candidates))

        # 7. Step 4: Idempotent Upsert to PostgreSQL
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM daily_confluence_scans WHERE scan_date = :dt"), {"dt": resolved_date})
        upsert_scans(engine, scored_records)
        upsert_summary(engine, summary_record)

        logger.info("----------------------------------------------------------------------")
        logger.info(f"  ASYMMETRIC RADAR SCAN COMPLETE: {resolved_date}")
        logger.info(f"  Watchlist Scanned  : {summary_record['total_watchlist_count']}")
        logger.info(f"  Qualifying Plays   : {summary_record['total_scanned_count']} (Top 10 Capped)")
        logger.info(f"  Bull Springs       : {summary_record['qualifying_bull_spring_count']}")
        logger.info(f"  Bear Exhaustions   : {summary_record['qualifying_bear_exhaustion_count']}")
        logger.info(f"  Top Catalyst       : {summary_record['top_catalyst_ticker']} ({summary_record['top_catalyst_expiry']})")
        logger.info(f"  Market Regime      : {summary_record['market_regime_summary']}")
        logger.info("======================================================================")
        return 0

    except Exception as ex:
        logger.error(f"Market Confluence pipeline failed: {ex}", exc_info=True)
        try:
            from common_lib.connectors.alerts import dispatch_pipeline_failure_alert
            dispatch_pipeline_failure_alert(
                pipeline_name="Market Confluence",
                error=ex,
                session_date=target_date_str,
                config=config
            )
        except Exception as alert_ex:
            logger.error(f"Failed to dispatch failure alert: {alert_ex}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="Daily Market Confluence ETL Pipeline")
    parser.add_argument("--date", help="Optional target market session date (YYYY-MM-DD)")
    parser.add_argument("--min-premium", type=float, default=1_000_000.0, help="Minimum total flow premium per symbol ($)")
    args = parser.parse_args()

    exit_code = run_pipeline(target_date_str=args.date, min_premium=args.min_premium)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
