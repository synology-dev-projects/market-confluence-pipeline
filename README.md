# Market Confluence Pipeline

A standalone daily batch pipeline that combines Options Flow with GEX/DEX dealer positioning to classify structural setups and pre-compute 100% of all metrics into PostgreSQL (`daily_confluence_scans` and `daily_confluence_summary`).

## Daily Execution (NAS Bash Runner)
Configured as Step 3/3 in `/volume2/homes/rachardv/scripts/run_daily_quant_pipelines.sh`:
```bash
python src/scripts/daily_incremental.py
```
