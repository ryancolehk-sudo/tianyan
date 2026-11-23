import { test, expect } from '@playwright/test';
import { signIn } from './researchTestHelpers';
import type { SourceIndex } from '../../src/generated/schema';

// Exact implemented connection identities, not a statement of purchased
// entitlement or complete market coverage. New adapters need explicit review.
const expectedTushareFamilies = [
  'tushare_moneyflow_mkt_dc', 'tushare_stock_basic', 'tushare_calendar_sse', 'tushare_calendar_szse',
  'tushare_rt_k', 'tushare_market_rt', 'tushare_rt_indices', 'tushare_market_limits',
  'tushare_daily', 'tushare_index_daily', 'tushare_daily_basic', 'tushare_adj_factor', 'tushare_moneyflow',
  'tushare_limit_list_ths', 'tushare_limit_list_d', 'tushare_income', 'tushare_balancesheet', 'tushare_cashflow',
  'tushare_fina_indicator', 'tushare_top_list', 'tushare_top_inst', 'tushare_block_trade', 'tushare_margin_detail',
  'tushare_ths_index', 'tushare_ths_member', 'tushare_ths_daily', 'tushare_shibor', 'tushare_cn_cpi',
  'tushare_stk_auction', 'tushare_dc_index', 'tushare_dc_member', 'tushare_direction_industry',
  'tushare_direction_industry_funds', 'tushare_moneyflow_ind_dc', 'tushare_moneyflow_dc', 'tushare_limit_step',
  'tushare_stk_premarket', 'tushare_index_basic_sse', 'tushare_index_basic_szse', 'tushare_stk_limit', 'tushare_dividend',
];

test('purchased source priority, honest missing entitlements and bounded setup at two widths', async ({ page }, info) => {
  await page.goto('/data?view=sources');
  await signIn(page);
  const sourceResponse = await page.request.get('/api/v1/data-sources');
  expect(sourceResponse.ok()).toBe(true);
  const sourceIndex = (await sourceResponse.json()).data as SourceIndex;
  const adapters = sourceIndex.available_adapters.filter(adapter => adapter.family.startsWith('tushare_'));
  expect(adapters.map(adapter => adapter.family).sort()).toEqual([...expectedTushareFamilies].sort());
  expect(sourceIndex.items.filter(source => source.family.startsWith('tushare_'))).toEqual([]);
  for (const adapter of adapters) {
    const sourceCard = page.getByRole('article', { name: adapter.name, exact: true });
    await expect(sourceCard).toContainText(adapter.family);
    await expect(sourceCard).toContainText('未配置');
    await expect(sourceCard).toContainText('待本机配置凭据');
    await expect(sourceCard).not.toContainText('完整返回');
    await expect(sourceCard.getByRole('button', { name: '采集一次', exact: true })).toBeDisabled();
  }
  for (const family of ['tushare_moneyflow_mkt_dc', 'tushare_ths_daily', 'tushare_direction_industry', 'tushare_direction_industry_funds'])
    expect(adapters.find(adapter => adapter.family === family)?.capabilities).toEqual(['reference.tushare']);
  expect(adapters.flatMap(adapter => adapter.capabilities)).not.toContain('bar.minute');
  expect(adapters.flatMap(adapter => adapter.capabilities)).not.toContain('news.public');
  const overview = page.getByLabel('Tushare接入概况', { exact: true });
  await expect(overview).toContainText(`${adapters.length} 条有界连接`);
  await expect(overview).toContainText('已配置 0 条');
  await expect(overview).toContainText('已实测批量行情、四指数和全市场限制价返回成功');
  await expect(overview).toContainText('接口预检不代表已经部署');
  await expect(overview).toContainText('MCP工具目录不代表全部权益');
  await expect(overview).toContainText('实时分钟、历史分钟与新闻仍需重新核验');
  await expect(overview).toContainText('北交所日历未核，不代填日历');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await overview.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`tushare-priority-${width}.png`), animations: 'disabled' });
  }
  const card = page.getByRole('article', { name: 'Tushare · 个股行情快照', exact: true });
  await expect(card).toContainText('未配置');
  await expect(card).toContainText('共用两秒本地请求间隔');
  await expect(card).toContainText('供应商额度尚未提供，实际限流继续退避');
  await expect(card).toContainText('待本机配置凭据');
  await expect(card).not.toContainText('完整返回');
  const policy=card.getByLabel('盘中行情采集策略');
  await expect(policy).toContainText('09:30至11:30、13:00至15:00');
  await expect(policy).toContainText('11:30午休暂停');
  await expect(policy).toContainText('15:00收盘停止');
  await expect(policy).not.toContainText('11:31');
  await expect(policy).not.toContainText('15:01');
  await expect(policy).toContainText('已核当日开市日历');
  const quoteAdapter = adapters.find(adapter => adapter.family === 'tushare_rt_k')!;
  await expect(card.locator('details').filter({ has: page.getByText('查看来源范围与配置依据', { exact: true }) })).toContainText(`本机最低间隔 ${quoteAdapter.minimum_interval} 秒`);
  await expect(policy).toContainText('采集间隔不等于行情延迟');
  await expect(policy).toContainText('页面读取本机缓存');
  await expect(policy).toContainText('源行情时间与接收时间分别保留');
  for (const name of ['沪市指数基础资料','深市指数基础资料','所选股票每日涨跌停价格','所选股票分红送股资料',
    'DC概念板块目录（日频）','DC概念板块历史成分','DC概念板块盘后资金','DC个股盘后资金','当日盘前股本','收盘连板天梯']) {
    await expect(page.getByRole('article',{name:'Tushare · '+name,exact:true})).toContainText('未配置');
  }
  const topList=page.getByRole('article',{name:'Tushare · 龙虎榜',exact:true});
  await expect(topList.getByLabel('盘后自动采集策略')).toContainText('16:30–21:30');
  await expect(topList.getByLabel('盘后自动采集策略')).toContainText('不保证上游发布时间');
  const dailyList=page.getByRole('article',{name:'Tushare · 涨跌停与炸板榜',exact:true});
  await expect(dailyList.getByLabel('盘后自动采集策略')).toContainText('16:00–21:30');
  const limit = page.getByRole('article',{name:'Tushare · 所选股票每日涨跌停价格',exact:true});
  await limit.getByRole('button',{name:'配置来源',exact:true}).click();
  await expect(page.getByRole('textbox',{name:'采集标的',exact:true})).toHaveValue('SZSE:stock:000001');
});

test('paid pool wins without field mixing, valid empty remains empty and failed paid read uses free cached table', async ({ page }) => {
  let state = 'paid';
  const id='00000000-0000-4000-8000-000000000092';
  const meta={request_id:id,app_mode:'demo',generated_at:'2020-01-03T02:00:00Z',as_of_time:null,quality:{status:'partial',reasons:['Synthetic source priority fixture']}};
  await page.route('**/api/v1/trading-datasets?*', async route => {
    const dataset = new URL(route.request().url()).searchParams.get('dataset') ?? '';
    if (!['tushare.limit_list_d','market.mairui.limit_up'].includes(dataset)) return route.fulfill({json:{meta,data:{items:[],available_days:[],trade_day:null}}});
    const paid=dataset.startsWith('tushare.');
    if (paid && state==='error') return route.fulfill({status:503,json:{request_id:id,error:{code:'unavailable',message:'合成已购表读取失败'}}});
    const row=paid?{ts_code:'000001.SZ',name:'合成已购股池',trade_date:'20200102',limit:'U',close:'12.30',pct_chg:'10',limit_times:'2',up_stat:'2/2'}:
      {symbol:'sz000001',name:'合成免费股池',p:'10.25',zf:'8',lbc:'3'};
    const item={id,source_id:id,source_name:paid?'合成已购来源':'合成免费来源',family:paid?'tushare_limit_list_d':'mairui_pools',
      trade_day:'2020-01-02',data_mode:paid?'after_market':'reference',source_key:'reference:'+dataset,instrument_id:null,received_at:meta.generated_at,published_at:null,content_hash:'9'.repeat(64),
      payload:{dataset,title:'合成股池',source_url:'https://example.com/synthetic-pool',attribution:'Synthetic fixture only',scope:'Synthetic source priority test.',observed_day:'2020-01-02',
        columns:Object.keys(row).map(key=>({key,label:key,unit:null})),rows:paid && state==='empty'?[]:[row]}};
    await route.fulfill({json:{meta,data:{items:state==='denied'?[]:[item],available_days:['2020-01-02'],trade_day:'2020-01-02'}}});
  });
  await page.goto('/workbench?legacy=1');await signIn(page);
  const pools=page.getByRole('region',{name:'涨跌停股池参考',exact:true});
  const writes:string[]=[];
  page.on('request',request=>{if(new URL(request.url()).pathname.startsWith('/api/')&&request.method()!=='GET')writes.push(request.url());});
  await expect(pools).toContainText('合成已购股池');
  await expect(pools).not.toContainText('合成免费股池');
  const table=pools.getByRole('region',{name:'连板梯队与封板明细',exact:true});
  await expect(table).toContainText('+10%');
  await expect(table).toContainText('2连板');
  await expect(table).toContainText('2天2板');
  await pools.getByText('原始数据与口径',{exact:true}).click();
  await expect(pools.getByRole('region',{name:'合成股池原始数据',exact:true})).toContainText('12.30');
  state='empty';await pools.getByRole('button',{name:'刷新',exact:true}).click();
  await expect(pools).toContainText('当前分类没有匹配记录');
  await expect(table).toHaveCount(0);
  await expect(pools).not.toContainText('合成已购股池');
  await expect(pools).not.toContainText('合成免费股池');
  state='error';await pools.getByRole('button',{name:'刷新',exact:true}).click();
  await expect(pools).toContainText('Tushare读取失败，显示麦蕊参考');
  await expect(table).toContainText('合成免费股池');
  await expect(table).toContainText('+8%');
  await expect(table).toContainText('3连板 · 供应商口径');
  const raw=pools.getByRole('region',{name:'合成股池原始数据',exact:true});
  if(!await raw.isVisible())await pools.getByText('原始数据与口径',{exact:true}).click();
  await expect(raw).toContainText('10.25');
  await expect(raw).not.toContainText('12.30');
  state='denied';await pools.getByRole('button',{name:'刷新',exact:true}).click();
  await expect(pools).toContainText('所选日期暂无已存资料');
  await expect(pools).not.toContainText('合成免费股池');
  await expect(pools).not.toContainText('合成已购股池');
  expect(writes).toEqual([]);
});
