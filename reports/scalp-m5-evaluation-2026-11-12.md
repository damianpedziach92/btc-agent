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
| pierwsza | M5 range rejection | 198 | 14/3 | 3 | -1.08 | 1.08 |
| pierwsza | M5 breakout retest | 29 | 15/7 | 7 | -2.41 | 3.17 |
| druga | agent baseline | 14 | 0/0 | 1 | -1.19 | 1.90 |
| druga | M15 trend | 49 | 0/0 | 6 | +10.02 | 4.50 |
| druga | M15 breakout retest | 6 | 0/0 | 2 | -2.44 | 5.78 |
| druga | M15 mean reversion | 6 | 0/0 | 5 | -5.25 | 10.06 |
| druga | M5 pullback retest | 66 | 9/3 | 3 | -2.06 | 2.06 |
| druga | M5 range rejection | 193 | 10/3 | 3 | -1.42 | 1.42 |
| druga | M5 breakout retest | 36 | 10/4 | 4 | -2.07 | 3.59 |

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

Przed wdrożeniem ostatnie pięć analiz M5 to **5 WAIT / 5**, bez diagnostyki
kandydatów. W pięciu kolejnych zapisanych analizach po końcowych poprawkach
(15:35, 15:40, 15:45, 15:50, 15:55 UTC) również było **5 WAIT / 5**. Różnica
dotyczy aktywności przed wejściem: zapisano 30 ocen kandydatów M5,
w tym 13 `INVALID_TRIGGER`, 12 `RR_LOW` i 5 `VOLATILITY_FILTER`.
W czterech analizach był potencjalnie poprawny LIMIT; agent złożył dwie
propozycje `SCALP_M5 / M5_RANGE_REJECTION` i backend zaakceptował obie.
Żadna nie została wypełniona. Jedno zlecenie anulował agent, a drugie
anulował sprawdzalny warunek po zamkniętej świecy. Nie było odrzuceń
backendu w tych pięciu decyzjach, transakcji ani powiadomień o pozycji.

Pierwsza planowa analiza 15:45 UTC zwróciła niespójną decyzję strukturalną.
Po dodaniu jednej próby naprawy wywołano `--force` i zapisano tę świecę
raz. W odzyskanej decyzji model błędnie uznał 150 sekund limitu M1 za
limit świeżości M5 i anulował pending. Instrukcja została doprecyzowana:
backend dopuszcza zamkniętą M5 do 10 minut. W późniejszych przebiegach
nie było takiego błędu.

Stan rachunku przed migracją i po pięciu analizach: gotówka
`982.467248066934 USD`, 13 historycznych pozycji, 0 otwartych pozycji,
66 wpisów ledger z sumą `982.467248066934 USD`. Po obserwacji było
0 aktywnych pending i 0 nowych wpisów outbox OPENED/CLOSED.
Migracja `a5d50bc16f3e` została wykonana po backupie
`/home/damian/btc-agent-backups/btc-agent-scalp-m5-20261112T144420Z.dump`,
którego zawartość sprawdzono przez `pg_restore --list`. Scheduler jest aktywny.

Udział WAIT nie spadł w tej krótkiej próbie. Dwa poprawne pending pokazują,
że agent potrafi zaplanować wejście, ale nie są dowodem poprawy wyników.
