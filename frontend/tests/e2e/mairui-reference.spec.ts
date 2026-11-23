import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const id = '00000000-0000-4000-8000-000000000023';
const meta = { request_id: id, app_mode: 'demo', generated_at: '2020-01-03T02:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic personal-reference fixture'] } };
function item(table: string, empty = false) {
  const funds = table.endsWith('flow');
  return { id, source_id: id, source_name: '合成本人参考来源', family: funds ? 'mairui_funds' : 'mairui_pools', source_key: 'reference:market.mairui.' + table, instrument_id: null,
    trade_day:'2020-01-02',data_mode:funds?'after_market':'reference',received_at: meta.generated_at, published_at: null, content_hash: '3'.repeat(64), payload: { dataset: 'market.mairui.' + table, title: '合成参考表', source_url: 'https://example.com/synthetic-reference', attribution: 'Synthetic fixture only', observed_day: '2020-01-02',
      scope: '合成本人研究参考，不是实时行情。原更新时间时区未明，股池日期依请求参数，不补精确发布时点。', columns: [{ key: 'name', label: '名称' },{key:'jlr',label:'净流入',unit:'元'}],
      rows: empty ? [] : Array.from({ length: 10 }, (_, index) => ({ source_order: String(index + 1), symbol: 'sz' + String(index + 1).padStart(6, '0'), name: '合成参考' + (index + 1),
        jlr: index === 0 ? '0' : index === 1 ? null : '-120000000', updated_raw: '2020-01-0215:40:00', p: '10.25', zf: '-1.25', lbc: index === 1 ? null : String(index % 3 + 2) })) } };
}

test('funds and three pools show source date, unknowns, supplier ladders and narrow layouts', async ({ page }, info) => {
  await page.route('**/api/v1/trading-datasets?*', route => {
    const dataset = new URL(route.request().url()).searchParams.get('dataset') ?? '';
    const table=dataset.split('.').at(-1)!;
    return route.fulfill({ json: { meta, data: { items: dataset.startsWith('market.mairui.')?[item(table)]:[],available_days:['2020-01-02'],trade_day:'2020-01-02' } } });
  });
  await page.goto('/workbench?legacy=1'); await signIn(page);
  const funds = page.getByRole('region', { name: '资金流向排行', exact: true });
  const pools = page.getByRole('region', { name: '涨跌停股池参考', exact: true });
  await funds.getByRole('radio', { name: '全部', exact: true }).click();
  const fundTable=funds.getByRole('region',{name:'资金流向排名',exact:true});
  await expect(fundTable.locator('tbody tr')).toHaveCount(10);
  await expect(funds).toContainText('2020-01-02');
  await expect(funds).toContainText('盘后资料 · 非实时');
  await expect(fundTable.locator('tbody tr').filter({hasText:'合成参考1'}).first()).toContainText('0元');
  await expect(fundTable.locator('tbody tr').filter({hasText:'合成参考2'})).toContainText('—');
  await expect(fundTable).toContainText('-1.2亿');
  await funds.getByRole('radio',{name:'行业',exact:true}).click();
  await expect(fundTable.locator('tbody tr')).toHaveCount(10);
  await expect(pools).toContainText('2020-01-02');
  const poolTable=pools.getByRole('region',{name:'连板梯队与封板明细',exact:true});
  await expect(poolTable.locator('tbody tr')).toHaveCount(10);
  await expect(poolTable).toContainText('2连板 · 供应商口径');
  await expect(poolTable).toContainText('板数未知');
  await expect(poolTable.getByRole('cell',{name:'—',exact:true}).first()).toBeVisible();
  await pools.getByRole('radio', { name: '炸板', exact: true }).click();
  await expect(poolTable.locator('tbody tr')).toHaveCount(10);
  await expect(poolTable).toContainText('原因未提供');
  await pools.getByRole('radio', { name: '跌停', exact: true }).click();
  await expect(poolTable.locator('tbody tr')).toHaveCount(10);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const [name, region] of [['funds', funds], ['pools', pools]] as const) {
      await region.scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: info.outputPath('mairui-' + name + '-' + width + '.png'), animations: 'disabled' });
    }
  }
  await funds.getByText('原始数据与口径',{exact:true}).click();
  await expect(funds.getByRole('link',{name:'原始出处 ↗'})).toHaveAttribute('href','https://example.com/synthetic-reference');
  await funds.getByRole('radio',{name:'当前股',exact:true}).click();
  await expect(funds).toContainText('先选择一只股票');
});

test('loading, failed read, retry, empty and uncollected references never retain old rows as success', async ({ page }) => {
  let state = 'loading'; let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/trading-datasets?*', async route => {
    const dataset = new URL(route.request().url()).searchParams.get('dataset') ?? '';
    if(!dataset.startsWith('market.mairui.'))return route.fulfill({json:{meta,data:{items:[],available_days:[],trade_day:null}}});
    if (state === 'loading') await ready;
    if (state === 'error') return route.fulfill({ status: 503, json: { request_id: id, error: { code: 'unavailable', message: '合成参考读取失败' } } });
    const table = new URL(route.request().url()).searchParams.get('dataset')!.split('.').at(-1)!;
    return route.fulfill({ json: { meta, data: { items: state === 'absent' ? [] : [item(table, true)],available_days:['2020-01-02'],trade_day:'2020-01-02' } } });
  });
  await page.goto('/workbench?legacy=1'); await signIn(page);
  const funds = page.getByRole('region', { name: '资金流向排行', exact: true });
  await expect(funds).toContainText('正在读取…');
  state = 'error'; release();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成参考读取失败' });
  await expect(notice).toBeVisible();
  state = 'empty'; await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(funds).toContainText('没有匹配记录');
  await expect(funds.getByRole('region',{name:'资金流向排名',exact:true}).locator('tbody tr')).toHaveCount(0);
  state = 'absent'; await funds.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(funds).toContainText('所选日期暂无已存资料');
  await expect(funds.getByRole('region',{name:'资金流向排名',exact:true})).toHaveCount(0);
});

test('data center exposes two adapters, local-only credential state and shared free quota', async ({ page }, info) => {
  await page.goto('/data?view=sources'); await signIn(page);
  const funds = page.getByRole('article', { name: '麦蕊免费盘后资金参考', exact: true });
  const pools = page.getByRole('article', { name: '麦蕊免费涨跌停股池', exact: true });
  for (const card of [funds, pools]) {
    await expect(card).toContainText('未配置'); await expect(card).toContainText('共享本机450次/日');
    await expect(card).toContainText('尚未检测到证书'); await expect(card).toContainText('覆盖数按表版本计');
  }
  await expect(funds).toContainText('每小时2次'); await expect(pools).toContainText('每15分钟3次');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 }); await pools.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('mairui-source-' + width + '.png'), animations: 'disabled' });
  }
});
