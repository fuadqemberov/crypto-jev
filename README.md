# Crypto Jev

Python + FastAPI ilə Azərbaycan dilində kripto analiz paneli. Binance USD-M bazar məlumatlarını deterministik indikatorlarla hesablayır, TypeSafe **Jev** modelindən strukturlaşdırılmış qiymətləndirmələr alır və Jev-in seçdiyi `LONG / SHORT / WAIT` tədqiqat siqnalını göstərir. **Əsas qərarverən Jev-dir.**

**Real və virtual order icrası yoxdur. API açarı olmadıqda texniki analiz işləyir, yekun qərar WAIT qalır.** Jev confidence və diaqnostik texniki bal qazanc ehtimalı deyil. Gəlirlilik və strategiyanın əlavə proqnoz gücü backtest/out-of-sample sınaqla təsdiqlənməyib.

## Windows — sürətli başlanğıc

Python 3.11+ (3.12 tövsiyə edilir) quraşdırın. Reponu klonlayın və ya ZIP olaraq açın. Layihə qovluğunda `run.cmd` işlədin. İlk dəfə asılılıqlar və `.env` hazırlanır. Panel: **http://127.0.0.1:8082**.

Jev-i aktivləşdirmək üçün lokal `.env` faylında:

```dotenv
TYPESAFE_API_KEY=your-key-here
TYPESAFE_MODEL=jev-latest
```

Proqramı yenidən başladın. Açar yalnız serverdə TypeSafe-a göndərilir; HTML, status API, tarixçə və Git-ə daxil edilmir. `.env` Git-ə daxil edilmir. Mövcud `crypto` layihəsinə dəyişiklik edilmir.

## Linux / macOS

```bash
git clone https://github.com/fuadqemberov/crypto-jev.git
cd crypto-jev
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
cp .env.example .env
# .env faylını redaktə edin
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8082 --workers 1
```

Bir proses/worker işlədin. Çoxsaylı worker və ya `--reload` ayrıca skanlar və əlavə API xərci yarada bilər. 8082 portu mövcud tətbiqlərlə paralel istifadə üçündür.

## İstəyə bağlı server xidməti

Lokal istifadə üçün bu bölməyə ehtiyac yoxdur; `run.cmd` kifayətdir.

`deploy/crypto-jev.service` systemd nümunəsidir: əvvəl `cryptojev` sistem istifadəçisini, `/opt/crypto-jev`, `.venv` və yazı icazəli `data` qovluğunu yaradın. `.env`-i həmin istifadəçinin oxuya bildiyi `0600` icazəsi ilə saxlayın. Sonra service faylını `/etc/systemd/system/`-ə yerləşdirib `sudo systemctl daemon-reload` və `sudo systemctl enable --now crypto-jev` icra edin. Bu repo serverə avtomatik deploy etmir.

## Analiz axını

1. BTC, ETH, SOL, BNB, XRP üçün 15m / 1h / 4h üzrə 301 şam sorğulanır. `SYMBOLS` ilə 1–20 USDT simvolu seçilə bilər.
2. Yalnız bağlanmış, ardıcıl, etibarlı və təzə şamlar qəbul edilir; minimum 250 şam tələb olunur. Son şam ən çox bir period + 30 saniyə əvvəl bağlanmış olmalıdır. Bid/ask və mark yaşı maksimum 60 saniyədir.
3. EMA20/50/200 (SMA seed), Wilder RSI14/ATR14, MACD12/26/9, əvvəlki 20 şama görə relative volume və iki sağ/iki sol şamla təsdiqlənən 120 şamlıq pivot dəstək/müqaviməti hesablanır. Pivotlar yalnız diaqnostik göstərilir, giriş filtri deyil.
4. Texniki bal: hər timeframe EMA trendi 15 (cəmi 45), 15m MACD güclənməsi 15, 1h MACD istiqaməti 10, 15m RSI 10, 1h RSI 10, relative volume ≥1.2 üçün 10. Maksimum 100.
5. Python əvvəlcə trend, momentum, RSI, həcm, volatillik, EMA20 məsafəsi, dəstək/müqavimət yaxınlığı və bazar keyfiyyəti etiketləri hazırlayır. **Texniki bal Jev-ə ötürülmür və qərar həddi kimi istifadə edilmir.**
6. Jev **direction** sualı ilə 1–4 saatlıq tədqiqat istiqamətini seçir. Model trend davamı, pullback, dönüş və ya qeyri-müəyyənlik haqqında konteksti özü qiymətləndirir. Ayrı **momentum, regime, risk, driver** sualları həmin qərarın AI kontekstini göstərir. `driver` sərbəst mətn izahı deyil, kontekst kateqoriyasıdır; qərara sərt filtr tətbiq etmir.
7. Yekun LONG/SHORT yalnız Jev həmin istiqaməti seçdikdə mümkündür. Direction/momentum/regime/risk confidence minimum 0.85, momentum istiqaməti uyğun, risk acceptable və regime unclear olmamalıdır. Range və transition avtomatik rədd edilmir. **10/100 texniki bal belə təkbaşına Jev qərarını bloklamır.**
8. Python istiqaməti seçmir və ya əksinə çevirmir; yalnız risk veto-su tətbiq edir: ATR/qiymət 0.1–5%, markın bağlanışdan maksimum 1 ATR uzaqlığı, spread, funding, RSI 22–78 və xərclərdən sonrakı R:R. Köhnə/etibarsız məlumat və Jev xətası WAIT yaradır.
9. Panel hər 5 saniyədə cache oxuyur; səhifənin yenilənməsi Jev sorğusu yaratmır. Hər siqnalın xam AI seçimi, confidence və tam ehtimal bölgüsü görünür.

Təsdiqlənmiş siqnalda indikativ giriş ask/bid, SL 2×ATR, TP 4×ATR-dir. Hər tərəf üçün 0.05% komissiya və 3 bps slippage fərziyyəsi ilə xalis R:R ≥1.5 tələb olunur. Funding rate giriş filtri olsa da, funding settlement bu R:R hesabına daxil deyil. Faktiki exchange fees, fill, liquidation və PnL simulyasiyası yoxdur.

Jev confidence cavab bölgüsündən çıxarılan qeyri-müəyyənlik göstəricisidir. Jev qaydaları ziddiyyətli qiymətləndirə bilər; istiqaməti Jev seçir, Python təhlükəsizlik veto-su ilə WAIT edə bilər. Structured output zəmanəti bazar proqnozunun düzgünlüyünə zəmanət deyil.

## Konfiqurasiya

| Parametr | Standart | Mənası |
|---|---|---|
| `TYPESAFE_API_KEY` | boş | Jev açarı; boşdursa AI çağırılmır |
| `TYPESAFE_MODEL` | `jev-latest` | Alias; cavabdakı faktiki model tarixçədə saxlanır |
| `SYMBOLS` | BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT | USDT bazarları |
| `SCAN_SECONDS` | 300 | Skan bitdikdən sonrakı gözləmə; minimum 60 |
| `MIN_AI_CONFIDENCE` | 0.85 | Qərar/momentum/regime/risk minimum confidence |
| `MAX_SPREAD_BPS` | 15 | Bid/ask spread limiti |
| `MAX_FUNDING_RATE` | 0.0003 | Ödəniş istiqamətində funding limiti; 0.03% |
| `DATA_DIR` | data | SQLite yeri |
| `DEMO_MODE` | false | Sintetik qiymətlər, AI çağırışı yoxdur |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | boş | İkisini birlikdə təyin edin |

`DEMO_MODE=true` yalnız paneli oflayn yoxlamaq üçündür; ayrıca `demo.db` istifadə edir, canlı məlumat kimi təqdim edilmir. Şəbəkə xətasında avtomatik demo keçidi yoxdur.

Jev eyni semantik vəziyyət və eyni bağlanmış şamlar üçün yaddaşda cache edilir (200 qeyd, restartda təmizlənir). Fərqli vəziyyətlər yeni sorğu yaradır; Jev hesabındakı xərcləri izləyin. Binance sorğuları minimum 400ms aralıdır; 418/429 ümumi cooldown yaradır. Jev müvəqqəti xətalarında maksimum 3 cəhd var. Uzun Retry-After olduqda erkən təkrar əvəzinə növbəti skan gözlənilir. İstifadəçi skanı minimum 60 saniyə aralı başlada bilər.

## Məlumat və API

SQLite son **2000 simvol analizi** saxlayır; daha köhnələr avtomatik silinir. Panel son 30 qeydi göstərir, `/api/history` son 100 qeydi qaytarır. Bu, audit/order jurnalı deyil. Backup zamanı proqramı dayandırıb bütün `data` qovluğunu götürün. Cari radar restartdan sonra təzə skanla dolur; tarixçə ayrıca qalır. Köhnəlmiş cari nəticələr `WAIT` göstərilir. Disk xətası paneldə bildirilir.

- `GET /` — Azərbaycan dilində panel
- `GET /api/status` — cari analizlər; API açarı daxil deyil
- `GET /api/history` — analiz tarixçəsi
- `GET /health` — prosesin işləməsi; upstream xidmətlərin sağlamlığı demək deyil
- `POST /api/scan` — skan; `X-Crypto-Jev: 1` başlığı tələb olunur

## Test

```bash
.venv/bin/pip install 'pytest>=8,<10'
.venv/bin/python -m pytest -q
```

Testlər indikator sərhədləri, açıq/köhnə/boşluqlu şamlar, NaN/inf, LONG/SHORT, aşağı texniki balda belə Jev-in qərar üstünlüyü, Jev HTTP müqaviləsi və cavab yoxlamaları, API xətalarında WAIT, cache, Binance cooldown, SQLite restart, auth və cross-site POST qorumasını yoxlayır. Bunlar gəlirlilik backtest-i deyil. Canlı Jev testi üçün istifadəçinin açarı lazımdır. AI qiymətləndirməsinin proqnoz üstünlüyü hələ ölçülməyib.

## Mənbələr

- [TypeSafe API](https://docs.typesafe.ai/api)
- [Jev confidence](https://docs.typesafe.ai/confidence)
- [Jev məhdudiyyətləri — hesablamaları kodda saxlayın](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
- [Binance rəsmi futures connector mənbəyi](https://github.com/binance/binance-futures-connector-python)
- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)

İlham: [Futures Lab](https://github.com/fuadqemberov/crypto). Bu ayrıca Python layihəsidir.
