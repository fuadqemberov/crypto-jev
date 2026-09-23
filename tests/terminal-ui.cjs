// Live read-only browser verification; mutation controls are intercepted locally.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1100}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8082');
  await page.waitForFunction(()=>document.querySelectorAll('#bids .book-row').length===8,{timeout:60000});
  await page.waitForFunction(()=>document.querySelectorAll('#markets .market').length>100);
  assert.equal(await page.locator('#symbol').innerText(),'BTCUSDT');
  assert.equal(await page.locator('#chart .candle-up, #chart .candle-down').count()>100,true);
  await page.screenshot({path:path.resolve('data/terminal-desktop.png'),fullPage:true});
  await page.locator('#market-search').fill('CGPT');
  await page.getByRole('button',{name:'CGPTUSDT',exact:true}).click();
  await page.waitForFunction(()=>document.getElementById('symbol').textContent==='CGPTUSDT');
  await page.locator('[data-interval="1h"]').click();
  await page.waitForFunction(()=>document.getElementById('market-meta').textContent.includes('1h ·'),{timeout:60000});
  await page.locator('[data-tab="trades-pane"]').click();
  assert.equal(await page.locator('#trades-pane').isVisible(),true);
  await page.locator('[data-tab="history-pane"]').click();
  await page.waitForFunction(()=>document.querySelectorAll('#history tr').length>0);
  await page.locator('[data-tab="positions-pane"]').click();
  await page.locator('#market-search').fill('');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.screenshot({path:path.resolve('data/terminal-mobile.png'),fullPage:true});
  await page.route('**/api/status',r=>r.abort());
  await page.evaluate(()=>refresh());
  assert.equal(await page.locator('#wallet').innerText(),'—');
  assert.equal(await page.locator('#book-price').innerText(),'—');
  assert.equal(await page.locator('#pause-entries').isDisabled(),true);
  assert.deepEqual(errors,[]);
  console.log('PASS: live candles/order book, 528-market search, selection, timeframe, tabs, mobile width, offline clearing; no browser errors.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
