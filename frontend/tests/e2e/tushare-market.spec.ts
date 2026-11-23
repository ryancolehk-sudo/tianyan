import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';
import { openLegacyWorkbench } from './legacyWorkbench';

const id='00000000-0000-4000-8000-000000000019';
const meta={request_id:id,app_mode:'demo',generated_at:'2026-10-23T02:40:00Z',as_of_time:null,quality:{status:'partial',reasons:['Synthetic fixtures only']}};
const point={id,received_at:'2026-10-23T02:40:00Z',limit_up:18,limit_down:55,advancing:400,declining:5000,
  returned:5594,comparable:5573,unknown:21,old_day:21,fresh:5500,source_time_min:'2026-10-23T02:38:00Z',source_time_max:'2026-10-23T02:39:58Z',limit_evidence_id:id};
const paid={meta,data:{observation_day:'2026-10-23',timezone:'Asia/Shanghai',time_basis:'received_at',source_name:'Tushare · 合成批量行情',
  source_family:'tushare_market_rt',scope:'合成可比样本，非真实行情',items:[point],latest:point,truncated:false}};
const free={id,source_id:id,source_name:'合成免费备用来源',family:'ashare_breadth',source_key:'reference:market.ashare.breadth',instrument_id:null,
  received_at:meta.generated_at,published_at:null,content_hash:'0'.repeat(64),payload:{dataset:'market.ashare.breadth',title:'合成备用参考',source_url:'https://example.com/synthetic',
  attribution:'Synthetic',scope:'合成旧参考',columns:[{key:'limit_up',label:'涨停'},{key:'limit_down',label:'跌停'}],rows:[{limit_up:'18',limit_down:'68'}],observed_day:null}};

test('paid exact-limit counts beat conflicting free counts on both pages',async({page},info)=>{
  await page.route('**/api/v1/market-breadth-history*',route=>route.fulfill({json:paid}));
  await page.route('**/api/v1/reference-datasets?dataset=market.ashare.breadth',route=>route.fulfill({json:{meta,data:{items:[free]}}}));
  await page.goto('/workbench?legacy=1');await signIn(page);await page.locator('.workbench-market-history > summary').click();
  const tile=page.getByLabel('市场广度参考',{exact:true});
  await expect(tile.getByTestId('breadth-limit_down')).toHaveText('55');
  await expect(tile.getByTestId('breadth-limit_up')).toHaveText('18');
  await expect(tile).toContainText('Tushare · 可比 5573 / 未判定 21');
  await expect(page.getByTestId('breadth-history-limit_down')).toHaveText('55');
  await expect(page.getByText('旧免费数据不接入此曲线，不补造过去时点。',{exact:false})).toBeVisible();
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath(`tushare-counts-${width}.png`),animations:'disabled'});
  }
  await page.setViewportSize({width:1440,height:1000});
  await openLegacyWorkbench(page, '竞价观察');
  await expect(tile.getByTestId('breadth-limit_down')).toHaveText('55');
});

test('unknown daily limits stay blank instead of borrowing free or previous-day counts',async({page})=>{
  const unknown={...point,limit_up:null,limit_down:null,comparable:0,unknown:5594};
  await page.route('**/api/v1/market-breadth-history*',route=>route.fulfill({json:{...paid,data:{...paid.data,items:[unknown],latest:unknown}}}));
  await page.route('**/api/v1/reference-datasets?dataset=market.ashare.breadth',route=>route.fulfill({json:{meta,data:{items:[free]}}}));
  await page.goto('/auction?legacy=1');await signIn(page);
  const tile=page.getByLabel('市场广度参考',{exact:true});
  await expect(tile.getByTestId('breadth-limit_down')).toHaveText('—');
  await expect(tile).toContainText('Tushare · 可比 0 / 未判定 5594');
});

test('old morning history is independently accessible and slow responses cannot switch sources',async({page},info)=>{
  const oldPoint={...point,id:'00000000-0000-4000-8000-000000000020',received_at:'2026-10-23T01:35:00Z',limit_up:7,limit_down:12};
  const legacy={...paid,data:{...paid.data,source_family:'ashare_breadth',source_name:'合成旧来源',items:[oldPoint],latest:null}};
  let hold=false;let pending=false;let release:()=>void=()=>{};
  await page.route('**/api/v1/market-breadth-history*',async route=>{
    const old=new URL(route.request().url()).searchParams.get('source')==='ashare_breadth';
    if(old && hold){pending=true;await new Promise<void>(resolve=>{release=resolve;});}
    await route.fulfill({json:old?legacy:paid});
  });
  await page.goto('/workbench?legacy=1');await signIn(page);await page.locator('.workbench-market-history > summary').click();
  const chart=page.getByRole('region',{name:'涨跌停数量走势',exact:true});
  const choose=chart.getByLabel('涨跌停走势来源',{exact:true});
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('55');
  await choose.selectOption('ashare_breadth');
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('12');
  await expect(chart).toContainText('09:35:00');
  await expect(page.getByTestId('breadth-limit_down')).toHaveText('55');
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});await chart.scrollIntoViewIfNeeded();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath(`old-history-${width}.png`),animations:'disabled'});
  }
  await page.reload();await page.locator('.workbench-market-history > summary').click();await expect(choose).toHaveValue('ashare_breadth');
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('12');
  await choose.selectOption('tushare_market_rt');await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('55');
  hold=true;await choose.selectOption('ashare_breadth');await expect.poll(()=>pending).toBe(true);
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('—');
  await choose.selectOption('tushare_market_rt');await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('55');
  await Promise.all([page.waitForResponse(r=>r.url().includes('source=ashare_breadth')),Promise.resolve().then(()=>release())]);
  await expect(choose).toHaveValue('tushare_market_rt');
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('55');
});
