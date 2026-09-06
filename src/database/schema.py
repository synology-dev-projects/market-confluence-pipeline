import logging
from typing import List, Dict, Any, Optional
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger("quant.pipeline.confluence.schema")

METADATA = sa.MetaData()

DAILY_CONFLUENCE_SCANS = sa.Table(
    "daily_confluence_scans",
    METADATA,
    sa.Column("scan_date", sa.Date, primary_key=True),
    sa.Column("ticker", sa.String(16), primary_key=True),
    sa.Column("spot_price", sa.Numeric(12, 2)),
    sa.Column("formatted_spot_price", sa.String(24)),
    sa.Column("total_flow_premium", sa.Numeric(18, 2)),
    sa.Column("formatted_flow_premium", sa.String(32)),
    sa.Column("call_premium", sa.Numeric(18, 2)),
    sa.Column("put_premium", sa.Numeric(18, 2)),
    sa.Column("call_premium_pct", sa.Numeric(5, 1)),
    sa.Column("put_premium_pct", sa.Numeric(5, 1)),
    sa.Column("flow_bias", sa.String(16)),
    sa.Column("whale_prints_count", sa.Integer, default=0),
    sa.Column("top_whale_premium", sa.Numeric(18, 2)),
    sa.Column("formatted_top_whale_premium", sa.String(32)),
    sa.Column("top_whale_action", sa.String(128)),
    sa.Column("net_sentiment_score", sa.Numeric(5, 2)),
    sa.Column("gamma_regime", sa.String(128)),
    sa.Column("net_gex", sa.Numeric(18, 2)),
    sa.Column("formatted_net_gex", sa.String(32)),
    sa.Column("net_dex", sa.Numeric(18, 2)),
    sa.Column("formatted_net_dex", sa.String(32)),
    sa.Column("zero_gamma_flip", sa.Numeric(12, 2)),
    sa.Column("spot_vs_flip_pct", sa.Numeric(5, 1)),
    sa.Column("call_wall", sa.Numeric(12, 2)),
    sa.Column("put_wall", sa.Numeric(12, 2)),
    sa.Column("wall_spread_range", sa.String(128)),
    sa.Column("confluence_status", sa.String(64), nullable=False),
    sa.Column("confluence_score", sa.Numeric(5, 1), nullable=False),
    sa.Column("confluence_rationale", sa.Text),
    sa.Column("play_type", sa.String(32)),
    sa.Column("rank", sa.Integer),
    sa.Column("exposure_imbalance_pct", sa.Numeric(5, 1)),
    sa.Column("imbalance_type", sa.String(16)),
    sa.Column("pin_wall_strike", sa.Numeric(12, 2)),
    sa.Column("pin_wall_type", sa.String(32)),
    sa.Column("pin_expiration", sa.String(32)),
    sa.Column("pin_dte", sa.Integer),
    sa.Column("pin_dist_pct", sa.Numeric(5, 1)),
    sa.Column("flow_hits_count", sa.Integer, default=0),
    sa.Column("flow_call_put_ratio", sa.Numeric(8, 2)),
    sa.Column("viability_score", sa.Numeric(5, 1)),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now())
)

DAILY_CONFLUENCE_SUMMARY = sa.Table(
    "daily_confluence_summary",
    METADATA,
    sa.Column("scan_date", sa.Date, primary_key=True),
    sa.Column("session_label", sa.String(64), nullable=False),
    sa.Column("total_scanned_count", sa.Integer, nullable=False),
    sa.Column("confirmed_bull_count", sa.Integer, nullable=False),
    sa.Column("confirmed_bear_count", sa.Integer, nullable=False),
    sa.Column("vol_pin_count", sa.Integer, nullable=False),
    sa.Column("divergent_count", sa.Integer, nullable=False),
    sa.Column("top_whale_ticker", sa.String(16)),
    sa.Column("top_whale_premium", sa.Numeric(18, 2)),
    sa.Column("formatted_top_whale_premium", sa.String(32)),
    sa.Column("market_regime_summary", sa.String(64)),
    sa.Column("total_watchlist_count", sa.Integer, default=0),
    sa.Column("qualifying_bull_spring_count", sa.Integer, default=0),
    sa.Column("qualifying_bear_exhaustion_count", sa.Integer, default=0),
    sa.Column("top_catalyst_ticker", sa.String(16)),
    sa.Column("top_catalyst_expiry", sa.String(32)),
    sa.Column("scanned_at", sa.DateTime(timezone=True), server_default=sa.func.now())
)


def ensure_tables(engine: sa.Engine) -> None:
    """Ensures that daily_confluence_scans and daily_confluence_summary tables and new columns exist."""
    METADATA.create_all(engine)

    # Safe additive migrations for existing tables
    new_scan_cols = [
        ("play_type", "VARCHAR(32)"),
        ("rank", "INTEGER"),
        ("exposure_imbalance_pct", "NUMERIC(5, 1)"),
        ("imbalance_type", "VARCHAR(16)"),
        ("pin_wall_strike", "NUMERIC(12, 2)"),
        ("pin_wall_type", "VARCHAR(32)"),
        ("pin_expiration", "VARCHAR(32)"),
        ("pin_dte", "INTEGER"),
        ("pin_dist_pct", "NUMERIC(5, 1)"),
        ("flow_hits_count", "INTEGER DEFAULT 0"),
        ("flow_call_put_ratio", "NUMERIC(8, 2)"),
        ("viability_score", "NUMERIC(5, 1)"),
    ]
    new_summary_cols = [
        ("total_watchlist_count", "INTEGER DEFAULT 0"),
        ("qualifying_bull_spring_count", "INTEGER DEFAULT 0"),
        ("qualifying_bear_exhaustion_count", "INTEGER DEFAULT 0"),
        ("top_catalyst_ticker", "VARCHAR(16)"),
        ("top_catalyst_expiry", "VARCHAR(32)"),
    ]

    with engine.begin() as conn:
        for col_name, col_type in new_scan_cols:
            try:
                conn.execute(sa.text(f"ALTER TABLE daily_confluence_scans ADD COLUMN IF NOT EXISTS {col_name} {col_type}"))
            except Exception as e:
                logger.warning(f"Could not add column {col_name} to daily_confluence_scans: {e}")

        for col_name, col_type in new_summary_cols:
            try:
                conn.execute(sa.text(f"ALTER TABLE daily_confluence_summary ADD COLUMN IF NOT EXISTS {col_name} {col_type}"))
            except Exception as e:
                logger.warning(f"Could not add column {col_name} to daily_confluence_summary: {e}")

    logger.info("Verified/created daily_confluence_scans and daily_confluence_summary tables.")


def upsert_scans(engine: sa.Engine, records: List[Dict[str, Any]]) -> int:
    """
    Idempotently upserts a list of ticker scan dictionaries into daily_confluence_scans.
    Returns count of upserted records.
    """
    if not records:
        return 0

    valid_cols = set(col.name for col in DAILY_CONFLUENCE_SCANS.columns)
    with engine.begin() as conn:
        for raw_rec in records:
            rec = {k: v for k, v in raw_rec.items() if k in valid_cols}
            stmt = pg_insert(DAILY_CONFLUENCE_SCANS).values(**rec)
            update_dict = {
                col.name: stmt.excluded[col.name]
                for col in DAILY_CONFLUENCE_SCANS.columns
                if col.name not in ("scan_date", "ticker", "created_at")
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["scan_date", "ticker"],
                set_=update_dict
            )
            conn.execute(stmt)

    logger.info(f"Successfully upserted {len(records)} records into daily_confluence_scans.")
    return len(records)


def upsert_summary(engine: sa.Engine, summary: Dict[str, Any]) -> None:
    """
    Idempotently upserts the daily aggregate summary record into daily_confluence_summary.
    """
    if not summary:
        return

    with engine.begin() as conn:
        stmt = pg_insert(DAILY_CONFLUENCE_SUMMARY).values(**summary)
        update_dict = {
            col.name: stmt.excluded[col.name]
            for col in DAILY_CONFLUENCE_SUMMARY.columns
            if col.name not in ("scan_date", "scanned_at")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["scan_date"],
            set_=update_dict
        )
        conn.execute(stmt)

    logger.info(f"Successfully upserted daily_confluence_summary for scan_date: {summary.get('scan_date')}")
