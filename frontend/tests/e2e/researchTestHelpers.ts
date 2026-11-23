import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';

export const day = '2001-12-31';
export async function signIn(page: Page, username = process.env.JL_E2E_USERNAME ?? 'e2e') {
  await page.getByLabel('账号', { exact: true }).fill(username);
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('button', { name: /^(退出|退出账号)$/ })).toBeVisible();
}
export async function login(page: Page, username?: string) { await page.goto('/research/reviews?date=' + day); await signIn(page, username); }
export async function session(page: Page) { return (await (await page.request.get('/api/v1/session')).json()).data; }
export async function post(page: Page, path: string, data: unknown, head?: number) {
  const auth = await session(page);
  const response = await page.request.post('/api/v1/' + path, { headers: { Origin: new URL(page.url()).origin, 'X-CSRF-Token': auth.csrf_token, 'Idempotency-Key': crypto.randomUUID(), ...(head === undefined ? {} : { 'If-Match': '"' + head + '"' }) }, data });
  expect(response.status(), 'synthetic fixture API status').toBeLessThan(300);
  return (await response.json()).data;
}
export async function fixture(page: Page) {
  const key = 'research-browser-' + crypto.randomUUID();
  const providers = (await (await page.request.get('/api/v1/providers')).json()).data.items;
  const material = await post(page, 'evidence', { source_id: providers.find((row: { kind: string }) => row.kind === 'manual').id, source_key: key, source_revision: '1', kind: 'manual', authorship: 'original', payload: { text: '隔离演示材料，仅验证研究引用，不是行情或投资判断。' }, rights_basis: '本人自制的隔离测试材料' });
  const receipt = await post(page, 'snapshots', { as_of_time: material.received_at, required_capabilities: ['evidence.manual'], source_keys: [key] });
  let snapshot: any;
  await expect.poll(async () => { const response = await page.request.get('/api/v1/snapshots/' + receipt.result_ref.id); snapshot = (await response.json()).data; return snapshot.state; }, { timeout: 15_000 }).toBe('ready');
  expect(snapshot.items.map((row: { evidence_version_id: string }) => row.evidence_version_id)).toContain(material.id);
  const calendar = await post(page, 'calendar-versions', { exchange: 'SSE', version: key, source_ref: '合成日历，不是真实市场日程', rights_basis: '自制隔离测试', coverage_start: day, coverage_end: '2002-01-02', sessions: [{ trade_day: day, is_open: true, segments: [{ start: '09:30', end: '11:30', kind: 'continuous' }] }, { trade_day: '2002-01-01', is_open: false, segments: [] }, { trade_day: '2002-01-02', is_open: true, segments: [{ start: '09:30', end: '11:30', kind: 'continuous' }] }] });
  return { key, material, snapshot, calendar };
}
export function reviewBody(f: Awaited<ReturnType<typeof fixture>>, title: string, summary = '合成复盘第一版，保留原文。') {
  return { title, snapshot_id: f.snapshot.id, calendar_version_id: f.calendar.id, upstream_revision_ids: [], content: { type: 'review', summary, evidence_refs: [f.material.id], limitations: ['仅用于隔离浏览器测试'] } };
}
export async function begin(page: Page, f: Awaited<ReturnType<typeof fixture>>, title: string) {
  await page.getByRole('button', { name: '新建每日复盘', exact: true }).last().click();
  const dialog = page.getByRole('dialog', { name: '编辑每日复盘', exact: true });
  await dialog.getByLabel('研究标题', { exact: true }).fill(title);
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.snapshot.id);
  await dialog.getByLabel('日历版本', { exact: true }).selectOption(f.calendar.id);
  await dialog.getByLabel('复盘正文', { exact: true }).fill('合成复盘第一版 <script>window.researchInjected=true</script>');
  await dialog.getByRole('checkbox', { name: `引用材料 ${f.key} 第 1 版`, exact: true }).check();
  await dialog.getByLabel('材料与判断限制（每行一项）', { exact: true }).fill('仅用于隔离浏览器测试');
  return dialog;
}
export async function screenshots(page: Page, info: { outputPath: (name: string) => string }, name: string) {
  for (const width of [1920, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const modal = page.locator('dialog[open]');
    const isModal = await modal.count() > 0;
    if (isModal) await modal.evaluate((element, conflict) => {
      if (conflict === true) element.querySelector('[aria-label="版本冲突比较"]')?.scrollIntoView({ block: 'end' });
      else if (conflict === 'target') element.querySelector('.plan-target')?.scrollIntoView({ block: 'end' });
      else if (conflict === 'scenarios') element.querySelector('.scenario-editor')?.scrollIntoView({ block: 'end' });
      else if (conflict === 'late-material') element.querySelector('.observation-facts .permission-note')?.scrollIntoView({ block: 'end' });
      else if (conflict === 'validation-conflict') element.querySelector('[role="alert"]')?.scrollIntoView({ block: 'end' });
      else element.scrollTop = 0;
    }, name === 'review-conflict' ? true : name === 'plan-calendar-unknown' || name === 'plan-target' ? 'target' : name === 'plan-scenarios' ? 'scenarios' : name === 'observation-late-material' ? 'late-material' : name === 'validation-conflict' ? 'validation-conflict' : false);
    else if (await page.getByRole('dialog', { name: '消息通知', exact: true }).isVisible()) await page.getByRole('dialog', { name: '消息通知', exact: true }).scrollIntoViewIfNeeded();
    else if (name.endsWith('list-error')) await page.locator(name === 'review-list-error' ? '.research-records' : name.startsWith('validation-') ? '.validation-list' : '.observation-list').scrollIntoViewIfNeeded();
    if (name === 'validation-conflict') await expect(modal.getByRole('alert')).toBeInViewport();
    await page.screenshot({ path: info.outputPath(`${name}-${width}.png`), fullPage: !isModal && !name.endsWith('list-error') });
    if (isModal && width === 390 && name.endsWith('editor')) {
      await modal.evaluate(element => { element.scrollTop = element.scrollHeight; });
      await page.screenshot({ path: info.outputPath(`${name}-fields-390.png`) });
    }
  }
}
