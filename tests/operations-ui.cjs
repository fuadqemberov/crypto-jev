// Offline UI regression: all HTTP responses are fixtures; no exchange or trading API is contacted.
const {chromium} = require('playwright');
const {readFileSync, mkdirSync} = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try {
  const page=await browser.newPage({viewport:{width:1600,height:1100}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const state={rows:[],market_symbols:['BTCUSDT'],market_count:1,scanning:false,demo:true,
   strategy:'trend-reclaim-v1',performance_profile:'balanced',priority_symbols:['BTCUSDT'],
   execution:{connected:false,paused:true,positions:[],trades:[]},
   metrics:{scan_seconds:{radar:120,priority:3},symbol_p50_ms:220,symbol_p95_ms:800},cache:{hit_rate:.75},
   bridge:{health:'unavailable',blocks:['heartbeat'],rejections:{ttl:12,funding:3},executor_rejections:{spread:2}}};
  state.rows=[
   {symbol:'BTCUSDT',decision:'SHORT',candidate:'SHORT',observed_at:Date.now(),execution_ready:false,execution_action:'WAIT',execution_blocks:['pause']},
   {symbol:'ETHUSDT',decision:'LONG',candidate:'LONG',observed_at:Date.now(),execution_ready:true,execution_action:'LONG',execution_blocks:[],planned_leverage:4}
  ];
  state.market_symbols=['BTCUSDT','ETHUSDT'];state.market_count=2;state.actionable_count=1;
  let offline=false;
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/api/status')return offline?route.abort():route.fulfill({json:state});
   if(url.pathname==='/api/history')return route.fulfill({json:[]});
   if(url.pathname.startsWith('/api/'))return route.fulfill({status:503,json:{detail:'offline fixture'}});
   const name=url.pathname==='/'?'index.html':path.basename(url.pathname);
   assert(['index.html','app.js','style.css'].includes(name));
   return route.fulfill({contentType:name.endsWith('html')?'text/html':name.endsWith('js')?'application/javascript':'text/css',
    body:readFileSync(path.join(__dirname,'../app/static',name),'utf8')});
  });
  await page.goto('http://jev.test/');
  await page.waitForFunction(()=>document.getElementById('operations').textContent.includes('120'));
  assert.match(await page.locator('#bridge-blocks').innerText(),/Heartbeat yoxdur/);
  assert.match(await page.locator('#rejection-metrics').innerText(),/12/);
  assert.equal(await page.locator('#score').count(),0);
  assert.equal(await page.locator('#threshold').innerText(),'Bütün qaydalar');
  assert.equal(await page.locator('#model').innerText(),'trend-reclaim-v1');
  assert.equal((await page.locator('body').innerText()).includes('90%'),false);
  assert.equal((await page.locator('body').innerText()).includes('API açarı'),false);
  await page.locator('[data-filter="ready"]').click();
  assert.equal(await page.locator('#markets button').count(),1);
  assert.match(await page.locator('#markets').innerText(),/ETHUSDT/);
  assert.equal(await page.locator('#signals').innerText(),'1');
  await page.locator('[data-filter="direction"]').click();
  assert.equal(await page.locator('#markets button').count(),2);
  await page.locator('#markets button[aria-label="BTCUSDT"]').click();
  assert.equal(await page.locator('#decision').innerText(),'WAIT');
  assert.equal(await page.locator('#decision-large').innerText(),'WAIT');
  assert.match(await page.locator('#decision-description').innerText(),/dayandırılıb/);
  mkdirSync('data' ,{recursive:true});
  await page.screenshot({path:'data/operations-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:'data/operations-mobile.png',fullPage:true});
  offline=true;
  await page.evaluate(()=>refresh());
  assert.equal(await page.locator('#wallet').innerText(),'—');
  assert.equal(await page.locator('#pause-entries').isDisabled(),true);
  assert.deepEqual(errors,[]);
  console.log('PASS: operations metrics, rejection labels, desktop/mobile layout, offline controls; zero JS errors.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
