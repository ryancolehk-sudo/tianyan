import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { blank, day, days, listing, meta, raw, metrics, cohort } from './reviewTestFixtures';
import { terminalFixture, terminalDay, stockA, stockB } from './terminalTestFixtures';
import {atlas,panel as rotationPanel} from './rotationTestFixtures';
import type { LadderObservation, LadderRecord } from '../../src/features/terminal/ladderModel';

test.use({ screenshot: 'on' });

// All figures are explicit synthetic fixtures. Supplier access is not tested here.
async function fixture(page: Page, reversedAuctionAmounts = false) {
  const state = { analysis: [] as string[], fullTopTen: false, ladderPending: false, emptyPool: false, previousHeight: 2, noEligibleUp: false, rotationEmptyAndError: false, unknownPreviousHeight: false, ladderReads: 0, reads: [] as string[], writes: [] as Record<string,unknown>[], errors: [] as string[] };
  page.on('pageerror',e=>{state.errors.push(e.message);console.error('Synthetic page failure:',e.stack);});
  await page.addLocatorHandler(page.getByRole('dialog',{name:'正在准备你的工作空间'}),async()=>{await page.keyboard.press('Escape');});
  const enriched = {...metrics, relay:{...metrics.relay,by_height:Object.fromEntries(['one_to_two','two_to_three','three_to_four','four_plus'].map(k=>[k,{...cohort,promotion_valid_n:2,promotion_n:1,promotion_pct:'50',samples:[{ts_code:'600001.SH',previous_height:1,promotion:true}]}]))},limits:{...metrics.limits,supplier_reported:{...metrics.limits.supplier_reported,second_height:2}}};
  const panel=(d:string,rows:unknown[]=[])=>({trade_day:d,requested_day:d,status:'ready',source_label:'合成已存板块',source_time:null,received_at:null,reason:null,
    rows:state.fullTopTen?Array.from({length:10},(_,i)=>({rank:i+1,code:`60000${i}.SH`,name:`合成前十${i+1}`,pct_change:'1',net_amount:'100000000'})):rows,total_count:12});
  const auctionData=(query:URLSearchParams)=>{
    const data=terminalFixture(query);
    for(const item of [...data.stocks,...data.auction_days.flatMap(entry=>entry.items)]) {
      if(item.code===stockA)Object.assign(item,{
        node_amounts:{'09:15':200_000_000,'09:20':0,'09:25':null},node_source:'合成阶段封单',node_received_at:`${data.trade_day}T09:26:00+08:00`,
      });
      // Only the narrow sorting case reverses the actual成交 order; seals stay unchanged.
      else if(reversedAuctionAmounts&&item.code===stockB)item.auction_amount_cny=456_000_000;
    }
    return {...data,detail_revision:'synthetic-review-expansion-v1'};
  };
  const ladderData=(query:URLSearchParams):LadderObservation=>{
    const selected=query.get('trade_day')??days[1];
    const row=(tradeDay:string):LadderRecord=>({code:'600001.SH',name:'合成原日涨停',day:tradeDay,height:tradeDay===days[0]?state.previousHeight:3,
      previous_height:tradeDay===days[0]?state.previousHeight-1:2,status:'sealed',themes:['合成原日题材'],theme_source:`${tradeDay} 原日题材`,limit_type:null,
      pct_change:10,price:11,auction_price:null,first_time:'09:30:00',last_time:'14:20:00',seal_amount:null,amount:3_000_000,actual_float:null,actual_turnover:null,
      source_time:null,received_at:`${tradeDay}T16:00:00+08:00`,price_basis:'daily_close',stale:false});
    const records=[...days].reverse().filter(tradeDay=>tradeDay<=selected).map(tradeDay=>{
      const pending=state.ladderPending&&tradeDay===days[1],empty=state.emptyPool&&tradeDay===days[1],noUp=state.noEligibleUp&&tradeDay===days[1];
      return {day:tradeDay,previous_day:tradeDay===days[1]?days[0]:null,state:pending?'missing':'complete',rows:pending||empty||noUp?[]:[row(tradeDay)],eliminated:[],
        coverage:[{dataset:'pools',trade_day:tradeDay,state:pending?'missing':'complete',row_count:pending?null:empty?0:1,received_at:pending?null:`${tradeDay}T16:00:00+08:00`}]};
    });
    return {trade_day:selected,quote_day:days[1],available_days:[...days].reverse(),days:records,candles:{},latest_quotes:{},state:records.some(record=>record.state==='missing')?'partial':'complete',
      as_of_time:query.get('as_of_time'),built_at:`${days[1]}T18:00:00+08:00`,can_export:false,scope:'合成原日名单；缺失与空名单均不代表市场零涨停。'};
  };
  await page.route('**/api/v1/**',async route=>{
    const req=route.request(),url=new URL(req.url()),path=url.pathname;
    if(req.method()==='GET')state.reads.push(path+url.search);
    const ok=(data:unknown)=>route.fulfill({json:{meta,data}});
    if(path.endsWith('/session'))return ok({user:{id:'00000000-0000-4000-8000-000000000001',username:'合成研究',role:'admin'},csrf_token:'synthetic-fixture'});
    if(path.endsWith('/watchlist'))return ok({items:[]});
    if(path.endsWith('/ai/settings'))return ok({enabled:false,features:[]});
    if(path.endsWith('/collections') && req.method()==='POST'){state.writes.push(req.postDataJSON());return ok({batch_id:'00000000-0000-4000-8000-000000000002',state:'queued'});}
    if(path.includes('/collections/'))return ok({state:'complete',counts:{succeeded:1,queued:0,running:0,failed:0},jobs:[]});
    if(path.endsWith('/market-session'))return ok({trade_day:days[1],phase:'closed',is_trading_day:true,default_day:days[1]});
    if(path.endsWith('/workbench-view'))return ok(auctionData(url.searchParams));
    if(path.endsWith('/workbench-details')){
      const data=auctionData(url.searchParams);
      return ok({trade_day:data.trade_day,revision:data.detail_revision,stocks:data.stocks,auction_days:data.auction_days,events:[],seals:{},members:[],tracking_members:[],funds_history:[]});
    }
    if(path.endsWith('/auction-support'))return ok({state:'available',trade_day:terminalDay,previous_trade_day:'2001-12-28',rows:[],rankings:{},gaps:[],coverage:[],down_days:[]});
    if(path.endsWith('/auction-reference'))return ok({trade_day:terminalDay,state:'missing',reason:'合成测试未提供封单报告',report:null});
    if(path.endsWith('/ladder-observation')){state.ladderReads++;return ok(ladderData(url.searchParams));}
    if(path.endsWith('/screening/preview')){
      const body=req.postDataJSON();state.writes.push(body);
      return ok({trade_day:terminalDay,as_of_time:'2026-10-23T10:00:00+08:00',screen_context:'auction',definition:body.definition,scope:'market',counts:{total:5571,matched:201,not_matched:5369,insufficient:1},items:[{code:'600251.SH',name:'第251只合成',instrument_id:null,state:'matched',metrics:{auction_pct:'5',auction_amount_cny:'123450000',auction_turnover_rate:'1.5',auction_volume_ratio:'2.3',previous_height:'2'},checks:[]}],offset:body.offset,limit:100,has_more:body.offset===0,coverage:{source_rows:5571,usable_rows:5571}});
    }
    if(path.endsWith('/series'))return ok({shared_date:days[1],state:'partial',trade_days:days,records:days.map(d=>({trade_day:d,metrics:enriched,indices:day(d).indices,themes:{records:[]}}))});
    if(path.endsWith('/day'))return ok({...day(url.searchParams.get('trade_day')??days[1]),metrics:enriched});
    if(path.endsWith('/notes/days'))return ok({items:[]});
    if(path.endsWith('/notes'))return ok({trade_day:days[1],state:'empty',content:blank(),writable:true,head_id:null,is_current:true});
    if(path.endsWith('/stocks'))return ok({state:'complete',items:[]});
    if(path.endsWith('/theme-history'))return ok({state:'complete',items:[]});
    if(path.endsWith('/lhb'))return ok({...day(days[1]),records:[listing()],selected_record:null,history_dates:days,history_records:[],unmatched_seat_records:[]});
    if(path.endsWith('/analysis')){
      const kind=url.searchParams.get('kind');state.analysis.push(kind??'');
      if(kind==='emotion')return ok({kind,trade_days:days,state:'complete',records:days.map(d=>({trade_day:d,state:'complete',metrics:state.unknownPreviousHeight && d===days[0]?{...enriched,relay:{...enriched.relay,unclassified_previous_limit_up_n:1}}:enriched}))});
      if(kind==='groups')return ok({state:'complete',groups:{state:'complete',membership_state:'missing',membership_day:null,unassigned_codes:['600001.SH'],ambiguous_n:0,records:state.emptyPool?[]:[{...raw(),limit_times:'2',turnover_yuan:'3000000',first_time:'093000',last_time:'142000',open_times:'1'}],boards:state.emptyPool?[]:[{key:'2',label:'2板',codes:['600001.SH']}],themes:[],scope:'合成原日池',coverage:[{dataset:'pools',trade_day:days[1],state:'complete',row_count:state.emptyPool?0:1}]}});
      return ok({kind,trade_days:days,state:'complete',records:days.map(trade_day=>({trade_day,state:'complete',listing_state:'complete',quote:{open:'10',high:'12',low:'9',close:'11',amount:'3000'},listings:[{record_key:'lhb_one',reason:'单日合成原因'}]}))});
    }
    if(path.endsWith('/direction-data/sector-atlas'))return ok(atlas(url.searchParams));
    if(path.endsWith('/direction-data')){
      if(url.searchParams.get('lookback') && url.searchParams.get('sector_code') && state.rotationEmptyAndError)return ok({state:'partial',coverage:{requested_days:20,calendar_days:2,ranking_days:1,funds_days:1},records:days.map((d,i)=>({trade_day:d,status:i===0?'missing':'error',funds_status:i===0?'missing':'error',rank_complete:i===0,funds_complete:i===0,selection_state:i===0?'not_disclosed':'unavailable',rank:null,in_top_ten:null,pct_change:null,net_amount:null,total_count:0,top_ten:[]}))});
      if(url.searchParams.get('lookback'))return ok({state:'partial',coverage:{requested_days:20,calendar_days:3,ranking_days:2,funds_days:1},records:days.map((d,i)=>({trade_day:d,status:i===0?'missing':'ready',funds_status:'missing',selection_state:i===0?'unavailable':'ranked',rank:i===0?null:12,in_top_ten:i===0?null:false,pct_change:'1',net_amount:null,total_count:50,top_ten:[{code:'BK1001.DC',name:'合成轮动'}]}))});
      return ok({trade_day:days[1],sector_type:'concept',previous_trade_day:days[0],days:['2026-10-23','2026-10-22','2026-10-21','2026-10-20','2026-10-19'].map(d=>panel(d,[{rank:1,code:'BK1001.DC',name:'合成轮动',pct_change:'2'}])),funds:panel(days[0]),popularity:{ths:panel(days[1]),tdx:panel(days[1]),eastmoney:panel(days[1])},strength:panel(days[1])});
    }
    return ok({items:[]});
  });
  return state;
}

test('emotion history selects a date and shows fixed height denominators',async({page})=>{
  const state=await fixture(page);await page.goto(`/research/reviews?view=emotion&trade_day=${days[1]}`);
  await page.getByText('历史曲线与梯队晋级率',{exact:true}).click();
  await page.getByRole('button',{name:'梯队晋级率',exact:true}).click();
  await expect(page.getByRole('region',{name:'所选日梯队晋级样本'})).toHaveCount(0);
  await expect(page.getByRole('table',{name:'所选日梯队晋级样本'})).toContainText('50.00%');
  await page.getByRole('button',{name:new RegExp(`曲线 ${days[0]}`)}).click();
  await expect(page).toHaveURL(new RegExp(`trade_day=${days[0]}`));
  await expect(page.getByRole('button',{name:'梯队晋级率',exact:true})).toBeVisible();
  expect(state.analysis).toEqual(['emotion']);expect(state.errors).toEqual([]);
});

test('unknown original heights with classified means leave both performance curves blank',async({page})=>{
  const state=await fixture(page);state.unknownPreviousHeight=true;
  await page.goto(`/research/reviews?view=emotion&trade_day=${days[1]}`);
  await page.getByText('历史曲线与梯队晋级率',{exact:true}).click();
  await page.getByRole('button',{name:'昨日首板／连板表现',exact:true}).click();
  const unknown=page.getByRole('button',{name:new RegExp(`曲线 ${days[0]}`)});
  const known=page.getByRole('button',{name:new RegExp(`曲线 ${days[1]}`)});
  await expect(unknown).toHaveAttribute('aria-label',/昨日首板收盘均值缺失%，昨日连板收盘均值缺失%/);
  await expect(unknown.locator('circle')).toHaveCount(0);
  await expect(known).toHaveAttribute('aria-label',/昨日首板收盘均值2%，昨日连板收盘均值2%/);
  await expect(known.locator('circle')).toHaveCount(2);
  await expect(page.getByText(`昨日原板数缺失，首板／连板样本无法完整分类，两组表现曲线留空：${days[0]}。`,{exact:true})).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('native top ten matrix and funds remain available inside the fixed viewport',async({page},info)=>{
 const state=await fixture(page);state.fullTopTen=true;
 await page.goto(`/research/reviews?view=direction&trade_day=${days[1]}`);
 await expect(page.locator('.sr-matrix tbody tr')).toHaveCount(11);await expect(page.locator('.sr-funds tbody tr')).toHaveCount(10);
 for(const width of [1440,1280]){await page.setViewportSize({width,height:1050});expect(await page.locator('.sr-workspace').evaluate(e=>e.getBoundingClientRect().bottom<=innerHeight)).toBeTruthy();expect(await page.evaluate(()=>document.body.scrollWidth<=innerWidth+1)).toBeTruthy();}
 await page.screenshot({path:info.outputPath('sector-rotation-fixed-viewport.png')});expect(state.errors).toEqual([]);
});

test('original-day theme gaps remain visible and group reads are on demand',async({page})=>{
  const state=await fixture(page);await page.goto(`/research/reviews?view=direction&trade_day=${days[1]}`);
  await expect(page.getByRole('region',{name:'板块轮动工作区',exact:true})).toBeVisible();
  expect(state.analysis).toEqual([]);
  await page.getByRole('button',{name:'研究记录',exact:true}).click();
  await page.getByText('当日涨停分组 · 板数与原日题材',{exact:true}).click();
  await expect(page.getByRole('table',{name:'涨停分组明细'})).toContainText('14:20:00');
  const section=page.locator('.limit-group-section');
  await expect(section.locator('summary')).toContainText('2连板');
  const row=section.locator('tbody tr').first();
  expect((await row.boundingBox())!.height).toBeLessThanOrEqual(34);
  await section.locator('summary').click();
  await expect(row).not.toBeVisible();
  await section.locator('summary').click();
  await expect(row).toBeVisible();
  await page.getByRole('button',{name:'涨停复盘（按题材）',exact:true}).click();
  await expect(page.getByText('该日历史成分尚未完整取得，不能确认题材分组；未用当前成分回填。',{exact:true})).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('rotation separates complete unranked days from unknown partial coverage',async({page})=>{
 const state=await fixture(page);
 await page.route('**/api/v1/direction-data/sector-atlas?**',route=>{
  const panels=days.map((d,i)=>({...rotationPanel(d,'合成归档',Array.from({length:10},(_,j)=>({rank:j+1,code:'K'+(j+(i?1:0)),name:'合成题材'+(j+(i?1:0)),strength:'1'}))),top_ten_complete:i!==2,status:i===2?'partial':'ready'}));
  return route.fulfill({json:{meta,data:{days:panels,limitations:[]}}});
 });
 await page.goto(`/research/reviews?view=direction&trade_day=${days[1]}`);
 await page.locator('.sr-matrix button[data-code=K0]').first().click();const graph=page.getByRole('img',{name:'多板块前十排名轨迹'});
 await expect(graph.locator('title').filter({hasText:days[1]+' · 合成题材0 · 未进前十'})).toHaveCount(1);
 await expect(graph.locator('title').filter({hasText:days[2]+' · 合成题材0'})).toHaveCount(0);expect(state.errors).toEqual([]);
});

test('rotation keeps missing archives and source errors distinct',async({page})=>{
 const state=await fixture(page);
 await page.route('**/api/v1/direction-data/sector-atlas?**',route=>route.fulfill({json:{meta,data:{days:days.slice(0,2).map((d,i)=>({...rotationPanel(d,'合成来源',[]),status:i?'error':'missing',reason:i?'合成来源异常':'合成尚未归档'})),limitations:[]}}}));
 await page.goto(`/research/reviews?view=direction&trade_day=${days[1]}`);const first=page.locator('.sr-matrix tbody tr').first();await expect(first).toContainText('暂无资料');await expect(first).toContainText('读取失败');await expect(page.locator('.sr-volume-bar')).toHaveCount(0);expect(state.errors).toEqual([]);
});

test('LHB chart marker links to the original disclosure',async({page})=>{
  const state=await fixture(page);await page.goto(`/research/reviews?view=lhb&trade_day=${days[1]}&stock=600001.SH`);
  await page.getByText('个股 K 线与上榜日 · 600001.SH',{exact:true}).click();
  await expect(page.getByRole('img',{name:'龙虎榜个股日K与上榜标记'})).toBeVisible();
  await expect(page.getByRole('table',{name:'K线日与上榜披露'})).toContainText('0.03亿');
  await page.getByRole('button',{name:`上榜日 ${days[0]}`,exact:true}).click();
  await expect(page).toHaveURL(new RegExp(`trade_day=${days[0]}`));await expect(page).toHaveURL(/record_key=lhb_one/);
  expect(state.errors).toEqual([]);
});

test('history collection carries selected range and four-source restriction',async({page})=>{
  const state=await fixture(page);await page.goto(`/research/reviews?view=direction&trade_day=${days[1]}`);
  await page.getByRole('button',{name:'近50日',exact:true}).click();
  await page.locator('.dr-maintenance-tools summary').click();
  await page.getByRole('button',{name:'补齐资料',exact:true}).click();
  await expect.poll(()=>state.writes.length).toBe(1);
  expect(state.writes[0].source_scope).toBe('four_sources');expect(state.writes[0].lookback).toBe(50);
});

// ADR0086 removed the screening UI. Its metric/pagination API contract is
// exercised in backend tests; the current page must retain read context and
// distinct traded amounts and seals without mounting or invoking that control.
test('auction cleanup retains the original date, cutoff and separate traded and seal values without screening writes',async({page})=>{
  const state=await fixture(page),cutoff=`${terminalDay}T10:00:00+08:00`;
  await page.goto('/workbench?'+new URLSearchParams({view:'auction',trade_day:terminalDay,as_of_time:cutoff}));
  await expect(page.getByRole('heading',{name:'多日封单对照',exact:true})).toBeVisible();
  await expect(page.getByText('竞价成交筛选 · 完整已保存样本',{exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'筛选已存竞价',exact:true})).toHaveCount(0);
  await expect(page.getByRole('table',{name:'竞价成交筛选结果'})).toHaveCount(0);
  await expect(page.locator('.ty-replay-banner')).toContainText('当时已知回看');
  const current=page.getByRole('region',{name:`${terminalDay}竞价对照`,exact:true});
  const stock=current.locator('.ty-auction-stock-list li').filter({hasText:'合成甲'});
  await expect(stock.locator('.ty-auction-values>span').nth(0)).toHaveText('2.00');
  await expect(stock.locator('.ty-auction-values>span').nth(1)).toHaveText('0.00');
  await expect(stock.locator('.ty-auction-values>span').nth(2)).toHaveText('—');
  await expect(page.getByRole('table',{name:'所选股票跨日状态'}).locator('tbody tr').first()).toContainText('1.230亿');
  await page.getByRole('tab',{name:'实际竞价成交 · 跨日比较',exact:true}).click();
  await expect(page.getByRole('img',{name:'实际竞价成交金额跨日比较，单位亿元'})).toBeVisible();
  await expect.poll(()=>state.reads.some(path=>{const url=new URL(path,'http://synthetic.invalid');return url.pathname==='/api/v1/workbench-view'&&url.searchParams.get('day')===terminalDay&&url.searchParams.get('as_of_time')===cutoff;})).toBe(true);
  const query=new URL(page.url()).searchParams;
  expect(query.get('trade_day')).toBe(terminalDay);expect(query.get('as_of_time')).toBe(cutoff);
  await page.evaluate(()=>window.dispatchEvent(new Event('read-permission-denied')));
  await expect(page.locator('.ty-auction-stock-list li').filter({hasText:'合成甲'})).toHaveCount(0);
  await expect(page.getByRole('table',{name:'所选股票跨日状态'})).toHaveCount(0);
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('simplified auction controls remain usable inside a narrow viewport',async({page})=>{
  const state=await fixture(page,true);await page.setViewportSize({width:390,height:900});
  await page.goto(`/workbench?view=auction&trade_day=${terminalDay}`);
  const range=page.getByRole('group',{name:'竞价榜单范围',exact:true});
  await expect(range.getByRole('button',{name:'封单金额',exact:true})).toBeVisible();
  await range.getByRole('button',{name:'昨日连板',exact:true}).click();
  await expect(range.getByRole('button',{name:'昨日连板',exact:true})).toHaveAttribute('aria-pressed','true');
  const current=page.getByRole('region',{name:`${terminalDay}竞价对照`,exact:true});
  const names=current.locator('.ty-auction-stock-list .ty-stock-label');
  await expect(names).toHaveText(['合成甲','合成乙']);
  const sort=page.getByRole('button',{name:'按竞价成交额排序',exact:true});
  await expect(sort).toHaveAttribute('aria-pressed','false');
  await sort.click();
  await expect(sort).toHaveAttribute('aria-pressed','true');
  await expect(names).toHaveText(['合成乙','合成甲']);
  const amounts=page.getByRole('table',{name:'所选股票跨日状态'}).locator('tbody tr').first();
  await expect(amounts).toContainText('1.230亿');
  await current.getByRole('button',{name:'合成乙',exact:true}).click();
  await expect(amounts).toContainText('4.560亿');
  await expect(page.getByText('竞价成交筛选 · 完整已保存样本',{exact:true})).toHaveCount(0);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();
  expect(new URL(page.url()).searchParams.get('trade_day')).toBe(terminalDay);
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('historical ladder retains the card original day, height and stock across views',async({page})=>{
  const state=await fixture(page);await page.goto(`/workbench?view=ladder&trade_day=${days[1]}`);
  await expect(page.getByRole('tab',{name:'单日',exact:true})).toHaveAttribute('aria-selected','true');
  await page.getByRole('tab',{name:'多日',exact:true}).click();
  const previous=page.locator('.lo-days-matrix tbody tr td:nth-child(3) .lo-stock');
  await expect(previous).toHaveAttribute('aria-label',/2板/);
  await expect(previous).toHaveAttribute('title',new RegExp(`${days[0]} 收盘日线`));
  await previous.click();
  await expect(page).toHaveURL(new RegExp(`trade_day=${days[1]}`));await expect(page).toHaveURL(/stock=600001.SH/);
  await expect(page.getByLabel('选中股票详情',{exact:true})).toContainText(`${days[0]} 收盘日线`);
  await page.getByRole('button',{name:'关闭股票详情',exact:true}).click();
  await page.getByRole('columnheader',{name:new RegExp(days[0])}).getByRole('button').click();
  await expect(page).toHaveURL(new RegExp(`trade_day=${days[0]}`));
  await page.getByRole('tab',{name:'单日',exact:true}).click();
  await expect(page.locator('.lo-stock')).toHaveAttribute('aria-label',/2板/);
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('an active ladder reloads missing archives when the saved data changes',async({page})=>{
  const state=await fixture(page);state.ladderPending=true;
  await page.clock.install();
  await page.goto(`/workbench?view=ladder&trade_day=${days[1]}`);
  await expect(page.getByText('尚无可核对的连板资料',{exact:true})).toBeVisible();
  await expect(page.locator('.lo-footer')).toContainText('未采集');
  await expect(page.locator('.lo-stock')).toHaveCount(0);
  state.ladderPending=false;
  await page.clock.fastForward(301_000);
  await expect(page.locator('.lo-stock')).toHaveAttribute('aria-label',/3板/);
  await expect(page.locator('.lo-stock')).toHaveAttribute('title',new RegExp(`600001.SH.*\n${days[1]} 收盘日线`));
  await expect(page.locator('.lo-footer')).not.toContainText('未采集');
  expect(state.ladderReads).toBeGreaterThan(1);
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('an empty source pool stays unverified and an active ladder shows its later publication',async({page})=>{
  const state=await fixture(page);state.emptyPool=true;
  await page.clock.install();
  await page.goto(`/workbench?view=ladder&trade_day=${days[1]}`);
  await expect(page.getByText('来源返回空名单，连板状态待核',{exact:true})).toBeVisible();
  await expect(page.getByText('尚无可核对的原日涨停名单，不能据此判断市场为0只；暂不展示淘汰结论。',{exact:true})).toBeVisible();
  await page.getByRole('tab',{name:'多日',exact:true}).click();
  const current=page.getByRole('columnheader',{name:new RegExp(days[1])});
  const currentStocks=page.locator('.lo-days-matrix tbody tr td:nth-child(2) .lo-stock');
  const previousStocks=page.locator('.lo-days-matrix tbody tr td:nth-child(3) .lo-stock');
  await expect(current).toContainText('名单待核');
  await expect(currentStocks).toHaveCount(0);
  await expect(previousStocks).toHaveAttribute('aria-label',/2板/);
  await expect(previousStocks).toHaveAttribute('title',new RegExp(`${days[0]} 收盘日线`));
  await expect(page.locator('.lo-footer')).toContainText('原日名单待核');
  state.emptyPool=false;
  await page.clock.fastForward(301_000);
  await expect(currentStocks).toHaveAttribute('aria-label',/3板/);
  await expect(currentStocks).toHaveAttribute('title',new RegExp(`${days[1]} 收盘日线`));
  await expect(current).not.toContainText('名单待核');
  expect(state.ladderReads).toBeGreaterThan(1);
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('a nonempty pool without eligible up records is separate from an empty response',async({page})=>{
  const state=await fixture(page);state.noEligibleUp=true;
  await page.goto(`/workbench?view=ladder&trade_day=${days[1]}`);
  await expect(page.getByText('当前名单没有可展示的连板对象',{exact:true})).toBeVisible();
  await expect(page.getByText('当前来源范围未列出可展示对象，请核对本日资料；不代表其他来源或全市场。',{exact:true})).toBeVisible();
  await expect(page.getByText('来源返回空名单，连板状态待核',{exact:true})).toHaveCount(0);
  await expect(page.locator('.lo-stock')).toHaveCount(0);
  await page.getByText('本日资料与范围',{exact:true}).click();
  await expect(page.locator('.lo-empty-coverage')).toContainText('已归档 · 1条');
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('grouped limit ups identifies an empty source response before showing group counts',async({page})=>{
  const state=await fixture(page);state.emptyPool=true;
  await page.goto(`/research/reviews?view=direction&trade_day=${days[1]}`);
  await page.getByRole('button',{name:'研究记录',exact:true}).click();
  await page.getByText('当日涨停分组 · 板数与原日题材',{exact:true}).click();
  await expect(page.getByText('日榜待补齐',{exact:true})).toBeVisible();
  await expect(page.getByText('当前分组没有披露记录。',{exact:true})).toHaveCount(0);
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('prior archived heights keep their original date while the pending current pool is published later',async({page})=>{
  const state=await fixture(page);state.emptyPool=true;state.previousHeight=5;
  await page.clock.install();
  await page.goto(`/workbench?view=ladder&trade_day=${days[1]}`);
  await page.getByRole('tab',{name:'多日',exact:true}).click();
  const current=page.getByRole('columnheader',{name:new RegExp(days[1])});
  const currentStocks=page.locator('.lo-days-matrix tbody tr td:nth-child(2) .lo-stock');
  const previousStocks=page.locator('.lo-days-matrix tbody tr td:nth-child(3) .lo-stock');
  await expect(currentStocks).toHaveCount(0);
  await expect(current).toContainText('名单待核');
  await expect(previousStocks).toHaveAttribute('aria-label',/5板/);
  await expect(previousStocks).toHaveAttribute('title',new RegExp(`${days[0]} 收盘日线`));
  state.emptyPool=false;
  await page.clock.fastForward(301_000);
  await expect(currentStocks).toHaveAttribute('aria-label',/3板/);
  await expect(currentStocks).toHaveAttribute('title',new RegExp(`${days[1]} 收盘日线`));
  await expect(current).not.toContainText('名单待核');
  await expect(previousStocks).toHaveAttribute('aria-label',/5板/);
  await expect(previousStocks).toHaveAttribute('title',new RegExp(`${days[0]} 收盘日线`));
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('compact groups keep columns aligned across boards, themes and narrow screens',async({page},testInfo)=>{
  await fixture(page);
  await page.setViewportSize({width:1700,height:1000});
  const records=Array.from({length:16},(_,i)=>({...raw(),ts_code:`600${String(i).padStart(3,'0')}.SH`,name:`合成样本${i+1}`,close:'10.50',up_stat:i===0?'6/10':null,turnover_ratio:'5.3',board_height:i<2?4:i<5?2:1,first_time:i%2?'94745':'093000',last_time:'142000',open_times:i%3,turnover_yuan:'300000000',seal_amount_yuan:i===0?'120000000':null,lhb_status:i===0?'listed':i===1?'not_listed':'unavailable',lhb_count:i===0?2:0,group_field_details:i===1?{seal_amount_yuan:{state:'unit_unverified',source_value:'12345'}}:{}}));
  Object.assign(records[0],{limit_type:'一字板',change_reason:'合成异动原因',board_stat_label:'10天6板',actual_float_value_yuan:'500000000',float_market_value_yuan:'1000000000',total_market_value_yuan:'2000000000'});
  const boards=[4,2,1].map(n=>({key:String(n),label:`${n}板`,codes:records.filter(r=>r.board_height===n).map(r=>r.ts_code)}));
  const themes=[{key:'a',label:'消费（合成排版样本）',codes:records.slice(0,9).map(r=>r.ts_code)},{key:'b',label:'新能源（合成排版样本）',codes:records.slice(7).map(r=>r.ts_code)}];
  await page.route('**/analysis?**',async route=>{
    if(new URL(route.request().url()).searchParams.get('kind')!=='groups')return route.fallback();
    return route.fulfill({json:{meta,data:{state:'complete',groups:{state:'complete',records,boards,themes,unassigned_codes:[],membership_state:'complete',membership_day:days[1],scope:'合成数据，仅用于版式验证；题材可重复归属',coverage:[]}}}});
  });
  await page.goto(`/research/reviews?view=direction&trade_day=${days[1]}`);
  await page.getByRole('button',{name:'研究记录',exact:true}).click();
  const panel=page.locator('.limit-groups');
  await panel.locator(':scope > summary').click();
  await expect(panel.locator('.limit-group-section')).toHaveCount(3);
  await expect(panel.locator('tbody tr')).toHaveCount(16);
  await expect(panel).toContainText('09:47:45');
  await expect(panel.locator('table').first().locator('th')).toHaveText(['股票名称','股票代码','股价','涨幅','涨停类型','板数','连板','首次封板','最后封板','开板','封单额','成交额','换手率','实际流通','流通市值','总市值','异动原因','龙虎榜']);
  const cells=panel.locator('tbody tr').first().locator('td');
  await expect(cells.nth(2)).toHaveText('10.50');
  await expect(cells.nth(5)).toHaveText('10天6板');
  await expect(cells.nth(6)).toHaveText('4');
  await expect(cells.nth(12)).toHaveText('5.3%');
  for(const [i,value] of [[4,'一字板'],[13,'5.00亿'],[14,'10.00亿'],[15,'20.00亿'],[16,'合成异动原因']] as const)await expect(cells.nth(i)).toHaveText(value);
  await expect(cells.nth(13)).toHaveAttribute('title',/自由流通股本/);
  await expect(cells.nth(10)).toHaveText('1.20亿');
  await expect(cells.nth(17).getByRole('link',{name:'查看'})).toHaveAttribute('href',/view=lhb.*stock=600000.SH/);
  const second=panel.locator('tbody tr').nth(1);
  await expect(second).toContainText('无榜');
  await expect(second.getByRole('button',{name:'单位待核'})).toHaveAttribute('title',/12345/);
  await panel.screenshot({path:testInfo.outputPath('limit-groups-boards.png')});
  await panel.getByRole('button',{name:'涨停复盘（按题材）',exact:true}).click();
  await expect(panel.locator('.limit-group-section')).toHaveCount(2);
  await expect(panel.locator('tbody tr')).toHaveCount(18);
  await panel.screenshot({path:testInfo.outputPath('limit-groups-themes.png')});
  await panel.locator('.limit-stock-link').first().click();
  await expect(page).toHaveURL(/stock=600000.SH/);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  const scroll=panel.locator('.limit-group-scroll').first();
  expect(await scroll.evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(true);
});
