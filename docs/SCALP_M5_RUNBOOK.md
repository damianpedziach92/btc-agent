# SCALP_M5 PAPER

The scheduler still analyzes each closed M5 and collects M1 each minute. Each
analysis stores six deterministic M5 observations (three families, LONG and
SHORT) and rejection counts in `agent_decisions.analysis_trace`. The LLM may
propose `INTRADAY_M15` or `SCALP_M5`. New pending CREATE/AMEND and immediate
proposals must identify the strategy. Existing rows with `strategy IS NULL`
remain historical M15 records; their balances, fee snapshots and exits are
not rewritten.

`SCALP_M5_V1` uses ATR14 of consecutive closed M5, the last three closed M5
swings, a 0.20 ATR stop buffer and 0.60 ATR minimum stop distance. A scalp
position has a 60 minute timeout. The separate scalp risk profile caps risk
at 1% of equity per entry, daily realized scalp loss at 3%, consecutive
losses at three, cooldown after a loss at 15 minutes, and openings at four
per hour. Global margin and aggregate risk limits still apply. All values
are research settings for PAPER, not a claim of profitability.
An isolated 2% PAPER experiment can set `PAPER_SCALP_MAX_RISK_PER_POSITION=0.02`
and produce its own replay report; the deployed default remains 1%.

## Deployment

1. Record the current Alembic revision, account cash, number of open
   positions, pending orders and ledger rows. Stop
   `btc-agent-scheduler.service` before changing the schema.
2. Create a `pg_dump -Fc` backup with restrictive permissions and validate
   it with `pg_restore --list`.
3. Run `alembic upgrade head`, then `alembic check`. Verify that historical
   positions and pending orders have nullable strategy fields and that cash,
   position count and ledger count match the pre-deployment snapshot.
4. Run `python -m scripts.run_market_agent --preview`, restart the service,
   and inspect the journal. A preview calls the model but writes no decision,
   order or Telegram message.
5. Observe five new M5 decisions and compare WAIT share, eligible candidates,
   pending creations/fills and rejection codes with the preceding five.
   Do not infer an edge from a handful of decisions.

The migration is additive. Before new writes, a schema downgrade can remove
the new nullable columns. After new writes, preserve a fresh backup and fix
forward so the append-only ledger and new strategy snapshots remain intact.

## Research replay

`python -m scripts.replay_paper --start 2026-11-11T00:00:00Z --end
2026-11-12T00:00:00Z --output-prefix /tmp/btc-scalp-replay` compares the
existing baseline with three M5 families on the same stored closed OHLC data.
M5 LIMIT orders become eligible on a later M1 bar. Fills and intrabar barrier
order are assumptions; the report is descriptive, with two time splits and
Wilson intervals for win rate. It never writes the account or sends messages.
