import { openNotifications } from './isolated-browser.fixtures';
import {expect,test} from '@playwright/test';
import type {Page} from '@playwright/test';
import {serveTradingFixture,widgetsFixturePath} from '../fixtureServer';

const id='00000000-0000-4000-8000-000000000091';
const meta={request_id:id,app_mode:'demo',generated_at:'2020-01-03T10:00:00Z',quality:{status:'partial',reasons:['Synthetic fixtures, not market data']}};
type Row=Record<string,string|null>;
function record(dataset:string,family:string,rows:Row[],units:Record<string,string>={}) {
  return {id,source_id:id,source_name:'合成测试来源',family,source_key:dataset,instrument_id:null,received_at:meta.generated_at,published_at:null,trade_day:'2020-01-02',data_mode:'daily',content_hash:'9'.repeat(64),payload:{dataset,title:'合成资料',scope:'合成测试，不是真实市场数据',attribution:'Synthetic fixture only',observed_day:'2020-01-03',source_url:'https://example.com/synthetic',columns:[...new Set(rows.flatMap(row=>Object.keys(row)))].map(key=>({key,label:key,unit:units[key]??null})),rows}};
}
async function mount(page:Page,view:string,tables:ReturnType<typeof record>[],props:Record<string,unknown>={}) {
  await page.addInitScript(({view,props})=>{(window as unknown as {tradingFixture:unknown}).tradingFixture={view,props};},{view,props});
  await page.route('**/api/v1/data-sources',route=>route.fulfill({json:{meta,data:{items:[],available_adapters:[]}}}));
  await page.route('**/api/v1/trading-datasets?*',route=>{
    const q=new URL(route.request().url()).searchParams;
    return route.fulfill({json:{meta,data:{items:tables.filter(item=>item.payload.dataset===q.get('dataset')&&(!q.get('family')||item.family===q.get('family'))),available_days:['2020-01-02'],trade_day:'2020-01-02'}}});
  });
  await page.route('**/api/v1/instruments?*',route=>route.fulfill({json:{meta,data:{items:[{id,symbol:'000001',name:'合成甲',exchange:'SZSE',asset_type:'stock'}]}}}));
  await serveTradingFixture(page);
  await page.goto(widgetsFixturePath+'?trade_day=2020-01-02');
}

test('leaderboard chooses a single-day reason without adding multiple causes, filters old days and splits disclosed seat sides',async({page})=>{
  const rows=[
    {trade_date:'20200101',ts_code:'000002.SZ',name:'旧日股票',reason:'当日涨幅',net_amount:'999999',l_buy:'999999',l_sell:'0',pct_change:'10'},
    {trade_date:'20200102',ts_code:'000001.SZ',name:'合成甲',reason:'连续三个交易日涨幅偏离',net_amount:'990000',l_buy:'1000000',l_sell:'10000',pct_change:'10'},
    {trade_date:'20200102',ts_code:'000001.SZ',name:'合成甲',reason:'当日价格涨幅偏离',net_amount:'10000',l_buy:'20000',l_sell:'10000',pct_change:'10'},
  ];
  const seats=[
    {trade_date:'20200102',ts_code:'000001.SZ',reason:'当日价格涨幅偏离',exalter:'合成买入席位',buy:'20000',sell:'1000',net_buy:'19000',side:'0'},
    {trade_date:'20200102',ts_code:'000001.SZ',reason:'当日价格涨幅偏离',exalter:'合成卖出席位',buy:'500',sell:'10000',net_buy:'-9500',side:'1'},
    {trade_date:'20200102',ts_code:'000001.SZ',reason:'连续三个交易日涨幅偏离',exalter:'多日席位不应混入',buy:'999999',sell:'0',net_buy:'999999',side:'0'},
  ];
  await mount(page,'leaderboard',[record('tushare.top_list','tushare_top_list',rows,{net_amount:'CNY',l_buy:'CNY',l_sell:'CNY'}),record('tushare.top_inst','tushare_top_inst',seats,{buy:'CNY',sell:'CNY',net_buy:'CNY'})]);
  const table=page.getByRole('region',{name:'龙虎榜股票排行',exact:true});
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expect(table).not.toContainText('旧日股票');
  await expect(table).toContainText('+1万');
  await expect(table).toContainText('2 项原因');
  const reasonButton = table.getByRole('button',{name:/单日.*当日价格/});
  expect(await reasonButton.evaluate(element => getComputedStyle(element).whiteSpace)).toBe('normal');
  expect(await reasonButton.evaluate(element => element.getBoundingClientRect().width)).toBeLessThanOrEqual(180);
  await table.getByRole('button',{name:/查看 合成甲 行情/}).click();
  await expect(page).toHaveURL(/instrument=00000000-0000-4000-8000-000000000091/);
  await table.getByRole('button',{name:/单日.*当日价格/}).click();
  const drawer=page.getByRole('dialog',{name:'龙虎榜买卖席位'});
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole('region',{name:'买入席位',exact:true})).toContainText('合成买入席位');
  await expect(drawer.getByRole('region',{name:'买入席位',exact:true})).not.toContainText('合成卖出席位');
  await expect(drawer.getByRole('region',{name:'卖出席位',exact:true})).toContainText('合成卖出席位');
  await expect(drawer).not.toContainText('多日席位不应混入');
  await drawer.getByRole('combobox',{name:'上榜原因'}).selectOption('0');
  await expect(drawer).toContainText('多日席位不应混入');
  await page.keyboard.press('Escape');
  await expect(drawer).not.toBeVisible();
});

test('fund rankings sort actual same-day net inflow and net outflow with unknown versus zero preserved',async({page})=>{
  const rows=[
    {trade_day:'2020-01-02',symbol:'000001.SZ',name:'小流入',net_inflow_cny:'10000',inflow_cny:'20000',outflow_cny:'10000',change_percent:'1'},
    {trade_day:'2020-01-02',symbol:'000002.SZ',name:'大流入',net_inflow_cny:'200000000',inflow_cny:'300000000',outflow_cny:'100000000',change_percent:'2'},
    {trade_day:'2020-01-02',symbol:'000003.SZ',name:'大流出',net_inflow_cny:'-100000000',inflow_cny:'0',outflow_cny:'100000000',change_percent:'-2'},
    {trade_day:'2020-01-02',symbol:'000004.SZ',name:'小流出',net_inflow_cny:'-10000',inflow_cny:null,outflow_cny:'10000',change_percent:'-1'},
    {trade_day:'2020-01-02',symbol:'000005.SZ',name:'真实零',net_inflow_cny:'0',inflow_cny:'0',outflow_cny:'0',change_percent:'0'},
    {trade_day:'2020-01-01',symbol:'000006.SZ',name:'旧日排行',net_inflow_cny:'999999999',inflow_cny:'999999999',outflow_cny:'0',change_percent:'0'},
  ];
  await mount(page,'funds',[record('market.eastmoney.stock_funds','eastmoney_funds',rows,{net_inflow_cny:'CNY',inflow_cny:'CNY',outflow_cny:'CNY'})]);
  const table=page.getByRole('region',{name:'资金流向排名',exact:true});
  await expect(table.locator('tbody tr').first()).toContainText('大流入');
  await expect(table).not.toContainText('旧日排行');
  await page.getByRole('radio',{name:'净流出',exact:true}).click();
  await expect(table.locator('tbody tr').first()).toContainText('大流出');
  await expect(table).toContainText('-1亿');
  await page.getByRole('radio',{name:'全部',exact:true}).click();
  await expect(table).toContainText('真实零');
  await expect(table).toContainText('—');
  await page.getByRole('radio',{name:'概念',exact:true}).click();
  await expect(page.getByText('所选日期暂无已存资料',{exact:true})).toBeVisible();
  await expect(table).not.toBeVisible();
});

test('limit ladder uses continuous counts independently from N/T statistics and never invents broken-board reasons',async({page},info)=>{
  const rows=[
    {trade_date:'20200102',ts_code:'000001.SZ',name:'连续二板',limit:'U',limit_times:'2',up_stat:'12/23',pct_chg:'10',first_time:'093100',last_time:'100000',open_times:'0',fd_amount:'20000000'},
    {trade_date:'20200102',ts_code:'000002.SZ',name:'首板合成',limit:'U',limit_times:'1',up_stat:'1/1',pct_chg:'10',first_time:'100000',last_time:'100000',open_times:'1',fd_amount:null},
    {trade_date:'20200102',ts_code:'000003.SZ',name:'炸板合成',limit:'Z',limit_times:null,up_stat:null,pct_chg:'7',first_time:'094000',last_time:'094000',open_times:'2',fd_amount:null},
  ];
  await mount(page,'pools',[record('tushare.limit_list_d','tushare_limit_list_d',rows,{fd_amount:'CNY'})]);
  const table=page.getByRole('region',{name:'连板梯队与封板明细',exact:true});
  await expect(table).toContainText('2连板');
  await expect(table).toContainText('23天12板');
  await expect(table).toContainText('09:31:00');
  await expect(table).not.toContainText('12连板');
  await page.getByRole('radio',{name:'炸板',exact:true}).click();
  await expect(table).toContainText('原因未提供');
  await expect(table).not.toContainText('资金出逃');
  for(const width of [390,720]){
    await page.setViewportSize({width,height:900});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath(`limit-ladder-${width}.png`)});
  }
});

test('same-source refresh and transient failure preserve dated sorted rows; permission withdrawal clears them',async({page})=>{
  const rows=Array.from({length:32},(_,index)=>({trade_day:'2020-01-02',symbol:String(index+1).padStart(6,'0')+'.SZ',name:'合成排序'+index,net_inflow_cny:String(index*10000),inflow_cny:'1000000',outflow_cny:'0',change_percent:'1'}));
  const tableRecord=record('market.eastmoney.stock_funds','eastmoney_funds',rows,{net_inflow_cny:'CNY',inflow_cny:'CNY',outflow_cny:'CNY'});
  await mount(page,'funds',[tableRecord]);
  const table=page.getByRole('region',{name:'资金流向排名',exact:true});
  await table.getByRole('button',{name:/净流入/}).click();
  await expect(table.locator('tbody tr').first()).toContainText('合成排序1');
  await table.evaluate(element=>{(element as HTMLElement).dataset.testMarker='preserved';});
  const rowCount = await table.locator('tbody tr').count();
  let release!:()=>void;
  const pending=new Promise<void>(resolve=>{release=resolve;});
  let held = false;
  await page.route('**/api/v1/trading-datasets?*',async route=>{
    held = true; await pending;
    return route.fulfill({json:{meta,data:{items:new URL(route.request().url()).searchParams.get('family')==='eastmoney_funds'?[{...tableRecord,id:'00000000-0000-4000-8000-000000000092'}]:[],available_days:['2020-01-02'],trade_day:'2020-01-02'}}});
  });
  await page.getByRole('button',{name:'刷新',exact:true}).click();
  await expect(table).toHaveAttribute('data-test-marker','preserved');
  await expect.poll(() => held).toBe(true);
  await expect(table.locator('tbody tr')).toHaveCount(rowCount);
  const updated = page.waitForResponse(response => response.url().includes('trading-datasets') && new URL(response.url()).searchParams.get('family') === 'eastmoney_funds');
  release(); await updated;
  await expect(table).toHaveAttribute('data-test-marker','preserved');
  await expect(table.locator('tbody tr').first()).toContainText('合成排序1');
  await page.route('**/api/v1/trading-datasets?*',route=>route.fulfill({status:503,json:{error:{message:'合成读取失败'},request_id:id}}));
  await page.getByRole('button',{name:'刷新',exact:true}).click();
  await expect(await openNotifications(page)).toContainText('合成读取失败');
  await expect(table).toHaveAttribute('data-test-marker','preserved');
  await expect(table.locator('tbody tr').first()).toContainText('合成排序1');
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await page.route('**/api/v1/trading-datasets?*',route=>route.fulfill({status:403,json:{error:{message:'合成读取权限撤回'},request_id:id}}));
  await page.getByRole('button',{name:'刷新',exact:true}).click();
  await expect(table).toHaveCount(0);
  await expect(await openNotifications(page)).toContainText('权限');
});

test('financial comparison shows actual report and announcement periods, declared units, revisions and missing cells',async({page},info)=>{
  const financial=record('tushare.income','tushare_income',[
    {end_date:'20190930',ann_date:'20191030',total_revenue:'100000000',n_income_attr_p:'0',basic_eps:null,update_flag:'0'},
    {end_date:'20191231',ann_date:'20200102',total_revenue:'200000000',n_income_attr_p:'10000000',basic_eps:'0.00123000',update_flag:'1'},
  ],{total_revenue:'元',n_income_attr_p:'元',basic_eps:'元/股'});
  await mount(page,'dataset',[],{definition:{id:'income',title:'利润表'},record:financial,rows:financial.payload.rows});
  const table=page.getByRole('region',{name:'利润表报告期对比',exact:true});
  await expect(table).toContainText('2019-12-31');
  await expect(table).toContainText('公告 2020-01-02');
  await expect(table).toContainText('已修订');
  await expect(table).toContainText('元/股');
  await expect(table.getByRole('cell',{name:'0.00123000',exact:true})).toBeVisible();
  await expect(table).toContainText('—');
  await expect(table.getByRole('cell',{name:'0',exact:true})).toHaveCount(1);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('financial-390.png')});
});

test('news opens the saved source in-place and historical mode filters by publication day',async({page})=>{
  await page.route('**/api/v1/market-news',route=>route.fulfill({json:{meta,data:{items:[{id,source_name:'合成资讯源',family:'synthetic',published_at:'2020-01-02T02:00:00Z',received_at:meta.generated_at,content_hash:'9'.repeat(64),payload:{title:'合成市场消息',summary:'已保存摘要，不自动生成交易判断。',published_day:'2020-01-02',source_url:'https://example.com/synthetic',category:'财经'}}]}}}));
  await mount(page,'news',[]);
  await page.getByRole('button',{name:/合成市场消息/}).click();
  const drawer=page.getByRole('dialog',{name:'资讯详情'});
  await expect(drawer).toContainText('已保存摘要');
  await expect(drawer.getByRole('link',{name:'打开原文 ↗'})).toHaveAttribute('target','_blank');
  await expect(page).toHaveURL(/trading-widgets-host\.html\?trade_day=2020-01-02$/);
  await drawer.getByRole('button',{name:'关闭',exact:true}).click();
  await expect(drawer).not.toBeVisible();
});

test('real research records retain publication status and version detail without invented stock association',async({page})=>{
  const document={id,kind:'review',source_trade_day:'2020-01-02',target_trade_day:null,head_revision_id:id,head_revision_number:2,head_revision_title:'合成复盘记录',head_publication_status:'draft'};
  await page.route('**/api/v1/research-documents?*',route=>route.fulfill({json:{meta,data:{items:new URL(route.request().url()).searchParams.get('kind')==='review'?[document]:[],next_cursor:null}}}));
  await page.route('**/api/v1/revisions/*',route=>route.fulfill({json:{meta,data:{id,title:document.head_revision_title,publication_status:'draft',content:{type:'review',summary:'合成的实际保存正文',limitations:['合成测试，无交易结论']}}}}));
  await mount(page,'research',[]);
  const research=page.getByRole('region',{name:'我的研究记录',exact:true});
  await expect(research).toContainText('未按个股筛选');
  await expect(research).toContainText('草稿 · 第 2 版');
  await research.getByRole('button',{name:/合成复盘记录/}).click();
  await expect(page.getByRole('dialog',{name:'研究记录详情'})).toContainText('合成的实际保存正文');
});

test('board prices remain distinct from attention heat, constituent stocks require explicit board identity',async({page})=>{
  await page.route('**/api/v1/reference-datasets?*',route=>route.fulfill({json:{meta,data:{items:[]}}}));
  await mount(page,'sectors',[
    record('market.eastmoney.industry','eastmoney_boards',[{trade_day:'2020-01-02',code:'BK001',name:'合成行业板块',change_percent:'3',amount_cny:'100000000',up_count:'10',down_count:'2',lead_symbol:'000001.SZ',lead_name:'合成领涨'}],{amount_cny:'CNY'}),
    record('market.eastmoney.constituents','eastmoney_boards',[{trade_day:'2020-01-02',board_code:'BK999',symbol:'000002.SZ',name:'别板股票不混入',change_percent:'5',amount_cny:'30000000'}],{amount_cny:'CNY'}),
    {...record('market.eastmoney.constituents','eastmoney_boards',[{trade_day:'2020-01-02',board_code:'BK001',symbol:'000001.SZ',name:'明确成分',change_percent:'4',amount_cny:'20000000'}],{amount_cny:'CNY'}),source_key:'constituents:BK001'},
  ]);
  await page.getByRole('button',{name:/合成行业板块/}).click();
  const members=page.getByRole('region',{name:'板块成分股',exact:true});
  await expect(members).toContainText('明确成分');
  await expect(members).not.toContainText('别板股票不混入');
  await page.getByRole('radio',{name:'概念',exact:true}).click();
  await expect(page.getByText('所选日期暂无已存资料',{exact:true})).toBeVisible();
});

test('single-stock detail filters market-wide rows locally without an instrument query and retains precise factors',async({page})=>{
  const requests:URL[]=[];
  page.on('request',request=>{if(request.url().includes('/api/v1/trading-datasets'))requests.push(new URL(request.url()));});
  await page.route('**/api/v1/market-quotes?*',route=>route.fulfill({json:{meta,data:{items:[{instrument:{id,symbol:'000001',name:'合成甲',exchange:'SZSE',asset_type:'stock'}}]}}}));
  const margins=record('tushare.margin_detail','tushare_margin_detail',[
    {trade_date:'20200101',ts_code:'000001.SZ',name:'合成甲',rzye:'0.000123',rzmre:'0',rqye:'0'},
    {trade_date:'20200102',ts_code:'000001.SZ',name:'合成甲',rzye:'0.0012345678',rzmre:null,rqye:'0'},
    {trade_date:'20200102',ts_code:'000002.SZ',name:'其他股票不得混入',rzye:'999999',rzmre:'999999',rqye:'999999'},
  ],{rzye:'元',rzmre:'元',rqye:'元'});
  await mount(page,'reference',[margins],{selectedOnly:true,definition:{id:'margin-detail',title:'融资融券明细',scope:'market',family:'tushare_margin_detail',dataset:'tushare.margin_detail',filterSelected:true}});
  await page.goto(widgetsFixturePath+'?trade_day=2020-01-02&instrument='+id);
  const table=page.getByRole('region',{name:'融资融券明细',exact:true});
  await expect(table.locator('tbody tr')).toHaveCount(2);
  await expect(table).toContainText('0.0012345678元');
  await expect(table).not.toContainText('其他股票不得混入');
  await expect(page.getByLabel('融资融券明细查看范围')).toHaveCount(0);
  await expect(page.getByRole('figure',{name:'融资融券明细已存趋势'})).toBeVisible();
  expect(requests.filter(url=>url.searchParams.get('dataset')==='tushare.margin_detail').every(url=>!url.searchParams.has('instrument_id'))).toBe(true);
  const factor=record('tushare.adj_factor','tushare_adj_factor',[{trade_date:'20200102',ts_code:'000001.SZ',adj_factor:'1.000123456789'}]);
  await mount(page,'dataset',[],{definition:{id:'adj-factor',title:'复权记录'},record:factor,rows:factor.payload.rows});
  await expect(page.getByRole('cell',{name:'1.000123456789',exact:true})).toBeVisible();
});

test('all-stock table keeps query sorting paging and DOM when a row links the homepage stock',async({page})=>{
  const rows=Array.from({length:24},(_,index)=>({trade_date:'20200102',ts_code:index===23?'000001.SZ':String(index+2).padStart(6,'0')+'.SZ',name:'合成名单'+index,pre_close:'10',up_limit:String(11+index),down_limit:'9'}));
  const limits=record('tushare.market_limits','tushare_market_limits',rows);
  await page.route('**/api/v1/market-quotes?*',route=>route.fulfill({json:{meta,data:{items:[{instrument:{id,symbol:'000001',name:'合成甲',exchange:'SZSE',asset_type:'stock'}}]}}}));
  await mount(page,'reference',[limits],{definition:{id:'market-limits',title:'当日全市场限制价',scope:'market',family:'tushare_market_limits',dataset:'tushare.market_limits',filterSelected:true}});
  const table=page.getByRole('region',{name:'当日全市场限制价',exact:true});
  await page.getByRole('searchbox',{name:'当日全市场限制价查找'}).fill('合成名单');
  await table.getByRole('button',{name:/涨停价/}).click();
  await page.getByRole('button',{name:'下一页',exact:true}).first().click();
  await expect(table.locator('tbody tr')).toHaveCount(4);
  // The chosen row is at the top in descending order; choose ascending so it
  // lands on page two and proves pagination survives the selection event.
  await table.getByRole('button',{name:/涨停价/}).click();
  await page.getByRole('button',{name:'下一页',exact:true}).first().click();
  await expect(table.locator('tbody tr').last()).toContainText('合成名单23');
  await table.evaluate(element=>{(element as HTMLElement).dataset.testMarker='same-table';});
  await table.getByRole('button',{name:'查看 合成名单23 行情',exact:true}).click();
  await expect(page).toHaveURL(new RegExp('instrument='+id));
  await expect(table).toHaveAttribute('data-test-marker','same-table');
  await expect(page.getByRole('searchbox',{name:'当日全市场限制价查找'})).toHaveValue('合成名单');
  await expect(table.locator('tbody tr')).toHaveCount(4);
  await expect(table.getByRole('columnheader',{name:/涨停价/})).toHaveAttribute('aria-sort','ascending');
});

test('current stock funds bind exact instrument and trade day, show actual saved history and never convert unknown units',async({page})=>{
  const money=record('tushare.moneyflow','tushare_moneyflow',[
    {trade_date:'20200101',ts_code:'000001.SZ',net_mf_amount:'-1',buy_lg_amount:'2',sell_lg_amount:'3'},
    {trade_date:'20200102',ts_code:'000001.SZ',net_mf_amount:'5',buy_lg_amount:'8',sell_lg_amount:'3'},
    {trade_date:'20200103',ts_code:'000001.SZ',net_mf_amount:'99',buy_lg_amount:'99',sell_lg_amount:'0'},
    {trade_date:'20200102',ts_code:'000002.SZ',net_mf_amount:'999',buy_lg_amount:'999',sell_lg_amount:'0'},
  ],{net_mf_amount:'万元',sell_lg_amount:'万元'});
  Object.assign(money,{instrument_id:id,data_mode:'after_market'});
  const requests:URL[]=[];
  page.on('request',request=>{if(request.url().includes('/api/v1/trading-datasets'))requests.push(new URL(request.url()));});
  await page.route('**/api/v1/market-quotes?*',route=>route.fulfill({json:{meta,data:{items:[{instrument:{id,symbol:'000001',name:'合成甲',exchange:'SZSE',asset_type:'stock'}}]}}}));
  await mount(page,'funds',[money]);
  await page.getByRole('radio',{name:'当前股',exact:true}).click();
  await expect(page.getByText('先选择一只股票',{exact:true})).toBeVisible();
  await page.goto(widgetsFixturePath+'?trade_day=2020-01-02&instrument='+id);
  await page.getByRole('radio',{name:'当前股',exact:true}).click();
  const daily=page.getByRole('region',{name:'当前股资金明细',exact:true});
  await expect(daily.locator('tbody tr')).toHaveCount(1);
  await expect(daily).toContainText('+5万');
  await expect(daily).toContainText('8（原单位未核）');
  await expect(daily).not.toContainText('999');
  await page.getByRole('radio',{name:'已存历史',exact:true}).click();
  const history=page.getByRole('region',{name:'当前股资金已存历史',exact:true});
  await expect(page.getByRole('figure',{name:'当前股资金已存趋势'}).locator('circle[data-trend-day]')).toHaveCount(2);
  await expect(history.locator('tbody tr')).toHaveCount(2);
  await expect(history).toContainText('2020-01-01');
  await expect(history).not.toContainText('2020-01-03');
  await expect(history).not.toContainText('999');
  expect(requests.some(url=>url.searchParams.get('dataset')==='tushare.moneyflow'&&url.searchParams.get('instrument_id')===id&&url.searchParams.get('day')==='2020-01-02')).toBe(true);
  await page.goto(widgetsFixturePath+'?trade_day=2020-01-02&instrument=00000000-0000-4000-8000-000000000093');
  await page.getByRole('radio',{name:'当前股',exact:true}).click();
  await expect(page.getByText('请选择股票',{exact:true})).toBeVisible();
  await expect(page.getByRole('region',{name:'当前股资金明细',exact:true})).not.toBeVisible();
});

test('financial history uses publication-filtered view rows, retains original evidence and respects a valid empty view',async({page})=>{
  const rows=[
    {ts_code:'000001.SZ',end_date:'20190930',ann_date:'20191030',total_revenue:'101'},
    {ts_code:'000001.SZ',end_date:'20191231',ann_date:'20200330',total_revenue:'909'},
  ];
  const income=record('tushare.income','tushare_income',rows,{total_revenue:'元'});
  Object.assign(income,{instrument_id:id,trade_day:null,data_mode:'reference',view_rows:[rows[0]]});
  await page.route('**/api/v1/market-quotes?*',route=>route.fulfill({json:{meta,data:{items:[{instrument:{id,symbol:'000001',name:'合成甲',exchange:'SZSE',asset_type:'stock'}}]}}}));
  await mount(page,'reference',[income],{definition:{id:'income',title:'利润表',description:'合成测试',defaultVisible:false,family:'tushare_income',dataset:'tushare.income',scope:'selected'}});
  await page.goto(widgetsFixturePath+'?trade_day=2020-01-02&instrument='+id);
  const comparison=page.getByRole('region',{name:'利润表报告期对比',exact:true});
  await expect(comparison).toContainText('2019-09-30');
  await expect(comparison).toContainText('101');
  await expect(comparison).not.toContainText('2019-12-31');
  await page.getByText('原始数据与口径',{exact:true}).click();
  const raw=page.getByRole('region',{name:'合成资料原始数据',exact:true});
  await expect(raw).toContainText('20191231');
  await expect(raw).toContainText('909');
  await expect(page.locator('.trade-details').filter({has:raw})).toContainText('9'.repeat(64));
  Object.assign(income,{view_rows:[]});
  await page.reload();
  await expect(comparison).toHaveCount(0);
  await expect(page.getByText('所选日期暂无已存资料',{exact:true})).toBeVisible();
  await page.getByText('原始数据与口径',{exact:true}).click();
  await expect(raw.locator('tbody tr')).toHaveCount(2);
});

test('saved macro trends preserve zero, break missing values and exclude future dates and months',async({page})=>{
  const rates=record('tushare.shibor','tushare_shibor',[
    {date:'20200101',on:'0',field_1w:'1'},
    {date:'20200102',on:null,field_1w:'2'},
    {date:'20200103',on:'2',field_1w:'3'},
    {date:'20200104',on:'999',field_1w:'999'},
  ],{on:'%',field_1w:'%'});
  await mount(page,'reference',[rates],{definition:{id:'shibor',title:'SHIBOR',description:'合成测试',defaultVisible:false,family:'tushare_shibor',dataset:'tushare.shibor',scope:'market'}});
  await page.goto(widgetsFixturePath+'?trade_day=2020-01-03');
  const chart=page.getByRole('figure',{name:'SHIBOR已存趋势'});
  await expect(chart.locator('circle')).toHaveCount(2);
  await expect(chart.locator('circle').first()).toContainText('0 %');
  await expect(chart.locator('path[data-saved-trend]')).toHaveCount(2);
  await expect(chart).toContainText('3 个日期，1 个缺失值');
  await expect(chart).not.toContainText('999');
  await page.getByLabel('SHIBOR趋势指标').selectOption('field_1w');
  await expect(chart.locator('circle')).toHaveCount(3);
  await expect(chart.locator('path[data-saved-trend]')).toHaveCount(1);
  const cpi=record('tushare.cn_cpi','tushare_cn_cpi',[{month:'201911',nt_yoy:'0',nt_mom:'-0.2'},{month:'201912',nt_yoy:'1',nt_mom:'0.2'},{month:'202002',nt_yoy:'999',nt_mom:'999'}],{nt_yoy:'%',nt_mom:'%'});
  Object.assign(cpi,{trade_day:null,view_rows:cpi.payload.rows});
  await mount(page,'reference',[cpi],{definition:{id:'cpi',title:'CPI',description:'合成测试',defaultVisible:false,family:'tushare_cn_cpi',dataset:'tushare.cn_cpi',scope:'market'}});
  const monthly=page.getByRole('figure',{name:'CPI已存趋势'});
  await expect(monthly.locator('circle')).toHaveCount(2);
  await expect(monthly).not.toContainText('999');
});
