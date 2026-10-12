'use strict';
/* 20 strategy virtual trading dashboard. Read-only: it only GETs /api/* and never changes accounts. */

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 10000;
const state = { overview: null, curves: null, curvesAt: 0, detail: null, view: '', sort: { col: 'rank', dir: 1 }, charts: {}, clockOffset: 0 };

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const money = (v, sign = false) => isNum(v) ? (sign && v > 0 ? '+' : '') + v.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—';
const pct = (v, sign = true) => isNum(v) ? (sign && v > 0 ? '+' : '') + v.toFixed(2) + '%' : '—';
const num = (v, d = 2) => isNum(v) ? v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
const cls = (v) => (isNum(v) && v > 0 ? 'pos' : isNum(v) && v < 0 ? 'neg' : '');
const pf = (v) => v === 'inf' ? '∞' : isNum(v) ? v.toFixed(2) : '—';
function price(v) {
  if (!isNum(v)) return '—';
  const d = v >= 1000 ? 2 : v >= 1 ? 4 : v >= 0.01 ? 6 : 8;
  return v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function when(ms) {
  if (!isNum(ms)) return '—';
  return new Date(ms).toLocaleString('az-AZ', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}
function duration(ms) {
  if (!isNum(ms) || ms < 0) return '—';
  const h = ms / 3600000;
  return h >= 48 ? (h / 24).toFixed(1) + ' gün' : h >= 1 ? h.toFixed(1) + ' saat' : Math.round(ms / 60000) + ' dəq';
}
const color = (i, n = 20) => `hsl(${Math.round(i * 360 / n)}, 72%, 60%)`;

async function getJSON(url) {
  const r = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

/* ---------- routing ---------- */
function route() {
  const m = location.hash.match(/^#\/s\/([a-z0-9_]+)$/);
  state.view = m ? m[1] : '';
  $('view-overview').hidden = !!state.view;
  $('view-strategy').hidden = !state.view;
  state.detail = null;
  renderNav();
  refresh(true);
  window.scrollTo(0, 0);
}
function ordered() {
  return state.overview ? [...state.overview.strategies].sort((a, b) => a.order - b.order) : [];
}
function step(delta) {
  const list = ordered();
  if (!list.length) return;
  const i = list.findIndex((s) => s.key === state.view);
  const next = list[(i + delta + list.length) % list.length];
  location.hash = '#/s/' + next.key;
}

/* ---------- status & nav ---------- */
function renderStatus(st) {
  state.clockOffset = st.now - Date.now();
  const mode = $('mode');
  mode.textContent = st.mode === 'demo' ? 'DEMO (sintetik bazar)' : 'CANLI BAZAR · VİRTUAL HESAB';
  mode.className = 'mode' + (st.mode === 'demo' ? ' demo' : '');
  $('st-tf').textContent = st.timeframe;
  $('st-universe').textContent = st.universe ? `${st.universe}${st.symbols === 'ALL' ? ' (top ' + st.top_n + ')' : ''}` : '—';
  $('st-candle').textContent = when(st.last_candle);
  const age = st.last_price_at ? Math.round((Date.now() - st.last_price_at) / 1000) : null;
  $('st-price').textContent = age === null ? '—' : `${age} s əvvəl`;
  $('notice').textContent = st.error ? `Bazar məlumatı xətası: ${st.error.message} — növbəti dövrdə yenidən cəhd edilir.` :
    (!st.last_candle ? 'İlk bağlanmış şam gözlənilir / yüklənir… Hesablar 2000 $ ilə başlayır; mövqelər yalnız təzə siqnal gələndə açılır.' : '');
  state.status = st;
  tickClock();
}
function tickClock() {
  const st = state.status;
  if (!st) return;
  if (st.mode === 'demo') { $('st-next').textContent = when(st.next_candle) + ' (demo saatı)'; return; }
  const left = Math.max(0, st.next_candle - (Date.now() + state.clockOffset));
  const m = Math.floor(left / 60000), s = Math.floor(left / 1000) % 60;
  $('st-next').textContent = `${m}:${String(s).padStart(2, '0')}`;
}

function renderNav() {
  const list = ordered();
  $('nav-list').innerHTML = list.map((s) => `
    <a class="nav-item${state.view === s.key ? ' active' : ''}" href="#/s/${esc(s.key)}">
      <span class="nav-rank">#${s.rank}</span>
      <span class="nav-name">${esc(s.name)}<small>${esc(s.family)} · ${s.open_positions} mövqe · ${s.trades} əməliyyat</small></span>
      <span class="nav-ret ${cls(s.return_pct)}">${pct(s.return_pct)}</span>
    </a>`).join('');
  document.querySelector('.nav-overview').classList.toggle('active', !state.view);
  const sel = $('nav-select');
  sel.innerHTML = `<option value="">★ Ümumi baxış (müqayisə)</option>` + list.map((s) =>
    `<option value="${esc(s.key)}">${s.order}. ${esc(s.name)} — ${pct(s.return_pct)}</option>`).join('');
  sel.value = state.view;
}

/* ---------- charts ---------- */
function lineChart(id, datasets, money$ = true) {
  if (typeof Chart === 'undefined') return;
  const xs = datasets.flatMap((d) => d.data.map((p) => p.x));
  const [xmin, xmax] = xs.length ? [Math.min(...xs), Math.max(...xs)] : [undefined, undefined];
  const old = state.charts[id];
  if (old) {
    old.data.datasets = datasets;
    Object.assign(old.options.scales.x, { min: xmin, max: xmax });
    old.update('none');
    return;
  }
  state.charts[id] = new Chart($(id), {
    type: 'line',
    data: { datasets },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
      interaction: { mode: 'nearest', intersect: false },
      elements: { point: { radius: 0, hitRadius: 6 }, line: { borderWidth: 1.6, tension: 0 } },
      scales: {
        x: { type: 'linear', min: xmin, max: xmax, ticks: { color: '#848e9c', maxTicksLimit: window.innerWidth < 760 ? 4 : 8, maxRotation: 0, callback: (v) => when(v) }, grid: { color: '#222830' } },
        y: { ticks: { color: '#848e9c', callback: (v) => money$ ? '$' + Number(v).toLocaleString('en-US') : v }, grid: { color: '#222830' } },
      },
      plugins: {
        legend: { position: 'bottom', labels: { color: '#c9ced6', boxWidth: 10, boxHeight: 2, font: { size: 11 } } },
        tooltip: { callbacks: { title: (items) => when(items[0].parsed.x), label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } },
      },
    },
  });
}
const series = (pts) => (pts || []).map(([x, y]) => ({ x, y }));

/* ---------- overview ---------- */
const COLUMNS = [
  ['rank', '#', (s) => s.rank],
  ['name', 'Strategiya', (s) => `<b>${esc(s.name)}</b><br><span class="muted">${esc(s.family)}</span>`, 'l'],
  ['equity', 'Balans', (s) => money(s.equity)],
  ['return_pct', 'Gəlir', (s) => `<span class="${cls(s.return_pct)}">${pct(s.return_pct)}</span>`],
  ['realized', 'Realizə PnL', (s) => `<span class="${cls(s.realized)}">${money(s.realized, true)}</span>`],
  ['unrealized', 'Açıq PnL', (s) => `<span class="${cls(s.unrealized)}">${money(s.unrealized, true)}</span>`],
  ['trades', 'Əməliyyat', (s) => s.trades],
  ['win_rate', 'Uduş %', (s) => isNum(s.win_rate) ? s.win_rate.toFixed(1) + '%' : '—'],
  ['profit_factor', 'Mənfəət faktoru', (s) => pf(s.profit_factor)],
  ['max_dd_pct', 'Maks. geriləmə', (s) => `<span class="neg">${isNum(s.max_dd_pct) && s.max_dd_pct > 0 ? '-' + s.max_dd_pct.toFixed(2) + '%' : '0.00%'}</span>`],
  ['open_positions', 'Açıq mövqe', (s) => `${s.open_positions}/${state.status?.max_positions ?? ''}`],
  ['exposure_pct', 'Bazarda', (s) => pct(s.exposure_pct, false)],
];
const sortValue = (s, col) => col === 'profit_factor' ? (s[col] === 'inf' ? 1e9 : s[col] ?? -1) : col === 'name' ? s.name : (s[col] ?? -1e9);

function renderOverview(ov) {
  const rows = ov.strategies;
  const best = rows[0], worst = rows[rows.length - 1];
  const avg = rows.reduce((a, s) => a + s.return_pct, 0) / rows.length;
  const total = rows.reduce((a, s) => a + s.equity, 0);
  const winners = rows.filter((s) => s.return_pct > 0).length;
  const b = ov.benchmark;
  const beat = b ? rows.filter((s) => s.return_pct > b.return_pct).length : null;
  $('ov-kpis').innerHTML = [
    ['Ən yaxşı strategiya', best.name, `<span class="${cls(best.return_pct)}">${pct(best.return_pct)}</span> · ${money(best.equity)}`],
    ['Ən zəif strategiya', worst.name, `<span class="${cls(worst.return_pct)}">${pct(worst.return_pct)}</span> · ${money(worst.equity)}`],
    ['Orta gəlir (20 hesab)', `<span class="${cls(avg)}">${pct(avg)}</span>`, `cəmi ${money(total)} / ${money(rows.length * ov.status.capital)}`],
    ['Mənfəətdə olan', `${winners} / ${rows.length}`, 'strategiya'],
    ['Benchmark', b ? `<span class="${cls(b.return_pct)}">${pct(b.return_pct)}</span>` : '—', b ? `${esc(b.symbol)} al və saxla · ${beat} strategiya qabaqdadır` : 'ilk şamdan sonra'],
    ['Cəmi əməliyyat', rows.reduce((a, s) => a + s.trades, 0), `${rows.reduce((a, s) => a + s.open_positions, 0)} açıq mövqe`],
  ].map(([t, v, sub]) => `<div class="kpi"><small>${t}</small><strong>${v.toString().startsWith('<') ? v : esc(v)}</strong><span>${sub}</span></div>`).join('');

  const { col, dir } = state.sort;
  const sorted = [...rows].sort((a, b2) => {
    const x = sortValue(a, col), y = sortValue(b2, col);
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
  });
  const head = '<thead><tr>' + COLUMNS.map(([k, label, , align]) =>
    `<th class="sortable${align ? ' ' + align : ''}${k === col ? ' sorted' : ''}" data-col="${k}">${label}${k === col ? (dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('') + '</tr></thead>';
  const body = sorted.map((s) => `<tr class="link" data-key="${esc(s.key)}">` +
    COLUMNS.map(([, , f, align]) => `<td${align ? ` class="${align}"` : ''}>${f(s)}</td>`).join('') + '</tr>').join('');
  const benchRow = b ? `<tr class="bench"><td>—</td><td class="l">Benchmark: ${esc(b.symbol)} al və saxla</td><td>${money(b.equity)}</td><td>${pct(b.return_pct)}</td>${'<td></td>'.repeat(COLUMNS.length - 4)}</tr>` : '';
  $('ov-table').innerHTML = head + '<tbody>' + body + benchRow + '</tbody>';

  $('ov-events').innerHTML = '<thead><tr><th>Vaxt</th><th class="l">Strategiya</th><th class="l">Əməliyyat</th><th class="l">Coin</th><th>Qiymət</th><th>Nəticə</th><th>%</th></tr></thead><tbody>' +
    (ov.events.length ? ov.events.map((e) => `<tr class="link" data-key="${esc(e.key)}"><td>${when(e.ts)}</td><td class="l">${esc(e.name)}</td>
      <td class="l"><span class="side ${e.side === 'BUY' ? 'buy' : 'sell'}">${e.side === 'BUY' ? 'ALIŞ' : 'SATIŞ'}</span></td><td class="l">${esc(e.symbol)}</td>
      <td>${price(e.price)}</td><td class="${cls(e.pnl)}">${isNum(e.pnl) ? money(e.pnl, true) : ''}</td><td class="${cls(e.ret)}">${isNum(e.ret) ? pct(e.ret * 100) : ''}</td></tr>`).join('')
      : '<tr><td colspan="7" class="empty">Hələ əməliyyat yoxdur — strategiyalar yeni siqnal gözləyir.</td></tr>') + '</tbody>';
}

function renderCurves(curves) {
  const list = ordered();
  const datasets = list.map((s, i) => ({ label: s.name, data: series(curves[s.key]), borderColor: color(i), backgroundColor: color(i) }));
  if (curves._benchmark && curves._benchmark.length) {
    datasets.push({ label: 'Benchmark (al və saxla)', data: series(curves._benchmark), borderColor: '#9aa3ae', backgroundColor: '#9aa3ae', borderDash: [6, 4], borderWidth: 2 });
  }
  lineChart('ov-chart', datasets);
}

/* ---------- strategy section ---------- */
function renderDetail(d) {
  const s = d.summary;
  $('s-name').textContent = `${s.order}. ${s.name}`;
  $('s-family').textContent = s.family;
  $('s-rank').textContent = `· reytinq #${s.rank} / 20 · başlanğıc ${when(s.started_at)}`;
  $('s-rules').textContent = d.rules;
  document.title = `${s.name} — 20 Strategiya`;
  const kpis = [
    ['Balans', money(s.equity), `<span class="${cls(s.return_pct)}">${pct(s.return_pct)}</span> (başlanğıc ${money(s.capital)})`],
    ['Realizə olunmuş PnL', `<span class="${cls(s.realized)}">${money(s.realized, true)}</span>`, 'bağlanmış əməliyyatlar'],
    ['Açıq PnL', `<span class="${cls(s.unrealized)}">${money(s.unrealized, true)}</span>`, `${s.open_positions} açıq mövqe`],
    ['Nağd USDT', money(s.cash), `bazarda ${pct(s.exposure_pct, false)}`],
    ['Əməliyyat sayı', s.trades, `${s.wins} uduş · ${s.trades - s.wins} itki`],
    ['Uduş faizi', isNum(s.win_rate) ? s.win_rate.toFixed(1) + '%' : '—', 'bağlanmış əməliyyatlar'],
    ['Mənfəət faktoru', pf(s.profit_factor), 'ümumi qazanc / ümumi itki'],
    ['Maks. geriləmə', s.max_dd_pct > 0 ? `<span class="neg">-${s.max_dd_pct.toFixed(2)}%</span>` : '0.00%', 'pik balansdan düşüş'],
    ['Orta əməliyyat', `<span class="${cls(s.avg_trade_pct)}">${pct(s.avg_trade_pct)}</span>`, `ən yaxşı ${pct(s.best_trade_pct)} · ən pis ${pct(s.worst_trade_pct)}`],
    ['Orta saxlama', isNum(s.avg_hold_hours) ? duration(s.avg_hold_hours * 3600000) : '—', 'mövqe müddəti'],
    ['Ödənilmiş komissiya', money(s.fees), 'bağlanmış əməliyyatlar üzrə'],
  ];
  $('s-kpis').innerHTML = kpis.map(([t, v, sub]) => `<div class="kpi"><small>${t}</small><strong>${v}</strong><span>${sub}</span></div>`).join('');

  const ds = [{ label: s.name, data: series(d.equity), borderColor: '#f0b90b', backgroundColor: '#f0b90b', borderWidth: 2 }];
  if (d.benchmark.length) ds.push({ label: `Benchmark: ${d.benchmark_symbol} al və saxla`, data: series(d.benchmark), borderColor: '#9aa3ae', backgroundColor: '#9aa3ae', borderDash: [6, 4] });
  lineChart('s-chart', ds);
  $('s-bench-note').textContent = d.benchmark_symbol ? `müqayisə: ${d.benchmark_symbol}` : '';

  const sig = d.signals || {};
  const held = new Set(d.positions.map((p) => p.symbol));
  const chips = (arr, kind) => arr && arr.length ? `<div class="chips">${arr.map((x) => `<span class="chip ${kind}${kind === '' && held.has(x) ? ' held' : ''}">${esc(x)}</span>`).join('')}</div>` : '<div class="muted">—</div>';
  $('s-sig-candle').textContent = sig.candle ? `şam: ${when(sig.candle)}` : 'hələ şam işlənməyib';
  $('s-signals').innerHTML = `
    <h3>Alındı (yeni siqnal)</h3>${chips(sig.entries, 'buy')}
    <h3>Satıldı (çıxış siqnalı)</h3>${chips(sig.exits, 'sell')}
    <h3>Boş slot / nağd çatmadığı üçün buraxıldı</h3>${chips(sig.skipped, 'skip')}
    <h3>Mövqe rejimi aktiv olan coinlər (${(sig.active || []).length}${sig.evaluated ? ' / ' + sig.evaluated : ''}) — sarı çərçivə: hesabda var</h3>${chips(sig.active, '')}`;

  $('s-pos-note').textContent = `${d.positions.length} / ${d.status.max_positions} slot`;
  $('s-positions').innerHTML = '<thead><tr><th>Coin</th><th>Açılış</th><th>Giriş qiyməti</th><th>Cari qiymət</th><th>Miqdar</th><th>Qoyulan</th><th>Dəyər (satış)</th><th>PnL</th><th>%</th></tr></thead><tbody>' +
    (d.positions.length ? d.positions.map((p) => `<tr><td><b>${esc(p.symbol)}</b></td><td>${when(p.opened_at)}</td><td>${price(p.entry_price)}</td><td>${price(p.price)}</td>
      <td>${num(p.qty, p.qty >= 100 ? 2 : 6)}</td><td>${money(p.cost)}</td><td>${money(p.value)}</td><td class="${cls(p.pnl)}">${money(p.pnl, true)}</td><td class="${cls(p.ret_pct)}">${pct(p.ret_pct)}</td></tr>`).join('')
      : '<tr><td colspan="9" class="empty">Açıq mövqe yoxdur</td></tr>') + '</tbody>';

  $('s-trades-note').textContent = `son ${d.trades.length} əməliyyat`;
  $('s-trades').innerHTML = '<thead><tr><th>Coin</th><th>Açılış</th><th>Bağlanış</th><th>Müddət</th><th>Giriş</th><th>Çıxış</th><th>Qoyulan</th><th>Nəticə</th><th>%</th><th>Komissiya</th></tr></thead><tbody>' +
    (d.trades.length ? d.trades.map((t) => `<tr><td><b>${esc(t.symbol)}</b></td><td>${when(t.opened_at)}</td><td>${when(t.closed_at)}</td><td>${duration(t.closed_at - t.opened_at)}</td>
      <td>${price(t.entry_price)}</td><td>${price(t.exit_price)}</td><td>${money(t.cost)}</td><td class="${cls(t.pnl)}">${money(t.pnl, true)}</td><td class="${cls(t.ret)}">${pct(t.ret * 100)}</td><td>${money(t.fees)}</td></tr>`).join('')
      : '<tr><td colspan="10" class="empty">Hələ bağlanmış əməliyyat yoxdur</td></tr>') + '</tbody>';
}

/* ---------- refresh loop ---------- */
let busy = false;
async function refresh(force = false) {
  if (busy) return;
  busy = true;
  try {
    const view = state.view;
    const [ov, detail] = await Promise.all([
      getJSON('/api/overview'),
      view ? getJSON('/api/strategy/' + encodeURIComponent(view)) : Promise.resolve(null),
    ]);
    if (view !== state.view) return;  // user switched section while loading
    state.overview = ov;
    renderStatus(ov.status);
    renderNav();
    if (view) {
      renderDetail(detail);
    } else {
      renderOverview(ov);
      if (force || Date.now() - state.curvesAt > 30000 || !state.curves) {
        state.curves = await getJSON('/api/curves');
        state.curvesAt = Date.now();
      }
      renderCurves(state.curves);
    }
  } catch (err) {
    $('notice').textContent = 'Serverlə əlaqə yoxdur: ' + err.message;
  } finally {
    busy = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  window.addEventListener('hashchange', route);
  $('prev').addEventListener('click', () => step(-1));
  $('next').addEventListener('click', () => step(1));
  $('nav-select').addEventListener('change', (e) => { location.hash = e.target.value ? '#/s/' + e.target.value : '#/'; });
  document.addEventListener('keydown', (e) => {
    if (!state.view || e.target.tagName === 'SELECT' || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === 'ArrowLeft') step(-1);
    if (e.key === 'ArrowRight') step(1);
  });
  const go = (e) => { const tr = e.target.closest('tr[data-key]'); if (tr) location.hash = '#/s/' + tr.dataset.key; };
  $('ov-table').addEventListener('click', (e) => {
    const th = e.target.closest('th[data-col]');
    if (th) {
      const col = th.dataset.col;
      state.sort = { col, dir: state.sort.col === col ? -state.sort.dir : (col === 'rank' || col === 'name' ? 1 : -1) };
      renderOverview(state.overview);
      return;
    }
    go(e);
  });
  $('ov-events').addEventListener('click', go);
  route();
  setInterval(() => refresh(false), REFRESH_MS);
  setInterval(tickClock, 1000);
});
