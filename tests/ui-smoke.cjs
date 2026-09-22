// Start the dashboard with DEMO_MODE=true before running. All balances below are fixtures.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1100}});
    const errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await page.goto('http://127.0.0.1:8082');
    await page.waitForFunction(()=>document.getElementById('wallet').textContent==='—');
    assert.equal(await page.locator('#pause-entries').isDisabled(),true);
    await page.route('**/api/status',async route=>{
      const response=await route.fetch();const body=await response.json();
      body.execution={...body.execution,connected:true,bridge_configured:true,state:'running',
        observed_at:Date.now(),wallet:2002,free:1862,used:140,realized_pnl:2,total_pnl:3,wins:1,losses:0,
        positions:[{pair:'BTC/USDT:USDT',is_short:false,open_rate:100,current_rate:101,
          stake_amount:140,profit_abs:1,stop_loss_abs:98,enter_tag:'jev:fixture:98:104',funding_fees:-.01}],
        trades:[{pair:'ETH/USDT:USDT',is_short:true,open_rate:100,close_rate:98,close_profit_abs:2,
          is_open:false,funding_fees:.02,exit_reason:'risk_target'}]};
      await route.fulfill({response,json:body});
    });
    await page.reload();
    await page.waitForFunction(()=>document.getElementById('wallet').textContent==='2,002');
    assert.match(await page.locator('#positions').innerText(),/BTC\/USDT:USDT/);
    assert.match(await page.locator('#trades').innerText(),/risk_target/);
    await page.locator('#pause-entries').click();
    await page.waitForFunction(()=>document.getElementById('pause-entries').textContent==='Girişləri davam etdir');
    await page.locator('#pause-entries').click();
    await page.waitForFunction(()=>document.getElementById('pause-entries').textContent==='Girişləri dayandır');
    await page.screenshot({path:'/tmp/crypto-jev-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    await page.screenshot({path:'/tmp/crypto-jev-mobile.png',fullPage:true});
    await page.unroute('**/api/status');
    await page.route('**/api/status',route=>route.abort());
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#wallet').innerText(),'—');
    assert.equal(await page.locator('#pause-entries').isDisabled(),true);
    assert.deepEqual(errors,[]);
    console.log('UI smoke passed: desktop, mobile, PnL, pause/resume, offline clearing. Fixture data only.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
