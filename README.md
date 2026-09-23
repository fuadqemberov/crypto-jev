# Crypto Jev · Freqtrade Paper Lab

**JEV qərar verir, Python riskləri yoxlayır, Freqtrade virtual əməliyyatları icra edir.**
Azərbaycan dilində mövcud FastAPI paneli saxlanılıb; ayrıca Freqtrade prosesi ilə iki istiqamətli inteqrasiya əlavə olunub.

> Bu versiya yalnız **paper trading** üçündür. Başlanğıc hesab **2,000 USDT**, mövqe sayı üçün sabit limit yoxdur. Leverage-i **JEV seçir**; birja və stop məsafəsi tətbiq olunan dəyəri azalda bilər.
> Gəlirlilik sübut edilməyib. JEV confidence qazanc ehtimalı deyil. Real pul və exchange API açarı lazım deyil.

## Niyə Python?

Freqtrade-in native strategiya interfeysi və mövcud layihə Python-dadır. Əsas gecikmə şəbəkə/JEV sorğularıdır.
Rust-a keçmək model cavabını sürətləndirmir, əlavə servis və sinxronizasiya riski yaradır.
İki bazar paralel analiz edilir; Binance üçün ümumi rate limiter saxlanılır.
Freqtrade JEV-ni gözləmir: ayrı thread yalnız lokal, artıq hazırlanmış siqnalları oxuyur. SL/TP Freqtrade dövrəsində qalır.
Bu HFT deyil: analiz 15m/1h/4h kontekstlidir, Freqtrade 1m şam və təxminən 5 saniyəlik dövrə istifadə edir.

## Windows — Docker olmadan

1. Python **3.12**, Git və internet bağlantısı tələb olunur.
2. Reponu klonlayın və `run-paper.cmd` işlədin. Skript virtual mühit, asılılıqlar və lokal bağlantı konfiqurasiyası yaradır.
3. Lokal `.env` faylında `TYPESAFE_API_KEY=...` yazın. Açarı Git-ə və ya söhbətə göndərməyin.
4. Dashboard prosesini yenidən başladın. Panel: **http://127.0.0.1:8082**.

`run-paper.cmd` dashboard üçün ayrıca pəncərə, Freqtrade üçün cari terminal açır.
İkinci dəfə başlamazdan əvvəl köhnə prosesləri dayandırın. İlk startda API açarı yoxdursa əməliyyat açılmır.
Mövcud virtual balans və əməliyyat bazası restartda sıfırlanmır. Lokal işləmək üçün hər iki proses açıq qalmalıdır.
Python/TA-Lib quraşdırılması Windows-da problem yaradarsa WSL2 alternativdir; Docker tələb deyil.

Yalnız əvvəlki analiz paneli üçün `run.cmd` hələ də işləyir; Freqtrade olmadan order icrası yoxdur.

### Mövcud quraşdırmanı yeniləmək

İki prosesi dayandırın, `git pull` edin və `run-paper.cmd` işlədin. Lokal config avtomatik yenilənir;
API açarları, simvol siyahısı, database yolu və əməliyyat tarixçəsi saxlanılır. Köhnə config-in məxfi backup-ı yaradılır.
Linux-da restartdan əvvəl `.venv/bin/python -m app.paper --upgrade` işlədin.
Dashboard və Freqtrade birlikdə yenilənməlidir: yeni bridge protokolu v2-dir, köhnə siqnallar giriş yaratmır.
Yeniləmə açıq mövqenin leverage-ini dəyişmir; yalnız yeni girişlər üçün seçim edilir.

## Linux / macOS — iki terminal

```bash
git clone https://github.com/fuadqemberov/crypto-jev.git
cd crypto-jev
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[paper,test]'
.venv/bin/python -m app.paper
# .env daxilində TYPESAFE_API_KEY əlavə edin.
```

Terminal 1:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8082 --workers 1
```

Terminal 2, eyni repo qovluğunda:

```bash
.venv/bin/python -m freqtrade trade --config user_data/config.paper.json --strategy-path user_data/strategies
```

`python -m app.paper` unikal bağlantı tokeni və Freqtrade API parolu yaradır.
Mövcud `.env` API açarını saxlayır; mövcud `config.paper.json` üzərinə yazmır.
İki tətbiq eyni lokal maşında işləməlidir. Freqtrade **2026.8** versiyasına pin olunub.
Setup-dan sonra `SYMBOLS` dəyişərsə Freqtrade config-də `exchange.pair_whitelist` də uyğunlaşdırılmalıdır.

## Komponentlər və məlumat axını

| Komponent | Məsuliyyət |
|---|---|
| `app/market.py`, `indicators.py` | Binance USD-M, təzə bid/ask/mark, bağlanmış 15m/1h/4h şamlar və indikatorlar |
| `app/jev.py` | TypeSafe System One API; istiqamət, rejim, momentum, risk, driver və açıq mövqe üçün HOLD/CLOSE |
| `app/engine.py` | Maksimum iki paralel analiz, cache, təhlükəsizlik filtrləri və tarixçə |
| `app/execution.py` | Tokenlə qorunan siqnal yayımı; Freqtrade-dən yalnız GET ilə hesab/mövqe məlumatı |
| `user_data/strategies/JevBridgeStrategy.py` | Yeganə order icraçısı; Freqtrade virtual orderləri, stake, SL/TP və çıxışlar |
| `app/static/` | Azərbaycan dilində radar, JEV cavabları, balans, margin, PnL və trade tarixçəsi |

JEV açıq mövqenin ID, istiqamət və əvvəlcədən hesablanmış mənfəət/zərər vəziyyətini də alır.
`CLOSE` yalnız həmin trade ID-yə aiddir; köhnə qərar yeni mövqeni bağlaya bilməz.
`WAIT` “mövcud mövqeni bağla” demək deyil. Hesablamalar/SL/TP rəqəmləri AI-yə həvalə edilmir.
JEV driver sərbəst düşüncə mətni yox, strukturlaşdırılmış səbəb kateqoriyasıdır.

## İcra və risk qaydaları

- **Yalnız dry-run:** strategiya real ticarət və backtest/hyperopt rejimində başlamır.
- **Yalnız JEV istiqaməti:** model olmadan, API xətasında və ya demo rejimində giriş yoxdur.
- **85% confidence:** direction/momentum/regime/risk/leverage üçün hədd tətbiq edilir. Leverage cavabı yoxdursa və ya etibarsızdırsa giriş yoxdur.
- **Mövqe sayı:** `max_open_trades=-1`. Balans, minimum order, aktiv simvol siyahısı, gündəlik zərər və cooldown qaydaları qalır. Freqtrade hər cüt üçün bir açıq mövqe saxlayır. Standart 5 simvol izlənirsə, say limitinin götürülməsi təkbaşına yeni simvollar əlavə etmir.
- **JEV leverage:** model `1, 2, 3, 5, 10, 15, 20, 25, 50, 75, 100` seçimlərindən birini verir. Tətbiq olunan tam ədəd leverage `min(JEV seçimi, birja maksimumu, floor(0.50 / (stop məsafəsi + 0.0016)))` ilə məhdudlaşır. Stop məsafəsi giriş qiymətinə nisbətdir. Bu konservativ yoxlamadır, dəqiq liquidation qiyməti deyil; Freqtrade əlavə liquidation buffer tətbiq edir. 100× təklif hər zaman 100× icra demək deyil. Məsələn, stop 2% uzaqdadırsa 100× təklif ən çox 23× olur.
- **Məbləğ:** Freqtrade limitsiz mövqe sayı + `stake_amount="unlimited"` qəbul etmir. Config-dəki 140 başlanğıc rəqəmdir; hər dövrədə kapitalın 7%-i/sərbəst balans ilə yenilənir, risk callback-i bunu daha da azalda bilər. Bu rəqəm birjanın leverage tier yoxlaması üçün yuxarı sərhəddir.
- **Siqnalın yaşı:** standart 120 saniyə, bazar snapshot vaxtından hesablanır. Gələcək tarixli və köhnə siqnal rədd edilir.
- **Təkrarsız giriş:** simvol + istiqamət + bağlanmış 15m şam əsasında ID. Freqtrade-in saxlanmış trade tarixçəsi restartdan sonra təkrar girişi də bloklayır. Trade bazasını silmək bu yaddaşı itirər.
- **Margin:** Freqtrade-in istifadə oluna bilən ümumi stake kapitalının maksimum 7%-i; 2,000 üçün 140 USDT. Açıq unrealized PnL bu stake bazasına əlavə edilmir.
- **Mövqe riski:** planlaşdırılmış stop və təxmini round-trip xərc birlikdə kapitalın maksimum 0.5%-i. Buna görə margin bəzən 7%-dən azdır. Bu, gap/slippage zamanı zəmanətli zərər tavanı deyil.
- **Yeni giriş veto-su:** həmin UTC günündə reallaşmış zərər ilkin virtual hesabın 3%-inə çatarsa girişlər bloklanır. 2,000 üçün 60 USDT. Bu limit açıq zərəri avtomatik bağlamır.
- **Cooldown:** hər mövqedən sonra 5 dəqiqə; 60 dəqiqədə 3 stop-loss sonrası 30 dəqiqəlik StoplossGuard.
- **Cari order yoxlaması:** təzə order-book spread maksimum 15 bps; siqnal girişindən qiymət fərqi maksimum 0.3%; xərc sonrası R:R minimum 1.5.
- **SL/TP:** başlanğıc plan SL 2×ATR, TP 4×ATR. Leverage ilə uyğunlaşmaq üçün margin stop fallback-i 5%-dən 50%-ə dəyişib; normal stop yenə qiymət üzrə ATR planıdır. Leverage yoxlaması planlaşdırılmış stop + xərcin margin-in 50%-ni keçməsinə icazə vermir. Kapital üzrə 0.5% risk büdcəsi saxlanılır. Bu limitlər gap/slippage zamanı zəmanət deyil. Target və maksimum 4 saat saxlama limiti qalır. Mövcud mövqenin saxlanmış stop-u genişləndirilmir.
- **Fasilə:** paneldə “Girişləri dayandır” diskdə saxlanılır. Yalnız yeni girişlər dayanır; açıq mövqelərin SL/TP, müddət və JEV çıxışları davam edir. Bu, bütün mövqeləri bağlayan emergency düymə deyil.
- **Disk xətası:** jurnala yazılmayan analiz icraya buraxılmır.
- **Bağlantı xətası:** təzə Freqtrade telemetry yoxdursa giriş siqnalları bağlanır. Panel köhnə balansı cari məlumat kimi göstərmir.

Stop və target ilkin entry tag-də saxlanılır, restartda bərpa olunur.
SL/TP bot səviyyəsindədir: Freqtrade prosesi dayansa simulyasiya da dayanır. Exchange üzərində real qoruma iddiası yoxdur.
Eyni hesabı başqa order icraçısı ilə paylaşmaq bu versiyanın əhatəsində deyil.

## Panel və PnL

- Wallet, sərbəst vəsait və istifadə olunan margin ayrı göstərilir.
- Reallaşmış və ümumi PnL Freqtrade API-dən oxunur; tətbiq ayrıca uyğunsuz balans hesablamır.
- Açıq mövqelərdə istiqamət, giriş/cari qiymət, margin, PnL, SL/TP və funding göstərilir.
- JEV panelində təklif edilən leverage, mövqe sətrində faktiki tətbiq olunan leverage görünür. Risk büdcəsi sabit olduğundan yüksək leverage margin-i azalda bilər; qazancın eyni dəfə artacağı vədi yoxdur.
- Son 30 trade içindən bağlı əməliyyatlar göstərilir; bütün tarixçə Freqtrade SQLite bazasındadır.
- 0.05% hər tərəf üçün komissiya konfiqurasiya olunub. Funding Freqtrade-in əldə etdiyi məlumat qədər hesablanır; onu ikinci dəfə PnL-dən çıxmırıq.
- Dry-run real fill, order-book queue, likvidlik, slippage və faktiki hesab funding-i ilə tam ekvivalent deyil.
- Risk R:R hesabında hər tərəfə 3 bps slippage fərziyyəsi var; bu, Freqtrade PnL-nə ayrıca məcburi yazılmış xərc deyil.

## Konfiqurasiya

| Parametr | Standart / məna |
|---|---|
| `TYPESAFE_API_KEY` | Boşdursa JEV çağırılmır və giriş yoxdur |
| `TYPESAFE_MODEL` | `jev-latest`; faktiki model analiz tarixçəsində saxlanır |
| `SYMBOLS` | BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT |
| `SCAN_SECONDS` | Yeni `.env.example` üçün 60; skan bitəndən sonrakı gözləmə |
| `MIN_AI_CONFIDENCE` | 0.85; gəlirlilik ehtimalı deyil |
| `MAX_SPREAD_BPS` | Analizdə 15; executor ayrıca maksimum 15 yoxlayır |
| `MAX_FUNDING_RATE` | Ödəniş istiqamətində 0.0003; funding R:R hesabına daxil deyil |
| `SIGNAL_TTL_SECONDS` | 120; icra siqnalının maksimum yaşı |
| `BRIDGE_TOKEN` | Setup yaradır; minimum 32 simvol; frontend-ə ötürülmür |
| `FREQTRADE_URL` | `http://127.0.0.1:8083`; yalnız lokal HTTP |
| `FREQTRADE_USER/PASSWORD` | Setup yaradır; dashboard yalnız GET endpoint-ləri oxuyur |
| `DASHBOARD_USER/PASSWORD` | İkisi birlikdə; uzaq giriş üçün HTTPS autentifikasiyası da tələb olunur |
| `DEMO_MODE` | `true`: sintetik vizual demo, AI/order yoxdur |
| `DATA_DIR` | Analiz və pause SQLite bazası, standart `data` |

`Settings`-in env verilməyən fallback scan intervalı geriyə uyğunluq üçün 300 saniyədir.
Mövcud `.env` intervalı avtomatik dəyişdirilmir; daha tez analiz üçün `SCAN_SECONDS=60` seçə bilərsiniz.
1–20 simvol dəstəklənir. Çox simvol/uzun AI cavabı bəzi siqnalları vaxtdan sala bilər; təhlükəsizlik üçün TTL-i kor-koranə artırmayın.
JEV eyni semantik vəziyyət + şam + mövqe konteksti üçün cache edilir; maksimum 200 qeyd, restartda təmizlənir.

## Saxlama və təhlükəsizlik

- `data/analysis.db`: son 2,000 analiz və davamlı giriş pause vəziyyəti. `demo.db` ayrıdır.
- `data/freqtrade-paper.sqlite`: Freqtrade wallet/trade tarixçəsi. Virtual nəticələri saxlamaq üçün silməyin.
- `.env`, `user_data/config.paper.json`, bazalar və loglar Git-ə daxil edilmir.
- 8082 və 8083 yalnız loopback-də dinləyir. Portları birbaşa internetə açmayın.
- Token yalnız `/api/execution/signals` üçün keçərlidir, dashboard idarəetmə hüququ vermir.
- Bir dashboard worker və bir Freqtrade prosesi işlədin. `--reload` istifadə etməyin.
- Backup üçün prosesləri dayandırıb `data` və lokal config-i təhlükəsiz saxlayın.
- Mövcud systemd nümunəsi yalnız dashboard üçündür; avtomatik server deploy-u edilmir.

## API

| Endpoint | Məqsəd |
|---|---|
| `GET /api/status` | Radar + sanitizasiya edilmiş Freqtrade telemetry |
| `GET /api/history` | Son 100 analiz |
| `GET /api/execution/signals` | Bearer bridge token ilə TTL-li icra siqnalları |
| `POST /api/execution/pause`, `/resume` | Yeni girişləri dayandır/davam etdir |
| `POST /api/scan` | Əlavə skan; minimum 60 saniyə interval |
| `GET /health` | Prosesin işləməsi; upstream sağlamlıq zəmanəti deyil |

POST üçün `X-Crypto-Jev: 1` tələb olunur. Cross-site browser POST bloklanır.

## Test və məhdudiyyətlər

```bash
.venv/bin/python -m pip install -e '.[paper,test]'
.venv/bin/python -m pytest -q
node --check app/static/app.js
```

Testlər real Freqtrade 2026.8 strategy resolver/config schema ilə işləyir; exchange/AI cavabları testdə mock edilir.
UI smoke testi üçün `playwright` və Chromium quraşdırın, dashboard-u `DEMO_MODE=true` ilə başladın və
`node tests/ui-smoke.cjs` işlədin. UI testindəki balans/əməliyyatlar **fixture**-dir.

**Canlı JEV və Binance ilə uzunmüddətli paper sınağı bu dəyişiklik zamanı edilməyib.**
Real API açarı olmadan gəlirlilik, siqnal keyfiyyəti və faktiki fill davranışı sübut edilə bilməz.

**Tarixi JEV backtest-i qəsdən bloklanıb.** Bugünkü AI siqnalını keçmiş şamlara tətbiq etmək lookahead bias yaradar.
Etibarlı backtest üçün vaxt möhürlü tarixi snapshot/AI cavab dataset-i və ayrıca replay strategiyası lazımdır; bu versiyaya daxil deyil.
JEV-li və JEV-siz strategiyaların müqayisəsi də hələ nəticəsi olan performans hesabatı deyil.
Bu versiya yoxlanılan inteqrasiya bazasıdır, “zəmanətli qazanc sistemi” deyil.

## Rəsmi mənbələr

- [Freqtrade strategy callbacks](https://www.freqtrade.io/en/stable/strategy-callbacks/)
- [Freqtrade REST API](https://www.freqtrade.io/en/stable/rest-api/)
- [Freqtrade leverage](https://www.freqtrade.io/en/stable/leverage/)
- [TypeSafe System One API](https://docs.typesafe.ai/api)
- [Jev confidence](https://docs.typesafe.ai/confidence)
