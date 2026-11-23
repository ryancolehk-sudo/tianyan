import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { ladderFixture } from './ladderTestFixtures';
import { terminalDay, stockA } from './terminalTestFixtures';

async function prepare(page:Page,initialFailure=false) {
  const state={calls:0,empty:false,emptyPool:false,fail:initialFailure,missingLatest:false,utcLive:false,fewLong:false,errors:[] as string[],writes:[] as string[]};
  let signedIn=false;
  page.on('pageerror',e=>state.errors.push(e.message));
  await page.route('**/api/v1/**',route=>{
    const req=route.request(),url=new URL(req.url());
    const ok=(data:unknown)=>route.fulfill({json:{meta:{app_mode:'real',request_id:'00000000-0000-4000-8000-000000000001',generated_at:terminalDay+'T18:00:00+08:00',quality:{status:'partial',reasons:['合成浏览器测试']}},data}});
    if(url.pathname==='/api/v1/session'){if(req.method()==='POST')signedIn=true;return signedIn?ok({user:{id:'ladder-fixture',username:'synthetic',role:'admin'},csrf_token:'synthetic-only'}):route.fulfill({status:401,json:{error:{message:'请登录'}}});}
    if(req.method()!=='GET'){state.writes.push(url.pathname);return route.abort('blockedbyclient');}
    if(url.pathname==='/api/v1/ladder-observation'){
      state.calls++;if(state.fail)return route.fulfill({status:503,json:{error:{message:'合成读取失败'}}});
      const data=ladderFixture(state.empty),day=url.searchParams.get('trade_day');
      if(state.emptyPool){
        const current=data.days.find(item=>item.day===terminalDay)!;
        const seed=current.rows[0];
        current.eliminated=[...current.rows.map(row=>({...row,status:'eliminated'})),...current.eliminated,...Array.from({length:14},(_,index)=>({...seed,code:`${998000+index}.SZ`,name:'合成待核'+index,status:'eliminated'}))];
        current.rows=[];
        current.coverage=[{dataset:'pools',trade_day:terminalDay,state:'complete',row_count:0,received_at:terminalDay+'T17:31:00+08:00'},{dataset:'daily',trade_day:terminalDay,state:'complete',row_count:5573,received_at:terminalDay+'T17:30:00+08:00'}];
        data.can_export=true;
      }
      if(state.missingLatest)delete data.latest_quotes[stockA];
      if(state.utcLive)Object.assign(data.latest_quotes[stockA],{price_basis:'intraday',source_time:terminalDay+'T02:00:00+00:00'});
      if(state.fewLong)data.days.forEach(d=>d.rows.forEach((r,i)=>{r.themes=['合成较长的原生涨停原因分类'+i%6];}));
      if(day&&day!==terminalDay){data.trade_day=day;data.days=data.days.filter(d=>d.day<=day);}
      return ok(data);
    }
    if(url.pathname==='/api/v1/market-session')return ok({trade_day:terminalDay,phase:'after_close',is_trading_day:true,current_day_active:false,automatic_live:false,source_time:terminalDay+'T18:00:00+08:00'});
    return ok({items:[],next_cursor:null});
  });
  await page.addLocatorHandler(page.getByRole('dialog',{name:'正在准备你的工作空间'}),()=>page.keyboard.press('Escape'));
  await page.goto('/workbench?view=ladder');
  await page.getByLabel('账号',{exact:true}).fill('synthetic');
  await page.getByLabel('密码',{exact:true}).fill('synthetic-only');
  await page.getByRole('button',{name:'进入工作台'}).click();
  await expect(page.getByRole('tab',{name:'单日',exact:true})).toHaveAttribute('aria-selected','true');
  if(initialFailure)await expect(page.getByText('连板资料读取失败',{exact:true})).toBeVisible();
  else await expect(page.locator('.lo-stock').first()).toBeVisible();
  const watermark = new URL('/__test__/ladder-synthetic.css', page.url()).href;
  await page.route(watermark, route => route.fulfill({contentType:'text/css',body:'body::after{content:"合成测试 · 非真实行情";position:fixed;bottom:2px;left:50%;transform:translateX(-50%);z-index:20000;color:#e7bf69;background:#10212b;font:11px sans-serif;padding:2px 8px;pointer-events:none}'}));
  await page.addStyleTag({url:watermark});
  await expect.poll(() => page.locator('body').evaluate(body => getComputedStyle(body, '::after').content)).toBe('"合成测试 · 非真实行情"');
  return state;
}

test('four views switch immediately over one read, preserve date and selection, and export respects rights',async({page},info)=>{
  const state=await prepare(page);
  await expect(page.locator('.lo-height-row')).toHaveCount(5);
  await expect(page.locator('.lo-stock').first()).toContainText('09:25:01');
  await page.locator('.lo-stock').first().click();
  await expect(page.getByLabel('选中股票详情',{exact:true})).toContainText('合成甲');
  expect(new URL(page.url()).searchParams.get('stock')).toBe(stockA);
  await page.getByRole('button',{name:'关闭股票详情'}).click();
  const reads=state.calls;
  for(const mode of ['多日','列表','板块','单日']){
    await page.getByRole('tab',{name:mode,exact:true}).click();
    await expect(page.getByRole('tab',{name:mode,exact:true})).toHaveAttribute('aria-selected','true');
    expect(new URL(page.url()).searchParams.get('view')).toBe('ladder');
    expect(new URL(page.url()).searchParams.get('stock')).toBe(stockA);
    await expect(page.getByLabel('观察日期',{exact:true})).toContainText(terminalDay);
    await page.screenshot({path:info.outputPath(`view-${mode}.png`),fullPage:true});
  }
  expect(state.calls).toBe(reads);
  await expect(page.getByRole('button',{name:'Excel',exact:true})).toBeDisabled();
  expect(state.errors).toEqual([]);expect(state.writes).toEqual([]);
});

test('sorts both directions with zero preserved, filters themes, shows failed stocks, and cards are one button',async({page})=>{
  await prepare(page);
  await page.getByLabel('淘汰股',{exact:true}).check();
  await expect(page.locator('.lo-stock').filter({hasText:'合成淘汰'})).toBeVisible();
  await expect(page.locator('.lo-stock').filter({hasText:'合成炸板'})).toBeVisible();
  const card=page.locator('.lo-stock').filter({hasText:'合成甲'}).first();
  await expect(card).toContainText('0.00%');
  expect(await card.locator('button,a,[role=button]').count()).toBe(0);
  await page.getByRole('group',{name:'排序规则'}).getByRole('button',{name:'涨幅'}).click();
  await expect(page.getByRole('group',{name:'排序规则'}).getByRole('button',{name:'涨幅'})).toContainText('↑');
  const firstSort=page.getByRole('group',{name:'排序规则'}).getByRole('button',{name:'首封',exact:true});
  await firstSort.click();await expect(firstSort).toContainText('↑');await firstSort.click();await expect(firstSort).toContainText('↓');
  const chips=page.getByRole('group',{name:'题材筛选'});
  await chips.getByRole('button',{name:/合成题材乙/}).click();
  await expect(page.locator('.lo-stock').filter({hasText:'合成甲'})).toHaveCount(0);
  await chips.getByRole('button',{name:/全部/}).click();
  await page.getByRole('tab',{name:'列表',exact:true}).click();
  await expect(page.getByRole('columnheader',{name:/^首封/})).toBeVisible();
  await page.getByRole('columnheader',{name:'成交额',exact:true}).getByRole('button').click();
  await expect(page.getByRole('columnheader',{name:/成交额/})).toHaveAttribute('aria-sort','descending');
});

test('multi-day relation and latest mode do not leak a later candle into an earlier day',async({page})=>{
  await prepare(page);
  await page.getByRole('tab',{name:'多日',exact:true}).click();
  await page.getByLabel('K线',{exact:true}).check();
  const old=page.locator('.lo-days-matrix tbody tr').first().locator('td').last();
  for(const chart of await old.locator('svg.lo-candles').all())expect(await chart.getAttribute('aria-label')).not.toContain('至 2001-12-31');
  const card=page.locator('.lo-stock').filter({hasText:'合成甲'}).first();
  await card.click();await page.getByRole('button',{name:'关闭股票详情'}).click();
  expect(await page.locator('.lo-stock.is-selected').count()).toBeGreaterThan(1);
  await page.getByRole('group',{name:'多日行情口径'}).getByRole('button',{name:/最新行情/}).click();
  await expect(card).toContainText('+7.00%');
  await page.getByRole('tab',{name:'单日',exact:true}).click();
  await expect(page.locator('.lo-stock').filter({hasText:'合成甲'}).first()).toContainText('0.00%');
  await page.getByRole('tab',{name:'多日',exact:true}).click();
  await page.getByRole('group',{name:'多日行情口径'}).getByRole('button',{name:'当日数据'}).click();
  await expect(card).toContainText('0.00%');
});

test('phone preserves access to views, dates and quote basis within its screen',async({page},info)=>{
  await page.setViewportSize({width:390,height:844});const state=await prepare(page);
  state.fewLong=true;await page.getByRole('button',{name:'刷新连板数据'}).click();
  await expect(page.locator('.lo-themes button')).toHaveCount(7);
  await expect(page.getByRole('button',{name:'展开',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'展开',exact:true}).click();
  await expect(page.getByRole('button',{name:'收起',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'收起',exact:true}).click();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  expect(await page.evaluate(()=>document.documentElement.scrollHeight-innerHeight)).toBeLessThanOrEqual(2);
  await page.screenshot({path:info.outputPath('phone-single-390.png'),fullPage:true});
  await page.getByRole('tab',{name:'多日',exact:true}).click();
  await expect(page.getByRole('group',{name:'多日行情口径'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.screenshot({path:info.outputPath('phone-multi-390.png'),fullPage:true});
});

test('latest quote gaps stay empty and UTC native clocks display Beijing time',async({page})=>{
  const state=await prepare(page);state.missingLatest=true;
  await page.getByRole('button',{name:'刷新连板数据'}).click();
  await page.getByRole('tab',{name:'多日',exact:true}).click();
  await page.getByRole('group',{name:'多日行情口径'}).getByRole('button',{name:/最新行情/}).click();
  const card=page.locator('.lo-stock').filter({hasText:'合成甲'}).first();
  await expect(card).toHaveAttribute('title',/最新行情未覆盖/);
  await expect(card).not.toContainText('0.00%');
  state.missingLatest=false;state.utcLive=true;
  await page.getByRole('button',{name:'刷新连板数据'}).click();
  await expect(card).toHaveAttribute('title',/盘中行情 2001-12-31 10:00:00/);
});

test('date calendar disables unverified days, arrows follow sessions, and tabs have keyboard navigation',async({page})=>{
  await prepare(page);
  await page.getByRole('tab',{name:'单日',exact:true}).focus();await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab',{name:'多日',exact:true})).toBeFocused();
  await page.getByLabel('观察日期',{exact:true}).click();
  await expect(page.getByRole('button',{name:'2001-12-29',exact:true})).toBeDisabled();
  await page.getByRole('button',{name:'2001-12-28',exact:true}).click();
  await expect(page).toHaveURL(/trade_day=2001-12-28/);
  await page.getByRole('button',{name:'后一交易日（观察日期）',exact:true}).click();
  await expect(page).toHaveURL(/trade_day=2001-12-31/);
  await page.getByLabel('观察日期',{exact:true}).click();await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog',{name:'观察日期日历'})).toHaveCount(0);
  await expect(page.getByLabel('观察日期',{exact:true})).toBeFocused();
});

test('empty and error states keep the four views, recover without fabricated zero rows',async({page})=>{
  const state=await prepare(page);state.empty=true;
  await page.getByRole('button',{name:'刷新连板数据'}).click();
  await expect(page.getByText('当前名单没有可展示的连板对象',{exact:true})).toBeVisible();
  await expect(page.locator('.lo-stock')).toHaveCount(0);
  state.empty=false;state.fail=true;
  await page.getByRole('button',{name:'刷新连板数据'}).click();
  await expect(page.getByRole('alert').filter({hasText:'合成读取失败'})).toBeVisible();
  state.fail=false;await page.getByRole('alert').getByRole('button',{name:'重试'}).click();
  await expect(page.locator('.lo-stock').first()).toBeVisible();
});

test('an initial read failure remains unknown and recovers automatically without a retry click',async({page})=>{
  await page.clock.install();
  const state=await prepare(page,true);
  await expect(page.getByRole('group',{name:'题材筛选'}).getByRole('button',{name:'全部 —',exact:true})).toBeVisible();
  await expect(page.getByText(/暂时无法读取连板资料，将自动重试/)).toBeVisible();
  await expect(page.locator('.lo-footer')).toContainText('读取失败');
  await expect(page.locator('.lo-footer')).not.toContainText('未采集');
  await expect(page.getByText(/所选日期的原日名单尚未就绪/)).toHaveCount(0);
  const failedReads=state.calls;
  state.fail=false;
  await page.clock.fastForward(31_001);
  await expect(page.locator('.lo-stock').first()).toBeVisible();
  expect(state.calls).toBeGreaterThan(failedReads);
  await expect(page.getByText('连板资料读取失败',{exact:true})).toHaveCount(0);
  await expect(page.getByLabel('观察日期',{exact:true})).toContainText(terminalDay);
  await expect(page.locator('.lo-footer')).toContainText('收盘 · 16只');
  expect(state.errors).toEqual([]);expect(state.writes).toEqual([]);
});

test('an empty archived source pool stays unverified and cannot turn yesterday stocks into confirmed failures',async({page},info)=>{
  const state=await prepare(page);state.emptyPool=true;
  await page.getByRole('button',{name:'刷新连板数据'}).click();
  await expect(page.getByText('来源返回空名单，连板状态待核',{exact:true})).toBeVisible();
  await expect(page.getByText(/不能据此判断市场为0只/)).toBeVisible();
  await expect(page.locator('.lo-footer')).toContainText('原日名单待核');
  await expect(page.locator('.lo-footer')).not.toContainText('收盘 · 0只');
  await expect(page.getByLabel('淘汰股',{exact:true})).toBeDisabled();
  await expect(page.getByRole('button',{name:'Excel',exact:true})).toBeDisabled();
  await expect(page.locator('.lo-stock')).toHaveCount(0);
  await page.locator('.lo-empty-coverage summary').click();
  await expect(page.locator('.lo-empty-coverage')).toContainText('来源返回空名单 · 0条');
  await expect(page.locator('.lo-empty-coverage')).toContainText('已归档 · 5573条');
  await expect(page.locator('.lo-empty-coverage')).toContainText('32 条待核候选');
  expect(await page.locator('.lo-empty-coverage dd').first().evaluate(element=>element.getBoundingClientRect().width)).toBeGreaterThan(200);
  const monitor=new URL((await page.getByRole('link',{name:'查看运行监控',exact:true}).getAttribute('href'))!, 'http://127.0.0.1');
  expect(monitor.pathname).toBe('/data'); expect(monitor.hash).toBe('#data-recovery');
  expect(monitor.searchParams.get('view')).toBe('monitor');
  expect(monitor.searchParams.get('trade_day')).toBe(terminalDay);
  expect(monitor.searchParams.get('return_to')).toBe('/workbench?view=ladder');
  expect(monitor.searchParams.get('source_id')).toBeNull();
  await page.screenshot({path:info.outputPath('synthetic-empty-source-pool.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await expect(page.locator('.lo-empty-coverage dd').first()).toBeInViewport();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.screenshot({path:info.outputPath('synthetic-empty-source-pool-phone.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole('tab',{name:'多日',exact:true}).click();
  await page.getByLabel('淘汰股',{exact:true}).check();
  await expect(page.getByRole('columnheader',{name:new RegExp(terminalDay)})).toContainText('名单待核');
  for(const cell of await page.locator('.lo-days-matrix tbody tr td:first-of-type').all())await expect(cell.locator('.lo-stock')).toHaveCount(0);
  await expect(page.locator('.lo-stock').filter({hasText:'合成甲'}).first()).toBeVisible();
  await page.locator('.lo-stock').filter({hasText:'合成甲'}).first().click();
  const unverifiedTrajectory=page.locator('.lo-trajectory').getByRole('button').filter({hasText:terminalDay.slice(5)});
  await expect(unverifiedTrajectory).toBeDisabled();
  await expect(unverifiedTrajectory).toContainText('名单待核');
  await expect(unverifiedTrajectory).not.toContainText('淘汰');
  await page.getByRole('button',{name:'关闭股票详情'}).click();
  await page.getByRole('tab',{name:'单日',exact:true}).click();
  await page.getByRole('button',{name:/切换至 .* 已存名单/}).click();
  await expect(page).toHaveURL(/trade_day=2001-12-28/);
  await expect(page.locator('.lo-stock').first()).toBeVisible();
  await expect(page.getByLabel('淘汰股',{exact:true})).toBeEnabled();
  expect(state.errors).toEqual([]);expect(state.writes).toEqual([]);
});

test('filter misses preserve the source total and empty-pool recovery keeps the observed cutoff',async({page})=>{
  const state=await prepare(page);
  await page.getByLabel('搜索股票或题材',{exact:true}).fill('没有匹配的合成条件');
  await expect(page.getByText('没有匹配的股票',{exact:true})).toBeVisible();
  await expect(page.locator('.lo-footer')).toContainText('收盘 · 16只');
  await expect(page.locator('.lo-footer')).not.toContainText('名单待核');
  await page.getByRole('button',{name:'清除筛选',exact:true}).click();
  await expect(page.locator('.lo-stock').first()).toBeVisible();
  state.emptyPool=true;
  const cutoff=terminalDay+'T18:00:00+08:00';
  await page.goto('/workbench?view=ladder&as_of_time='+encodeURIComponent(cutoff));
  await expect(page.getByText('来源返回空名单，连板状态待核',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:/切换至 .* 已存名单/}).click();
  await expect(page.locator('.lo-stock').first()).toBeVisible();
  expect(new URL(page.url()).searchParams.get('trade_day')).toBe('2001-12-28');
  expect(new URL(page.url()).searchParams.get('as_of_time')).toBe(cutoff);
  expect(state.errors).toEqual([]);expect(state.writes).toEqual([]);
});

test('normal cards, failed promotions and historical Klines render in every view',async({page},info)=>{
  await prepare(page);
  await page.getByLabel('精简',{exact:true}).uncheck();
  await page.getByLabel('K线',{exact:true}).check();
  await page.getByLabel('淘汰股',{exact:true}).check();
  for(const mode of ['单日','多日','列表','板块']){
    await page.getByRole('tab',{name:mode,exact:true}).click();
    await expect(page.locator('.lo-candles').first()).toBeVisible();
    await page.screenshot({path:info.outputPath(`normal-kline-${mode}.png`),fullPage:true});
  }
});

for(const size of [{width:1440,height:900},{width:1920,height:1080},{width:1024,height:768}])test(`dense layout ${size.width} has no page scroll and all controls remain accessible`,async({page},info)=>{
  await page.setViewportSize(size);const state=await prepare(page);
  await page.screenshot({path:info.outputPath(`single-${size.width}.png`),fullPage:true});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  const overflow=await page.evaluate(()=>document.documentElement.scrollHeight-innerHeight);
  expect(overflow).toBeLessThanOrEqual(2);
  await expect(page.locator('.lo-footer')).toBeInViewport();
  await expect(page.getByRole('tab',{name:'板块',exact:true})).toBeInViewport();
  expect(state.errors).toEqual([]);
});
