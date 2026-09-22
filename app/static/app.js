'use strict';
const $ = id => document.getElementById(id);
let selected = null, state = null, lastHistory = null;
const fmt = (x, digits=2) => x == null ? '—' : Number(x).toLocaleString('en-US', {maximumFractionDigits:digits});
const clock = x => x ? new Date(x).toLocaleString('az-AZ',{timeZone:'Asia/Baku',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
function node(tag, text, cls){const n=document.createElement(tag); if(text!=null)n.textContent=text; if(cls)n.className=cls; return n;}
function badge(value){return node('span',value,'badge '+(value==='WAIT'?'wait':value==='SHORT'?'short':''));}
async function api(url, options){try{const r=await fetch(url, options); if(!r.ok){const j=await r.json().catch(()=>({})); throw Error(j.detail||`HTTP ${r.status}`);} const data=await r.json();if(url==='/api/status')execution(data.execution);return data;}catch(e){if(url==='/api/status')execution(null);throw e;}}
function pnl(id, value){$(id).textContent=fmt(value);$(id).className=value==null?'':value>=0?'pass':'fail';}
function execution(data){
  const connected=data?.connected===true;
  $('execution-status').textContent=connected?`PAPER TRADING · ${data.state} · ${data.paused?'Yeni girişlər dayandırılıb':'Yeni girişlər aktivdir'} · ${clock(data.observed_at)} Bakı`:data?.error||'Freqtrade bağlantısı yoxdur. Son rəqəmlər gizlədildi.';
  $('pause-entries').disabled=!data?.bridge_configured;
  $('pause-entries').textContent=data?.paused?'Girişləri davam etdir':'Girişləri dayandır';
  $('wallet').textContent=connected?fmt(data.wallet):'—';
  $('free-margin').textContent=connected?`${fmt(data.free)} / ${fmt(data.used)}`:'—';
  pnl('realized-pnl',connected?data.realized_pnl:null);pnl('total-pnl',connected?data.total_pnl:null);
  $('positions').replaceChildren();$('trades').replaceChildren();
  $('trade-summary').textContent=connected?`${fmt(data.wins,0)} qazanc / ${fmt(data.losses,0)} zərər`:'—';
  for(const t of connected?data.positions||[]:[]){
    const tr=node('tr'),plan=(t.enter_tag||'').split(':');
    [t.pair,t.is_short?'SHORT':'LONG',fmt(t.open_rate,5),fmt(t.current_rate,5),fmt(t.stake_amount),fmt(t.profit_abs),`${fmt(t.stop_loss_abs,5)} / ${plan.length===4?fmt(Number(plan[3]),5):'—'}`,fmt(t.funding_fees,4)].forEach((v,i)=>tr.append(node('td',v,i===5?(t.profit_abs>=0?'pass':'fail'):'')));
    $('positions').append(tr);
  }
  if(!$('positions').children.length){const tr=node('tr'),td=node('td',connected?'Açıq mövqe yoxdur.':'Freqtrade qoşulmayıb.');td.colSpan=8;tr.append(td);$('positions').append(tr);}
  for(const t of connected?data.trades||[]:[]){
    if(t.is_open)continue;
    const tr=node('tr');[t.pair,t.is_short?'SHORT':'LONG',fmt(t.open_rate,5),fmt(t.close_rate,5),fmt(t.close_profit_abs),fmt(t.funding_fees,4),t.exit_reason||'—'].forEach((v,i)=>tr.append(node('td',v,i===4?(t.close_profit_abs>=0?'pass':'fail'):'')));$('trades').append(tr);
  }
}
function detail(row){
  $('symbol').textContent=row.symbol; $('price').textContent=fmt(row.mark,5); $('decision').replaceWith(Object.assign(badge(row.decision),{id:'decision'}));
  $('source').textContent=row.source||'Məlumat yoxdur'; $('indicators').replaceChildren(); $('market-meta').replaceChildren(); $('levels').replaceChildren(); $('ai').replaceChildren(); $('reasons').replaceChildren(); $('rules').replaceChildren(); $('chart').replaceChildren();
  $('score').textContent=row.score==null?'—':`Diaqnostik bal: ${row.score}/100`;
  for(const reason of [row.error,row.ai_error,row.stale?'Məlumat köhnədir. Yeni skanı gözləyin.':null,...(row.reasons||[])].filter(Boolean)) $('reasons').append(node('p',reason));
  if(!row.error && row.decision!=='WAIT')$('reasons').append(node('p','Jev bu istiqaməti seçdi; risk yoxlamaları keçildi.'));
  for(const [tf,f] of Object.entries(row.frames||{})){
    const tr=node('tr'); [tf,f.trend,fmt(f.rsi),fmt(f.macd_hist,5),fmt(f.atr_pct),fmt(f.support,5),fmt(f.resistance,5)].forEach(v=>tr.append(node('td',v))); $('indicators').append(tr);
  }
  if(row.spread_bps!=null)[`Spread ${fmt(row.spread_bps)} bps`,`Funding ${fmt(row.funding_rate*100,4)}%`,`Məlumat vaxtı ${clock(row.observed_at)} · Bakı`].forEach(v=>$('market-meta').append(node('span',v)));
  if(row.levels){const l=row.levels; [`Giriş ${fmt(l.entry,5)}`,`SL ${fmt(l.stop,5)}`,`TP ${fmt(l.target,5)}`,`Xalis R:R ${fmt(l.net_rr)}`,l.note].forEach(v=>$('levels').append(node('span',v)));}
  const names={direction:'İstiqamət',momentum:'Momentum',regime:'Bazar rejimi',risk:'Risk',driver:'Əsas kontekst'};
  if(!row.ai)$('ai').append(node('p',row.ai_error||'Jev qiymətləndirməsi yoxdur.','empty'));
  for(const [key,a] of Object.entries(row.ai?.answers||{})){
    const card=node('div',null,'ai-card'); card.append(node('small',names[key]||key),node('strong',a.choice)); const bar=node('progress');bar.max=1;bar.value=a.confidence;bar.setAttribute('aria-label',`${names[key]} confidence`);card.append(bar,node('small',`Confidence: ${fmt(a.confidence*100)}%`),node('br'),node('small',Object.entries(a.probabilities).map(([k,p])=>`${k}: ${fmt(p*100,1)}%`).join(' · ')));$('ai').append(card);
  }
  for(const r of [...(row.rules||[]),...(row.guards||[])]){const n=node('div',null,'rule');n.append(node('span',r.label),node('span',r.maximum!=null?`${r.points}/${r.maximum}`:r.passed?'Keçdi':'Keçmədi',r.passed?'pass':'fail'));$('rules').append(n);}
  const bars=row.candles||[]; if(bars.length){const ns='http://www.w3.org/2000/svg',values=bars.map(b=>b.close),lo=Math.min(...values),hi=Math.max(...values),range=hi-lo||1;for(let i=0;i<4;i++){const y=20+i*55,line=document.createElementNS(ns,'line');for(const [k,v]of Object.entries({x1:0,x2:720,y1:y,y2:y}))line.setAttribute(k,v);const label=document.createElementNS(ns,'text');label.setAttribute('x','726');label.setAttribute('y',y+4);label.textContent=fmt(hi-range*i/3,4);$('chart').append(line,label);}const line=document.createElementNS(ns,'polyline');line.setAttribute('points',values.map((v,i)=>`${i*710/(values.length-1)},${20+(hi-v)/range*165}`).join(' '));$('chart').append(line);}
}
async function history(){try{const rows=await api('/api/history');$('history').replaceChildren();for(const r of rows.slice(0,30)){const tr=node('tr');[new Date(r.observed_at).toLocaleString('az-AZ',{timeZone:'Asia/Baku'}),r.symbol,r.decision,r.score==null?'—':`${r.score}/100`,r.ai?.model||'—'].forEach(v=>tr.append(node('td',v)));$('history').append(tr);}}catch(e){$('notice').textContent=e.message;}}
async function refresh(){try{state=await api('/api/status');$('notice').textContent=state.demo?'DEMO REJİMİ — qiymətlər sintetikdir, Jev sorğuları göndərilmir.':!state.ai_configured?'Jev API açarı əlavə edilməyib. Texniki analiz aktivdir; yekun qərarlar WAIT qalır.':'';if(state.storage_error)$('notice').textContent+=' Tarixçəni diskə yazmaq mümkün olmadı.';$('mode').textContent=state.demo?'DEMO':state.ai_configured?'JEV AKTİV':'TEXNİKİ ANALİZ';$('count').textContent=state.rows.length;$('signals').textContent=state.rows.filter(r=>r.decision!=='WAIT').length;$('threshold').textContent=`${fmt(state.min_confidence*100)}%`;$('updated').textContent=clock(state.last_scan);$('scan-state').textContent=state.scanning?'Skan davam edir…':'Bakı vaxtı · növbəti '+clock(state.next_scan);$('model').textContent=state.model;$('scan').disabled=state.scanning;$('markets').replaceChildren();if(!state.rows.length)$('markets').append(node('p','İlk skan hazırlanır…','empty'));for(const row of state.rows){if(!selected)selected=row.symbol;const b=node('button',null,'market '+(selected===row.symbol?'active':''));const a=node('span',row.symbol);a.append(node('small',row.error?'Məlumat xətası':row.stale?'Köhnəlmiş analiz':`${fmt(row.mark,4)} · ${row.score}/100`));b.append(a,badge(row.decision));b.onclick=()=>{selected=row.symbol;detail(row);document.querySelectorAll('.market').forEach(n=>n.classList.remove('active'));b.classList.add('active');};$('markets').append(b);}const row=state.rows.find(r=>r.symbol===selected);if(row)detail(row);if(lastHistory!==state.last_scan){lastHistory=state.last_scan;await history();}}catch(e){$('notice').textContent='Bağlantı xətası. Göstərilən məlumatı cari siqnal kimi istifadə etməyin.';$('mode').textContent='OFFLINE';$('decision').textContent='WAIT';$('levels').replaceChildren();$('scan').disabled=false;}}
$('scan').onclick=async()=>{try{$('scan').disabled=true;await api('/api/scan',{method:'POST',headers:{'X-Crypto-Jev':'1'}});await refresh();}catch(e){$('notice').textContent=e.message;$('scan').disabled=false;}};
$('history-refresh').onclick=history;
$('pause-entries').onclick=async()=>{try{await api('/api/execution/'+(state?.execution?.paused?'resume':'pause'),{method:'POST',headers:{'X-Crypto-Jev':'1'}});await refresh();}catch(e){$('execution-status').textContent=e.message;}};
(async function poll(){await refresh();setTimeout(poll,5000);})();
history();
