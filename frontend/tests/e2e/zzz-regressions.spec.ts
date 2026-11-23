import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { day, fixture, post, reviewBody, signIn } from './researchTestHelpers';

const mainNav = (page: Page) => page.getByRole('navigation', { name: '主导航', exact: true });
const catalog = (page: Page) => page.locator('.integration-catalog');
const search = async (page: Page, value: string) => {
  await page.goto('/workbench?legacy=1');
  await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill(value);
  await page.getByRole('button', { name: '搜索标的', exact: true }).click();
  return page.getByRole('region', { name: '标的查询结果', exact: true });
};

async function longReview(page: Page) {
  await page.goto('/research/reviews?date=' + day); await signIn(page);
  const f = await fixture(page);
  for (let index = 0; index < 7; index += 1) {
    const document = await post(page, 'research-documents', { kind: 'review', source_trade_day: day,
      revision: reviewBody(f, '合成滚动复盘 ' + index) });
    await post(page, 'revisions/' + document.head_revision_id + '/publish', {}, 1);
  }
  await page.reload();
  await expect(page.locator('.research-records')).toContainText('合成滚动复盘 6');
  await page.locator('.review-core').evaluate(element => window.scrollTo(0, element.getBoundingClientRect().top + scrollY - 100));
  const position = await page.evaluate(() => scrollY);
  expect(position).toBeGreaterThan(700);
  return position;
}

test('catalog display rights govern search at read time while closing collection does not', async ({ page }, info) => {
  await page.goto('/data?view=sources'); await signIn(page);
  // Data tools deliberately hide the global header search. Exercise the real
  // workbench search in the same authenticated browser, retaining the catalog.
  const lookup = await page.context().newPage();
  const d01 = catalog(page).getByRole('row').filter({ has: page.getByText('标的与代码搜索', { exact: true }) });
  await expect(d01).toContainText('目录已导入');
  const hit = await search(lookup, '002724');
  await expect(hit.getByRole('link')).toHaveCount(1);
  await expect(hit).toContainText('合成目录乙');

  const sources = (await (await page.request.get('/api/v1/data-sources')).json()).data;
  const source = sources.items.find((row: { family: string }) => row.family === 'cninfo');
  expect(source).toBeTruthy();
  const base = { family: 'cninfo', symbols: [], interval_seconds: source.interval_seconds,
    rights_basis: source.rights_basis, rights: source.provider.rights,
    entitlement_status: source.provider.entitlement_status };

  // Closing collection stops new fetches but must not hide the saved catalogue.
  await post(page, 'data-sources', { ...base, enabled: false });
  await page.reload();
  await expect(d01).toContainText('目录已导入');
  await expect((await search(lookup, '002724')).getByRole('link')).toHaveCount(1);

  // Revoking display/cache hides the saved values without deleting instruments or other sources.
  await post(page, 'data-sources', { ...base, enabled: false, entitlement_status: 'denied',
    rights: { ...source.provider.rights, display: 'denied', cache: 'denied' } });
  await page.reload();
  await expect(d01).toContainText('展示权限已撤回');
  await expect(d01).toContainText('本机检索可用');
  const hidden = await search(lookup, '002724');
  await expect(hidden.getByText('没有匹配的标的', { exact: true })).toBeVisible();
  await expect(hidden.getByRole('link')).toHaveCount(0);
  // The independently authorized quote source keeps its own identities searchable.
  await expect((await search(lookup, '000001')).getByRole('link')).toHaveCount(2);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await d01.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`catalog-display-revoked-${width}.png`), animations: 'disabled' });
  }

  // Restoring display without collecting again restores the saved catalogue.
  await post(page, 'data-sources', { ...base, enabled: false, entitlement_status: 'verified',
    rights: { ...source.provider.rights, display: 'allowed', cache: 'allowed' } });
  await page.reload();
  await expect(d01).toContainText('目录已导入');
  await expect((await search(lookup, '002724')).getByRole('link')).toHaveCount(1);
  await page.screenshot({ path: info.outputPath('catalog-display-restored-1440.png'), animations: 'disabled' });
  await lookup.close();
});

test('a slow research list after browser back still restores the saved scroll position', async ({ page }, info) => {
  await longReview(page);

  // Reproduce the CI mechanism: the records request issued by the back navigation
  // arrives after first paint, so the returned document is too short to reach the
  // saved position until that request completes.
  await page.route('**/api/v1/research-documents**', async route => {
    await new Promise(resolve => setTimeout(resolve, 2500));
    await route.continue();
  });
  const departure = mainNav(page).getByRole('link', { name: '盘中工作台', exact: true });
  // Measure independently at the user's actual departure. Late layout/scroll
  // anchoring before the click can legitimately move an earlier setup position.
  // Capture runs before the app's delegated navigation handler; no router state
  // or implementation values are used as the expected result.
  await departure.evaluate(link => link.addEventListener('click', () => {
    document.documentElement.dataset.e2eDepartureY = String(scrollY);
  }, { capture: true, once: true }));
  await departure.click();
  const position = await page.evaluate(() => {
    const value = Number(document.documentElement.dataset.e2eDepartureY);
    delete document.documentElement.dataset.e2eDepartureY;
    return value;
  });
  expect(position).toBeGreaterThan(700);
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  await page.goBack();
  await expect(page.getByRole('heading', { name: '每日复盘', exact: true })).toBeVisible();
  // The saved position must survive the late list growth, not just the first paint.
  await expect.poll(async () => Math.abs(await page.evaluate(() => scrollY) - position), { timeout: 8000 }).toBeLessThan(2);
  await expect(page.locator('.research-records')).toContainText('合成滚动复盘 6');
  await page.screenshot({ path: info.outputPath('smooth-return-scroll-slow-list-1440.png') });
});

test('reader scrolling cancels restoration before delayed records resize the page', async ({ page }, info) => {
  await longReview(page);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let arrived!: () => void;
  const pending = new Promise<void>(resolve => { arrived = resolve; });
  await page.route('**/api/v1/research-documents**', async route => { arrived(); await gate; await route.continue(); });
  await mainNav(page).getByRole('link', { name: '盘中工作台', exact: true }).click();
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  await page.goBack(); await pending;
  await expect(page.getByRole('heading', { name: '每日复盘', exact: true })).toBeVisible();
  await page.mouse.move(1000, 300);
  await page.mouse.wheel(0, -10000);
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  release();
  await expect(page.locator('.research-records')).toContainText('合成滚动复盘 6');
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  await page.screenshot({ path: info.outputPath('smooth-return-user-scroll-1440.png') });
});
