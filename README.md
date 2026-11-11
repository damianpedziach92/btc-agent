# BTC/USD market data and analyst

The collector fetches closed 1-minute Kraken candles every minute. Complete UTC windows produce 15m, 1h, 4h and 1d candles. Indicator snapshots contain SMA50, SMA100, SMA200 and Bollinger Bands (20 periods, 2 population standard deviations) when enough consecutive candles exist.

The market analyst runs at `:00:20`, `:15:20`, `:30:20` and `:45:20` UTC, after the collector at second 5. It has five read-only tools:

| Tool | Data |
| --- | --- |
| `get_price` | Latest Kraken spot trade, best bid/ask, spread per BTC and retrieval time |
| `get_indicators` | Up to 50 recent indicator readings for a timeframe, with earlier candles used for warm-up |
| `get_candles` | Up to 50 recent closed candles for a timeframe |
| `get_paper_account` | USD paper balance, equity estimate, margin, costs and limits |
| `get_open_positions` | All open paper positions with entry reasons, protection and costs |

It saves one structured `BUY`, `SELL` or `WAIT` decision per closed 15m candle in `agent_decisions`, including an optional `open_proposal` and `position_actions`. New decisions also save `analysis_trace`: the starting PAPER account/position snapshot, each actual tool name and arguments, result status, returned point count and newest timestamp, and the observed public ticker quote. This lets the reviewer distinguish data the analyst requested from data reconstructed later. Older decisions have no persisted trace; their tool calls may still be visible in scheduler logs. Inspect recent calls with `.venv/bin/python -m scripts.show_decision_tool_usage`. Active observations remain in `agent_monitor_items` as untrusted hypotheses. The agent receives an account and full open-position snapshot before tool use. The orchestrator validates proposals against current state and can open or close **simulated** positions. No real trading tools, private Kraken endpoints or trading keys are used. `market_analyses` remains in place for historical text reports.

The analyst is an educational, aggressive scalping simulation. It may propose a direction from a current 1m setup without waiting for every higher timeframe to agree. Before a BUY/SELL or CLOSE proposal, its instructions require candle and indicator calls for 1m, 15m and 1h, plus a concrete 15m and 1h observation in the reason or an explicit data gap. A triggered observation leads to `BUY` or `SELL` when its signal is still valid; a reversed or stale setup is invalidated. It can ask to `HOLD` or `CLOSE` an existing position. The risk engine determines size and may reject a proposal; each rejection is visible to the next analysis.

The tools can show price action, momentum and volatility from market data. They do not measure news or social sentiment. Each analysis can make at most 10 tool calls, each history call returns at most 50 points, and the combined tool responses are limited to 50,000 characters to bound model context size.

## Two-hour decision review

The read-only reviewer checks saved BTC/USD agent decisions in completed UTC windows such as `14:00–16:00`. It starts at `HH:02:20` after each even UTC hour and retries every five minutes if a review is missing or an API call fails. On startup, it checks the latest completed window. A unique database constraint prevents duplicate saved reviews or ordinary duplicate Telegram notifications. `OPENAI_API_KEY` enables it along with the market analyst; `DECISION_REVIEW_MODEL` can override `OPENAI_MODEL` for the reviewer (default: `gpt-6-luna`).

The reviewer now evaluates **each action**: proposed LONG/SHORT entries, HOLD/CLOSE instructions, and automatic PAPER closes. It sees the original proposal, backend acceptance or rejection, the linked position, fees, gross/net result, position events and any 1m candle that triggered TP/SL. For each decision it also sees the last five closed 1m, 15m and 1h candles and reconstructed MA50/100/200 and Bollinger Bands. It compares the 1m signal with 15m/1h context, SL/TP and costs, then reports one verdict and supporting evidence per action. An unrecorded position makes a CLOSE/HOLD action insufficiently verifiable. A loss or later SL alone is not proof of a poor entry. Reports, input snapshots and the instruction version are saved in `decision_reviews`. The reviewer cannot trade or change the PAPER account.

The original ticker readings and tool transcripts were not saved for older decisions, so historical claims about the exact live quote cannot be independently checked. New decisions retain a compact tool trace, including the quote and the timeframe of each candle/indicator query. The report is an educational critique, not a ground truth label. Windows with no actions are recorded without calling the model.

To inspect the most recent completed window without saving or notifying, run:

```bash
.venv/bin/python -m scripts.run_decision_review --preview
```

To audit a specific past window, pass its ending even UTC hour, for example `--window-end 2026-11-11T16:00:00Z`. Without `--preview`, the command saves the result once and sends a separate Telegram review notification if Telegram is configured. Apply database migrations and restart the scheduler after updating the code.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed_paper_account --okx-eea-taker-profile
```

The seed creates a single default account with **1000.00 USD** and never resets its balance on restart. The `--okx-eea-taker-profile` switch applies the requested **0.10% spot taker fee on both entry and exit**. OKX Europe lists this rate for Regular EEA users with an opened X-Perps account; rates for spot-only accounts differ. This is a fixed PAPER assumption, not a live account fee lookup. The current profile applies a **fixed 4 bps PAPER spread** around the midpoint of the current Kraken bid/ask, or around the last trade when no quote is available. Slippage, margin opening fee and four-hour rollover are all **zero**. This is the user-requested simulation, not a claim about live OKX spread or borrowing costs. The market-data source remains Kraken. See the [OKX Europe fee table](https://www.okx.com/en-eu/help/what-are-the-new-trading-fees-for-eea-users) and [Spot Margin borrowing explanation](https://www.okx.com/en-eu/help/eea-spot-margin-faq).

A newly seeded account without a profile is `UNCONFIGURED` and blocks new positions. Re-running the same seed and profile is idempotent; changing profile rates creates a new version and each position keeps its opening fee snapshot. The older `--estimated-demo-profile` remains available for comparisons but installs the former 0.26% simulated trade fee. Re-running it on the active account would switch the active profile back to that demo rate.

For custom simulation rates, pass all six `--trade-entry-fee-rate`, `--trade-exit-fee-rate`, `--margin-opening-fee-rate`, `--rollover-rate-per-4h`, `--slippage-bps`, `--spread-bps` plus `--fee-source` to `scripts.seed_paper_account`. Rates are fractions (`0.02% = 0.0002`); bps use whole basis-point units. No fee value is silently taken from a live Kraken account.

Set `DATABASE_URL` in `.env`. Add `OPENAI_API_KEY` to enable the analyst. `OPENAI_MODEL` is optional; the default is `gpt-6-luna`. Restart the scheduler after changing `.env`:

```bash
systemctl --user restart btc-agent-scheduler.service
```

Before adding a key, inspect the data tools without calling the model:

```bash
.venv/bin/python -m scripts.run_market_agent --check-tools
```

For a manual analysis of the latest 15m candle, use `.venv/bin/python -m scripts.run_market_agent --force`. To test edited instructions on the same candle without saving a decision, use `.venv/bin/python -m scripts.run_market_agent --preview`. The CLI prints JSON. Read saved decisions and historical reports with `.venv/bin/python -m scripts.show_market_analyses`. Restart the scheduler after editing `agents/market_analyst.py` so scheduled runs load the new instructions.

## Telegram notifications

After each **new saved 15m agent decision**, the scheduler can send a Telegram summary with the decision, PAPER execution result, all currently open positions, positions closed since the previous analysis, active monitor observations and the account balance/equity. A `WAIT` decision also produces a message. Section labels use HTML bold and structured prices show two decimal places. A long summary is split into multiple Telegram messages. Notifications are sent after the paper command is processed; if execution is still pending for over two minutes, the message says so. Failed deliveries are retried by the minute collector. A successful delivery is recorded in PostgreSQL, preventing ordinary duplicates after restart. An API timeout or a partial multi-message delivery can still lead to a duplicate on retry. `--preview` and an already saved 15m candle do not queue messages.

1. Create a bot with Telegram's BotFather and send `/start` to that bot from the target chat.
2. Put `TELEGRAM_BOT_TOKEN` in `.env`, then run `.venv/bin/python -m scripts.telegram_setup --list-chats` to find the `chat_id` without printing the token.
3. Put `TELEGRAM_CHAT_ID` in `.env`, run `.venv/bin/python -m scripts.telegram_setup --send-test`, then `systemctl --user restart btc-agent-scheduler.service`.

Both variables are required. If either is absent, new decisions are not queued for Telegram; enabling notifications later starts with future decisions, without sending an old backlog. Keep the bot token out of logs and version control. The message uses UTC timestamps. Telegram's [Bot API `sendMessage` and HTML formatting documentation](https://core.telegram.org/bots/api#sendmessage) describes the chat ID, text and formatting fields.

Inspect the paper account and open positions with `.venv/bin/python -m scripts.show_paper_account`. The collector processes completed 1m candles after storage and aggregation; it can close paper positions even when the LLM is unavailable. Saved but unfinished paper commands are retried with idempotency keys after restart. There is no FastAPI server in this repository, so diagnostics use CLI scripts.

## Paper risk and accounting

Risk limits are configurable in `.env` using the names in [`.env.example`](.env.example). Defaults: at most 1% of current equity at risk per new position with an SL, 3% across open positions, 20% equity margin per position, 60% total margin, 20% free margin reserve, five open positions, and **1x maximum leverage**. Opposing BTC/USD positions are rejected. Opening also requires three consecutive fresh closed 1m candles. Missing SL is rejected by default; explicitly enabling it limits allocation to 5% equity and holding time to four hours, with an emergency drawdown check. All positions time out after 24 hours by default. Agent proposals never choose quantity or bypass these limits. A proposed TP must yield at least `PAPER_MIN_TP_NET_PROFIT_USD` (default $0.01) after the simulated entry price, entry trade fee, margin opening fee, exit trade fee at the exact TP and a conservative allowance for every completed four-hour rollover period through the configured maximum hold time. The check uses the final rounded position size and the fee profile snapshot. A gross-positive but net-negative TP is rejected; a missing TP remains allowed. Fee rates are included in the agent's account snapshot so it can estimate a viable target before proposing one.

`cash_balance` is the initial 1000 USD plus realized gross PnL minus paid entry, exit and holding costs. Margin reservation is recorded as a separate ledger delta, not deducted from cash a second time. `equity_estimate` uses estimated immediate liquidation prices after the configured spread and exit trade fees; entry and holding costs are already in cash. A position's `unrealized_net_estimate` includes its entry and holding costs, estimated exit spread and exit fee. The append-only ledger can reconcile cash and reserved margin; position events record accepted, rejected, fee and close decisions. Amounts use `Decimal`. An extreme gap loss is capped at available paper cash, marks the account `BANKRUPT`, and records the uncovered loss separately.

The `SPOT_MARGIN` model can charge trade fees, margin opening and completed four-hour rollover periods when configured; the active built-in profile charges only trade fees and fixed spread. A LONG uses a USD borrowing basis. A SHORT models borrowed BTC and converts its BTC-denominated margin fee to USD at the entry or a close-to-due 1m mark; a missing near-due mark uses the known entry price as a flagged estimate. This simplified conversion cannot reproduce Kraken's actual margin funding or execution. `PERPETUAL` funding has a separate signed-rate calculator and an importable historical-rate table (`scripts.import_paper_funding_rates`); perpetual position opening is disabled in this MVP until a complete sourced funding profile is available. Positive funding costs LONG and credits SHORT; negative funding reverses the flow. Kraken describes [hourly EEA perpetual funding](https://support.kraken.com/gb/articles/perpetual-contract-specifications-for-clients-in-the-eea).

An agent entry uses a fresh **post-analysis** public ticker and applies half the configured fixed PAPER spread on the adverse side of the Kraken bid/ask midpoint: LONG buys above the midpoint and SHORT sells below it. The current profile applies no extra slippage. When no bid/ask is available, the last trade or historical reference price serves as the midpoint. Protection starts with the first complete 1m candle after entry. A completed candle's high/low detects TP/SL; if both hit, **SL wins**. The PAPER model now assumes an exact fill at the configured TP or SL whenever a closed 1m candle touches it. The exit trade fee is charged on that level; no exit spread or slippage is added to barrier fills. This intentionally idealized assumption also applies across gaps and cannot be verified as a realizable exchange fill from OHLC alone. The collector closes at the next minute run after the triggering candle completes, before the next 15m agent analysis. Other exits (`AGENT`, `TIMEOUT`, `RISK_LIMIT`) use the configured half-spread on the adverse side of a fresh quote midpoint, or a later available 1m open as the reference midpoint. Before executing an agent action, pending candle triggers are checked again so automatic TP/SL has priority. The event records the candle-end signal time separately from the processing time and marks the source `1m_ohlc_barrier_assumption`. Spot OHLC lacks order-book liquidity, spread history, intrabar path and liquidation prices. Reported `ESTIMATED` PnL is not actual net trading PnL. A historical close can be corrected only after verifying its first post-entry barrier candle, using `scripts.correct_paper_barrier_fill`; the original close remains in an append-only audit event and a separate ledger adjustment records the cash difference.

Active observations may live at most 24 hours from creation, and at most 20 may exist at once. Configure these limits with `AGENT_MAX_MONITOR_HOURS` and `AGENT_MAX_MONITOR_ITEMS` if needed. An omitted observation stays active until explicitly closed or expired. Closed keys cannot be reused.

Run tests with `.venv/bin/pip install -r requirements-dev.txt` and `.venv/bin/python -m pytest -q`. PostgreSQL integration tests use `DATABASE_URL`; isolated test transactions are rolled back and the concurrency test deletes its temporary test account.
