# 20 Strategiya — canlı virtual ticarət

Python 3.12+ · FastAPI · Binance spot açıq bazar məlumatı · Azərbaycan dilində dashboard.

20 klassik texniki analiz strategiyasının **hər biri ayrıca 2000 $ virtual hesabla**, öz qaydası ilə
canlı Binance qiymətləri üzərində ticarət edir. Dashboard-da strategiyalar arasında bölmə-bölmə keçid
edib hansının uğurlu olduğunu özünüz müşahidə edirsiniz.

**Yalnız virtual (paper) ticarət.** Real order, exchange API açarı, leverage və pul yoxdur.
Binance-dən yalnız açıq market-data GET sorğuları edilir.

## Quraşdırma və işə salma

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock      # və ya: pip install -e '.[test]'
cp .env.example .env
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8082 --workers 1
```

Dashboard: <http://127.0.0.1:8082>. Windows: Python 3.12+ quraşdırın və `run.cmd` işlədin.
Tək proses: engine və dashboard birlikdədir; `--workers 1` saxlayın (bir neçə worker hesabları ikiləşdirər).

Şəbəkəsiz sınaq: `.env`-də `DEMO_MODE=true` — sintetik bazar, sürətləndirilmiş saat
(hər `DEMO_CANDLE_SECONDS` saniyədə bir şam). Demo ayrıca `data/demo.db` istifadə edir.

## Necə işləyir

| Addım | Təsvir |
|---|---|
| Coin siyahısı | `SYMBOLS=ALL`: 24 saatlıq həcmə görə ən böyük `TOP_N` (default 50) USDT spot cütü. Stablecoin, fiat, qızıl, wrapped və UP/DOWN tokenləri çıxarılır. Siyahı hər şamda yenilənir. |
| Qiymət | Hər `PRICE_REFRESH_SECONDS` (15 s) bütün qiymətlər yenilənir, 20 hesabın balansı bazar qiyməti ilə yenidən hesablanır. |
| Siqnal | Şam bağlananda (default 1h) son `HISTORY_BARS` bağlanmış şam yüklənir və hər strategiya `backtest/strategies.py`-dəki **eyni** funksiya ilə hesablanır. Formalaşan şam istifadə edilmir. |
| Çıxış | Strategiyanın tutduğu coin-də mövqe vəziyyəti 0 olarsa satılır. |
| Giriş | Vəziyyət bağlanmış şamda 0 → 1 dəyişibsə (təzə siqnal) və boş slot varsa alınır. Bir neçə siqnal olarsa həcmi böyük olan coin üstündür; yer çatmayanlar “buraxıldı” kimi göstərilir. |
| Ölçü | Mövqe = hesab balansı / `MAX_POSITIONS` (default 10 → ~200 $), nağddan çox olmamaq şərti ilə; minimum `MIN_ORDER_USDT`. |
| Xərc | Hər tərəfə 0.10% komissiya + 0.05% sürüşmə. Açıq mövqelər satış dəyəri ilə (xərclər çıxılmış) qiymətləndirilir. |
| Benchmark | BTCUSDT “al və saxla” — eyni 2000 $ ilk şamda alınmış kimi. |

Strategiyalar başlanğıcda artıq “mövqedə” olan coinlərə girmir — yalnız yeni siqnal gözləyir; ona görə
ilk saatlarda bəzi hesablar boş qala bilər. Hər hesab tamamilə müstəqildir: bir strategiyanın mövqeləri,
nağdı və nəticəsi digərlərinə təsir etmir.

| # | Strategiya | Ailə |
|---|---|---|
| 1 | SMA kəsişməsi (20/50) | Trend |
| 2 | EMA kəsişməsi (12/26) | Trend |
| 3 | RSI həddindən artıq alış/satış | Orta qiymətə qayıdış |
| 4 | MACD siqnal xətti kəsişməsi | Momentum |
| 5 | Bollinger orta qiymətə qayıdış | Orta qiymətə qayıdış |
| 6 | Bollinger qırılması | Qırılma |
| 7 | Stoxastik osilyator | Momentum |
| 8 | ADX trend izləmə | Trend |
| 9 | İçimoku buludu | Trend |
| 10 | VWAP strategiyası | Həcm |
| 11 | Supertrend (10, 3) | Trend |
| 12 | Donçian kanal qırılması | Qırılma |
| 13 | Williams %R | Orta qiymətə qayıdış |
| 14 | CCI (Əmtəə Kanal İndeksi) | Orta qiymətə qayıdış |
| 15 | Parabolik SAR | Trend |
| 16 | Keltner kanalı | Qırılma |
| 17 | ATR izləyən stop | Qırılma |
| 18 | Üçqat EMA (TEMA 10/30) | Trend |
| 19 | Pul axını indeksi (MFI) | Həcm |
| 20 | RSI + MACD kombinasiyası | Momentum |

Dəqiq qaydalar hər strategiyanın bölməsində və `backtest/strategies.py`-dədir.

## Dashboard

- **Ümumi baxış** — 20 hesabın balans qrafiki (benchmark ilə), sıralana bilən reytinq cədvəli
  (balans, gəlir, realizə/açıq PnL, əməliyyat sayı, uduş faizi, mənfəət faktoru, maks. geriləmə,
  açıq mövqe, bazarda olma payı) və son əməliyyatlar.
- **Strategiya bölmələri (20)** — sol menyudan (telefonda açılan siyahıdan), `‹ ›` düymələri və ya
  klaviaturada ← → ilə keçid. Hər bölmədə: qayda, KPI-lar, balans vs benchmark qrafiki, son şamın
  alış/satış/buraxılan siqnalları, açıq mövqelər və tam əməliyyat tarixçəsi.
- Səhifə hər 10 saniyədə yenilənir. Dashboard yalnız oxuyur — HTTP ilə hesablara heç nə yazmaq mümkün deyil.

API: `GET /api/overview`, `GET /api/curves`, `GET /api/strategy/{key}`, `GET /health`.

## Məlumat, restart və sıfırlama

- Hesablar, mövqelər, əməliyyatlar və balans tarixçəsi `data/strategies.db` (SQLite) faylındadır.
  Restart-dan sonra hər şey qaldığı yerdən davam edir; eyni şam iki dəfə işlənmir.
- Proses dayanıqda qiymət izlənmir: çıxış siqnalları növbəti işlənən şamda icra olunur.
- **Yarışı sıfırdan başlatmaq**: prosesi dayandırın, `data/strategies.db*` fayllarını silin, yenidən başladın.
- `START_CAPITAL`, `MAX_POSITIONS`, `TIMEFRAME` və ya coin siyahısını dəyişsəniz müqayisənin ədalətli
  qalması üçün DB-ni sıfırlamağınız tövsiyə olunur (mövcud hesablar köhnə başlanğıc kapitalını saxlayır).

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

Eyni 20 strategiya tarixi məlumat üzərində də test edilə bilər:

```bash
python -m backtest --days 183 --capital 2000 --out crypto_strategies_report.html
# məhdud siyahı: python -m backtest --symbols BTCUSDT ETHUSDT SOLUSDT
```

- Məlumat: `data-api.binance.vision` (Binance-in rəsmi açıq market-data API-si; `api.binance.com`
  bəzi regionlarda 451 qaytarır). Şamlar `data/klines/` qovluğunda keşlənir.
- Kapital coinlərə bərabər bölünür, yalnız long spot. Siqnal şam bağlananda, icra növbəti şamın açılışında.
- Son nəticə nümunəsi: `reports/crypto_strategies_report.html` (12 Aprel — 12 Oktyabr 2026, 383 coin).

Backtest bütün coinlərə bərabər bölünmüş kapitalla, canlı sistem isə top-N coin və slot limiti ilə işləyir,
ona görə rəqəmlər birbaşa üst-üstə düşməyəcək; strategiya qaydaları eynidir.

## Testlər

```bash
.venv/bin/python -m pytest -q
node --check app/static/app.js
```

Testlər mühasibat (komissiya/sürüşmə, equity = kapital + realizə + açıq PnL), təzə siqnalla giriş,
çıxış, slot prioriteti, köhnə şamın rədd edilməsi, restart, demo bazarda 20 strategiya, API təhlükəsizliyi
və konfiqurasiya yoxlamalarını əhatə edir.

**Xəbərdarlıq:** bu sistem investisiya məsləhəti deyil. Virtual nəticələr real likvidlik, gecikmə
və sürüşmə şəraitində fərqli ola bilər; keçmiş və cari nəticələr gələcək gəliri göstərmir.
