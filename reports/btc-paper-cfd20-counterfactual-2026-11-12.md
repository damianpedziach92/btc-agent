# CFD20 — osobna symulacja kontrfaktyczna

CFD20 | spread $20/BTC | fee 0 | simulation only

Próba: 12 pozycji. Oryginalny ledger pozostaje bez zmian.

Oryginalny wynik netto: -18.33 USD. Wynik przy tych samych godzinach wyjścia i kosztach CFD20: -8.04 USD.

Powtórna analiza TP/SL jest osobna: zmiana semantyki bariery może zmienić czas wyjścia, a nie tylko koszt.

| ID | Strona | Oryginał netto | CFD20 te same wyjścia | CFD20 TP/SL |
| --- | --- | ---: | ---: | --- |
| 60b76178 | LONG | -0.82 | -0.43 | STOP_LOSS -0.41 USD |
| 9a5c0f2c | SHORT | -0.51 | -0.09 | STOP_LOSS -0.28 USD |
| 6406c004 | LONG | 0.45 | 0.84 | TAKE_PROFIT 0.87 USD |
| 8b869128 | LONG | 0.65 | 1.62 | TAKE_PROFIT 1.69 USD |
| 420c327c | LONG | -1.42 | -0.57 | AGENT -0.57 USD |
| 51fc594a | SHORT | -3.71 | -2.65 | AGENT -2.65 USD |
| b6284d6f | LONG | -1.45 | -0.40 | AGENT -0.40 USD |
| 6197f94b | SHORT | -2.20 | -1.14 | AGENT -1.14 USD |
| 2be76111 | LONG | -3.84 | -2.79 | AGENT -2.79 USD |
| e83cbb72 | SHORT | -1.41 | -0.36 | AGENT -0.36 USD |
| 622833f9 | LONG | -2.20 | -1.25 | STOP_LOSS -1.18 USD |
| 22b9b137 | SHORT | -1.88 | -0.83 | AGENT -0.83 USD |

Ceny odniesienia odtworzono z założeń dawnego modelu; OHLC nie pokazuje kolejności ticków ani płynności CFD. Próba nie dowodzi skuteczności strategii.
