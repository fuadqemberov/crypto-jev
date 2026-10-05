// Offline UI regression: all HTTP responses are fixtures; no exchange or trading API is contacted.
const {chromium} = require('playwright');
const {readFileSync, mkdirSync} = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1600,height:1100}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const state={rows:[],market_symbols:['BTCUSDT'],market_count:1,scanning:false,demo:true,
   ai_configured:false,min_confidence:.9,model:'jev-latest',performance_profile:'balanced',priority_symbols:['BTCUSDT'],
   execution:{connected:false,paused:true,positions:[],trades:[]},
   metrics:{scan_seconds:{radar:120,priority:3},symbol_p50_ms:220,symbol_p95_ms:800},cache:{hit_rate:.75},
   bridge:{health:'unavailable',blocks:['heartbeat'],rejections:{ttl:12,leverage_confidence:3},executor_rejections:{spread:2}}};
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
  await page.waitForFunction(()=>document.getElementById('operations').textContent.includes('75'));
  assert.match(await page.locator('#bridge-blocks').innerText(),/Heartbeat yoxdur/);
  assert.match(await page.locator('#rejection-metrics').innerText(),/12/);
  assert.equal(await page.locator('#score').count(),0);
  mkdirSync('data',{recursive:true});
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
