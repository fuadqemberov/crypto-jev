# 20 Strategiya — canlı virtual futures ticarəti

Python 3.12+ · FastAPI · Binance USDT-M futures açıq bazar məlumatı · Azərbaycan dilində dashboard.

20 klassik texniki analiz strategiyasının **hər biri ayrıca 2000 $ virtual futures hesabı ilə**, öz qaydası
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
| Qiymət | Hər 15 s bütün **mark qiymətlər** və funding yenilənir; 20 hesab yenidən qiymətləndirilir. |
| LONG | Strategiyanın öz qaydası (`backtest/strategies.py`) bağlanmış şamda. |
| SHORT | Eyni qayda qiymətin **güzgü seriyasına** (hər qiymət → 1/qiymət, high↔low) tətbiq olunur — bazarın tam əksi. Məs. SMA20 SMA50-nin altına düşəndə, RSI 70-i aşağı kəsəndə, Donçian minimumu qırılanda SHORT. |
| Açılış | İstiqamət bağlanmış şamda dəyişibsə (təzə siqnal). Eyni şamda LONG→SHORT olarsa köhnə bağlanır, yenisi açılır („Çevrilmə“). |
| Bağlanış | 1) xalis mənfəət ≥ **`TAKE_PROFIT_USDT` (15 $)** — hər 15 s yoxlanır; 2) strategiyanın istiqaməti dəyişəndə; 3) likvidasiya. |
| Paralel | **Mövqe sayına limit yoxdur.** Hər coin-də bir strategiyanın bir mövqeyi ola bilər; yeni mövqe yalnız sərbəst marja çatmayanda buraxılır. Çox siqnal gələndə həcmi böyük coin üstündür. |
| Marja | Hər mövqe: cari balansın `ORDER_MARGIN_PCT` (5% → ~100 $), isolated margin. Nominal = marja × leverage. |
| Xərc | Hər tərəfə 0.05% taker komissiyası + 0.05% sürüşmə; hər 8 saatlıq funding (long müsbət dərəcədə ödəyir, short alır) marjadan çıxılır/əlavə olunur. |
| Likvidasiya | Marja + açıq PnL ≤ 0.5% × nominal olduqda bütün marja itir. Hər qiymət yoxlamasında və tam saxlanılmış şamın high/low-u ilə yoxlanılır. |
| Benchmark | BTCUSDT 1× „al və saxla“. |

### Leverage-i strategiya özü seçir

Hər strategiya gözlədiyi əks hərəkəti ATR(14) əmsalı ilə və öz leverage tavanı ilə bəyan edir. Hər girişdə:

```text
leverage = floor(0.5 / (ATR% × atr_əmsalı)),  1× … min(strategiyanın tavanı, MAX_LEVERAGE)
```

Yəni strategiyanın tipik əks hərəkəti marjanın ən çox ~50%-ni aparır, likvidasiya isə təxminən iki dəfə uzaqdadır.
Sakit coin-də leverage yüksək, dəyişkən coin-də aşağı olur.

| Strategiya | ATR əmsalı | Öz tavanı | Strategiya | ATR əmsalı | Öz tavanı |
|---|---:|---:|---|---:|---:|
| SMA kəsişməsi | 3.0 | 10× | Supertrend | 3.0 | 10× |
| EMA kəsişməsi | 3.0 | 10× | Donçian | 3.5 | 8× |
| RSI | 2.0 | 15× | Williams %R | 2.0 | 15× |
| MACD | 2.5 | 12× | CCI | 2.0 | 15× |
| Bollinger qayıdış | 2.0 | 15× | Parabolik SAR | 2.5 | 10× |
| Bollinger qırılma | 3.0 | 8× | Keltner | 3.0 | 8× |
| Stoxastik | 2.0 | 12× | ATR izləyən stop | 3.0 | 10× |
| ADX | 3.0 | 10× | TEMA | 2.5 | 12× |
| İçimoku | 3.0 | 10× | MFI | 2.0 | 12× |
| VWAP | 2.5 | 12× | RSI + MACD | 2.5 | 12× |

Profil `app/futures.py` faylındadır (`LEVERAGE_PROFILE`).

## Dashboard

- **Ümumi baxış** — 20 hesabın balans qrafiki (benchmark ilə), sıralana bilən reytinq (balans, gəlir, realizə/açıq PnL,
  əməliyyat, uduş %, mənfəət faktoru, maks. geriləmə, TP bağlanışları, likvidasiyalar, orta və faktiki leverage,
  açıq LONG/SHORT) və son əməliyyatlar.
- **Strategiya bölmələri (20)** — sol menyu, telefonda açılan siyahı, `‹ ›` düymələri və ya ← → oxları ilə keçid.
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

## 20 strategiyalı backtest modulu (`backtest/`)

Eyni 20 strategiyanın LONG qaydaları tarixi məlumat üzərində də test edilə bilər (backtest spot, yalnız long, 1×):

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

Testlər futures mühasibatı (long/short, komissiya, sürüşmə, funding, likvidasiya qiyməti,
equity = kapital + realizə + açıq PnL), 15 $ TP, çevrilmə, limitsiz paralel mövqelər, strategiya leverage-i,
güzgü SHORT qaydası, restart, demo bazarda 20 strategiya, Binance futures cavablarının emalı, API təhlükəsizliyi
və konfiqurasiya yoxlamalarını əhatə edir.

**Xəbərdarlıq:** bu sistem investisiya məsləhəti deyil. Leverage-li futures real hesabda qısa müddətdə bütün marjanı itirə bilər. Virtual nəticələr real likvidlik, gecikmə
və sürüşmə şəraitində fərqli ola bilər; keçmiş və cari nəticələr gələcək gəliri göstərmir.
