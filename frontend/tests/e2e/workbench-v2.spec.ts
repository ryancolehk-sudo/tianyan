import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
const id='00000000-0000-4000-8000-000000000071';
const meta={app_mode:'demo',request_id:id,generated_at:'2026-10-23T13:42:00+08:00',quality:{status:'partial',reasons:['Synthetic layout fixtures only']}};
const stock=(i:number)=>({instrument_id:'00000000-0000-4000-8000-'+String(i+100).padStart(12,'0'),ts_code:String(600000+i)+'.SH',name:['合成机电','合成科技','合成通信','合成智造','合成材料','合成装备','合成电力','合成算力'][i],pct_change:10-i*.72,price:12+i,amount_cny:(i+1)*1.8e9,change_5m:i<5?2.3-i*.4:null,theme:i<4?'合成机器人':'合成算力',sector_code:i<4?'BK0001':'BK0002',previous_height:i<2?4:i<4?3:2,current_state:'涨停价 · 盘口待核',turnover_rate:12.8+i,seal_amount_cny:null,first_observed_at:null,last_change_at:null,source_time:'2026-10-23T13:42:00+08:00',received_at:'2026-10-23T13:42:06+08:00',auction_amount_cny:(i+1)*2.3e8,auction_pct_change:3.2+i,auction_price:10+i,node_0915_cny:null,node_0920_cny:null,node_0925_cny:null,net_cny:2e8-i*1e7,fund_trade_day:'2026-10-23'});
const sectors=Array.from({length:8},(_,i)=>({code:'BK'+String(i+1).padStart(4,'0'),name:['合成机器人','合成算力','合成芯片','合成通信','合成电力','合成医药','合成银行','合成光伏'][i],net_cny:(5-i)*7.1e8,pct_change:2.8-i*.6,change_5m:null,rank:i+1,rank_change:null,trade_day:'2026-10-23'}));
function fixture(query:URLSearchParams){
 const stocks=Array.from({length:8},(_,i)=>stock(i));
 const selected=stocks.find(s=>s.instrument_id===query.get('instrument_id'))??stocks[0];
 return {view:query.get('view')??'overview',trade_day:query.get('trade_day')??'2026-10-23',available_days:['2026-10-23','2026-10-22','2026-10-21','2026-10-20'],as_of_time:'2026-10-23T13:42:00+08:00',source_label:'Tushare · 合成测试缓存',mode:'review',scope:'仅用于版面验证，不是真实市场数据',indices:[{code:'000001.SH',name:'上证指数',price:3256.18,pct_change:.62},{code:'399001.SZ',name:'深证成指',price:10132.65,pct_change:.88},{code:'399006.SZ',name:'创业板指',price:2018.94,pct_change:1.21}],stats:{advancing:3178,declining:2289,turnover_cny:1.28e12,limit_up:63,limit_down:14,broken_count:27,max_height:5},stocks,sectors,selected_stock:selected,selected_sector:sectors.find(s=>s.code===query.get('sector_code'))??sectors[0],watchlist_ids:[stocks[0].instrument_id],breadth:Array.from({length:80},(_,i)=>({time:new Date(Date.parse('2026-10-23T09:30:00+08:00')+i*60000).toISOString(),up:Math.round(6+i*.7+Math.sin(i/5)*3),down:Math.round(2+i*.12+Math.sin(i/8)*2)})),auction_days:['2026-10-23','2026-10-22','2026-10-21','2026-10-20'].map(day=>({day,rows:stocks})),fund_history:sectors.flatMap((s,i)=>['2026-10-20','2026-10-21','2026-10-22','2026-10-23'].map((day,j)=>({day,sector_code:s.code,net_cny:s.net_cny*(j+1)/4,pct_change:s.pct_change,turnover_cny:1.88e10,limit_up:7}))),broken_stocks:[{...stocks[6],pct_change:-4.32},{...stocks[7],pct_change:-7.86}],capabilities:[{key:'auction_order',status:'unavailable',reason:'三节点封单尚未接入',source_time:null,received_at:null},{key:'fund_intraday',status:'unavailable',reason:'分钟资金待接入',source_time:null,received_at:null}]};
}
async function mock(page:Page,fail=false){
 await page.route('**/api/v1/**',async route=>{
  const url=new URL(route.request().url());
  if(url.pathname.endsWith('/session'))return route.fulfill({json:{meta,data:{user:{id,username:'synthetic',role:'admin'},csrf_token:'fixture'}}});
  if(url.pathname.endsWith('/workbench-view'))return fail?route.fulfill({status:503,json:{error:{message:'合成错误，重试测试'}}}):route.fulfill({json:{meta,data:fixture(url.searchParams)}});
  if(url.pathname.endsWith('/watchlist'))return route.fulfill({json:{meta,data:{items:[{position:0,instrument:{id:stock(0).instrument_id,symbol:'600000',name:stock(0).name,exchange:'SSE',asset_type:'stock'}}]}}});
  if(url.pathname.endsWith('/instruments'))return route.fulfill({json:{meta,data:{items:[{id:stock(1).instrument_id,name:stock(1).name,symbol:'600001',exchange:'SSE',asset_type:'stock'}]}}});
  return route.fulfill({json:{meta,data:{items:[],available_days:[],trade_day:null}}});
 });
}
for(const width of [1920,1672,1440,390])test('fixed four-page layouts '+width,async({page},info)=>{
 test.setTimeout(60000);await mock(page);await page.setViewportSize({width,height:941});
 for(const [view,title] of [['overview','涨跌停家数走势'],['auction','多日封单对照'],['ladder','连板梯队比较'],['funds','板块资金脉络']]){
  await page.goto('/workbench?view='+view);await expect(page.getByRole('heading',{name:title,exact:true})).toBeVisible();await expect(page.locator('.wb-page')).toHaveAttribute('aria-busy','false');
  await expect(page.getByText('演示环境 · 合成样本',{exact:true})).toBeVisible();
  expect(await page.locator('body').evaluate(e=>e.scrollWidth<=innerWidth+2)).toBeTruthy();
  await page.screenshot({path:info.outputPath(view+'-'+width+'.png'),fullPage:true});
 }
});
test('selection follows tabs, refresh, history and the auction alias',async({page})=>{
 await mock(page);await page.goto('/workbench');await page.locator('.wb-bottom-three').getByRole('button',{name:'合成科技',exact:true}).first().click();await expect(page).toHaveURL(/instrument=/);
 await page.getByRole('link',{name:'连板观察',exact:true}).click();await expect(page.locator('.wb-observer')).toContainText('合成科技');await page.reload();await expect(page.locator('.wb-observer')).toContainText('合成科技');
 await page.goBack();await expect(page.getByRole('heading',{name:'个股涨幅榜',exact:true})).toBeVisible();
 await page.goto('/auction?trade_day=2026-10-22');await expect(page.getByRole('link',{name:'竞价观察',exact:true})).toHaveAttribute('aria-current','page');await expect(page.getByLabel('观察交易日')).toHaveValue('2026-10-22');
 await expect(page.locator('.wb-node-values').filter({hasText:'0.00'})).toHaveCount(0);
});
test('daily funds and unavailable intraday remain distinct',async({page})=>{
 await mock(page);await page.goto('/workbench?view=funds');await page.getByRole('button',{name:'合成算力',exact:true}).click();await expect(page).toHaveURL(/sector=BK0002/);
 await expect(page.locator('.wb-observer')).toContainText('合成算力');await page.getByRole('button',{name:'盘中框架 · 待接入',exact:true}).click();await expect(page.getByText('盘中资金曲线待接入',{exact:true})).toBeVisible();await page.getByRole('button',{name:'盘后资金 · 日频',exact:true}).click();await expect(page.getByText('盘中资金曲线待接入',{exact:true})).toHaveCount(0);
});
test('errors retain the four-page structure and offer retry',async({page})=>{
 await mock(page,true);await page.goto('/workbench');await expect(page.getByText(/合成错误，重试测试/)).toBeVisible();await expect(page.getByRole('button',{name:'重试',exact:true})).toBeVisible();await expect(page.getByRole('heading',{name:'全市场观察',exact:true})).toBeVisible();
});