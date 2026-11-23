import { openNotifications } from './isolated-browser.fixtures';
import { createHash } from 'node:crypto';
import { expect, test } from '@playwright/test';
import { post, screenshots, signIn } from './researchTestHelpers';

// These outputs are invented test text. Never copy personal reports into tests.
function source(path: string, text: string) {
  const raw = Buffer.from(text, 'utf8');
  return { path, text, sha256: createHash('sha256').update(raw).digest('hex'),
    blob: createHash('sha1').update(Buffer.concat([Buffer.from(`blob ${raw.length}\0`), raw])).digest('hex') };
}
function body(version = 1, withSummary = true) {
  const header = `当前复盘数据截止2001-12-31 15:10:00。\n以下内容针对目标交易日2002-01-02进行复盘：\n依据2001-12-31收盘资料｜第${version}版｜晚间复盘定稿2001-12-31 21:00:00`;
  const text = `${header}\n\n# 市场判断\n这是合成完整稿，保留原有段落与依据日，不是真实行情。\n\n# 方向与角色\n合成方向用于检查阅读顺序；没有真实股票或观点。\n\n# 条件与反证\n只核查合成材料是否满足原条件，不推断当前市场。\n\n# 原文边界\n<script>window.archiveInjected=true</script>\n![外链图片](https://invalid.example/private.png)\n\n# 完整稿末段\n仅供隔离浏览器验收，不是交易建议。`;
  const card = { schema: '2.0', body: { elements: [{ tag: 'column_set', columns: [{ tag: 'column', elements: [
    { tag: 'markdown', content: `<font color='grey'>${header}</font>` },
    { tag: 'markdown', content: '**市场判断**\n原作者的合成总结，只展示原句，不由阅读页生成。' },
    { tag: 'markdown', content: '**风险与边界**\n保留条件与反证 &#91;原文&#93;，无真实市场结论。' },
  ] }] }] } };
  return { source_repository: 'example/research-archive', source_commit: createHash('sha1').update(crypto.randomUUID()).digest('hex'),
    full: source(`synthetic/report-v${version}.md`, text),
    summary: withSummary ? source(`synthetic/summary-v${version}.json`, JSON.stringify(card)) : null,
    rights_basis: '本人自制隔离验收样本，不含私人研究或真实行情' };
}
async function open(page: import('@playwright/test').Page) {
  await page.goto('/research/reviews?legacy=1'); await signIn(page);
  await expect(page.getByRole('radio', { name: '归档阅读', exact: true })).toBeChecked();
  await expect(page.getByLabel('研究日期', { exact: true })).toHaveCount(0);
  await expect(page.locator('.research-toolbar-controls').getByLabel('归档适用日', { exact: true })).toBeVisible();
  await expect(page.locator('.archive-catalog').getByLabel('归档适用日', { exact: true })).toHaveCount(0);
}
const collection = /\/api\/v1\/review-archives(?:\?.*)?$/;

test('archive catalogue distinguishes failure, retry and truly empty results', async ({ page }, info) => {
  await page.route(collection, route => route.fulfill({ status: 503, json: { error: { code: 'synthetic_unavailable', message: '合成目录故障，请重试。' }, request_id: '00000000-0000-4000-8000-000000000001' } }));
  await open(page);
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成目录故障，请重试。' });
  await expect(notice).toBeVisible();
  await expect(page.getByText('尚未导入历史复盘', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('archive-catalog-error-1440.png'), fullPage: true });
  await page.unroute(collection);
  await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(page.getByText('尚未导入历史复盘', { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('archive-empty-1440.png'), fullPage: true });
});

test('same-version original summary and full text retain exact provenance, literal safety and URL history', async ({ page }, info) => {
  await open(page);
  const input = body(); const saved = await post(page, 'review-archives', input);
  const requests: string[] = [];
  page.on('request', request => { if (request.url().includes('invalid.example')) requests.push(request.url()); });
  await page.getByRole('button', { name: '刷新归档', exact: true }).click();
  await page.getByRole('navigation', { name: '已导入的复盘版本' }).getByRole('link').filter({ hasText: '第 1 版' }).click();
  const reader = page.getByLabel('复盘归档正文', { exact: true });
  await expect(reader.getByText('原作者的合成总结，只展示原句，不由阅读页生成。', { exact: true })).toBeVisible();
  await expect(reader.getByText('保留条件与反证 [原文]，无真实市场结论。', { exact: true })).toBeVisible();
  await expect(reader.getByText('依据 2001-12-31 收盘资料 · 晚间复盘', { exact: true })).toBeVisible();
  await screenshots(page, info, 'archive-summary');
  await page.getByRole('radio', { name: '完整稿', exact: true }).focus(); await page.keyboard.press('Enter');
  await expect(reader.getByRole('heading', { name: '完整稿末段', exact: true })).toBeVisible();
  await expect(reader.getByText('<script>window.archiveInjected=true</script>', { exact: false })).toBeVisible();
  expect(await page.evaluate(() => (window as any).archiveInjected)).toBeUndefined();
  expect(requests).toEqual([]);
  expect(new URL(page.url()).searchParams.get('archive')).toBe(saved.id);
  expect(new URL(page.url()).searchParams.get('report')).toBe('full');
  await page.reload(); await expect(reader.getByRole('heading', { name: '完整稿末段', exact: true })).toBeVisible();
  await reader.getByText('来源、版本与本机导入记录', { exact: true }).click();
  await expect(reader.getByText(input.full.sha256, { exact: true })).toBeVisible();
  await expect(reader.getByRole('link', { name: '查看固定来源文件', exact: true }).first()).toHaveAttribute('href', `https://github.com/${input.source_repository}/blob/${input.source_commit}/${input.full.path}`);
  await screenshots(page, info, 'archive-full-source');
  await page.getByRole('radio', { name: '原总结', exact: true }).click();
  await page.goBack(); await expect(page.getByRole('radio', { name: '完整稿', exact: true })).toBeChecked();
  await page.goForward(); await expect(page.getByRole('radio', { name: '原总结', exact: true })).toBeChecked();
  await page.getByLabel('归档适用日', { exact: true }).fill('2002-01-02');
  await expect(reader.getByRole('heading', { name: '2002-01-02 · 第 1 版', exact: true })).toBeVisible();
  await expect(page.getByRole('radio', { name: '归档阅读', exact: true })).toBeChecked();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.evaluate(() => window.scrollTo(0, 0));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`review-date-${width}.png`) });
  }
  await page.getByLabel('归档适用日', { exact: true }).fill('2002-01-03');
  await expect(page.getByText('这个适用日没有已导入的复盘', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: info.outputPath('review-date-empty-1440.png') });
  await page.reload(); await expect(page.getByLabel('归档适用日', { exact: true })).toHaveValue('2002-01-03');
  await page.goBack(); await expect(page.getByLabel('归档适用日', { exact: true })).toHaveValue('2002-01-02');
  await expect(reader.getByRole('heading', { name: '2002-01-02 · 第 1 版', exact: true })).toBeVisible();
  await page.goForward(); await expect(page.getByText('这个适用日没有已导入的复盘', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看全部日期', exact: true }).click();
  await expect(page.getByLabel('归档适用日', { exact: true })).toHaveValue('');
  expect(new URL(page.url()).searchParams.has('archive_date')).toBe(false);
  await expect(reader.getByRole('heading', { name: '2002-01-02 · 第 1 版', exact: true })).toBeVisible();
  await page.getByRole('radio', { name: '人工研究', exact: true }).click();
  await expect(page.getByRole('heading', { name: '市场环境与情绪判断', exact: true })).toBeVisible();
  await expect(page.getByLabel('研究日期', { exact: true })).toBeVisible();
});

test('missing same-version summary stays explicit; original loading and 404 have retry', async ({ page }, info) => {
  await open(page);
  const saved = await post(page, 'review-archives', body(2, false));
  await page.goto(`/research/reviews?workspace=archive&archive=${saved.id}`);
  await expect(page.getByRole('heading', { name: '完整稿末段', exact: true })).toBeVisible();
  await expect(page.getByText('此版未附总结，以下展示完整原稿。', { exact: true })).toBeVisible();
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const detail = `**/api/v1/review-archives/${saved.id}`;
  await page.route(detail, async route => { await pending; await route.continue(); });
  await page.goto(`/research/reviews?workspace=archive&archive=${saved.id}&report=summary`);
  await expect(page.getByText('正在读取原稿…', { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('archive-loading-1440.png'), fullPage: true });
  release();
  await expect(page.getByText('这一版没有附总结卡片', { exact: true })).toBeVisible();
  await page.unroute(detail);
  await page.screenshot({ path: info.outputPath('archive-missing-summary-1440.png'), fullPage: true });
  await page.getByRole('button', { name: '阅读这一版完整稿', exact: true }).click();
  await expect(page.getByRole('heading', { name: '完整稿末段', exact: true })).toBeVisible();
  await page.route(detail, route => route.fulfill({ status: 404, json: { error: { code: 'not_found', message: '合成归档不存在或不可访问。' }, request_id: '00000000-0000-4000-8000-000000000002' } }));
  await page.reload();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成归档不存在或不可访问。' });
  await expect(notice).toBeVisible();
  await page.screenshot({ path: info.outputPath('archive-detail-error-1440.png'), fullPage: true });
  await page.unroute(detail);
  await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0); await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(page.getByRole('heading', { name: '完整稿末段', exact: true })).toBeVisible();
});

async function readingScreens(page: import('@playwright/test').Page, info: { outputPath: (name: string) => string }, name: string) {
  for (const width of [1920, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: info.outputPath(`${name}-${width}.png`), fullPage: true });
  }
}

test('original paragraphs have stable private-version URLs, keyboard focus, reload, history and failed-read recovery', async ({ page }, info) => {
  await open(page);
  const input = body(3, false); const header = input.full.text.split('\n\n')[0];
  const original = `${header}\n\n合成第一段，保持原句；没有标题，不自动补写市场判断。\n\n合成第二段，保留材料与假设的原有顺序。\n\n合成第三段，定位仅用于回查，不发布预案。\n\n合成末段，原文没有结论之外的新增解释。`;
  input.full = source(input.full.path, original);
  const saved = await post(page, 'review-archives', input);
  await page.goto(`/research/reviews?workspace=archive&archive=${saved.id}&report=full`);
  await expect(page.locator('.archive-prose h3')).toHaveCount(0);
  await page.locator('.archive-outline summary').click();
  await expect(page.getByRole('navigation', { name: '原文段落', exact: true }).getByRole('link')).toHaveCount(5);
  await page.getByRole('link', { name: '定位原文第4段', exact: true }).focus(); await page.keyboard.press('Enter');
  await expect(page.getByLabel('原文第4段', { exact: true })).toBeFocused();
  await expect(page.getByLabel('原文第4段', { exact: true })).toContainText('合成第三段');
  expect(new URL(page.url()).searchParams.get('paragraph')).toBe('3');
  expect(new URL(page.url()).searchParams.get('archive')).toBe(saved.id);
  await page.reload(); await expect(page.getByLabel('原文第4段', { exact: true })).toBeFocused();
  await page.locator('.archive-outline summary').click();
  await page.getByRole('link', { name: '定位原文第5段', exact: true }).click();
  await expect(page.getByLabel('原文第5段', { exact: true })).toBeFocused();
  await page.goBack(); await expect(page.getByLabel('原文第4段', { exact: true })).toBeFocused();
  await page.goForward(); await expect(page.getByLabel('原文第5段', { exact: true })).toBeFocused();
  await readingScreens(page, info, 'reading-paragraphs');
  const position = page.url();
  const detail = `**/api/v1/review-archives/${saved.id}`;
  await page.route(detail, route => route.fulfill({ status: 500, json: { error: { code: 'synthetic_error', message: '合成读取失败，原位置保留。' }, request_id: '00000000-0000-4000-8000-000000000003' } }));
  await page.reload();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成读取失败，原位置保留。' });
  await expect(notice).toBeVisible();
  expect(page.url()).toBe(position);
  await page.unroute(detail);
  await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await expect(page.getByLabel('原文第5段', { exact: true })).toBeFocused();
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(page.getByRole('button', { name: /^消息通知/ })).toBeFocused();
  expect(page.url()).toBe(position);
  await page.goto(`/research/reviews?workspace=archive&archive=${saved.id}&report=full&paragraph=9999`);
  await expect(page.getByText('定位段落不存在或未绑定这份原稿，完整原文仍可阅读。', { exact: false })).toBeVisible();
  await expect(page.getByLabel('原文第5段', { exact: true })).toContainText('合成末段');
  await page.setViewportSize({ width: 1440, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: info.outputPath('reading-invalid-position-1440.png'), fullPage: true });
  await page.getByRole('button', { name: '返回报告开头', exact: true }).first().click();
  expect(new URL(page.url()).searchParams.has('paragraph')).toBe(false);
  await expect(page.getByLabel('原文阅读工具与段落', { exact: true })).toBeFocused();
  const other = await post(page, 'review-archives', body(4, false));
  await page.locator('.archive-outline summary').click(); await page.getByRole('link', { name: '定位原文第4段', exact: true }).click();
  await page.getByRole('button', { name: '刷新归档', exact: true }).click();
  await page.locator(`.archive-list a[href*="${other.id}"]`).click();
  expect(new URL(page.url()).searchParams.has('paragraph')).toBe(false);
  expect((await (await page.request.get('/api/v1/review-archives/' + saved.id)).json()).data.full_text).toBe(original);
});

test('local literal find supports match navigation and no-results without sending or storing private search text', async ({ page }, info) => {
  await open(page);
  const input = body(5); const header = input.full.text.split('\n\n')[0];
  const original = `${header}\n\n合成观察[*]：同一句观察[*]，仅测试字面查找。\n\n另一段观察[*]；<script>window.readingInjected=true</script>仍是原文字面内容。`;
  input.full = source(input.full.path, original);
  const saved = await post(page, 'review-archives', input);
  await page.goto(`/research/reviews?workspace=archive&archive=${saved.id}&report=full`);
  const requests: string[] = []; page.on('request', request => requests.push(request.url()));
  const search = page.getByRole('search', { name: '原文页内查找', exact: true });
  const field = search.getByRole('searchbox', { name: '查找原文', exact: true });
  await field.fill('观察[*]'); await expect(field).toBeFocused();
  await expect(search.getByRole('status')).toHaveText('共 3 处匹配');
  await expect(page.locator('.archive-prose mark')).toHaveCount(3);
  await field.press('Enter'); await expect(page.locator('.archive-prose mark').nth(0)).toBeFocused();
  await search.getByRole('button', { name: '下一处', exact: true }).click();
  await expect(page.locator('.archive-prose mark').nth(1)).toBeFocused();
  await field.focus(); await field.press('Shift+Enter');
  await expect(page.locator('.archive-prose mark').nth(0)).toBeFocused();
  await search.getByRole('button', { name: '上一处', exact: true }).click();
  await expect(page.locator('.archive-prose mark').nth(2)).toBeFocused();
  await expect(search.getByRole('status')).toHaveText('第 3 / 3 处匹配');
  await readingScreens(page, info, 'reading-find');
  expect(page.url()).not.toContain(encodeURIComponent('观察[*]'));
  expect(requests.some(url => url.includes(encodeURIComponent('观察[*]')) || url.includes('观察'))).toBe(false);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  expect(await page.evaluate(() => (window as any).readingInjected)).toBeUndefined();
  await field.fill('这句合成词并不存在');
  await expect(search.getByRole('status')).toHaveText('原文中没有匹配文字');
  await expect(search.getByRole('button', { name: '下一处', exact: true })).toBeDisabled();
  await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ path: info.outputPath('reading-no-matches-390.png'), fullPage: true });
  await field.press('Escape'); await expect(field).toHaveValue(''); await expect(page.locator('.archive-prose mark')).toHaveCount(0);
  await field.fill('观察'); await page.getByRole('radio', { name: '原总结', exact: true }).click();
  expect(new URL(page.url()).searchParams.has('paragraph')).toBe(false);
  await expect(page.getByRole('searchbox', { name: '查找原文', exact: true })).toHaveValue('');
  await page.getByRole('radio', { name: '完整稿', exact: true }).click();
  await expect(page.getByRole('searchbox', { name: '查找原文', exact: true })).toHaveValue('');
  expect((await (await page.request.get('/api/v1/review-archives/' + saved.id)).json()).data.full_source.sha256).toBe(input.full.sha256);
});
