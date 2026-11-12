# BTC agent — Kraken data, PAPER CFD20

The collector imports closed Kraken BTC/USD 1m candles each minute and aggregates 5m, 15m, 1h, 4h and 1d. MA50/100/200, Bollinger Bands, ATR and market structure are available to the market analyst. The analyst runs every 5 minutes after a completed M5; the read-only decision reviewer checks completed two-hour UTC windows. No broker orders, private exchange endpoints or live trading keys are used.

## PAPER CFD20

**CFD20 | spread $20/BTC | fee 0 | simulation only.** This is a user-requested assumption, not an observed Plus500 or Kraken CFD quote. Kraken remains the reference market. The reference midpoint is `(best bid + best ask) / 2`; when the two-sided quote is unavailable, the system labels the last trade as `last_trade_mid_assumption`. The simulated executable BUY price is `mid + $10/BTC`; SELL is `mid - $10/BTC`. There is no separate commission, financing, margin opening charge or slippage. Full round-trip spread cost at unchanged midpoint is `$20 × BTC quantity`: 0.01 BTC costs $0.20. Leverage remains 1×.

The account is versioned in `paper_fee_profiles`. New positions require the active `CFD_FIXED_20_USD` profile. Older positions keep their original `fee_snapshot`, use their historical fee rules on close, and remain in the append-only ledger. The seed command without a profile flag only ensures that the account exists; legacy profile flags are rejected once CFD20 is active. Neither migration nor activation resets cash, positions, decisions or history.

SL and TP levels are **executable closing-side CFD prices**. LONG closes at simulated bid (`mid - 10`), SHORT at simulated ask (`mid + 10`). Closed M1 high/low determines whether a barrier was touched. Both barriers in one minute mean SL first. A worse opening price after an SL gap is used and marked `GAP_FILL_ASSUMPTION`; ordinary touch fills at the exact barrier. Historical legacy positions keep their legacy barrier interpretation. OHLC cannot reveal intrabar order or real liquidity, so fills and PnL are estimates.

Risk sizing uses the same executable exit convention as post-entry equity checks. The configured risk and margin limits, fresh M1 feed, structural M15 stop and opposing-position prohibition remain in force. `.env.example` documents the limits. `PAPER_MAX_TOTAL_MARGIN=0.90` and `PAPER_MIN_FREE_MARGIN=0.05` permit substantial exposure even at 1×.
Every new CFD20 entry requires executable net reward/risk of at least **1.5**, checked again at actual fill. SL and TP are required. Rejections include `INSUFFICIENT_RISK_REWARD` diagnostics; existing positions are unchanged. `PAPER_EXPERIMENTAL_MIN_RR_AFTER_COSTS` can tighten the threshold further; `PAPER_EXPERIMENTAL_MAX_CHASE_ATR` is optional. Both default to zero.

## Pending orders

Every 5-minute analysis can issue structured `pending_order_actions`: CREATE, AMEND, CANCEL or HOLD. The minute collector evaluates saved orders on **complete M1** after processing existing positions' SL/TP. The agent is not called each minute. `to_monitor` is informational and cannot execute a trade.

| Order | Executable CFD trigger | Meaning |
| --- | --- | --- |
| BUY_LIMIT | ask ≤ level | LONG on pullback |
| BUY_STOP | ask ≥ level | LONG on breakout |
| SELL_LIMIT | bid ≥ level | SHORT on rebound |
| SELL_STOP | bid ≤ level | SHORT on breakdown |

Orders cannot fill at CREATE. The first eligible candle must start at or after the order's creation or last amendment minute boundary. LIMIT fills at its level or a better candle open. STOP fills at its trigger or a worse candle open after a gap. Order size is computed at fill using the same PAPER risk engine. Up to three active orders are allowed, with one per scenario and no opposing pending sides. CREATE does not reserve cash; potential exposure is checked, and all limits are checked again at fill. Default maximum TTL is 60 minutes. An expired order cannot fill even if a later candle crosses its level. Processing is idempotent by execution key. A data gap is never inferred as a trigger. A fill starts SL/TP protection from the next complete M1, because the intrabar path is unknown.
An optional machine-checkable `invalidating_condition` can cancel the order when a closed candle crosses a specified level; the condition is evaluated before a trigger in the same bar. Free-form text never cancels or fills an order.

The agent receives active order IDs and revisions. AMEND/CANCEL/HOLD require the exact ID and revision; a filled order cannot be amended. Order events are stored in `paper_order_events`. Telegram sends one OPENED alert when an order fills and no alert for CREATE, AMEND, CANCEL or EXPIRE. `.venv/bin/python -m scripts.show_paper_account` prints active and recent orders, cash, equity, positions and execution model.

## Market agent and reviewer

The analyst considers H1/M15 context and chooses either `INTRADAY_M15` or `SCALP_M5`, with M1 timing. SCALP_M5 supports `M5_PULLBACK_RETEST`, `M5_RANGE_REJECTION` and `M5_BREAKOUT_RETEST`; every analysis stores six closed-candle candidates and rejection counts. Read-only tools return latest price, candles, indicators, PAPER account, positions and a net R:R preflight calculator. Decisions, tool trace, proposals, position actions and pending order actions are saved to `agent_decisions`. For new decisions, `candle_timestamp` means the start of the triggering closed M5; `analysis_trace.trigger_timeframe`, `trigger_candle_end`, input size and token usage record the new cadence. Older decisions keep their M15 interpretation. The existing unique `(instrument, candle_timestamp)` key prevents a duplicate when an old M15 and new M5 start share a timestamp. The reviewer evaluates every action with historical closed candles, execution results and costs. Large contexts are compacted per action and processed in bounded batches; saved metadata includes `batch_count`, `covered_action_ids` and removed redundant fields. A failed batch does not save a partial review.

Telegram uses only the transactional `paper_position_notifications` outbox for actual OPENED and CLOSED events, including automatic TP/SL between analyses. Messages contain the saved execution price, net PnL on close, short position ID and source. No WAIT, decision summary, pending order status or reviewer report is sent. The migration marks older queued reports `DISABLED` without deleting them.

```bash
.venv/bin/python -m scripts.run_market_agent --check-tools
.venv/bin/python -m scripts.run_market_agent --preview
.venv/bin/python -m scripts.run_market_agent --force
.venv/bin/python -m scripts.run_decision_review --preview
.venv/bin/python -m scripts.run_decision_review --window-end 2026-11-11T16:00:00Z
.venv/bin/python -m scripts.run_decision_review --backfill-from 2026-11-10T00:00:00Z --backfill-to 2026-11-11T16:00:00Z
```

`--preview` calls the model but saves no decision, order, position, review or notification. Backfill is limited to seven days per command and skips already saved windows. An API failure stops the run, leaving the failed window for retry. Historic model outputs are educational evidence, not a statistical proof of strategy performance.

## Read-only replay

The replay compares saved agent proposals with deterministic H1/M15 rules and all three SCALP_M5 families. It divides the selected UTC period into in-sample and out-of-sample halves, uses only candles closed at each signal, models M5 LIMIT orders on later M1 bars, and applies CFD20 execution, risk sizing and protective barrier functions. The CSV contains every simulated trade; JSON contains metrics and the detailed records; Markdown contains a compact comparison.

```bash
.venv/bin/python -m scripts.replay_paper --start 2026-10-01T00:00:00Z --end 2026-11-01T00:00:00Z --output-prefix reports/cfd20-replay
```

The replay makes no LLM calls and never writes to the PAPER account. OHLC does not show intrabar execution sequence or actual CFD quotes. Treat the metrics as deterministic experiments, not forecasts. Original ledger history stays unchanged; any CFD20 result for legacy positions is a separate counterfactual simulation.
The agent baseline replays saved entry proposals and CLOSE actions; under CFD20 a protective barrier can close the simulated position before the agent's later CLOSE. It therefore need not match the original 12-position ledger.

For the 12 previously closed positions, [a separate counterfactual report](reports/btc-paper-cfd20-counterfactual-2026-11-12.md) shows the cost-only change at the original exit times and a second run of CFD-side TP/SL. Rebuild it read-only with `.venv/bin/python -m scripts.cfd20_counterfactual --output-prefix reports/btc-paper-cfd20-counterfactual-2026-11-12`. The original report and ledger are untouched.

## Setup and deployment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed_paper_account --cfd20
.venv/bin/python -m scripts.show_paper_account
systemctl --user restart btc-agent-scheduler.service
```

Set `DATABASE_URL`, `OPENAI_API_KEY` and optional `OPENAI_MODEL` in `.env`; the default model is `gpt-6-luna`. `DECISION_REVIEW_MODEL` can override it for the reviewer. Telegram needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The position outbox retries failed sends. Do not commit `.env`. The current deployment procedure is in [docs/SCALP_M5_RUNBOOK.md](docs/SCALP_M5_RUNBOOK.md); the original CFD20 activation procedure is in [docs/CFD20_RUNBOOK.md](docs/CFD20_RUNBOOK.md).

For isolated PostgreSQL test runs, use a separate database or schema with its own `DATABASE_URL`, apply Alembic there, then run `.venv/bin/python -m pytest -q`. Integration tests use real transactions; they do not contact a trading API.
