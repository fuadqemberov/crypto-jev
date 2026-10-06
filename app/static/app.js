'use strict';
const $=id=>document.getElementById(id);
let selected='BTCUSDT',state=null,interval='15m',marketFilter='all',lastHistory=null,view=null,viewKey='',viewAt=0,viewBusy=false;
const fmt=(x,d=2)=>x==null||!Number.isFinite(Number(x))?'—':Number(x).toLocaleString('en-US',{maximumFractionDigits:d});
const clock=x=>x?new Date(x).toLocaleTimeString('az-AZ',{timeZone:'Asia/Baku',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
function node(tag,text,cls){const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;}
function badge(value){return node('span',value,'badge '+(value==='WAIT'||value==='KÖHNƏ'?'wait':value==='SHORT'?'short':''));}
async function api(url,options){const r=await fetch(url,options);if(!r.ok){const d=await r.json().catch(()=>({}));throw Error(d.detail||`HTTP ${r.status}`);}return r.json();}
function pnl(id,x){$(id).textContent=fmt(x);$(id).className=x==null?'':x>=0?'pass':'fail';}
function emptyTable(id,cols,text){const tr=node('tr'),td=node('td',null,'empty-cell');td.colSpan=cols;td.append(node('span','▤','empty-icon'),node('span',text));tr.append(td);$(id).append(tr);}
function execution(data){
 const connected=data?.connected===true;
 $('execution-status').textContent=connected?`${data.paused?'Yeni girişlər dayandırılıb':'Avtomatik girişlər aktivdir'} · ${clock(data.observed_at)} Bakı`:data?.error||'Freqtrade bağlantısı yoxdur.';
 $('pause-entries').disabled=!data?.bridge_configured;
 $('pause-entries').textContent=data?.paused?'Avtomatik girişləri başlat':'Yeni girişləri dayandır';
 $('wallet').textContent=connected?fmt(data.wallet):'—';
 // Wallet changes only when a trade closes; equity adds the open positions' unrealized PnL.
 const openPnl=connected?(data.positions||[]).reduce((sum,t)=>sum+(Number(t.profit_abs)||0),0):null;
 $('equity').textContent=connected&&data.wallet!=null?fmt(Number(data.wallet)+openPnl):'—';
 $('equity').className=openPnl==null?'':openPnl>=0?'pass':'fail';$('free-margin').textContent=connected?`${fmt(data.free)} / ${fmt(data.used)}`:'—';
 pnl('realized-pnl',connected?data.realized_pnl:null);pnl('total-pnl',connected?data.total_pnl:null);
 $('positions').replaceChildren();$('trades').replaceChildren();
 const positions=connected?data.positions||[]:[];$('position-count').textContent=positions.length;
 $('trade-summary').textContent=connected?`${data.wins||0} qazanc / ${data.losses||0} zərər`:'Qoşulmayıb';
 for(const t of positions){const tr=node('tr'),plan=(t.enter_tag||'').split(':');[t.pair,t.is_short?'SHORT':'LONG',fmt(t.leverage,0)+'×',fmt(t.open_rate,6),fmt(t.current_rate,6),fmt(t.stake_amount),fmt(t.profit_abs),`${fmt(t.stop_loss_abs,6)} / ${plan.length>=4?fmt(Number(plan[3]),6):'—'}`,fmt(t.funding_fees,4)].forEach((v,i)=>tr.append(node('td',v,i===6?(t.profit_abs>=0?'pass':'fail'):i===1?(t.is_short?'fail':'pass'):'')));$('positions').append(tr);}
 if(!positions.length)emptyTable('positions',9,connected?'Açıq mövqe yoxdur · uyğun Strategiya siqnalı gözlənilir':'Freqtrade bağlantısı gözlənilir');
 for(const t of connected?data.trades||[]:[]){if(t.is_open)continue;const tr=node('tr');[t.pair,t.is_short?'SHORT':'LONG',fmt(t.open_rate,6),fmt(t.close_rate,6),fmt(t.close_profit_abs),fmt(t.funding_fees,4),t.exit_reason||'—'].forEach((v,i)=>tr.append(node('td',v,i===4?(t.close_profit_abs>=0?'pass':'fail'):'')));$('trades').append(tr);}
 if(!$('trades').children.length)emptyTable('trades',7,'Bağlanmış əməliyyat yoxdur');
}
function direction(row){return row.candidate||row.decision||'—';}
function executionReason(row){const labels={risk_error:'Risk ölçüləndirməsi mümkün deyil',ttl:'Siqnalın vaxtı keçib',telemetry:'İcra məlumatı yoxdur',heartbeat:'İcra heartbeat-i yoxdur',pause:'Yeni girişlər dayandırılıb',daily_loss:'Gündəlik zərər limiti',storage_error:'Disk xətası',schema:'Siqnal formatı etibarsızdır',cooldown:'Təkrar giriş fasiləsi',funding:'Funding yoxlaması keçilmədi',levels:'Giriş / SL / TP planı yoxdur',discovery:'Bazar siyahısı yenilənmədi',worker_error:'Skan xətası',demo:'Demo rejimi',analysis_error:'Analiz xətası'};return (row.execution_blocks||[]).map(c=>labels[c]||c).join(' · ');}
function reason(row){if(row.error)return row.error;if(!row.observed_at)return 'Analiz növbəsində';if(row.stale)return `Yenilənmə növbəsində · ${Math.floor((row.analysis_age_seconds??(Date.now()-row.observed_at)/1000)/60)} dəq əvvəl`;return executionReason(row)||row.reasons?.[0]||'Bridge girişə buraxıb; Freqtrade yekun yoxlamanı edir';}
function allRows(){const rows=new Map((state?.rows||[]).map(r=>[r.symbol,r]));for(const s of state?.market_symbols||[])if(!rows.has(s))rows.set(s,{symbol:s,decision:'WAIT'});return [...rows.values()];}
function selectedRow(){return allRows().find(r=>r.symbol===selected)||{symbol:selected,decision:'WAIT'};}
function renderMarkets(){
 const query=$('market-search').value.trim().toUpperCase();let rows=allRows().filter(r=>r.symbol.includes(query));
 if(marketFilter==='ready')rows=rows.filter(r=>r.execution_ready===true);
 if(marketFilter==='direction')rows=rows.filter(r=>['LONG','SHORT'].includes(direction(r)));
 rows.sort((a,b)=>a.symbol.localeCompare(b.symbol));const scroll=$('markets').scrollTop;$('markets').replaceChildren();
 for(const row of rows){const b=node('button',null,'market'+(row.symbol===selected?' active':''));b.setAttribute('aria-label',row.symbol);const left=node('span',row.symbol);left.append(node('small',`Namizəd ${direction(row)}`,direction(row)==='LONG'?'pass':direction(row)==='SHORT'?'fail':''));const right=node('span',fmt(row.mark,6),'market-right');const status=row.stale?'Yenilənir':!row.observed_at?'Növbədə':row.execution_action||'WAIT';right.append(node('small',status,status==='LONG'?'pass':status==='SHORT'?'fail':''));b.title=reason(row);b.append(left,right);b.onclick=()=>selectMarket(row.symbol);$('markets').append(b);}
 if(!rows.length)$('markets').append(node('p','Uyğun bazar tapılmadı.','empty'));$('markets').scrollTop=scroll;$('visible-markets').textContent=`${rows.length} bazar`;
}
function selectMarket(symbol){selected=symbol;view=null;viewKey='';viewAt=0;renderMarkets();detail(selectedRow());clearBook('Yeni bazar yüklənir');drawChart([]);pollView();}
function detail(row){
 $('symbol').textContent=row.symbol;$('rule-direction').textContent=direction(row);$('rule-direction').className=direction(row)==='LONG'?'pass':direction(row)==='SHORT'?'fail':'';
 $('decision').replaceWith(Object.assign(badge(row.stale?'KÖHNƏ':row.execution_action||'WAIT'),{id:'decision'}));
 $('decision-large').textContent=!row.observed_at?'Növbədə':row.stale?'KÖHNƏ':row.execution_action||'WAIT';$('decision-large').className=row.execution_action==='LONG'?'pass':row.execution_action==='SHORT'?'fail':'muted';$('decision-description').textContent=reason(row);
 $('indicators').replaceChildren();$('setup').replaceChildren();$('reasons').replaceChildren();$('rules').replaceChildren();$('levels').replaceChildren();
 for(const message of [...new Set([row.error,row.stale?'Analiz köhnədir; yeni giriş üçün təzə yoxlama gözlənilir.':null,executionReason(row),...(row.reasons||[])].filter(Boolean))])$('reasons').append(node('p',message));
 if(!$('reasons').children.length)$('reasons').append(node('p',row.observed_at?'Risk filtrləri keçilib. İcra cari qiyməti, balansı və təkrar girişləri ayrıca yoxlayır.':'Bu coin analiz növbəsindədir.'));
 for(const [tf,f] of Object.entries(row.frames||{})){const tr=node('tr');[tf,f.trend,fmt(f.rsi),fmt(f.macd_hist,6),fmt(f.atr_pct),fmt(f.support,6),fmt(f.resistance,6)].forEach(v=>tr.append(node('td',v)));$('indicators').append(tr);}
 if(!$('indicators').children.length)emptyTable('indicators',7,'Bu coin üçün analiz hələ tamamlanmayıb');
 if(row.levels){const l=row.levels;[`Giriş ${fmt(l.entry,6)}`,`SL ${fmt(l.stop,6)}`,`TP ${fmt(l.target,6)}`,`Xalis R:R ${fmt(l.net_rr)}`,`Risk leverage ${row.planned_leverage?row.planned_leverage+'×':'—'}`].forEach(v=>$('levels').append(node('span',v)));}
 $('setup').append(node('p',row.strategy||'Analiz gözlənilir'),node('p',`Mövqe qərarı: ${row.exit_action||'HOLD'}`));
 for(const r of (row.guards||[])){const div=node('div',null,'rule');div.append(node('span',r.label),node('span',r.passed?'Keçdi':'Keçmədi',r.passed?'pass':'fail'));$('rules').append(div);}
 if(!view||view.symbol!==selected){$('price').textContent=fmt(row.mark,6);$('mark-price').textContent=fmt(row.mark,6);$('funding').textContent=row.funding_rate==null?'—':`${fmt(row.funding_rate*100,4)}%`;if(row.candles?.length&&interval==='15m'){drawChart(row.candles);$('quote-state').textContent='Son analiz qiyməti';$('source').textContent=row.source||'—';}}
}
function svg(tag,attrs,text){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);if(text!=null)e.textContent=text;return e;}
function drawChart(bars){
 const el=$('chart');el.replaceChildren();$('chart-empty').hidden=bars.length>0;if(!bars.length)return;
 const w=820,top=18,bottom=282,volTop=310,volBottom=365;let lo=Math.min(...bars.map(b=>Number(b.low))),hi=Math.max(...bars.map(b=>Number(b.high)));const padding=(hi-lo)*.08||hi*.002||1;lo-=padding;hi+=padding;const y=p=>bottom-(p-lo)/(hi-lo)*(bottom-top),step=w/bars.length,body=Math.max(2,step*.62),maxVol=Math.max(...bars.map(b=>Number(b.volume)),1);
 for(let i=0;i<6;i++){const v=hi-(hi-lo)*i/5,py=y(v);el.append(svg('line',{x1:0,x2:w,y1:py,y2:py,class:'grid'}),svg('text',{x:w+8,y:py+4,class:'axis'},fmt(v,6)));}
 for(let i=0;i<bars.length;i++){const b=bars[i],x=i*step+step/2,up=b.close>=b.open,cls=up?'candle-up':'candle-down',py=Math.min(y(b.open),y(b.close)),height=Math.max(1,Math.abs(y(b.open)-y(b.close)));const wick=svg('line',{x1:x,x2:x,y1:y(b.high),y2:y(b.low),class:cls,'stroke-width':1});const rect=svg('rect',{x:x-body/2,y:py,width:body,height,class:cls,'stroke-width':0});rect.append(svg('title',{},`O ${b.open} H ${b.high} L ${b.low} C ${b.close}`));el.append(wick,rect,svg('rect',{x:x-body/2,y:volBottom-b.volume/maxVol*(volBottom-volTop),width:body,height:Math.max(1,b.volume/maxVol*(volBottom-volTop)),class:cls,opacity:.4,'stroke-width':0}));if(i%20===0){el.append(svg('line',{x1:x,x2:x,y1:0,y2:volBottom,class:'grid'}),svg('text',{x,y:384,class:'axis','text-anchor':'middle'},clock(b.time||b.open_time||b.close_time)));}}
 const last=bars[bars.length-1],color=last.close>=last.open?'#0ecb81':'#f6465d',ly=y(last.close);el.append(svg('line',{x1:0,x2:w,y1:ly,y2:ly,stroke:color,'stroke-dasharray':'3 3',opacity:.7}),svg('rect',{x:w,y:ly-9,width:80,height:19,fill:color}),svg('text',{x:w+5,y:ly+4,fill:'#0b0e11','font-size':10},fmt(last.close,6)));
 $('chart-ohlc').textContent=`O ${fmt(last.open,5)} H ${fmt(last.high,5)} L ${fmt(last.low,5)} C ${fmt(last.close,5)}`;
}
function clearBook(message){$('asks').replaceChildren();$('bids').replaceChildren();$('book-price').textContent='—';$('spread').textContent='—';$('book-status').textContent=message;}
function renderView(data){
 view=data;$('price').textContent=fmt(data.mark,6);$('mark-price').textContent=fmt(data.mark,6);$('book-price').textContent=fmt(data.mark,6);$('funding').textContent=`${fmt(data.funding_rate*100,4)}%`;$('quote-state').textContent=`Canlı · ${clock(data.observed_at)}`;$('source').textContent='Binance USD-M';drawChart(data.candles);
 for(const [id,raw,cls] of [['asks',data.asks,'ask'],['bids',data.bids,'bid']]){let cumulative=0;const list=raw.slice(0,8).map(([price,qty])=>({price:Number(price),qty:Number(qty),total:cumulative+=Number(qty)})),max=cumulative||1;$ (id).replaceChildren();for(const r of id==='asks'?[...list].reverse():list){const div=node('div',null,`book-row ${cls}`);div.style.setProperty('--depth',`${r.total/max*100}%`);div.append(node('span',fmt(r.price,6)),node('span',fmt(r.qty,2)),node('span',fmt(r.total,2)));$(id).append(div);}}
 const ask=Number(data.asks[0]?.[0]),bid=Number(data.bids[0]?.[0]),spread=(ask-bid)/((ask+bid)/2)*10000;$('spread').textContent=`${fmt(spread,2)} bps`;$('book-status').textContent=`Binance · ${clock(data.observed_at)} Bakı`;$('market-meta').replaceChildren(...[`Spread ${fmt(spread)} bps`,`Funding ${fmt(data.funding_rate*100,4)}%`,`${interval} · son şam davam edir`].map(t=>node('span',t)));
}
async function pollView(){
 if(viewBusy||!state)return;const symbol=selected,tf=interval,key=`${symbol}:${tf}`;if(viewKey===key&&Date.now()-viewAt<5000)return;viewBusy=true;
 try{const data=await api(`/api/market/${encodeURIComponent(symbol)}?interval=${tf}`);if(selected===symbol&&interval===tf){viewKey=key;viewAt=Date.now();renderView(data);}}
 catch(e){if(selected===symbol&&interval===tf){view=null;clearBook(e.message);$('quote-state').textContent='Canlı məlumat alınmadı';$('price').textContent='—';$('mark-price').textContent='—';$('funding').textContent='—';}}
 finally{viewBusy=false;}
}
async function history(){try{const rows=await api('/api/history');$('history').replaceChildren();for(const r of rows.slice(0,40)){const tr=node('tr');[clock(r.observed_at),r.symbol,direction(r),r.decision,r.error||r.reasons?.[0]||'Filtrlər keçildi'].forEach(v=>tr.append(node('td',v)));$('history').append(tr);}if(!rows.length)emptyTable('history',5,'Analiz tarixçəsi boşdur');}catch(e){$('notice').textContent=e.message;}}
function renderOperations(){
 const m=state.metrics||{}, b=state.bridge||{};
 const labels={discovery:'Bazar siyahısı yenilənmədi',worker_error:'Skan prosesi xətası',trend:'Trend uyğun deyil',reclaim:'Yeni EMA20 keçidi yoxdur',momentum:'Momentum təsdiqi yoxdur',volume:'Həcm aşağıdır',extension:'Qiymət uzanıb',market_data:'Bazar məlumatı etibarsızdır',volatility:'Volatility limiti',price_gap:'Qiymət fərqi',rsi:'RSI limiti',telemetry:'İcra məlumatı yoxdur',heartbeat:'Heartbeat yoxdur',pause:'Girişlər dayandırılıb',storage_error:'Disk xətası',demo:'Demo rejimi',daily_loss:'Gündəlik zərər limiti',ttl:'Siqnal köhnəlib',analysis_error:'Analiz xətası',cooldown:'Təkrar giriş fasiləsi',funding:'Funding məlumatı yoxdur',rr:'R:R aşağıdır',spread:'Spread yüksəkdir',slippage:'Qiymət dəyişib',margin:'Margin limiti',duplicate:'Təkrar siqnal',risk_error:'Risk məlumatı yoxlanmadı',accepted:'Giriş təsdiqləndi',bridge:'Bridge xətası',schema:'Yanlış siqnal strukturu'};
 const root=$('operations');root.replaceChildren();
 const values=[['Skan profili',state.performance_profile],['Prioritet bazarlar',state.priority_symbols?.length||0],['Tam skan müddəti',`${fmt(m.scan_seconds?.radar)} san`],['Prioritet dövrü',`${fmt(m.scan_seconds?.priority)} san`],['Analiz p50 / p95',`${fmt(m.symbol_p50_ms)} / ${fmt(m.symbol_p95_ms)} ms`],['Bridge',b.health==='healthy'?'Sağlam':'Əlaqə gözlənilir'],['Bridge gecikməsi',b.bridge_lag_ms==null?'—':`${fmt(b.bridge_lag_ms)} ms`]];
 for(const [label,value] of values){const line=node('div',null,'account-line');line.append(node('span',label),node('b',String(value??'—')));root.append(line);}
 $('bridge-blocks').textContent=(b.blocks||[]).map(k=>labels[k]||k).join(' · ')||'Yeni girişlər üçün bridge hazırdır';
 const reasons=$('rejection-metrics');reasons.replaceChildren();
 for(const [name,counts] of [['Hazırkı siqnallar',b.rejections],['İcra sayğacları',b.executor_rejections]])for(const [key,value] of Object.entries(counts||{})){if(value)reasons.append(node('span',`${name} · ${labels[key]||key}: ${value}`));}
}
async function refresh(){try{state=await api('/api/status');execution(state.execution);renderOperations();$('notice').textContent=[state.demo?'DEMO — sintetik məlumat':null,state.discovery_error,state.priority_budget_exceeded?'Prioritet skan TTL büdcəsini aşır; watchlist/paralellik ayarlarını yoxlayın.':null,state.storage_error?'Tarixçəni diskə yazmaq mümkün olmadı.':null].filter(Boolean).join(' ');$('mode').textContent=state.demo?'DEMO':'● LOKAL STRATEGİYA';$('count').textContent=state.market_count??state.rows.length;$('signals').textContent=state.actionable_count??0;$('threshold').textContent='Bütün qaydalar';$('updated').textContent=clock(state.last_scan);$('scan-state').textContent=state.scanning?`Analiz ${state.scan_completed??state.rows.length} / ${state.market_count??state.rows.length}`:`Növbəti skan ${clock(state.next_scan)}`;$('model').textContent=state.strategy;$('scan').disabled=state.scanning;renderMarkets();detail(selectedRow());if(lastHistory!==state.last_scan){lastHistory=state.last_scan;await history();}await pollView();}
 catch(e){execution(null);$('operations').replaceChildren();$('bridge-blocks').textContent='Server bağlantısı yoxdur — sistem statusu köhnədir';$('rejection-metrics').replaceChildren();$('notice').textContent='Server bağlantısı yoxdur. Qiymətlər cari deyil.';$('mode').textContent='OFFLAYN';$('decision').textContent='OFFLAYN';$('levels').replaceChildren();$('price').textContent='—';$('mark-price').textContent='—';$('funding').textContent='—';$('quote-state').textContent='Bağlantı yoxdur';clearBook('Bağlantı yoxdur');$('scan').disabled=false;}}
$('market-search').addEventListener('input',renderMarkets);
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{marketFilter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));renderMarkets();});
document.querySelectorAll('[data-interval]').forEach(b=>b.onclick=()=>{interval=b.dataset.interval;view=null;viewKey='';document.querySelectorAll('[data-interval]').forEach(x=>x.classList.toggle('active',x===b));drawChart([]);pollView();});
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.tab-content').forEach(x=>x.hidden=x.id!==b.dataset.tab);if(b.dataset.tab==='history-pane')history();});
$('scan').onclick=async()=>{try{$('scan').disabled=true;await api('/api/scan',{method:'POST',headers:{'X-Crypto-Radar':'1'}});await refresh();}catch(e){$('notice').textContent=e.message;$('scan').disabled=false;}};
$('history-refresh').onclick=history;
$('pause-entries').onclick=async()=>{try{await api('/api/execution/'+(state?.execution?.paused?'resume':'pause'),{method:'POST',headers:{'X-Crypto-Radar':'1'}});await refresh();}catch(e){$('execution-status').textContent=e.message;}};
let adminInfo=null,adminBusy=false;
const post=(url,body)=>api(url,{method:'POST',headers:{'X-Crypto-Radar':'1','Content-Type':'application/json'},body:JSON.stringify(body||{})});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function renderAdmin(){
 const a=adminInfo,ready=!!a?.control&&!adminBusy,git=ready&&!a.error;
 if(a)$('git-info').textContent=a.error?a.error:`Branch ${a.branch} · ${a.commit}${a.behind?` · ${a.behind} yeni commit`:a.behind===0?' · aktual':''}${a.dirty?' · lokal dəyişiklik var':''}`;
 const sel=$('branch'),keep=sel.value;if(a?.branches){sel.replaceChildren(...a.branches.map(b=>{const o=node('option',b);o.value=b;return o;}));sel.value=keep&&a.branches.includes(keep)?keep:a.branch;}
 $('git-fetch').disabled=!git;$('git-pull').disabled=$('switch-branch').disabled=!git||a.dirty;$('restart-app').disabled=$('reset-paper').disabled=!ready;
 if(adminBusy||!a)return;
 if(!a.control)$('admin-status').textContent='İdarəetmə qoşulmayıb: Windows-da run.cmd / run-paper.cmd ilə başladın, serverdə bir dəfə "sudo bash deploy/install-control.sh" işlədin.';
 else if(a.pending_stale)$('admin-status').textContent='Sorğu 30 saniyədən çoxdur icra olunmur; idarəetmə servisini yoxlayın (journalctl -u crypto-jev-control).';
}
async function loadAdmin(){try{adminInfo=await api('/api/admin/info');}catch(e){if(!adminBusy)$('admin-status').textContent=e.message;}renderAdmin();}
async function adminAction(action,body,message){
 try{
  adminBusy=true;renderAdmin();$('admin-status').textContent=message;
  const {id}=await post('/api/admin/'+action,body),started=Date.now();let down=false;
  // The executor writes a result with our id; a restart takes the server down meanwhile.
  while(Date.now()-started<300000){
   await sleep(1500);
   try{const info=await api('/api/admin/info');adminInfo=info;const r=info.result;
    if(r?.id===id){if(r.restarted||down){$('admin-status').textContent=r.message+'\nSəhifə yenilənir…';await sleep(1200);location.reload();return;}
     $('admin-status').textContent=(r.ok?'':'Xəta: ')+r.message;return;}
    if(info.pending_stale){$('admin-status').textContent='İcraçı sorğunu götürmür; idarəetmə servisini yoxlayın.';return;}
   }catch{down=true;$('admin-status').textContent=message+'\nTətbiq yenidən başladılır…';}
  }
  $('admin-status').textContent='5 dəqiqə ərzində cavab gəlmədi; servis loglarını yoxlayın.';
 }catch(e){$('admin-status').textContent=e.message;}finally{adminBusy=false;renderAdmin();}
}
$('git-fetch').onclick=()=>adminAction('fetch',null,'Uzaq repo yoxlanılır…');
$('git-pull').onclick=()=>confirm('Son dəyişiklikləri çəkib (git pull) tətbiqi restart edək?')&&adminAction('pull',null,'Git pull edilir…');
$('switch-branch').onclick=()=>{const b=$('branch').value;if(b&&b!==adminInfo?.branch&&confirm(`"${b}" branch-ına keçib tətbiqi restart edək?`))adminAction('switch',{branch:b},`${b} branch-ına keçilir…`);};
$('restart-app').onclick=()=>confirm('Dashboard və Freqtrade yenidən başladılsın?')&&adminAction('restart',null,'Restart edilir…');
$('reset-paper').onclick=()=>confirm('BÜTÜN açıq və bağlanmış virtual əməliyyatlar silinəcək, balans 2,000 USDT olacaq. Köhnə baza data/backups qovluğunda saxlanılır. Davam edək?')&&adminAction('reset',null,'Əməliyyatlar sıfırlanır…');
loadAdmin();
(async function poll(){await refresh();setTimeout(poll,5000);})();history();
