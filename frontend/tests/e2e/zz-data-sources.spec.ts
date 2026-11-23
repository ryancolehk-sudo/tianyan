import { openNotifications } from './isolated-browser.fixtures';
import { openLegacyWorkbench } from './legacyWorkbench';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { post, signIn } from './researchTestHelpers';

async function login(page: Page, username?: string) {
  await page.goto('/data?view=sources');
  await signIn(page, username);
  await expect(page.locator('.source-runtime')).toBeVisible();
}
const connection = (page: Page, name = '腾讯网页行情') => page.getByRole('article', { name, exact: true });
const view = (page: Page, name: string) => page.getByRole('radiogroup', { name: '数据管理工具' }).getByRole('radio', { name, exact: true }).click();

test('synthetic collection binds exact source, real counts and immutable calls across screen sizes', async ({ page }, info) => {
  await login(page);
  const unexpected: string[] = [];
  const origin = new URL(page.url()).origin;
  page.on('request', request => { if (new URL(request.url()).origin !== origin || request.method() !== 'GET') unexpected.push(request.method()); });
  await expect(page.getByTestId('summary-今日调用')).toHaveText('1');
  await expect(page.getByTestId('summary-预计费用')).toHaveText('—');
  await expect(connection(page)).toContainText('完整返回');
  await expect(connection(page)).toContainText('2 新增 / 0 重复 / 2 预期');
  await expect(connection(page)).toContainText('本机总开关关闭');
  await expect(connection(page).getByRole('button', { name: '采集一次', exact: true })).toBeDisabled();
  const catalog = page.locator('.integration-catalog');
  await catalog.getByRole('radio', { name: '候选来源', exact: true }).click();
  await catalog.getByLabel('清单关键词', { exact: true }).fill('腾讯网页');
  await expect(catalog.getByText('已配置', { exact: true })).toBeVisible();
  for (const width of [1920, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await connection(page).scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`source-state-${width}.png`), animations: 'disabled' });
  }
  await view(page, '调用记录');
  await expect(page.getByRole('region', { name: '来源调用记录表格' })).toContainText('腾讯网页行情');
  await expect(page.getByRole('region', { name: '来源调用记录表格' })).toContainText('2 / 0 / 2');
  await expect(page.getByRole('region', { name: '来源调用记录表格' })).toContainText('1 次 / 3 ms');
  await page.screenshot({ path: info.outputPath('source-calls-390.png'), animations: 'disabled' });
  await page.reload();
  await expect(page).toHaveURL(/view=calls/);
  await expect(page.getByRole('region', { name: '来源调用记录表格' })).toBeVisible();
  expect(unexpected).toEqual([]);
});

test('admin can save disabled source, unknown rights block enable and do not cause collection', async ({ page }, info) => {
  await login(page);
  const card = connection(page, '深交所官方日历');
  await card.getByRole('button', { name: '配置来源', exact: true }).click();
  const form = card.getByRole('form', { name: '配置深交所官方日历' });
  await form.getByLabel('使用依据', { exact: true }).fill('Self-created UI configuration test only; no grant for actual upstream data.');
  await form.getByRole('checkbox', { name: '启用此来源', exact: true }).check();
  await expect(form.getByRole('button', { name: '保存来源配置', exact: true })).toBeDisabled();
  await expect(form.getByRole('alert')).toContainText('启用需要先核实');
  await form.getByRole('checkbox', { name: '启用此来源', exact: true }).uncheck();
  await form.getByRole('button', { name: '保存来源配置', exact: true }).click();
  await expect(card.getByText('未启用', { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId('summary-今日调用')).toHaveText('1');
  await page.reload();
  await connection(page, '深交所官方日历').getByRole('button', { name: '修改配置', exact: true }).click();
  await expect(page.getByLabel('连接权益', { exact: true })).toHaveValue('unknown');
  await expect(page.getByLabel('数据缓存权利', { exact: true })).toHaveValue('unknown');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel('连接权益', { exact: true }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('source-config-390.png'), animations: 'disabled' });
});

test('read failure remains failure, retry recovers and empty source history is distinct', async ({ page }, info) => {
  await login(page);
  await post(page, 'data-sources', { family: 'szse_public', symbols: [], interval_seconds: 3600,
    rights_basis: 'Synthetic disabled source to test genuinely empty local call history.',
    rights: { display: 'unknown', cache: 'unknown', export: 'unknown', redistribute: 'unknown' }, entitlement_status: 'unknown', enabled: false });
  await expect(connection(page)).toBeVisible();
  await page.route('**/api/v1/data-sources', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'database_unavailable', message: '合成来源状态故障', details: [] }, request_id: '00000000-0000-4000-8000-000000000019' }) }));
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  const sourceNotice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成来源状态故障' });
  await expect(sourceNotice).toBeVisible();
  await expect(page.getByTestId('summary-今日调用')).toHaveText('—');
  await page.screenshot({ path: info.outputPath('source-read-error.png'), animations: 'disabled' });
  await page.unroute('**/api/v1/data-sources');
  await sourceNotice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(sourceNotice).toHaveCount(0); await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(connection(page)).toBeVisible();
  await view(page, '调用记录');
  await page.getByLabel('调用记录来源', { exact: true }).selectOption({ label: '深交所官方日历' });
  await expect(page.getByText('尚无采集记录', { exact: true })).toBeVisible();
  await page.route('**/api/v1/data-fetch-runs?**', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'database_unavailable', message: '合成调用记录故障', details: [] }, request_id: '00000000-0000-4000-8000-000000000020' }) }));
  await page.getByRole('button', { name: '刷新调用记录', exact: true }).click();
  const callsNotice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成调用记录故障' });
  await expect(callsNotice).toBeVisible();
  // Collection status remains unknown until a successful status read.
  await expect(page.getByText('尚无采集记录', { exact: true })).toHaveCount(0);
  await page.unroute('**/api/v1/data-fetch-runs?**');
  await callsNotice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(callsNotice).toHaveCount(0); await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(page.getByText('尚无采集记录', { exact: true })).toBeVisible();
});

test('viewer sees shared source status and calls with no configuration controls', async ({ page }) => {
  await login(page, 'e2e-viewer');
  await expect(connection(page)).toContainText('完整返回');
  await expect(page.locator('.source-runtime').getByRole('button')).toHaveCount(0);
  await expect(page.locator('.source-runtime')).toContainText('当前账号可查看状态');
  await view(page, '调用记录');
  await expect(page.getByRole('region', { name: '来源调用记录表格' })).toContainText('腾讯网页行情');
});

test('saved quote selection keeps same-code market identity, provenance and SPA history', async ({ page }, info) => {
  await login(page);
  await expect(page.locator('.integration-catalog').getByText('本机检索可用', {exact:true})).toBeVisible();
  await expect(page.locator('.integration-catalog').getByText('仅有较早报价', {exact:true})).toBeVisible();
  await openLegacyWorkbench(page);
  const card=page.locator('.quote-card');
  await expect(card).toContainText('沪市 · 指数 · 000001');
  await expect(card).toContainText('较早报价');
  await expect(card.locator('.quote-price strong')).toHaveText('12.30 点');
  await expect(page.getByRole('radio', { name: '日K', exact: true })).toBeChecked();
  await page.getByRole('radio', { name: '分时', exact: true }).click();
  await expect(page.getByRole('img', { name: '分时图表，暂无可用行情', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: '行情图表', exact: true })).toContainText('分钟来源未配置');
  const documents: string[]=[];
  page.on('request',request=>{if(request.resourceType()==='document')documents.push(request.url());});
  await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill('000001');
  await page.getByRole('button', { name: '搜索标的', exact: true }).click();
  const results=page.getByRole('region', { name: '标的查询结果', exact: true });
  await expect(results.getByRole('link')).toHaveCount(2);
  await expect(results).toContainText('SSE · 指数');
  await expect(results).toContainText('SZSE · 股票');
  const stockResult = results.getByRole('link').filter({ hasText: 'SZSE · 股票' });
  await expect(stockResult).toHaveCount(1);
  await expect(stockResult).toContainText('000001');
  await stockResult.click();
  await expect(card).toContainText('深市 · 股票 · 000001');
  await expect(card.locator('.quote-price strong')).toHaveText('12.30 元');
  await expect(page).toHaveURL(/\/workbench\?legacy=1&instrument=[a-f0-9-]+$/);
  await card.getByText('报价来源与数据详情', { exact: true }).click();
  await expect(card).toContainText('2001/12/31');
  await expect(card).toContainText('来源发布时间');
  await expect(card).toContainText('只能用于本机接收之后的研究');
  const id=new URL(page.url()).searchParams.get('instrument')!;
  const response=await page.request.get('/api/v1/market-quotes?instrument_id='+id);
  const record=(await response.json()).data.items[0];
  await expect(card).toContainText(record.provenance.content_hash);
  await page.goBack();
  await expect(card).toContainText('沪市 · 指数 · 000001');
  await page.goForward();
  await expect(card).toContainText('深市 · 股票 · 000001');
  expect(documents).toEqual([]);
  await page.reload();
  await expect(card).toContainText('深市 · 股票 · 000001');
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:width===390?844:1080});
    await card.scrollIntoViewIfNeeded();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath(`saved-quote-${width}.png`),animations:'disabled'});
  }
});

test('cached quote loading and failure never reuse a different selected instrument', async ({ page }, info) => {
  await login(page);
  const instruments=await (await page.request.get('/api/v1/instruments?q=000001')).json();
  const stock=instruments.data.items.find((item:{exchange:string;asset_type:string})=>item.exchange==='SZSE'&&item.asset_type==='stock');
  const selected='/api/v1/market-quotes?instrument_id='+stock.id;
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**'+selected,async route=>{
    await pending;
    await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{code:'database_unavailable',message:'合成报价读取故障',details:[]},request_id:'00000000-0000-4000-8000-000000000021'})});
  });
  await page.goto('/workbench?legacy=1&instrument='+stock.id);
  const card=page.locator('.quote-card');
  await expect(card.getByText('正在读取…',{exact:true})).toBeVisible();
  await expect(card.locator('.quote-price strong')).toHaveText('—');
  release();
  const quoteNotice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成报价读取故障' });
  await expect(quoteNotice).toBeVisible();
  await expect(card).not.toContainText('沪市 · 指数 · 000001');
  await page.screenshot({path:info.outputPath('saved-quote-error.png'),animations:'disabled'});
  await page.unroute('**'+selected);
  await quoteNotice.getByRole('button',{name:'重新读取',exact:true}).click();
  await expect(quoteNotice).toHaveCount(0); await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(card).toContainText('深市 · 股票 · 000001');
  await expect(card.locator('.quote-price strong')).toHaveText('12.30 元');
});

test('quote cache refresh preserves shell and source disable hides saved values without new upstream calls', async ({ page }) => {
  await login(page);
  await openLegacyWorkbench(page);
  const card=page.locator('.quote-card');
  await expect(card.locator('.quote-price strong')).toHaveText('12.30 点');
  await page.clock.install();
  await page.clock.runFor(30_100);
  await expect(card.locator('.quote-price strong')).toHaveText('12.30 点');
  await expect(page.getByRole('navigation',{name:'主导航',exact:true})).toBeVisible();
  const sources=(await (await page.request.get('/api/v1/data-sources')).json()).data;
  expect(sources.today_request_count).toBe(1);
  const source=sources.items.find((item:{family:string})=>item.family==='tencent_web');
  const body={family:'tencent_web',symbols:source.symbols,interval_seconds:source.interval_seconds,rights_basis:source.rights_basis,
    rights:source.provider.rights,entitlement_status:source.provider.entitlement_status,enabled:false};
  try{
    await post(page,'data-sources',body);
    await card.getByRole('button',{name:'刷新报价',exact:true}).click();
    await expect(card).toContainText('来源未启用');
    await expect(card.locator('.quote-price strong')).toHaveText('—');
  }finally{
    await post(page,'data-sources',{...body,enabled:true});
  }
  await card.getByRole('button',{name:'刷新报价',exact:true}).click();
  await expect(card.locator('.quote-price strong')).toHaveText('12.30 点');
  expect((await (await page.request.get('/api/v1/data-sources')).json()).data.today_request_count).toBe(1);
});
