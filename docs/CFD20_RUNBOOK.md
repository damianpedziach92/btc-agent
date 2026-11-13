# CFD20 PAPER deployment runbook

This change affects a simulated account only. Run the following steps on the target host during a maintenance window. `DATABASE_URL` is the SQLAlchemy application URL; `PG_DSN` is a separate libpq-compatible PostgreSQL DSN for `pg_dump` and `psql`. Keep both out of shell history and logs.

1. Stop `btc-agent-scheduler.service` and record the current Git commit and Alembic revision. This prevents old code from writing while the schema changes.
2. Back up PostgreSQL before any migration. Check the backup with `pg_restore --list` and keep it outside the repository.
3. Apply Alembic. Read one existing `paper_positions.fee_snapshot` to confirm old entries still lack `execution_model` and decode as `LEGACY_BPS`; never rewrite their JSON.
4. Activate CFD20 with the idempotent seed command. Read back the latest profile and verify the six extra charge fields are zero, spread USD/BTC is 20, status is ESTIMATED and source is `user_requested_cfd_fixed_20_usd_spread`. Repeating the command must keep the same version.
5. Inspect the account and open positions. Cash balance, position count and ledger total must match the pre-change snapshot. Existing positions retain their original profile version.
6. Restart the scheduler and monitor logs, new order events, decision notifications and account snapshot for at least one 15-minute cycle. New positions should use `CFD_FIXED_20_USD`; existing positions should still show `LEGACY_BPS`.

```bash
systemctl --user stop btc-agent-scheduler.service
git rev-parse HEAD
.venv/bin/alembic current
BACKUP_FILE="$HOME/btc-agent-before-cfd20-$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump -Fc -f "$BACKUP_FILE" "$PG_DSN"
pg_restore --list "$BACKUP_FILE" >/dev/null
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.show_paper_account
.venv/bin/python -m scripts.seed_paper_account --cfd20
.venv/bin/python -m scripts.seed_paper_account --cfd20
.venv/bin/python -m scripts.show_paper_account
psql "$PG_DSN" -c "SELECT version, execution_model, spread_usd_per_btc, trade_entry_fee_rate, trade_exit_fee_rate, margin_opening_fee_rate, rollover_rate_per_4h, slippage_bps, spread_bps, status, source FROM paper_fee_profiles ORDER BY version DESC LIMIT 2"
systemctl --user restart btc-agent-scheduler.service
journalctl --user -u btc-agent-scheduler.service -n 100 --no-pager
```

If validation fails before the scheduler restarts, leave the scheduler stopped and restore the backup into a **separate** database for comparison. A schema downgrade is safe only before new pending orders or positions are written, and only after checking dependencies. Once the new scheduler has written data, do not downgrade tables or delete new ledger rows to force a rollback. Stop the scheduler, preserve a new backup, fix forward or restore the pre-change backup under a controlled account recovery plan. Restoring the old backup discards every write made after it was taken.

The minute cycle processes saved position exits, then pending order expiry and fills, then queued decisions and Telegram. If Kraken import fails, it can still inspect stored M1; it will not infer a trigger through a missing candle. Broker fills, CFD liquidity and tick order are unavailable, so every fill remains `ESTIMATED_PAPER_ONLY`.
