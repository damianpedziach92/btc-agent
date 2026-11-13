# PAPER CFD20 replay

CFD20 | spread $20/BTC | fee 0 | simulation only

Closed OHLC only; entries at next M1 open, no tick sequence, no account writes

| Split | Strategy | Signals | Trades | Win rate | Gross USD | Net USD | Max DD USD | Spread USD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| in_sample | agent_baseline | 0 | 0 | n/a | 0.00 | 0.00 | 0.00 | 0.00 |
| in_sample | trend | 24 | 4 | 0.0% | -6.47 | -7.02 | 11.49 | 0.54 |
| in_sample | breakout_retest | 1 | 1 | 0.0% | -0.95 | -1.08 | 1.25 | 0.14 |
| in_sample | mean_reversion | 4 | 4 | 50.0% | 4.76 | 4.21 | 6.42 | 0.55 |
| out_of_sample | agent_baseline | 20 | 12 | 50.0% | 8.34 | 6.71 | 4.54 | 1.62 |
| out_of_sample | trend | 72 | 8 | 12.5% | -9.50 | -10.58 | 16.35 | 1.08 |
| out_of_sample | breakout_retest | 5 | 4 | 0.0% | -8.49 | -9.03 | 9.23 | 0.54 |
| out_of_sample | mean_reversion | 7 | 5 | 0.0% | -9.61 | -10.28 | 12.68 | 0.67 |

Historical OHLC cannot establish intrabar order, broker fills or liquidity.
