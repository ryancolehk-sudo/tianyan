import { expect, test } from '@playwright/test';
import type { AuctionReferenceReport, AuctionSupportRow } from '../../src/generated/schema';
import type { Page } from '@playwright/test';
import { auctionCode, auctionDay, auctionInstrument, liveAuctionFixture } from './auction-live-sync.fixtures';

test.beforeEach(async ({ page }) => {
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => { await page.keyboard.press('Escape'); });
});

function referenceFixture(day:string, stage:AuctionReferenceReport['stage']):AuctionReferenceReport {
  const stages=['09:15','09:20','09:25'],index=stages.indexOf(stage);
  const rows=Array.from({length:5},(_,i)=>{
    const amounts=[(10-i)*1e8,(i?9-i:12)*1e8,(i?8-i:20)*1e8];
    return {code:`99990${i+1}.SZ`,name:`合成封单0${i+1}`,tags:['合成机器人','合成视觉','3板'],
      node_amounts:Object.fromEntries(stages.map((key,k)=>[key,k<=index?String(amounts[k]):null])),
      seal_cny:String(amounts[index]),change_cny:index?String(amounts[index]-amounts[index-1]):null};
  });
  return {trade_day:day,stage,received_at:`${day}T${stage}:06+08:00`,source_name:'合成封单来源',
    batch_hash:'a'.repeat(64),evidence_ids:[auctionInstrument],row_count:10,valid_count:5,
    totals:Object.fromEntries(stages.map((key,k)=>[key,k<=index?String([100,80,90][k]*1e8):null])),
    total_change_cny:index?String(index===1?-20e8:10e8):null,
    themes:[{name:'合成机器人/合成视觉',total_cny:String(rows.reduce((sum,row)=>sum+Number(row.seal_cny),0)),members:rows.slice(0,2)}],
    strongest:rows,additions:rows.filter(row=>Number(row.change_cny)>0),reductions:rows.filter(row=>Number(row.change_cny)<0).slice(0,3)};
}

function denseReferenceFixture(day:string,stage:AuctionReferenceReport['stage']):AuctionReferenceReport {
  const report=referenceFixture(day,stage);
  const rows=Array.from({length:11},(_,i)=>({
    code:`9999${String(i+1).padStart(2,'0')}.SZ`,name:`合成封单${String(i+1).padStart(2,'0')}`,
    tags:['合成超长题材名称用于核对完整信息','合成机器人','合成视觉','3板'],
    node_amounts:{'09:15':'100000000','09:20':String((20-i-(i===1?0:i<8?1:-1))*1e8),'09:25':String((20-i)*1e8)},
    seal_cny:String((20-i)*1e8),change_cny:i===0?null:i===1?'0':String((i<8?1:-1)*1e8)
  }));
  return {...report,strongest:rows.slice(0,5),additions:rows.slice(5,8),reductions:rows.slice(8),
    themes:[0,1,2].map(i=>({name:`合成超长题材${i+1}/合成机器人/合成视觉`,total_cny:'12345678900',members:rows.slice(i,i+3)}))};
}

function supportFixture(day: string, downSeals = false, downCount = 2, bookRevision = 0) {
  const rows: AuctionSupportRow[] = Array.from({length:8}, (_, i) => ({
    code:`99990${i+1}.SZ`,name:`合成封单0${i+1}`,market:i%2?'创业板':'深主板',previous_state:i===7?'Z':'U',
    previous_height:i%3+1,themes:[i%2?'合成芯片':'合成机器人'],support_pct:i===6?null:i*10,
    auction_pct:i===6?null:i-2,auction_amount_cny:i===6?null:(i+1)*1e7,auction_turnover_pct:i+0.5,
    auction_volume_ratio:i+1,pct_change:i-3,current_seal_cny:(i+1)*1e8,amount_cny:(i+1)*2e8,
    float_mv_cny:(i+1)*20e8,actual_float_cny:(i+1)*10e8,volume_change_pct:i*20,
    previous_type:'一字',previous_close:10,previous_amount_cny:1e8,previous_turnover_pct:2,
    previous_seal_cny:1e8,previous_seal_time:'09:25:00',previous_open_times:0,popularity_rank:null,
    change_reason:'合成芯片+合成机器人',lhb_status:i===0?'listed':i===6?'unavailable':'not_listed',
    lhb_reasons:i===0?['合成上榜原因一','合成上榜原因二']:[],auction_at_limit:i===1,auction_at_down_limit:i===2,
    at_limit:i===1,previous_lhb:i===0,previous_broken:i===7,price_basis:'daily_close',source_time:`${day}T15:00:00+08:00`,
  }));
  const previous = day === auctionDay ? '2026-10-23' : '2026-10-22';
  const down_days = [day, previous].map(d => ({day:d,live:bookRevision>0&&d===day,
    book_state:bookRevision&&d===day?(bookRevision>0?'available':'stale'):undefined,
    latest_at:bookRevision&&d===day?`${day}T09:18:00+08:00`:undefined,
    source_name:bookRevision&&d===day?'合成竞价盘口':undefined,book_coverage:2,book_expected:6186,
    seal_coverage:downSeals?downCount:0,
    stage_coverage:Object.fromEntries(['09:15','09:20','09:25'].map(stage=>[stage,downSeals?downCount:0])), evidence_ids:[], rows:Array.from({length:downCount}, (_,i) => ({
    code:`${999910+i}.SZ`,name:`合成跌停${i+1}`,tags:['合成跌停题材'],previous_height:null,
    current_seal_cny:bookRevision>0&&d===day?(i+1)*bookRevision*1e8:undefined,book_time:`${d}T09:18:00+08:00`,
    node_amounts:{'09:15':downSeals||bookRevision>0?(i+1)*1e7:null,'09:20':downSeals?(i+1)*2e7:null,'09:25':downSeals?(i+1)*3e7:null},node_pct:{'09:15':-10,'09:20':i?-5:-10,'09:25':null},
    node_times:{'09:15':`${d}T09:15:06+08:00`,'09:20':`${d}T09:20:06+08:00`},
    auction_pct:i?-5:-10,auction_amount_cny:123456789,pct_change:i?-5:-10,price_basis:'daily_close',pool_seal_cny:i?null:1e8
  })).filter(()=>!(bookRevision<0&&d===day))}));
  return {trade_day:day,previous_trade_day:previous,available_days:[day,previous],state:'available',down_days,
    as_of_time:`${day}T16:00:00+08:00`,sources:[],missing_fields:['竞价净额','人气排名'],rows};
}

async function installAuction(page: Page, mode: 'real'|'demo' = 'real') {
  const origin = new URL(String(test.info().project.use.baseURL)).origin;
  const state = { coldEmpty: false, signedIn: false, after0925: false, previousPoolOnly: false, rollingIncrement: 0, reads: 0, supportReads:0, referenceReads:0, referenceStage:'09:20' as '09:15'|'09:20'|'09:25', referenceDay:auctionDay, referenceFailure:false, referenceDenied:false, referenceRecovered:false, partialBook:false, denseReference:false, downSeals:false, denseDown:false, bookRevision:0, errors: [] as string[], writes: [] as string[], outside: [] as string[] };
  const meta = { request_id: auctionInstrument, app_mode: mode, generated_at: `${auctionDay}T09:20:46+08:00`, as_of_time: null,
    quality: { status: 'partial', reasons: ['合成浏览器测试样本，不是真实行情'] } };
  page.on('pageerror', error => state.errors.push(error.message));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) { state.outside.push(url.origin); return route.abort('blockedbyclient'); }
    return route.fallback();
  });
  await page.route('**/api/v1/**', route => {
    const request = route.request(); const url = new URL(request.url());
    const ok = (data: unknown) => route.fulfill({ json: { meta, data } });
    if (url.pathname === '/api/v1/session') {
      if (request.method() === 'POST') state.signedIn = true;
      return state.signedIn ? ok({ user: { id: 'synthetic-auction-live', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic-only' })
        : route.fulfill({ status: 401, json: { error: { message: '请登录' } } });
    }
    if (request.method() !== 'GET') { state.writes.push(`${request.method()} ${url.pathname}`); return route.abort('blockedbyclient'); }
    if (url.pathname === '/api/v1/workbench-view') {
      state.reads++;
      const data = liveAuctionFixture(url.searchParams, state.after0925, state.rollingIncrement);
      if(state.coldEmpty) return ok({...data, trade_day:'', available_days:[], auction_days:[], stocks:[], selected_stock_code:null});
      data.stocks.push(...[0,1].map(i=>({...data.stocks[0],code:`99991${i}.SZ`,name:`合成跌停${i+1}`,
        pct_change:i?-5:-10,auction_pct:i?-5:-10,auction_amount_cny:123456789,
        node_amounts:{'09:15':null,'09:20':null,'09:25':null}})));
      if (state.previousPoolOnly) {
        const stock = data.auction_days.find(day => day.day === '2026-10-23')?.items.find(item => item.code === auctionCode);
        if (stock) {
          stock.node_amounts = { '09:15': null, '09:20': null, '09:25': null };
          stock.daily_pool = { trade_day: '2026-10-23', state: 'U', board_height: 1, source_name: '合成日榜',
            received_at: '2026-10-23T16:00:00+08:00', scope: '合成测试', first_limit_time: '09:30:33',
            last_limit_time: '09:31:30', seal_amount_cny: 26_760_112, open_times: 1 };
        }
      }
      if(state.denseReference) data.auction_days.forEach(day=>{
        const stock=day.items.find(item=>item.code===auctionCode);
        if(stock) stock.daily_pool={trade_day:day.day,state:'U',board_height:3,source_name:'合成日榜',
          received_at:`${day.day}T15:05:00+08:00`,scope:'合成测试',first_limit_time:'09:25:00',last_limit_time:'09:25:00',seal_amount_cny:1e8,open_times:0};
      });
      return ok(data);
    }
    if (url.pathname === '/api/v1/auction-reference') {
      state.referenceReads++;
      if(state.referenceFailure) return route.fulfill({status:503,json:{error:{message:'合成读取故障'}}});
      const report=(state.denseReference?denseReferenceFixture:referenceFixture)(state.referenceDay,state.referenceStage);
      if(state.referenceRecovered){report.recovered=true;report.received_at=`${state.referenceDay}T11:34:06+08:00`;}
      return ok(state.referenceDenied ? {state:'unavailable',report:null,reason:'竞价封单来源未启用或读取权限不可用'} : {state:'available',report});
    }
    if (url.pathname === '/api/v1/auction-support') { state.supportReads++; const data=supportFixture(url.searchParams.get('trade_day') || auctionDay,state.downSeals,state.denseDown?24:2,state.bookRevision);if(state.partialBook){data.down_days[0].book_state='partial';data.down_days[0].rows=[];}return ok(data); }
    if (url.pathname === '/api/v1/watchlist') return ok({ items: [{ instrument: { id: auctionInstrument, exchange: 'SZSE', symbol: '999901', asset_type: 'stock', name: '合成封单01' } }] });
    if (url.pathname === '/api/v1/ai/settings') return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  return state;
}

async function openAuction(page: Page, day = auctionDay) {
  await page.goto(`/workbench?view=auction&trade_day=${day}&stock=${auctionCode}`);
  await page.getByLabel('账号', { exact: true }).fill('synthetic');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-only');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '多日封单对照', exact: true })).toBeVisible();
  // A live page may keep background reads in flight. Wait for its actual data.
  await expect(column(page, day).getByRole('button', { name: /^合成封单01/ })).toBeVisible();
}
function column(page: Page, day = auctionDay) { return page.getByRole('region', { name: `${day}竞价对照`, exact: true }); }
function realRows(page: Page, day = auctionDay) { return column(page, day).locator('.ty-auction-stock-list > li:not(.ty-auction-blank)'); }
function safe(state: Awaited<ReturnType<typeof installAuction>>) { expect(state.errors).toEqual([]); expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); }

test('09:20 snapshot shows source, supplier tags and indicative change while 09:25 and matched trades remain missing', async ({ page }) => {
  const state = await installAuction(page); await openAuction(page);
  await expect(page.locator('.ty-source-state')).toHaveCount(0);
  await expect(column(page).getByRole('button',{name:`按${auctionDay}当前封单排序`,exact:true})).toHaveAttribute('aria-pressed','true');
  await expect(column(page).getByRole('button',{name:`按${auctionDay} 09:20封单排序`,exact:true})).toHaveClass(/ty-gold/);
  await expect(column(page).getByLabel(`${auctionDay}封单来源`)).toHaveAttribute('title','短线侠当前表 · 采集 2026-10-26 09:20:46');
  await expect(column(page).getByLabel(`${auctionDay}封单来源`)).toContainText(/一字\s*5只封单\s*74亿/);
  const header = column(page).getByRole('heading', { name: `${auctionDay}涨停`, exact: true });
  await expect(header.locator('time')).toHaveText(auctionDay);
  await expect(header.locator('time')).toHaveAttribute('datetime', auctionDay);
  await expect(header.locator('.ty-auction-category')).toHaveText('涨停');
  await expect(column(page).locator('.ty-auction-node-head > button')).toHaveText(['9:15','9:20','9:25','当前','竞价涨幅']);
  await expect(column(page).getByText('封单来源与采集时间',{exact:true})).toHaveCount(0);
  const headGeometry = await column(page).locator('.ty-auction-node-head').evaluate(el=>({height:el.getBoundingClientRect().height,gap:el.getBoundingClientRect().top-el.previousElementSibling!.getBoundingClientRect().bottom}));
  expect(headGeometry.height).toBeLessThanOrEqual(28);
  expect(headGeometry.gap).toBeLessThanOrEqual(1);
  const first = realRows(page).first();
  await expect(first).toContainText('合成封单01'); await expect(first).toContainText('合成机器人'); await expect(first).toContainText('合成视觉'); await expect(first).toContainText('3板');
  await expect(first.locator('.ty-auction-values > span')).toHaveText(['1.00', '12.00', '—', '12.50', '+9.95%']);
  await expect(page.getByRole('tab', { name: '三个节点封单', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('img', { name: '三个节点的封单金额对比', exact: true }).locator('rect')).toHaveCount(5);
  await expect(page.locator('.ty-auction-metrics')).toContainText(/竞价成交额\s*—/);
  await expect(page.locator('.ty-auction-metrics')).toContainText(/竞价涨幅\s*—/);
  await expect(column(page).getByLabel(`${auctionDay}封单来源`)).toHaveAttribute('title',/短线侠当前表/);
  await expect(page.locator('.ty-auction-watchlist,.ty-auction-bottom,.ty-ai-follow')).toHaveCount(0);
  await expect(page.getByRole('heading',{name:'竞价封单观察',exact:true})).toBeVisible();
  await page.getByRole('heading', { name: '多日封单对照', exact: true }).click();
  await page.screenshot({ path: test.info().outputPath('auction-0920-synthetic-1440.png'), fullPage: true }); safe(state);
});

test('mouse wheel reaches every stock without pagination and selection reveals the same stock across days', async ({ page }) => {
  await page.setViewportSize({width:1440,height:600});
  const state = await installAuction(page); await openAuction(page);
  await expect(realRows(page)).toHaveCount(10);
  await expect(column(page).getByRole('button', { name: `${auctionDay}下一页封单` })).toHaveCount(0);
  const list = column(page).getByLabel(`${auctionDay}封单滚动列表`);
  await list.hover(); await page.mouse.wheel(0, 700);
  await expect.poll(() => list.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  await expect(column(page, '2026-10-23').locator('ol')).toHaveJSProperty('scrollTop', 0);
  await expect(realRows(page).nth(8)).toContainText('合成封单10');
  await expect(realRows(page).last()).toContainText('合成封单09');
  await expect(realRows(page).last().locator('.ty-auction-values > span').nth(3)).toHaveText('—');
  await expect(realRows(page).nth(8).locator('.ty-auction-rank')).toHaveText('9');
  await column(page).getByRole('button', { name: /^合成封单10/ }).click();
  await expect(page).toHaveURL(/stock=999910\.SZ/);
  await expect(page.locator('.ty-auction-detail')).toContainText('合成封单10');
  await expect(realRows(page).filter({ has: page.getByRole('button', { name: /^合成封单10/ }) })).toHaveAttribute('data-current', 'true');
  await expect.poll(() => column(page, '2026-10-23').locator('ol').evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  await column(page).getByRole('button',{name:`按${auctionDay}竞价涨幅排序`,exact:true}).click();
  await expect(realRows(page).first()).toContainText('合成封单01');
  for (const [index, target] of ['.ty-stock-label', '.ty-tag', '.ty-auction-values > span', '.ty-auction-rank'].entries()) {
    const row = realRows(page).nth(index + 1);
    await expect(row.getByRole('button')).toHaveCount(1);
    await row.locator(target).first().click();
    await expect(row).toHaveAttribute('data-current', 'true');
    await expect(row.getByRole('button')).toHaveAttribute('aria-pressed', 'true');
    await expect(page).toHaveURL(/view=auction/);
  }
  const firstAction = realRows(page).first().getByRole('button');
  await firstAction.focus(); await page.keyboard.press('Tab');
  await expect(realRows(page).nth(1).getByRole('button')).toBeFocused();
  await page.keyboard.press('Space');
  await expect(realRows(page).nth(1)).toHaveAttribute('data-current', 'true');
  const third = realRows(page).nth(2);
  await third.getByRole('button').click({position:{x:2,y:2}});
  await expect(third).toHaveAttribute('data-current', 'true');
  safe(state);
});

test('historical tables retain their own receipt, all three stages and actual matched results on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const state = await installAuction(page); await openAuction(page, '2026-10-23');
  await expect(column(page,'2026-10-23').getByRole('button',{name:'按2026-10-23 09:25封单排序',exact:true})).toHaveAttribute('aria-pressed','true');
  const history = column(page, '2026-10-23');
  await expect(history.getByLabel('2026-10-23封单来源')).toHaveAttribute('title','短线侠历史表 · 采集 2026-10-23 09:25:06');
  await expect(realRows(page, '2026-10-23').first().locator('.ty-auction-values > span')).toHaveText(['1.00', '12.00', '20.00', '+9.95%']);
  await expect(page.locator('.ty-auction-metrics')).toContainText(/竞价成交额\s*0\.12\s*亿/);
  await page.getByRole('tab', { name: '实际竞价成交 · 跨日比较', exact: true }).click();
  await expect(page.getByRole('img', { name: '实际竞价成交金额跨日比较，单位亿元', exact: true })).toBeVisible();
  await page.getByRole('heading', { name: '多日封单对照', exact: true }).click();
  await page.screenshot({ path: test.info().outputPath('auction-history-synthetic-390.png'), fullPage: true }); safe(state);
});

test('previous first-board state and matched trades remain visible when auction seals are absent', async ({ page }) => {
  const state = await installAuction(page); state.previousPoolOnly = true; await openAuction(page);
  const previous = page.getByRole('table', { name: '所选股票跨日状态' }).getByRole('row').filter({ hasText: '10/23' });
  await expect(previous).toContainText('涨停 · 1板');
  await expect(previous).toContainText('首封 09:30');
  await expect(previous).toContainText('0.123亿');
  await expect(previous.getByRole('cell',{name:'该日无已存竞价封单样本',exact:true})).toHaveAttribute('title','该日无已存竞价封单样本');
  // Daily closing seal never becomes a missing auction bar.
  await expect(page.getByRole('img', { name: '三个节点的封单金额对比', exact: true }).locator('rect')).toHaveCount(2);
  safe(state);
});

test('visible auction reads replace the saved snapshot as the next supplier stage arrives', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-26T01:20:46Z') });
  const state = await installAuction(page); await openAuction(page);
  await expect(realRows(page).first().locator('.ty-auction-values > span').nth(2)).toHaveText('—');
  const before = state.reads;
  state.after0925 = true;
  await page.clock.fastForward(270_000);
  await expect.poll(() => state.reads).toBeGreaterThan(before);
  await expect(realRows(page).first().locator('.ty-auction-values > span').nth(2)).toHaveText('20.00');
  await expect(column(page).getByLabel(`${auctionDay}封单来源`)).toHaveAttribute('title',/09:25:06/);
  await expect(column(page).getByRole('button',{name:`按${auctionDay} 09:25封单排序`,exact:true})).toHaveClass(/ty-gold/);
  safe(state);
});

test('current seal refreshes within ten seconds during auction without filling a future stage', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-26T01:20:46Z') });
  const state = await installAuction(page); await openAuction(page);
  await expect(realRows(page).first().locator('.ty-auction-values > span').nth(3)).toHaveText('12.50');
  const before = state.reads; state.rollingIncrement = 1;
  await page.clock.fastForward(10_000);
  await expect.poll(() => state.reads).toBeGreaterThan(before);
  await expect(realRows(page).first().locator('.ty-auction-values > span').nth(3)).toHaveText('13.50');
  await expect(realRows(page).first().locator('.ty-auction-values > span').nth(2)).toHaveText('—');
  await expect(column(page).getByLabel(`${auctionDay}封单来源`)).toHaveAttribute('title',/09:20:56/);
  safe(state);
});


test('auction uses direct yesterday-ladder filtering without the removed tools or duplicate rankings', async ({page}) => {
  const state=await installAuction(page);await openAuction(page);
  for(const selector of ['.page-outline','.ty-auction-toolbar','.ty-data-timestamp','.ty-observation-ranking','.ty-coverage','.ty-auction-comparison select']) {
    await expect(page.locator(selector)).toHaveCount(0);
  }
  await expect(page.getByText('节点增减',{exact:true})).toHaveCount(0);
  await expect(page.getByText('竞价成交筛选 · 完整已保存样本',{exact:true})).toHaveCount(0);
  await expect(page.getByRole('heading',{name:'观察日竞价榜单',exact:true})).toHaveCount(0);
  const all=await realRows(page).count();
  await page.getByRole('button',{name:'昨日连板',exact:true}).press('Enter');
  await expect(page.getByRole('button',{name:'昨日连板',exact:true})).toHaveAttribute('aria-pressed','true');
  const fixture=liveAuctionFixture(new URLSearchParams({day:auctionDay}),false,0);
  for(const entry of fixture.auction_days.slice(0, 2)) {
    const names=entry.items.filter(stock=>(stock.previous_height??0)>=2).map(stock=>stock.name).sort();
    await expect.poll(async()=> (await realRows(page,entry.day).locator('.ty-stock-label').allTextContents()).sort()).toEqual(names);
  }
  expect(await realRows(page).count()).toBeLessThan(all);
  expect(await realRows(page).count()).toBeGreaterThan(0);
  await page.getByRole('group',{name:'竞价榜单范围'}).getByRole('button',{name:'封单金额',exact:true}).click();
  await expect(realRows(page)).toHaveCount(all);
  await page.getByRole('button',{name:'按竞价成交额排序',exact:true}).click();
  await expect(page.getByRole('button',{name:'按竞价成交额排序',exact:true})).toHaveAttribute('aria-pressed','true');
  safe(state);
});

test('auction retains the required demo disclosure after real-source badges are removed', async ({page}) => {
  const state=await installAuction(page,'demo');await openAuction(page);
  await expect(page.locator('.ty-source-badge')).toHaveText('演示环境 · 非真实行情');
  await expect(page.getByRole('link',{name:/当前运行/})).toHaveCount(0);
  safe(state);
});


test('stage bulletin replaces its prior content, survives reload, and keeps stock selection inside auction',async({page})=>{
  await page.clock.install({time:new Date('2026-10-26T01:15:06Z')});
  const state=await installAuction(page);state.referenceStage='09:15';await openAuction(page);
  const report=page.locator('.ty-auction-reference');
  await expect(report.locator('.ty-panel-action')).toContainText(`${auctionDay} 09:15`);
  await expect(report.getByLabel('三阶段封单')).toContainText('100.00亿');
  await expect(report.getByLabel('三阶段封单')).not.toContainText('80.00亿');
  await expect(report.getByRole('heading',{name:'封单减少',exact:true})).toHaveCount(0);
  state.referenceStage='09:20';await page.clock.fastForward(300_000);
  await expect(report.locator('.ty-panel-action')).toContainText(`${auctionDay} 09:20`);
  await expect(report.getByLabel('三阶段封单')).toContainText('80.00亿');
  await expect(report.getByLabel('三阶段封单')).not.toContainText('90.00亿');
  state.referenceStage='09:25';await page.clock.fastForward(300_000);
  await expect(report.locator('.ty-panel-action')).toContainText(`${auctionDay} 09:25`);
  await expect(report.getByLabel('三阶段封单')).toContainText('90.00亿');
  await expect(page.getByRole('heading',{name:'竞价封单观察',exact:true})).toHaveCount(1);
  await page.reload();await expect(report.locator('.ty-panel-action')).toContainText(`${auctionDay} 09:25`);
  await report.getByRole('button',{name:'合成封单02',exact:true}).first().click();
  await expect(page).toHaveURL(/view=auction.*stock=999902.SZ/);
  await expect(page.locator('.ty-auction-detail')).toContainText('合成封单02');safe(state);
});

test('stored previous-day bulletin stays until a new successful stage, failures retain it and revoked rights clear it',async({page})=>{
  await page.clock.install({time:new Date('2026-10-26T01:15:06Z')});
  const state=await installAuction(page);state.referenceDay='2026-10-23';state.referenceStage='09:25';await openAuction(page);
  const report=page.locator('.ty-auction-reference');
  await expect(report.locator('.ty-panel-action')).toContainText('2026-10-23 09:25');
  state.referenceFailure=true;await page.clock.fastForward(6_000);
  await expect(report.getByRole('button',{name:'重新读取封单观察',exact:true})).toBeVisible();
  await expect(report.locator('.ty-panel-action')).toContainText('2026-10-23 09:25');
  state.referenceFailure=false;state.referenceDay=auctionDay;state.referenceStage='09:15';
  await report.getByRole('button',{name:'重新读取封单观察',exact:true}).click();
  await expect(report.locator('.ty-panel-action')).toContainText(`${auctionDay} 09:15`);
  state.referenceDenied=true;await page.clock.fastForward(6_000);
  await expect(report).toContainText('竞价封单来源未启用或读取权限不可用');
  await expect(report.locator('.ty-panel-action')).toHaveCount(0);
  await expect(report.getByLabel('三阶段封单')).toHaveCount(0);safe(state);
});

test('auction fills the desktop viewport and removes lower AI, watch pool and tracking timeline',async({page})=>{
  const state=await installAuction(page);state.referenceStage='09:25';await openAuction(page);
  for(const [width,height] of [[1920,1080],[1440,900],[1366,768],[1024,768],[390,844]]){
    await page.setViewportSize({width,height});
    await expect(page.locator('.ty-auction-bottom,.ty-auction-watchlist,.ty-ai-follow,.ty-auction-bottom .ty-ai-overview')).toHaveCount(0);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
    if(width>1150){
      const board=await page.locator('.ty-auction-support-workspace').boundingBox(),rail=await page.locator('.ty-auction-rail').boundingBox();
      expect(board!.y+board!.height).toBeLessThanOrEqual(height);
      expect(board!.y+board!.height).toBeGreaterThan(height-15);
      expect(Math.abs(board!.y+board!.height-rail!.y-rail!.height)).toBeLessThan(2);
      const report=await page.locator('.ty-auction-reference').boundingBox();
      expect(Math.abs(board!.y+board!.height-report!.y-report!.height)).toBeLessThan(2);
      const list=await column(page).locator('ol').boundingBox();
      const support=await page.locator('.ty-auction-support').boundingBox();
      expect(support!.y).toBeGreaterThan(list!.y+list!.height);
      if(width>=1920) expect(await page.locator('.ty-auction-rankings .ty-support-scroll').evaluate(element=>element.scrollWidth<=element.clientWidth+1)).toBe(true);
    }
    await page.screenshot({path:test.info().outputPath(`auction-layout-${width}.png`),fullPage:width<=1024});
  }
  safe(state);
});

test('compact bulletin stays under node detail with eleven reachable stocks and a full width right rail',async({page})=>{
  const state=await installAuction(page);state.referenceStage='09:25';state.denseReference=true;await openAuction(page);
  const report=page.locator('.ty-auction-reference'),rows=report.locator('.ty-auction-reference-stocks tbody tr');
  await expect(rows).toHaveCount(11);
  await expect(report.getByLabel('题材封单居前')).toHaveCount(0);
  await expect(report.locator('.ty-auction-reference-themes')).toHaveCount(0);
  await expect(rows.first().locator('.ty-auction-reference-tags')).toContainText('合成超长题材名称用于核对完整信息');
  await expect(rows.nth(0).locator('td').nth(1)).toHaveText('—');
  await expect(rows.nth(1).locator('td').nth(1)).toHaveText('0.00');
  await expect(rows.nth(10)).toContainText('合成封单11');
  await rows.nth(10).locator('td').nth(1).focus();
  await expect(rows.nth(10).locator('td').nth(1)).toHaveAttribute('title','09:20 11.00亿 → 09:25 10.00亿');
  await expect(rows.nth(10).getByRole('button',{name:'合成封单11',exact:true})).toHaveAttribute('title',/合成超长题材名称用于核对完整信息/);
  for(const [width,height] of [[1920,1080],[1440,900],[1366,768]]){
    await page.setViewportSize({width,height});
    await page.locator('.ty-auction-detail-stack').evaluate(element=>element.scrollTop=0);
    const detail=await page.locator('.ty-auction-detail').boundingBox(),bulletin=await report.boundingBox();
    expect(bulletin!.y).toBeGreaterThanOrEqual(detail!.y+detail!.height);
    expect(Math.abs(detail!.width-bulletin!.width)).toBeLessThan(2);
    expect(Math.abs(detail!.x-bulletin!.x)).toBeLessThan(2);
    await expect(page.locator('.ty-auction-rail> :not(.ty-auction-detail-stack)')).toHaveCount(0);
    await expect(page.getByRole('table',{name:'所选股票跨日状态'}).locator('tbody tr')).toHaveCount(4);
    await expect(page.locator('.ty-auction-metrics>span')).toHaveCount(4);
    await expect.poll(()=>page.locator('.ty-auction-detail').evaluate(element=>
      element.scrollHeight<=element.clientHeight+1 && element.lastElementChild!.getBoundingClientRect().bottom<=innerHeight
    )).toBe(true);
    await report.locator('.ty-auction-reference-receipt').scrollIntoViewIfNeeded();
    await expect.poll(()=>report.evaluate(element=>{
      const body=element.querySelector('.ty-auction-reference-body')!,last=element.querySelector('.ty-auction-reference-receipt')!;
      const rowRects=[...element.querySelectorAll('tbody tr')].map(row=>row.getBoundingClientRect());
      const rect=element.getBoundingClientRect();
      return body.scrollHeight<=body.clientHeight+1 && rowRects.every(row=>row.top>=rect.top && row.bottom<=rect.bottom)
        && last.getBoundingClientRect().bottom<=innerHeight && document.documentElement.scrollHeight<=innerHeight+1;
    })).toBe(true);
    await page.screenshot({path:test.info().outputPath(`auction-detail-${width}.png`)});
  }
  state.denseReference=false;await page.reload();
  await expect(rows).toHaveCount(5);
  await expect(report.locator('.ty-auction-reference-stocks').getByRole('button',{name:'合成封单01',exact:true})).toHaveCount(1);
  safe(state);
});

test('support filters and ascending sorting preserve zero and keep missing values last', async({page})=>{
  const state=await installAuction(page);await openAuction(page);
  const table=page.getByRole('table',{name:'竞价承接个股',exact:true}),rows=table.locator('tbody tr');
  await expect(rows).toHaveCount(7);
  await expect(rows.first()).toContainText('合成封单06');
  await table.getByRole('button',{name:'竞价承接',exact:true}).click();
  await expect(rows.first()).toContainText('合成封单01');
  await expect(rows.last()).toContainText('合成封单07');
  await expect(rows.first().locator('td').nth(4)).toHaveText('0.00%');
  await page.getByRole('group',{name:'竞价承接',exact:true}).getByLabel('<5%',{exact:true}).uncheck();
  await expect(rows).toHaveCount(5);
  await expect(rows.filter({hasText:'合成封单07'})).toHaveCount(0);
  await page.getByRole('button',{name:'重置筛选',exact:true}).click();
  await page.getByRole('group',{name:'涨停板块',exact:true}).getByLabel('合成芯片 (3)',{exact:true}).check();
  await expect(rows).toHaveCount(3);
  await page.reload();await expect(rows).toHaveCount(3);
  await expect(table.getByRole('columnheader',{name:'竞价承接',exact:true})).toHaveAttribute('aria-sort','ascending');
  safe(state);
});

test('ranking categories switch locally and stock selection keeps auction detail and bulletin in place',async({page})=>{
  const state=await installAuction(page);await openAuction(page);
  const panel=page.locator('.ty-auction-rankings'),reads=state.supportReads;
  await panel.getByRole('button',{name:'昨炸板',exact:true}).click();
  await expect(panel.locator('tbody tr')).toHaveCount(1);
  await panel.getByRole('button',{name:'合成封单08',exact:true}).click();
  await expect(page).toHaveURL(/view=auction.*stock=999908.SZ/);
  await expect(page.locator('.ty-auction-detail')).toContainText('合成封单08');
  await panel.getByRole('button',{name:'竞价跌停',exact:true}).click();
  await expect(panel.locator('tbody tr')).toHaveCount(1);
  await expect(panel.locator('tbody tr')).toContainText('合成封单03');
  await panel.getByRole('button',{name:'竞价净额',exact:true}).click();
  await expect(panel).toContainText('已接入来源未提供竞价净额');
  await expect(panel.locator('tbody tr')).toHaveCount(0);
  expect(state.supportReads).toBe(reads);
  await expect(page.getByRole('heading',{name:'竞价封单观察',exact:true})).toBeVisible();safe(state);
});

test('support exposes original listing reasons and lets users choose columns',async({page})=>{
  const state=await installAuction(page);await openAuction(page);
  const table=page.getByRole('table',{name:'竞价承接个股',exact:true});
  await table.getByRole('button',{name:'查看',exact:true}).click();
  const listing=page.getByRole('dialog',{name:'合成封单01昨日龙虎榜'});
  await expect(listing).toContainText('2026-10-23');
  await expect(listing).toContainText('合成上榜原因一');await expect(listing).toContainText('合成上榜原因二');
  await page.keyboard.press('Escape');await expect(listing).toHaveCount(0);
  await page.getByLabel('自定义显示列',{exact:true}).click();
  await page.locator('.ty-support-columns').getByLabel('人气排名',{exact:true}).uncheck();
  await expect(table.getByRole('columnheader',{name:'人气排名',exact:true})).toHaveCount(0);
  await page.reload();await expect(table.getByRole('columnheader',{name:'人气排名',exact:true})).toHaveCount(0);safe(state);
});

test('downside columns keep missing seals empty without percentage or daily-seal substitution', async({page}) => {
  const state=await installAuction(page);await openAuction(page);
  await expect(page.locator('.ty-auction-days > .ty-auction-day')).toHaveCount(4);
  const down=page.getByRole('region',{name:`${auctionDay}跌停竞价对照`,exact:true});
  await expect(down.getByRole('button',{name:'封单金额',exact:true})).toHaveCount(0);
  await expect(down.getByLabel(`${auctionDay}跌停汇总`)).toContainText('2只封单 —');
  await expect(down.getByLabel(`${auctionDay}跌停汇总`)).toHaveAttribute('title',/09:25卖一封单/);
  await expect(down.getByRole('button',{name:'切换跌停节点指标',exact:true})).toHaveCount(0);
  const header = down.getByRole('heading', { name: `${auctionDay}跌停`, exact: true });
  await expect(header.locator('time')).toHaveText(auctionDay);
  await expect(header.locator('time')).toHaveAttribute('datetime', auctionDay);
  await expect(header.locator('.ty-auction-category')).toHaveText('跌停');
  await expect(down.locator('.ty-auction-node-head')).toHaveText('9:159:209:25涨幅');
  await expect(down.locator('li').first()).toContainText('合成跌停1');
  await expect(down.locator('li').first().locator('.ty-auction-values > span')).toHaveText(['—','—','—','-10.00%']);
  await expect(down).not.toContainText('日榜封单');
  await expect(down).not.toContainText('节点涨幅');
  await down.getByRole('button',{name:`按${auctionDay}跌停当前涨幅排序`,exact:true}).click();
  await down.getByRole('button',{name:`按${auctionDay}跌停当前涨幅排序`,exact:true}).click();
  await expect(down.locator('li').first()).toContainText('合成跌停2');
  const upColor=await column(page).locator('h3').evaluate(element=>getComputedStyle(element).backgroundColor);
  const downColor=await down.locator('h3').evaluate(element=>getComputedStyle(element).backgroundColor);
  expect(upColor).not.toBe(downColor);
  await down.getByRole('button',{name:'合成跌停2',exact:true}).click();
  await expect(page).toHaveURL(/view=auction/);await expect(page).toHaveURL(/stock=999911\.SZ/);
  await expect(page.locator('.ty-auction-detail')).toContainText('合成跌停2');
  await expect(down.locator('li[data-current]')).toHaveCount(1);
  const other = down.locator('li').nth(1);
  await expect(other.getByRole('button')).toHaveCount(1);
  await other.locator('.ty-tag').click();
  await expect(other).toHaveAttribute('data-current','true');
  await expect(page).toHaveURL(/stock=999910\.SZ/);
  await expect(page.locator('.ty-auction-detail .ty-title-code')).toHaveText('999910');
  await down.locator('li').first().getByRole('button').focus();
  await page.keyboard.press('Tab');
  await expect(other.getByRole('button')).toBeFocused();
  safe(state);
});

test('native downside seals sort as money and headers align with rows including the scrollbar gutter',async({page})=>{
  const state=await installAuction(page);state.downSeals=true;state.denseDown=true;await openAuction(page);
  const down=page.getByRole('region',{name:`${auctionDay}跌停竞价对照`,exact:true});
  await expect(down.locator('li').first()).toContainText('合成跌停24');
  await expect(down.locator('li').first().locator('.ty-auction-values > span')).toHaveText(['2.40','4.80','7.20','-5.00%']);
  await expect(down.getByLabel(`${auctionDay}跌停汇总`)).toContainText('封单 90亿');
  await down.getByRole('button',{name:`按${auctionDay} 09:25跌停封单排序`,exact:true}).click();
  await expect(down.locator('li').first()).toContainText('合成跌停1');
  for(const [width,height] of [[1920,1080],[1440,900],[1366,768]]){
    await page.setViewportSize({width,height});
    const alignment=await page.locator('.ty-auction-days > .ty-auction-day').evaluateAll(days=>days.map(day=>{
      const list=day.querySelector('.ty-auction-stock-list')!;
      const headers=[...day.querySelectorAll('.ty-auction-node-head > button')];
      const values=[...day.querySelectorAll('li:not(.ty-auction-blank) .ty-auction-values')][0];
      const cells=[...values.children];
      return {scrolls:list.scrollHeight>list.clientHeight,gutter:list.getBoundingClientRect().width-list.clientWidth,
        deltas:headers.map((head,i)=>head.getBoundingClientRect().right-cells[i].getBoundingClientRect().right),
        textDeltas:headers.map(head=>{const node=[...head.childNodes].find(n=>n.nodeType===Node.TEXT_NODE)!;
          const range=document.createRange();range.selectNodeContents(node);
          return range.getBoundingClientRect().right-head.getBoundingClientRect().right;})};
    }));
    expect(alignment.every(day=>day.deltas.every(delta=>Math.abs(delta)<1))).toBe(true);
    expect(alignment.every(day=>day.textDeltas.every(delta=>Math.abs(delta)<1))).toBe(true);
    expect(alignment.slice(2).every(day=>day.scrolls)).toBe(true);
    expect(alignment.every(day=>day.gutter>0)).toBe(true);
  }
  safe(state);
});

test('live downside amounts refresh independently of frozen nodes and original seal-table columns',async({page})=>{
  const state=await installAuction(page);state.bookRevision=1;await openAuction(page);
  const down=page.getByRole('region',{name:`${auctionDay}跌停竞价对照`,exact:true});
  const upBefore=await column(page).locator('li').first().textContent();
  await expect(down.locator('li').first()).toContainText('合成跌停2');
  await expect(down.locator('li').first().locator('.ty-auction-board')).toHaveText('2.00亿');
  await expect(down.locator('li').first().locator('.ty-auction-values>span')).toHaveText(['0.20','—','—','-5.00%']);
  await expect(down.getByLabel(`${auctionDay}跌停汇总`)).toContainText('封单 3亿');
  await expect(down.getByLabel(`${auctionDay}跌停汇总`)).toHaveAttribute('title',/合成竞价盘口.*有效盘口 2\/6186/);
  state.bookRevision=2;await page.evaluate(()=>window.dispatchEvent(new Event('read-cache-revalidate')));
  await expect(down.locator('li').first().locator('.ty-auction-board')).toHaveText('4.00亿');
  await expect(down.locator('li').first().locator('.ty-auction-values>span')).toHaveText(['0.20','—','—','-5.00%']);
  await expect(column(page).locator('li').first()).toHaveText(upBefore);
  await down.getByRole('button',{name:`按${auctionDay}当前跌停封单排序`,exact:true}).click();
  await expect(down.locator('li').first()).toContainText('合成跌停1');
  safe(state);
});

test('stale auction book is shown as missing rather than zero realtime limit-down stocks',async({page})=>{
  const state=await installAuction(page);state.bookRevision=-1;await openAuction(page);
  const down=page.getByRole('region',{name:`${auctionDay}跌停竞价对照`,exact:true});
  await expect(down).toContainText('盘口更新中');
  await expect(down.getByLabel(`${auctionDay}跌停汇总`)).toContainText('—只');
  await expect(down.locator('li')).toHaveCount(0);safe(state);
});

test('same-day recovery keeps its actual receipt and partial books never claim zero',async({page})=>{
  const state=await installAuction(page);state.referenceRecovered=true;state.referenceStage='09:25';state.bookRevision=1;state.partialBook=true;
  await openAuction(page);
  const report=page.locator('.ty-auction-reference');await expect(report).toContainText(`${auctionDay} 09:25 · 补采`);
  await expect(report.locator('.ty-auction-reference-receipt')).toContainText('11:34 接收');
  const down=page.getByRole('region',{name:`${auctionDay}跌停竞价对照`,exact:true});
  await expect(down).toContainText('竞价盘口覆盖未齐');await expect(down.getByLabel(`${auctionDay}跌停汇总`)).toContainText('—只');
  await expect(down.locator('li')).toHaveCount(0);safe(state);
});

test('late trade-day data replaces both empty downside columns without leaving an orphan', async({page}) => {
  const state=await installAuction(page);state.coldEmpty=true;
  await page.goto(`/workbench?view=auction&stock=${auctionCode}`);
  await page.getByLabel('账号',{exact:true}).fill('synthetic');
  await page.getByLabel('密码',{exact:true}).fill('synthetic-only');
  await page.getByRole('button',{name:'进入工作台'}).click();
  await expect(page.getByRole('heading',{name:'多日封单对照',exact:true})).toBeVisible();
  await expect(page.locator('.ty-auction-days > section')).toHaveCount(4);
  state.coldEmpty=false;
  await page.evaluate(()=>window.dispatchEvent(new Event('read-cache-revalidate')));
  await expect(column(page).getByRole('button',{name:/^合成封单01/})).toBeVisible();
  await expect(page.locator('.ty-auction-days > section')).toHaveCount(4);
  await expect(page.locator('.ty-auction-days > .is-down-day')).toHaveCount(2);
  safe(state);
});
