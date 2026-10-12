"""Self-contained dark-theme HTML report (Azerbaijani) with inline Chart.js."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import StrategyResult

ASSETS = Path(__file__).parent / 'assets'
MONTHS_AZ = ['Yanvar', 'Fevral', 'Mart', 'Aprel', 'May', 'İyun', 'İyul', 'Avqust', 'Sentyabr', 'Oktyabr', 'Noyabr', 'Dekabr']


def _clean(x):
    if isinstance(x, float):
        return None if not math.isfinite(x) else round(x, 4)
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating,)):
        return _clean(float(x))
    if isinstance(x, (np.integer,)):
        return int(x)
    return x


def _date(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f'{d.day} {MONTHS_AZ[d.month - 1]} {d.year}'


def _monthly(equity: pd.Series, capital: float) -> list[dict]:
    idx = pd.to_datetime(equity.index, unit='ms', utc=True)
    s = pd.Series(equity.to_numpy(), index=idx)
    month_end = s.groupby([idx.year, idx.month]).last()
    prev = month_end.shift().fillna(capital)
    out = []
    for (y, m), v in month_end.items():
        out.append(dict(label=f'{MONTHS_AZ[m - 1]} {y}', ret=(v / prev[(y, m)] - 1) * 100))
    return out


def _series(r: StrategyResult, step: int) -> list[float]:
    eq = r.equity.to_numpy()
    return [round(float(v), 2) for v in np.append(eq[::step], eq[-1])]


def _drawdown(r: StrategyResult, step: int) -> list[float]:
    eq = r.equity.to_numpy()
    dd = (eq / np.maximum.accumulate(eq) - 1) * 100
    return [round(float(v), 2) for v in np.append(dd[::step], dd[-1])]


def build_report(results: list[StrategyResult], benchmark: StrategyResult, data: dict[str, pd.DataFrame],
                 skipped: list[str], out_path: str | Path, capital: float, cost: float, top_n: int = 5) -> Path:
    index = benchmark.equity.index
    step = 6
    labels = [datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:00')
              for t in np.append(index.to_numpy()[::step], index[-1])]
    symbols = list(data)
    liquidity = {s: float((df.close * df.volume).mean()) for s, df in data.items()}
    top_liquid = sorted(symbols, key=liquidity.get, reverse=True)[:25]
    alloc = capital / len(symbols)

    def coin_returns(r):
        return {s: (c.equity[-1] / alloc - 1) * 100 for s, c in r.coins.items()}

    def coin_trades(r):
        return {s: [int(len(c.trade_returns)), float((c.trade_returns > 0).mean() * 100) if len(c.trade_returns) else 0.0]
                for s, c in r.coins.items()}

    strategies = []
    for rank, r in enumerate(results, 1):
        st = r.strategy
        strategies.append(dict(rank=rank, key=st.key, name=st.name, family=st.family, rules=st.rules,
                               metrics=r.metrics, equity=_series(r, step), drawdown=_drawdown(r, step),
                               monthly=_monthly(r.equity, capital), coins=coin_returns(r),
                               coin_trades=coin_trades(r) if rank <= top_n else None))
    bench = dict(name=benchmark.strategy.name, metrics=benchmark.metrics, equity=_series(benchmark, step),
                 monthly=_monthly(benchmark.equity, capital), coins=coin_returns(benchmark))
    payload = _clean(dict(
        labels=labels, strategies=strategies, benchmark=bench, symbols=[s.removesuffix('USDT') for s in symbols],
        symbolKeys=symbols, topLiquid=top_liquid, topN=top_n,
        meta=dict(start=_date(int(index[0])), end=_date(int(index[-1]) + 3_600_000), bars=len(index),
                  coins=len(symbols), skipped=len(skipped), capital=capital, cost=cost * 100,
                  generated=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))))
    chart_js = (ASSETS / 'chart.umd.min.js').read_text(encoding='utf-8').replace('</script', '<\\/script')
    html = (TEMPLATE.replace('/*__CHARTJS__*/', chart_js)
            .replace('__DATA__', json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')))
    out = Path(out_path)
    out.write_text(html, encoding='utf-8')
    return out


TEMPLATE = r"""<!DOCTYPE html>
<html lang="az">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kripto Strategiyaları — Backtest Hesabatı</title>
<style>
:root{--bg:#0b0f17;--panel:#121826;--panel2:#182033;--line:#243049;--text:#e6ebf5;--muted:#8b97b0;
--green:#16c784;--red:#ea3943;--gold:#f0b90b;--blue:#3b82f6;--purple:#a855f7}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 10% -10%,#1a2340 0,transparent 60%),var(--bg);color:var(--text);
font:14px/1.5 "Inter","Segoe UI",system-ui,-apple-system,sans-serif}
.wrap{max-width:1440px;margin:0 auto;padding:28px 24px 60px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:22px}
h1{margin:0;font-size:30px;letter-spacing:-.5px}
h1 span{background:linear-gradient(90deg,var(--gold),#ff8a00);-webkit-background-clip:text;background-clip:text;color:transparent}
h2{font-size:19px;margin:0 0 4px}
.sub{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:12px;color:var(--muted)}
.chip b{color:var(--text)}
nav{position:sticky;top:0;z-index:5;background:rgba(11,15,23,.85);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
margin:0 -24px 22px;padding:10px 24px;display:flex;gap:6px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;padding:6px 12px;border-radius:8px;font-size:13px}
nav a:hover{background:var(--panel2);color:var(--text)}
section{margin-bottom:28px}
.card{background:linear-gradient(180deg,var(--panel),#0f1522);border:1px solid var(--line);border-radius:14px;padding:18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px}
.kpi .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.6px}
.kpi .v{font-size:21px;font-weight:700;margin-top:4px}
.kpi .s{color:var(--muted);font-size:12px;margin-top:2px}
.pos{color:var(--green)}.neg{color:var(--red)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:1000px){.grid2{grid-template-columns:1fr}}
.chart{position:relative;height:380px}.chart.tall{height:480px}.chart.sm{height:150px}
.head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.btns button{background:var(--panel2);color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:6px 11px;cursor:pointer;font-size:12px}
.btns button.on,.btns button:hover{color:#111;background:var(--gold);border-color:var(--gold)}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:12px;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--panel)}
th:hover{color:var(--text)}
td:first-child,th:first-child,td.l,th.l{text-align:left}
tbody tr:hover{background:rgba(255,255,255,.03)}
.rank{display:inline-flex;width:26px;height:26px;border-radius:50%;align-items:center;justify-content:center;background:var(--panel2);font-weight:700;font-size:12px}
.r1{background:var(--gold);color:#111}.r2{background:#c0c7d4;color:#111}.r3{background:#cd7f32;color:#111}
.fam{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--panel2);color:var(--muted);border:1px solid var(--line)}
.bench td{color:var(--muted);font-style:italic}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.scard .head{flex-wrap:nowrap;align-items:flex-start}
.scard h3{margin:0;font-size:15px;min-width:0}
.scard .fam{flex-shrink:0}
.scard .rules{color:var(--muted);font-size:12px;margin:6px 0 10px;min-height:36px}
.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:10px}
.mini div{background:var(--panel2);border-radius:8px;padding:6px 8px}
.mini span{display:block;color:var(--muted);font-size:10.5px;text-transform:uppercase}
.mini b{font-size:14px}
.heat td{text-align:center;font-weight:600;font-size:12px;padding:7px 6px}
.heat td:first-child{text-align:left;font-weight:500}
input[type=search]{background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:8px 12px;border-radius:8px;min-width:240px}
.note{color:var(--muted);font-size:12.5px}
.note li{margin:4px 0}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:30px}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>Kripto Strategiyaları <span>Backtest Hesabatı</span></h1>
    <div class="sub" id="subtitle"></div>
  </div>
  <div class="chips" id="chips"></div>
</header>
<nav>
  <a href="#xulase">Xülasə</a><a href="#reytinq">Reytinq</a><a href="#qrafikler">Müqayisə qrafikləri</a>
  <a href="#ayliq">Aylıq gəlirlər</a><a href="#strategiyalar">Strategiyalar</a><a href="#coinler">Coin üzrə analiz</a>
  <a href="#metodologiya">Metodologiya</a>
</nav>

<section id="xulase"><div class="kpis" id="kpis"></div></section>

<section id="reytinq" class="card">
  <div class="head"><div><h2>Strategiyaların reytinqi</h2>
  <div class="sub">Ümumi gəlirə görə sıralanıb. Sütun başlığına klikləyərək sıralamanı dəyişin.</div></div></div>
  <div class="tbl-wrap"><table id="rankTable"></table></div>
</section>

<section id="qrafikler">
  <div class="card" style="margin-bottom:16px">
    <div class="head"><div><h2>Kapital əyriləri (Equity)</h2><div class="sub">Hər strategiya 2000 $ ilə başlayır; qırıq xətt — "Al və saxla" benchmark.</div></div>
    <div class="btns" id="eqBtns"><button data-m="top">Ən yaxşı 5</button><button data-m="bottom">Ən zəif 5</button><button data-m="all" class="on">Hamısı</button></div></div>
    <div class="chart tall"><canvas id="eqChart"></canvas></div>
  </div>
  <div class="grid2">
    <div class="card"><h2>Ümumi gəlir, %</h2><div class="sub">Benchmark ilə müqayisə</div><div class="chart tall"><canvas id="retChart"></canvas></div></div>
    <div class="card"><h2>Risk / gəlir xəritəsi</h2><div class="sub">X — maksimum geriləmə, Y — gəlir; dairə ölçüsü — əməliyyat sayı</div><div class="chart tall"><canvas id="riskChart"></canvas></div></div>
    <div class="card"><h2>Uduş faizi və mənfəət faktoru</h2><div class="sub">Sol ox — uduş %, sağ ox — mənfəət faktoru</div><div class="chart"><canvas id="wrChart"></canvas></div></div>
    <div class="card"><h2>Sharpe və Sortino əmsalları</h2><div class="sub">İllikləşdirilmiş, saatlıq gəlirlər əsasında</div><div class="chart"><canvas id="shChart"></canvas></div></div>
    <div class="card"><h2>Geriləmə (Drawdown) — ən yaxşı 5</h2><div class="sub">Pik nöqtədən faizlə düşüş</div><div class="chart"><canvas id="ddChart"></canvas></div></div>
    <div class="card"><h2>Əməliyyat sayı və bazarda olma müddəti</h2><div class="sub">Sol ox — əməliyyat sayı, sağ ox — bazarda olma %</div><div class="chart"><canvas id="trChart"></canvas></div></div>
  </div>
</section>

<section id="ayliq" class="card">
  <div class="head"><div><h2>Aylıq gəlirlər, %</h2><div class="sub">Hər strategiyanın təqvim ayı üzrə nəticəsi (UTC). İlk və son ay qismən dövrdür.</div></div></div>
  <div class="tbl-wrap"><table class="heat" id="monthTable"></table></div>
</section>

<section id="strategiyalar">
  <div class="head"><div><h2>Strategiya kartları</h2><div class="sub">20 strategiyanın hər biri müstəqil 2000 $ virtual balansla</div></div></div>
  <div class="cards" id="cards"></div>
</section>

<section id="coinler">
  <div class="card" style="margin-bottom:16px">
    <div class="head"><div><h2>Ən yaxşı strategiyalar üzrə coin analizi</h2><div class="sub">Hər coin üçün ayrılmış alt-hesabın gəliri (%). Ən yaxşı və ən zəif 12 coin.</div></div>
    <div class="tabs btns" id="coinTabs"></div></div>
    <div class="grid2"><div class="chart tall"><canvas id="bestCoins"></canvas></div><div class="chart tall"><canvas id="worstCoins"></canvas></div></div>
    <div class="kpis" id="coinKpis" style="margin-top:14px"></div>
  </div>
  <div class="card" style="margin-bottom:16px">
    <div class="head"><div><h2>İstilik xəritəsi — ən likvid 25 coin × 20 strategiya</h2><div class="sub">Coin üzrə gəlir, %. Likvidlik dövr ərzində orta saatlıq USDT dövriyyəsinə görədir.</div></div></div>
    <div class="tbl-wrap"><table class="heat" id="heatTable"></table></div>
  </div>
  <div class="card">
    <div class="head"><div><h2>Bütün coinlər — ən yaxşı 5 strategiya</h2><div class="sub">Coin üzrə gəlir %, əməliyyat sayı və uduş faizi</div></div>
    <input type="search" id="coinSearch" placeholder="Coin axtar (məs. BTC)"></div>
    <div class="tbl-wrap" style="max-height:560px;overflow:auto"><table id="coinTable"></table></div>
  </div>
</section>

<section id="metodologiya" class="card">
  <h2>Metodologiya və qeydlər</h2>
  <ul class="note" id="method"></ul>
</section>
<footer id="footer"></footer>
</div>

<script>/*__CHARTJS__*/</script>
<script>
(()=>{
const D = __DATA__;
const S = D.strategies, B = D.benchmark, M = D.meta;
const PALETTE = ['#f0b90b','#16c784','#3b82f6','#a855f7','#ef4444','#06b6d4','#f97316','#84cc16','#ec4899','#14b8a6',
  '#eab308','#8b5cf6','#22c55e','#0ea5e9','#f43f5e','#d946ef','#10b981','#fb923c','#6366f1','#facc15'];
const colorOf = {}; S.forEach((s,i)=>colorOf[s.key]=PALETTE[i%PALETTE.length]);
const f2 = (v,d=2)=>v==null?'∞':Number(v).toLocaleString('az-AZ',{minimumFractionDigits:d,maximumFractionDigits:d});
const pct = (v,d=2)=>(v>0?'+':'')+f2(v,d)+'%';
const usd = v=>(v<0?'−':'')+'$'+f2(Math.abs(v));
const cls = v=>v>0?'pos':v<0?'neg':'';
const el = id=>document.getElementById(id);
Chart.defaults.color='#8b97b0'; Chart.defaults.borderColor='rgba(36,48,73,.6)';
Chart.defaults.font.family='Inter,Segoe UI,system-ui,sans-serif';
Chart.defaults.plugins.legend.labels.boxWidth=12;
Chart.defaults.plugins.tooltip.backgroundColor='#0b0f17'; Chart.defaults.plugins.tooltip.borderColor='#243049'; Chart.defaults.plugins.tooltip.borderWidth=1;

// Header
el('subtitle').textContent = `${M.start} — ${M.end} · 1 saatlıq şamlar · Binance spot USDT bazarları`;
el('chips').innerHTML = [['Coin sayı',M.coins],['Strategiya',S.length],['Başlanğıc balans','$'+f2(M.capital,0)+' / strategiya'],
  ['Şam sayı',M.bars.toLocaleString('az-AZ')],['Komissiya+sürüşmə',f2(M.cost,2)+'% / tərəf']]
  .map(([k,v])=>`<span class="chip">${k}: <b>${v}</b></span>`).join('');

// KPIs
const best=S[0], worst=S[S.length-1];
const profitable=S.filter(s=>s.metrics.total_return>0).length;
const beatBench=S.filter(s=>s.metrics.total_return>B.metrics.total_return).length;
const avgRet=S.reduce((a,s)=>a+s.metrics.total_return,0)/S.length;
const bestSharpe=[...S].sort((a,b)=>b.metrics.sharpe-a.metrics.sharpe)[0];
const totalTrades=S.reduce((a,s)=>a+s.metrics.trades,0);
const kpi=(l,v,s,c='')=>`<div class="card kpi"><div class="l">${l}</div><div class="v ${c}">${v}</div><div class="s">${s}</div></div>`;
el('kpis').innerHTML = [
  kpi('🏆 Ən yaxşı strategiya',best.name,`${pct(best.metrics.total_return)} · son balans ${usd(best.metrics.final_balance)}`,''),
  kpi('Ən yüksək gəlir',pct(best.metrics.total_return),`Xalis mənfəət ${usd(best.metrics.net_profit)}`,cls(best.metrics.total_return)),
  kpi('📉 Ən zəif strategiya',worst.name,`${pct(worst.metrics.total_return)} · son balans ${usd(worst.metrics.final_balance)}`),
  kpi('Mənfəətli strategiyalar',`${profitable} / ${S.length}`,`Benchmarkı ötən: ${beatBench}`,profitable>=S.length/2?'pos':'neg'),
  kpi('Al və saxla (benchmark)',pct(B.metrics.total_return),`Maks. geriləmə ${f2(B.metrics.max_drawdown)}%`,cls(B.metrics.total_return)),
  kpi('Orta strategiya gəliri',pct(avgRet),`Ümumi əməliyyat: ${totalTrades.toLocaleString('az-AZ')}`,cls(avgRet)),
  kpi('Ən yaxşı Sharpe',f2(bestSharpe.metrics.sharpe),bestSharpe.name,cls(bestSharpe.metrics.sharpe)),
].join('');

// Ranking table
const cols=[['#','rank',null],['Strategiya','name',null,'l'],['Növ','family',null,'l'],['Son balans','final_balance',usd],
 ['Gəlir %','total_return',pct],['Xalis mənfəət','net_profit',usd],['Uduş %','win_rate',v=>f2(v,1)+'%'],
 ['Maks. geriləmə','max_drawdown',v=>'−'+f2(v)+'%'],['Sharpe','sharpe',f2],['Sortino','sortino',f2],['Calmar','calmar',f2],
 ['Əməliyyat','trades',v=>v.toLocaleString('az-AZ')],['Mənfəət faktoru','profit_factor',f2],['Orta əməliyyat','avg_trade',pct],
 ['Orta saxlama (saat)','avg_hold_hours',v=>f2(v,1)],['Bazarda %','exposure',v=>f2(v,1)+'%'],['Mənfəətli coin','profitable_coins',null]];
let sortKey='total_return', sortDir=-1;
function val(s,k){return k==='rank'?s.rank:k==='name'?s.name:k==='family'?s.family:s.metrics[k]}
function renderRank(){
  const rows=[...S].sort((a,b)=>{const x=val(a,sortKey),y=val(b,sortKey);
    return (typeof x==='string'?x.localeCompare(y,'az'):((x??1e9)-(y??1e9)))*sortDir});
  const head='<thead><tr>'+cols.map(c=>`<th class="${c[3]||''}" data-k="${c[1]}">${c[0]}${c[1]===sortKey?(sortDir<0?' ▼':' ▲'):''}</th>`).join('')+'</tr></thead>';
  const cell=(s,c)=>{const k=c[1];
    if(k==='rank')return `<td><span class="rank ${s.rank<=3?'r'+s.rank:''}">${s.rank}</span></td>`;
    if(k==='name')return `<td class="l"><span style="color:${colorOf[s.key]}">●</span> <b>${s.name}</b></td>`;
    if(k==='family')return `<td class="l"><span class="fam">${s.family}</span></td>`;
    if(k==='profitable_coins')return `<td>${s.metrics.profitable_coins} / ${s.metrics.coin_count}</td>`;
    const v=s.metrics[k]; const c2=['total_return','net_profit','avg_trade','sharpe','sortino','calmar'].includes(k)?cls(v):
      k==='profit_factor'?(v==null||v>1?'pos':'neg'):'';
    return `<td class="${c2}">${c[2](v)}</td>`};
  const bm=B.metrics;
  const benchRow=`<tr class="bench"><td>—</td><td class="l">${B.name}</td><td class="l"><span class="fam">Benchmark</span></td><td>${usd(bm.final_balance)}</td><td>${pct(bm.total_return)}</td><td>${usd(bm.net_profit)}</td><td>—</td><td>−${f2(bm.max_drawdown)}%</td><td>${f2(bm.sharpe)}</td><td>${f2(bm.sortino)}</td><td>${f2(bm.calmar)}</td><td>${bm.trades}</td><td>${f2(bm.profit_factor)}</td><td>${pct(bm.avg_trade)}</td><td>—</td><td>100%</td><td>${bm.profitable_coins} / ${bm.coin_count}</td></tr>`;
  el('rankTable').innerHTML=head+'<tbody>'+rows.map(s=>'<tr>'+cols.map(c=>cell(s,c)).join('')+'</tr>').join('')+benchRow+'</tbody>';
  el('rankTable').querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;
    if(k===sortKey)sortDir*=-1;else{sortKey=k;sortDir=['rank','name','family','max_drawdown'].includes(k)?1:-1}renderRank()});
}
renderRank();

// Equity chart
const benchDs={label:B.name,data:B.equity,borderColor:'#cbd5e1',borderDash:[6,4],borderWidth:2,pointRadius:0,tension:.1};
const eqDs=S.map(s=>({label:s.name,data:s.equity,borderColor:colorOf[s.key],borderWidth:1.6,pointRadius:0,tension:.1,key:s.key}));
const eqChart=new Chart(el('eqChart'),{type:'line',data:{labels:D.labels,datasets:[...eqDs,benchDs]},
  options:{responsive:true,maintainAspectRatio:false,animation:false,interaction:{mode:'nearest',intersect:false},
  scales:{x:{ticks:{maxTicksLimit:10}},y:{ticks:{callback:v=>'$'+v}}},
  plugins:{legend:{position:'right'},tooltip:{callbacks:{label:c=>`${c.dataset.label}: $${f2(c.parsed.y)}`}}}}});
el('eqBtns').querySelectorAll('button').forEach(b=>b.onclick=()=>{
  el('eqBtns').querySelectorAll('button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  const m=b.dataset.m;eqChart.data.datasets.forEach((ds,i)=>{if(i>=S.length)return;
    ds.hidden=m==='top'?i>=5:m==='bottom'?i<S.length-5:false});eqChart.update()});

// Return bar chart
const retRows=[...S.map(s=>({n:s.name,v:s.metrics.total_return,b:false})),{n:B.name,v:B.metrics.total_return,b:true}].sort((a,b)=>b.v-a.v);
new Chart(el('retChart'),{type:'bar',data:{labels:retRows.map(r=>r.n),datasets:[{data:retRows.map(r=>r.v),
  backgroundColor:retRows.map(r=>r.b?'#94a3b8':r.v>=0?'rgba(22,199,132,.8)':'rgba(234,57,67,.8)'),borderRadius:4}]},
  options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},
  tooltip:{callbacks:{label:c=>pct(c.parsed.x)}}},scales:{x:{ticks:{callback:v=>v+'%'}}}}});

// Risk/return bubble
const maxTr=Math.max(...S.map(s=>s.metrics.trades),1);
new Chart(el('riskChart'),{type:'bubble',data:{datasets:[...S.map(s=>({label:s.name,backgroundColor:colorOf[s.key]+'bb',borderColor:colorOf[s.key],
  data:[{x:s.metrics.max_drawdown,y:s.metrics.total_return,r:5+14*Math.sqrt(s.metrics.trades/maxTr)}]})),
  {label:B.name,backgroundColor:'#cbd5e1',data:[{x:B.metrics.max_drawdown,y:B.metrics.total_return,r:9}],pointStyle:'rectRot'}]},
  options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},
  tooltip:{callbacks:{label:c=>`${c.dataset.label}: gəlir ${pct(c.parsed.y)}, geriləmə −${f2(c.parsed.x)}%`}}},
  scales:{x:{title:{display:true,text:'Maksimum geriləmə, %'}},y:{title:{display:true,text:'Ümumi gəlir, %'}}}}});

const names=S.map(s=>s.name);
const shortNames=S.map(s=>s.name.length>18?s.name.slice(0,17)+'…':s.name);
new Chart(el('wrChart'),{data:{labels:shortNames,datasets:[
  {type:'bar',label:'Uduş %',data:S.map(s=>s.metrics.win_rate),backgroundColor:'rgba(59,130,246,.75)',yAxisID:'y',borderRadius:3},
  {type:'line',label:'Mənfəət faktoru',data:S.map(s=>s.metrics.profit_factor),borderColor:'#f0b90b',backgroundColor:'#f0b90b',yAxisID:'y1',tension:.2}]},
  options:{responsive:true,maintainAspectRatio:false,scales:{x:{ticks:{maxRotation:60,minRotation:45,font:{size:10}}},
  y:{ticks:{callback:v=>v+'%'}},y1:{position:'right',grid:{display:false}}},plugins:{tooltip:{callbacks:{title:i=>names[i[0].dataIndex]}}}}});
new Chart(el('shChart'),{type:'bar',data:{labels:shortNames,datasets:[
  {label:'Sharpe',data:S.map(s=>s.metrics.sharpe),backgroundColor:'rgba(168,85,247,.8)',borderRadius:3},
  {label:'Sortino',data:S.map(s=>s.metrics.sortino),backgroundColor:'rgba(6,182,212,.7)',borderRadius:3}]},
  options:{responsive:true,maintainAspectRatio:false,scales:{x:{ticks:{maxRotation:60,minRotation:45,font:{size:10}}}},
  plugins:{tooltip:{callbacks:{title:i=>names[i[0].dataIndex]}}}}});
new Chart(el('ddChart'),{type:'line',data:{labels:D.labels,datasets:S.slice(0,5).map(s=>({label:s.name,data:s.drawdown,
  borderColor:colorOf[s.key],backgroundColor:colorOf[s.key]+'22',fill:true,pointRadius:0,borderWidth:1.4}))},
  options:{responsive:true,maintainAspectRatio:false,animation:false,interaction:{mode:'index',intersect:false},
  scales:{x:{ticks:{maxTicksLimit:8}},y:{ticks:{callback:v=>v+'%'}}}}});
new Chart(el('trChart'),{data:{labels:shortNames,datasets:[
  {type:'bar',label:'Əməliyyat sayı',data:S.map(s=>s.metrics.trades),backgroundColor:'rgba(240,185,11,.7)',borderRadius:3,yAxisID:'y'},
  {type:'line',label:'Bazarda olma %',data:S.map(s=>s.metrics.exposure),borderColor:'#16c784',backgroundColor:'#16c784',yAxisID:'y1',tension:.2}]},
  options:{responsive:true,maintainAspectRatio:false,scales:{x:{ticks:{maxRotation:60,minRotation:45,font:{size:10}}},
  y1:{position:'right',grid:{display:false},ticks:{callback:v=>v+'%'}}},plugins:{tooltip:{callbacks:{title:i=>names[i[0].dataIndex]}}}}});

// Heat colour
function heat(v,scale){if(v==null)return '';const a=Math.min(Math.abs(v)/scale,1)*.75+.08;
  return `background:${v>=0?`rgba(22,199,132,${a})`:`rgba(234,57,67,${a})`};color:${a>.45?'#fff':'#e6ebf5'}`}

// Monthly table
const months=B.monthly.map(m=>m.label);
const mScale=Math.max(5,...S.flatMap(s=>s.monthly.map(m=>Math.abs(m.ret))))*.7;
el('monthTable').innerHTML='<thead><tr><th class="l">Strategiya</th>'+months.map(m=>`<th style="text-align:center">${m}</th>`).join('')+'<th style="text-align:center">Cəmi</th></tr></thead><tbody>'+
  [...S,{name:B.name,monthly:B.monthly,metrics:B.metrics}].map(s=>`<tr><td>${s.name}</td>`+s.monthly.map(m=>`<td style="${heat(m.ret,mScale)}">${pct(m.ret,1)}</td>`).join('')+
  `<td style="${heat(s.metrics.total_return,mScale*2)}">${pct(s.metrics.total_return,1)}</td></tr>`).join('')+'</tbody>';

// Strategy cards
el('cards').innerHTML=S.map(s=>{const m=s.metrics;return `<div class="card scard">
  <div class="head" style="margin:0"><h3><span class="rank ${s.rank<=3?'r'+s.rank:''}">${s.rank}</span> ${s.name}</h3><span class="fam">${s.family}</span></div>
  <div class="rules">${s.rules}</div><div class="chart sm"><canvas id="mini_${s.key}"></canvas></div>
  <div class="mini"><div><span>Gəlir</span><b class="${cls(m.total_return)}">${pct(m.total_return)}</b></div>
  <div><span>Son balans</span><b>${usd(m.final_balance)}</b></div><div><span>Uduş %</span><b>${f2(m.win_rate,1)}%</b></div>
  <div><span>Maks. geriləmə</span><b class="neg">−${f2(m.max_drawdown)}%</b></div><div><span>Sharpe</span><b class="${cls(m.sharpe)}">${f2(m.sharpe)}</b></div>
  <div><span>Mənfəət faktoru</span><b class="${m.profit_factor==null||m.profit_factor>1?'pos':'neg'}">${f2(m.profit_factor)}</b></div>
  <div><span>Əməliyyat</span><b>${m.trades.toLocaleString('az-AZ')}</b></div><div><span>Ən yaxşı əməliyyat</span><b class="pos">${pct(m.best_trade,1)}</b></div>
  <div><span>Ən pis əməliyyat</span><b class="neg">${pct(m.worst_trade,1)}</b></div></div></div>`}).join('');
S.forEach(s=>new Chart(el('mini_'+s.key),{type:'line',data:{labels:D.labels,datasets:[
  {data:s.equity,borderColor:s.metrics.total_return>=0?'#16c784':'#ea3943',backgroundColor:(s.metrics.total_return>=0?'#16c784':'#ea3943')+'1f',fill:true,pointRadius:0,borderWidth:1.5},
  {data:B.equity,borderColor:'#64748b',borderDash:[4,3],pointRadius:0,borderWidth:1}]},
  options:{responsive:true,maintainAspectRatio:false,animation:false,plugins:{legend:{display:false},tooltip:{enabled:false}},
  scales:{x:{display:false},y:{ticks:{font:{size:9},callback:v=>'$'+v},grid:{display:false}}}}}));

// Per-coin breakdown for top strategies
const topS=S.slice(0,D.topN);
const sym=k=>k.replace(/USDT$/,'');
let bestC,worstC;
function showCoins(i){
  const s=topS[i];el('coinTabs').querySelectorAll('button').forEach((b,j)=>b.classList.toggle('on',j===i));
  const arr=Object.entries(s.coins).sort((a,b)=>b[1]-a[1]);const b12=arr.slice(0,12),w12=arr.slice(-12).reverse();
  const mk=(id,rows,col,title)=>new Chart(el(id),{type:'bar',data:{labels:rows.map(r=>sym(r[0])),datasets:[{data:rows.map(r=>r[1]),backgroundColor:col,borderRadius:4}]},
    options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},title:{display:true,text:title,color:'#e6ebf5'},
    tooltip:{callbacks:{label:c=>pct(c.parsed.x)}}},scales:{x:{ticks:{callback:v=>v+'%'}}}}});
  bestC&&bestC.destroy();worstC&&worstC.destroy();
  bestC=mk('bestCoins',b12,'rgba(22,199,132,.8)',`${s.name} — ən yaxşı 12 coin`);
  worstC=mk('worstCoins',w12,'rgba(234,57,67,.8)',`${s.name} — ən zəif 12 coin`);
  const vals=arr.map(a=>a[1]);const med=[...vals].sort((a,b)=>a-b)[Math.floor(vals.length/2)];
  el('coinKpis').innerHTML=[kpi('Mənfəətli coinlər',`${s.metrics.profitable_coins} / ${s.metrics.coin_count}`,`${f2(s.metrics.profitable_coins/s.metrics.coin_count*100,1)}%`),
    kpi('Ən yaxşı coin',sym(arr[0][0]),pct(arr[0][1]),'pos'),kpi('Ən zəif coin',sym(arr[arr.length-1][0]),pct(arr[arr.length-1][1]),'neg'),
    kpi('Median coin gəliri',pct(med),'Coin alt-hesablarının medianı',cls(med))].join('');
}
el('coinTabs').innerHTML=topS.map((s,i)=>`<button>${i+1}. ${s.name}</button>`).join('');
el('coinTabs').querySelectorAll('button').forEach((b,i)=>b.onclick=()=>showCoins(i));showCoins(0);

// Heatmap
const hScale=40;
el('heatTable').innerHTML='<thead><tr><th class="l">Strategiya</th>'+D.topLiquid.map(k=>`<th style="text-align:center">${sym(k)}</th>`).join('')+'</tr></thead><tbody>'+
  [...S,{name:B.name,coins:B.coins}].map(s=>`<tr><td>${s.name}</td>`+D.topLiquid.map(k=>`<td style="${heat(s.coins[k],hScale)}" title="${sym(k)}: ${pct(s.coins[k])}">${f2(s.coins[k],1)}</td>`).join('')+'</tr>').join('')+'</tbody>';

// Full coin table
let cSort='0',cDir=-1;
function renderCoins(){
  const q=el('coinSearch').value.trim().toUpperCase();
  let keys=D.symbolKeys.filter(k=>!q||sym(k).includes(q));
  const v=(k)=>cSort==='sym'?sym(k):cSort==='bh'?B.coins[k]:topS[+cSort].coins[k];
  keys.sort((a,b)=>{const x=v(a),y=v(b);return (typeof x==='string'?x.localeCompare(y):x-y)*cDir});
  el('coinTable').innerHTML='<thead><tr><th class="l" data-k="sym">Coin</th>'+topS.map((s,i)=>`<th data-k="${i}">${s.name}<br><small>gəlir · əməl. · uduş</small></th>`).join('')+
    '<th data-k="bh">Al və saxla</th></tr></thead><tbody>'+keys.map(k=>`<tr><td class="l"><b>${sym(k)}</b></td>`+
    topS.map(s=>{const r=s.coins[k],t=s.coin_trades[k];return `<td><span class="${cls(r)}">${pct(r)}</span> · ${t[0]} · ${f2(t[1],0)}%</td>`}).join('')+
    `<td class="${cls(B.coins[k])}">${pct(B.coins[k])}</td></tr>`).join('')+'</tbody>';
  el('coinTable').querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(k===cSort)cDir*=-1;else{cSort=k;cDir=k==='sym'?1:-1}renderCoins()});
}
el('coinSearch').oninput=renderCoins;renderCoins();

// Methodology
el('method').innerHTML=[
  `Məlumat mənbəyi: Binance spot bazarının rəsmi açıq məlumat API-si (data-api.binance.vision). Dövr: <b>${M.start} — ${M.end}</b>, ${M.bars} saatlıq şam.`,
  `Coin siyahısı: bütün aktiv USDT spot cütləri; stablecoin, fiat, qızıl və "wrapped" aktivlər çıxarılıb. Dövrün tam tarixçəsi olmayan <b>${M.skipped}</b> cüt (yeni listinqlər, tokenləşdirilmiş səhmlər) nəzərə alınmayıb. Nəticədə <b>${M.coins}</b> coin test edilib.`,
  `Hər strategiya müstəqil <b>$${f2(M.capital,0)}</b> virtual balansla başlayır. Balans bütün coinlərə bərabər bölünür (coin başına $${f2(M.capital/M.coins,2)}), hər coin öz alt-hesabında ticarət edir. Strategiyanın kapitalı alt-hesabların cəmidir.`,
  `Yalnız alış (long) mövqeləri, leverage olmadan spot ticarət. Siqnal şam bağlananda hesablanır, əməliyyat <b>növbəti şamın açılış qiymətində</b> icra olunur — gələcək məlumatdan istifadə edilmir.`,
  `Hər alış və satışa ${f2(M.cost,2)}% xərc tətbiq olunur (0.10% komissiya + 0.05% sürüşmə). Dövrün sonunda açıq mövqelər son bağlanış qiymətinə bağlanır.`,
  `Sharpe/Sortino saatlıq kapital gəlirlərindən illikləşdirilib (√8760), risksiz faiz 0 qəbul edilib. Mənfəət faktoru = ümumi qazanc / ümumi zərər (USDT ilə). Maks. geriləmə portfelin pik dəyərindən ən böyük düşüşdür.`,
  `Benchmark "Al və saxla": eyni kapital bütün coinlərə bərabər bölünüb dövrün əvvəlində alınır və sonunda satılır.`,
  `<b>Xəbərdarlıq:</b> bu hesabat yalnız tarixi simulyasiyadır və investisiya məsləhəti deyil. Keçmiş nəticələr gələcək gəliri təmin etmir; real bazarda likvidlik, sürüşmə və gecikmə nəticələri dəyişə bilər.`
].map(x=>`<li>${x}</li>`).join('');
el('footer').textContent=`Hazırlanıb: ${M.generated} · 20 strategiya backtest modulu · Chart.js ilə`;
})();
</script>
</body>
</html>
"""
