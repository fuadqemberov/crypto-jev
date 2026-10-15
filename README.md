# 26 Strategiya — canlı virtual futures ticarəti

Python 3.12+ · FastAPI · Binance USDT-M futures açıq bazar məlumatı · Azərbaycan dilində dashboard.

20 klassik texniki analiz strategiyası və YouTube videolarından götürülmüş 6 taktika — **hər biri ayrıca 2000 $ virtual futures hesabı ilə**, öz qaydası
ilə LONG və SHORT açır, leverage-i özü seçir. Dashboard-da strategiyalar arasında bölmə-bölmə keçid edib
hansının uğurlu olduğunu özünüz müşahidə edirsiniz.

**Yalnız virtual (paper) ticarət.** Real order, exchange API açarı və pul yoxdur. Binance-dən yalnız açıq
market-data GET sorğuları (`/fapi/v1`) edilir. Leverage həm qazancı, həm itkini böyüdür — likvidasiyalar da simulyasiya olunur.

## Quraşdırma və işə salma

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock      # və ya: pip install -e '.[test]'
cp .env.example .env
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8082 --workers 1
```

Dashboard: <http://127.0.0.1:8082>. Windows: Python 3.12+ quraşdırın və `run.cmd` işlədin.
Tək proses: engine və dashboard birlikdədir; `--workers 1` saxlayın.

`fapi.binance.com` bəzi regionlarda (məs. ABŞ) HTTP 451 qaytarır; o halda serveri icazə verilən regionda
işlədin. Şəbəkəsiz sınaq: `DEMO_MODE=true` — sintetik futures bazarı, sürətləndirilmiş saat
(hər `DEMO_CANDLE_SECONDS` saniyədə bir şam), ayrıca `data/demo-futures.db`.

## Necə işləyir

| Addım | Təsvir |
|---|---|
| Bazar | USDT-M **perpetual** kontraktlar; `SYMBOLS=ALL` → 24 saatlıq həcmə görə ən böyük `TOP_N` (50). Stablecoin, kvartallıq, əmtəə/qeyri-kripto kontraktlar çıxarılır. |
| Qiymət | Hər 15 s bütün **mark qiymətlər** və funding yenilənir; 26 hesab yenidən qiymətləndirilir. |
| LONG | Strategiyanın öz qaydası (`backtest/strategies.py`) bağlanmış şamda. |
| SHORT | Eyni qayda qiymətin **güzgü seriyasına** (hər qiymət → 1/qiymət, high↔low) tətbiq olunur — bazarın tam əksi. Məs. SMA20 SMA50-nin altına düşəndə, RSI 70-i aşağı kəsəndə, Donçian minimumu qırılanda SHORT. |
| Açılış | İstiqamət bağlanmış şamda dəyişibsə (təzə siqnal). Eyni şamda LONG→SHORT olarsa köhnə bağlanır, yenisi açılır („Çevrilmə“). |
| Bağlanış | 1) **take-profit = 1.5R** (stop məsafəsinin 1.5 qatı, `TP_R_MULTIPLE`) — hər 15 s və şamın high/low-u ilə; istəyə görə sabit `TAKE_PROFIT_USDT` (default söndürülüb); 2) **stop-loss** (aşağıya bax) — hər 15 s və şamın high/low-u ilə; 3) strategiyanın istiqaməti dəyişəndə; 4) likvidasiya (yalnız stop söndürülübsə və ya qiymət stopdan sıçrayıb keçərsə). |
| Paralel | **Mövqe sayına limit yoxdur.** Hər coin-də bir strategiyanın bir mövqeyi ola bilər; yeni mövqe yalnız sərbəst marja çatmayanda buraxılır. Çox siqnal gələndə həcmi böyük coin üstündür. |
| Marja | Hər mövqe: cari balansın `ORDER_MARGIN_PCT` (5% → ~100 $), isolated margin. Nominal = marja × leverage. |
| Xərc | Hər tərəfə 0.05% taker komissiyası + 0.05% sürüşmə; hər 8 saatlıq funding (long müsbət dərəcədə ödəyir, short alır) marjadan çıxılır/əlavə olunur. |
| Likvidasiya | Marja + açıq PnL ≤ 0.5% × nominal olduqda bütün marja itir. Hər qiymət yoxlamasında və tam saxlanılmış şamın high/low-u ilə yoxlanılır. |
| Benchmark | BTCUSDT 1× „al və saxla“. |

### Leverage-i strategiya özü seçir

Hər strategiya gözlədiyi əks hərəkəti ATR(14) əmsalı ilə bəyan edir. Hər girişdə:

```text
leverage = floor(RISK_PER_MOVE / (ATR% × atr_əmsalı)),  1× … MAX_LEVERAGE (default 100×), RISK_PER_MOVE = 0.25
```

Yəni strategiyanın tipik əks hərəkəti (= stop məsafəsi) marjanın ~25%-ni aparır, likvidasiya isə təxminən dörd dəfə uzaqdadır.
Sakit coin-də leverage yüksək, dəyişkən coin-də aşağı olur. Məsələn ATR% = 0.5% olan coin-də Supertrend (əmsal 3.0) 16× seçir; ATR% = 0.3%-də 27×, ATR% = 2%-də 4×.

| Strategiya | ATR əmsalı | Strategiya | ATR əmsalı |
|---|---:|---|---:|
| SMA kəsişməsi | 3.0 | Supertrend | 3.0 |
| EMA kəsişməsi | 3.0 | Donçian | 3.5 |
| RSI | 2.0 | Williams %R | 2.0 |
| MACD | 2.5 | CCI | 2.0 |
| Bollinger qayıdış | 2.0 | Parabolik SAR | 2.5 |
| Bollinger qırılma | 3.0 | Keltner | 3.0 |
| Stoxastik | 2.0 | ATR izləyən stop | 3.0 |
| ADX | 3.0 | TEMA | 2.5 |
| İçimoku | 3.0 | MFI | 2.0 |
| VWAP | 2.5 | RSI + MACD | 2.5 |
| Likvidasiya ovu | 2.0 | MACD histoqram + Vix Fix | 2.5 |
| Sabit 2:1 R/R | 2.0 (tavan 20×) | EMA geri çəkilmə | 2.5 (tavan 25×) |
| Kanal scalp | 2.0 | Çakır Osilatör | 3.0 (tavan 5×) |

Diqqət: real Binance-də 100× yalnız BTC/ETH kimi böyük kontraktlarda və kiçik nominalda mümkündür; çox altcoin-də
real limit 20–75×-dir. Simulyasiya bu coin-limitlərini tətbiq etmir (onlar API açarı tələb edən endpoint-dədir) —
istəsəniz `MAX_LEVERAGE` ilə ümumi həddi azaldın. 100×-də likvidasiya ~0.5% əks hərəkətdədir.

Profil `app/futures.py` faylındadır (`LEVERAGE_PROFILE`).

### Risk / mükafat: stop −1R, TP +1.5R

Əvvəlki versiyada (leverage `0.5`, sabit 15 $ TP) bir stop ~−50% marja, bir TP ~+13% marja idi — başabaş üçün
~80% uduş lazım idi. İndi stop və TP eyni məsafəyə bağlıdır:

| ($120 marja, ATR% 0.5%, Supertrend) | Əvvəl | İndi |
|---|---|---|
| Leverage | 33× | 16× |
| Stopda | −$59 (−50%) | −$29 (−24%) |
| TP-də | +$15 (+13%) | +$43 (+36%) |
| Başabaş uduş faizi | ~80% | ~40% (komissiya ilə ~44%) |

TP qiyməti açılışda qeyd olunur: `giriş ± TP_R_MULTIPLE × |giriş − stop|`. Eyni şamda həm stop, həm TP fitili olarsa,
ehtiyatlı olaraq stop sayılır. Köhnə hesab qaydasına qayıtmaq: `RISK_PER_MOVE=0.5`, `TP_R_MULTIPLE=0`, `TAKE_PROFIT_USDT=15`.

### Stop-loss

Hər mövqe açılanda stop qiyməti qeyd olunur və dəyişmir:

```text
stop = giriş ∓ min(ATR_əmsalı × ATR(14),  STOP_LOSS_PCT% × giriş / leverage)
```

- Məsafə strategiyanın gözlədiyi əks hərəkətdir (leverage ilə eyni ATR əmsalı), amma zərər heç vaxt marjanın
  `STOP_LOSS_PCT`-indən (default **50%**) çox olmur. Likvidasiya ~100% zərərdə olduğu üçün stop həmişə ondan əvvəl gəlir.
- Hər 15 saniyədə mark qiymətlə yoxlanır (stop-market — cari qiymətlə bağlanır) və hər şam bağlananda şamın
  high/low-u ilə (fitil stopu keçibsə — stop qiymətində bağlanır).
- Dashboard-da səbəb **Stop**, açıq mövqelərdə stop qiyməti, reytinqdə stop sayı görünür.
- `STOP_LOSS_PCT=0` stopu söndürür (köhnə davranış: yalnız siqnal, TP və likvidasiya).
- Stop-dan əvvəl açılmış mövqelərə restart-da marja limitinə görə (50%) stop verilir.

## YouTube taktikaları (21–26)

Altı kripto YouTube videosunda göstərilən taktikalar yalnız OHLCV ilə ifadə oluna bilən hissəsinə endirilib
(`backtest/strategies.py`, `s_liq_sweep` … `s_confluence`). Videolardakı vədlər („zəmanətli“, „100×“)
deyil, qaydanın özü sınanır — digər 20 strategiya ilə eyni şərtlərdə müqayisə üçün.

| Açar | Mənbə | LONG qaydası | Çıxış | Videodan fərqi |
|---|---|---|---|---|
| `liq_sweep` | Doğan Çakır — „likvidasiya satın almaq“ | Şam son 20 şamın minimumunun altına fitil atır və yenidən onun üstündə bağlanır (stop/likvidasiya ovu). | Qarşı likvidlik: son 20 şamın maksimumu (hədəf) və ya ov fitilinin dibi (stop). | Coinglass likvidasiya xəritəsi OHLCV-də yoxdur — swing ekstremumlar istifadə olunur; 10 hissəli DCA yoxdur. |
| `macd_hist` | MACD histoqram rəngləri + CM Williams Vix Fix | Histoqram ardıcıl 2 şam „tünd yaşıl“ (>0 və artan), qiymət son yaşıl Vix Fix şamının dibindən (dəstək) yuxarı. | İlk „açıq yaşıl“ (histoqram azalır), histoqram ≤ 0 və ya dəstək qırılır. | Videoda 4 saatlıq qrafik nəzərdə tutulur — `TIMEFRAME=4h` ilə işlədin; stop-loss videoda yoxdur. |
| `rr_2to1` | „−10% stop / +20% TP, 20×“ | EMA20 > EMA50 və qiymət Bollinger orta xəttini yuxarı kəsir. | Girişdən −0.5% (stop) və ya +1% (hədəf) — 20×-də marjanın −10% / +20%-i. | BTC istiqamət filtri tək coin məlumatında yoxdur; „öz indikatoru“ açıqlanmayıb. |
| `ema_pullback` | „Sürdürülebilir kazanç — 3 kural“ | EMA20 > EMA50 > SMA100; qiymət EMA20-yə enir və üstündə bağlanır („fiyat sana gelsin“). | Stop EMA50-dən 0.25×ATR aşağı, hədəf 2R. Coin həmin gün stop/hədəf olarsa, gün sonuna qədər yeni giriş yoxdur. | Asiya (00 UTC) və London (07 UTC) açılışından ±1 saat giriş yoxdur, New York açıqdır. Xəbər günü filtri və „kasanın 100%-i ilə gir“ tətbiq edilmir (marja hamı üçün eynidir). |
| `range_scalp` | „Dakikalık scalp — kanal taktiği“ | MA20 > MA50 > MA100; son 24 şamın kanalı (eni ≥ 2×ATR), qiymət kanalın alt 25%-ində. | Hədəf kanalın 90%-i, stop kanal minimumundan 0.25×ATR aşağı. | Videoda 1 dəqiqəlik qrafik və 100–150× — burada `TIMEFRAME` nə qoyulubsa ona işləyir; FVG/BOS anlayışları yoxdur. |
| `confluence` | „Çakır Osilatör“ AI bot sinyalları | Güven xalı 100: qiymət > MA50 > MA200, Supertrend yuxarı, ADX > 20, RSI 50–70, MACD histoqramı > 0; giriş MA60-a geri çəkilmədə. | Supertrend aşağı dönür və ya bağlanış MA60 altındadır. | Botun öz xal düsturu açıqlanmayıb — 5 şərtin hamısı = 100 qəbul edilib; leverage tavanı videodakı kimi 5×. |

SHORT tərəfi digərləri kimi güzgü seriyasından gəlir (məs. 20 şamlıq maksimumun üstündə ov → SHORT).
Leverage tavanları videolardakı kimidir: `rr_2to1` 20×, `ema_pullback` 25×, `confluence` 5×. Ümumi stop-loss və 1.5R TP qaydası bu altı strategiyaya da tətbiq olunur.

## Dashboard

- **Ümumi baxış** — 26 hesabın balans qrafiki (benchmark ilə), sıralana bilən reytinq (balans, gəlir, realizə/açıq PnL,
  əməliyyat, uduş %, mənfəət faktoru, maks. geriləmə, TP bağlanışları, likvidasiyalar, orta və faktiki leverage,
  açıq LONG/SHORT) və son əməliyyatlar.
- **Strategiya bölmələri (26)** — sol menyu, telefonda açılan siyahı, `‹ ›` düymələri və ya ← → oxları ilə keçid.
  Hər bölmədə: LONG/SHORT/leverage qaydası, KPI-lar, balans qrafiki, son şamın siqnalları, açıq mövqelər
  (istiqamət, leverage, mark, likvidasiya qiyməti, marja, funding, xalis PnL, TP-yə qalan məbləğ) və tarixçə
  (bağlanma səbəbi: TP / Siqnal / Çevrilmə / Likvidasiya).
- Dashboard yalnız oxuyur — HTTP ilə hesablara heç nə yazmaq mümkün deyil.

API: `GET /api/overview`, `GET /api/curves`, `GET /api/strategy/{key}`, `GET /health`.

## Məlumat, restart və sıfırlama

- Hesablar, mövqelər, əməliyyatlar və balans tarixçəsi `data/futures.db` (SQLite) faylındadır. Restart-dan sonra
  hər şey qaldığı yerdən davam edir; eyni şam iki dəfə işlənmir. Köhnə spot versiyasının `data/strategies.db` faylı istifadə edilmir.
- Proses dayanıqda qiymət izlənmir: TP, likvidasiya və funding bu müddətdə yoxlanmır (növbəti şamda yalnız son şamın
  high/low-u ilə likvidasiya yoxlanır).
- **Yarışı sıfırdan başlatmaq**: prosesi dayandırın, `data/futures.db*` fayllarını silin, yenidən başladın.
  Parametrləri dəyişsəniz də müqayisənin ədalətli qalması üçün sıfırlamaq tövsiyə olunur.

## Serverdə 7/24

```bash
sudo cp deploy/crypto-strategies.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-strategies.service
sudo journalctl -u crypto-strategies -f
```

Nümunə `/opt/crypto-jev`, `cryptojev` istifadəçisi və hazır `.venv` fərz edir; `data/` yazıla bilən olmalıdır.
İnternetə açarkən HTTPS reverse proxy + `DASHBOARD_USER`/`DASHBOARD_PASSWORD` istifadə edin, 8082-ni loopback-da saxlayın.
Köhnə `crypto-jev*.service` unit-lərini dayandırıb silin (`systemctl disable --now ...`).

## 26 strategiyalı backtest modulu (`backtest/`)

Eyni 26 strategiyanın LONG qaydaları tarixi məlumat üzərində də test edilə bilər (backtest spot, yalnız long, 1×):

```bash
python -m backtest --days 183 --capital 2000 --out crypto_strategies_report.html
# məhdud siyahı: python -m backtest --symbols BTCUSDT ETHUSDT SOLUSDT
```

- Məlumat: `data-api.binance.vision` (Binance-in rəsmi açıq market-data API-si; `api.binance.com`
  bəzi regionlarda 451 qaytarır). Şamlar `data/klines/` qovluğunda keşlənir.
- Kapital coinlərə bərabər bölünür, yalnız long spot. Siqnal şam bağlananda, icra növbəti şamın açılışında.
- Son nəticə nümunəsi: `reports/crypto_strategies_report.html` (12 Aprel — 12 Oktyabr 2026, 383 coin).

Backtest spot, long-only və leverage-siz olduğu üçün canlı futures nəticələri ilə birbaşa üst-üstə düşməyəcək.

## Testlər

```bash
.venv/bin/python -m pytest -q
node --check app/static/app.js
```

Testlər futures mühasibatı (long/short, komissiya, sürüşmə, funding, likvidasiya qiyməti, stop-loss, R take-profit,
equity = kapital + realizə + açıq PnL), 15 $ TP, çevrilmə, limitsiz paralel mövqelər, strategiya leverage-i,
güzgü SHORT qaydası, 6 YouTube taktikasının giriş/çıxış qaydaları, restart, demo bazarda bütün strategiyalar, Binance futures cavablarının emalı, API təhlükəsizliyi
və konfiqurasiya yoxlamalarını əhatə edir.

**Xəbərdarlıq:** bu sistem investisiya məsləhəti deyil. Leverage-li futures real hesabda qısa müddətdə bütün marjanı itirə bilər. Virtual nəticələr real likvidlik, gecikmə
və sürüşmə şəraitində fərqli ola bilər; keçmiş və cari nəticələr gələcək gəliri göstərmir.
