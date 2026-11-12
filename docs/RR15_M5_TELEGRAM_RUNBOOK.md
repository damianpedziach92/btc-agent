# RR 1.5, M5 and position-only Telegram deployment

This changes an active PAPER scheduler. Preserve the current account, fee profile, open positions and ledger. Never create a synthetic trade or Telegram test message during deployment.

1. Record Git SHA, Alembic revision, account cash, position count, ledger count/sum, and pending legacy notification counts. Stop `btc-agent-scheduler.service`.
2. Take a PostgreSQL custom-format backup outside the repository and verify it with `pg_restore --list`. Keep credentials out of logs and shell history.
3. Run `.venv/bin/alembic upgrade head` and `.venv/bin/alembic check`. The new revision creates `paper_position_notifications` and marks old queued decision, review and order messages `DISABLED`. It does not change positions, ledger or fee profiles.
4. Compare cash, position/ledger counts and ledger sum to step 1. Confirm the existing `CFD_FIXED_20_USD` version is unchanged and all three old queues have zero `PENDING` rows. The new outbox starts empty. Do not continue if these checks fail.
5. Restart the user service. Verify minute collection and exits, then the next `:00/:05/.../:55 +25s UTC` market analysis. New decisions have `analysis_trace.trigger_timeframe='5m'`; the new outbox contains only actual OPENED/CLOSED events. No old report queue is sent.
6. Inspect rejected entries in `paper_position_events` for `INSUFFICIENT_RISK_REWARD`, and count `paper_position_notifications` by status. Do not modify preexisting positions with R:R below 1.5.

```bash
systemctl --user stop btc-agent-scheduler.service
pg_dump -Fc -f "$BACKUP_FILE" "$PG_DSN"
pg_restore --list "$BACKUP_FILE" >/dev/null
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/python -m scripts.show_paper_account
systemctl --user restart btc-agent-scheduler.service
systemctl --user status btc-agent-scheduler.service --no-pager
journalctl --user -u btc-agent-scheduler.service -n 100 --no-pager
```

If checks fail before restart, leave the scheduler stopped and investigate the backup in a separate database. After the new scheduler writes decisions or position events, avoid schema downgrade or deletion of ledger records; stop the scheduler, preserve another backup and fix forward. Telegram transport errors leave position notifications `PENDING` for retry and do not roll back PAPER trades.
