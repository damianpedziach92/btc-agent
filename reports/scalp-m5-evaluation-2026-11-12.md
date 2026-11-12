# SCALP_M5: pierwsza ocena PAPER

Okres replay: 2026-11-11 00:00 do 2026-11-12 14:00 UTC, podzielony chronologicznie
na dwie połowy. Źródło: zapisane świece PostgreSQL; CFD20 ($20/BTC pełnego
spreadu, bez prowizji), pending LIMIT na późniejszej M1, ponowna kontrola
R:R >= 1,5, struktury stopa i limitów ryzyka przy aktywacji. Dane są
historycznym OHLC, więc nie dowodzą rzeczywistej kolejności zdarzeń wewnątrz
świecy ani wykonania u brokera.

| Połowa | Wariant | Sygnały | Pending/fill | Transakcje | Netto USD | Max DD USD |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| pierwsza | agent baseline | 8 | 0/0 | 0 | 0.00 | 0.00 |
| pierwsza | M15 trend | 50 | 0/0 | 3 | +1.79 | 5.64 |
| pierwsza | M15 breakout retest | 1 | 0/0 | 1 | -0.74 | 1.45 |
| pierwsza | M15 mean reversion | 7 | 0/0 | 2 | +1.91 | 6.93 |
| pierwsza | M5 pullback retest | 54 | 7/3 | 3 | -1.80 | 1.80 |
| pierwsza | M5 range rejection | 198 | 14/3 | 3 | -1.07 | 1.08 |
| pierwsza | M5 breakout retest | 29 | 16/4 | 4 | -1.31 | 2.35 |
| druga | agent baseline | 14 | 0/0 | 1 | -1.19 | 1.90 |
| druga | M15 trend | 49 | 0/0 | 6 | +10.02 | 4.50 |
| druga | M15 breakout retest | 6 | 0/0 | 2 | -2.44 | 5.78 |
| druga | M15 mean reversion | 6 | 0/0 | 5 | -5.25 | 10.06 |
| druga | M5 pullback retest | 66 | 12/3 | 3 | -2.37 | 2.37 |
| druga | M5 range rejection | 193 | 10/3 | 3 | -1.42 | 1.42 |
| druga | M5 breakout retest | 36 | 13/4 | 4 | -1.99 | 4.25 |

W drugiej połowie warianty M5 miały odpowiednio 3, 3 i 4 transakcje.
Wszystkie trzy były na minusie po spreadzie. Liczba sygnałów oznacza
rozpoznane kandydatury przy każdej M5, także wtedy, gdy istnieje już
pozycja lub pending; liczba wysłanych zleceń jest oddzielna.
Przedziały ufności w pełnym JSON są przez to bardzo szerokie. Te wyniki
nie stanowią dowodu dodatniej przewagi ani podstawy do strojenia progów.
Baseline agentowy odtwarza tylko zapisane propozycje natychmiastowe i
stosuje dzisiejsze reguły M15. Historyczne propozycje, które nie spełniają
tych reguł, są odrzucane; nie jest to odtworzenie realnego ledger. Również
pozostałe warianty są symulacjami CFD20, a nie historią konta.

Pliki pełnego replay z tego uruchomienia: `/tmp/btc-scalp-m5-live-replay.json`,
`/tmp/btc-scalp-m5-live-replay.csv` i `/tmp/btc-scalp-m5-live-replay.md`.

## Porównanie działania agenta po wdrożeniu

Przed wdrożeniem ostatnie pięć analiz M5 to 5 WAIT / 5, bez diagnostyki
kandydatów. Ostateczne porównanie z pięcioma nowymi analizami zostanie
dopisane po ich zapisaniu przez scheduler.
