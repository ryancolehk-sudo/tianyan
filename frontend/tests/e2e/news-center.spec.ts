import { expect, test } from '@playwright/test';

// Bootstrap behavior is covered separately; these tests exercise each existing page.
test.beforeEach(async ({ page }) => {
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => {
    await page.keyboard.press('Escape');
  });
});
import type { Page, Route } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { ArticleState, EditionSummary, NewsArticle, NewsPeriod } from '../../src/features/news-center/model';

// Isolated synthetic articles only. No market vendor or model is contacted.
const uuid = (value: number) => '00000000-0000-4000-8000-' + String(value).padStart(12, '0');
const meta = { request_id: uuid(1), app_mode: 'demo', generated_at: '2026-10-25T08:00:00+08:00', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic news center UI fixtures'] } };
function article(index: number, category: NewsArticle['category'], title: string, summary: string | null): NewsArticle {
  return { id: uuid(index + 10), evidence_id: uuid(index + 100), title, summary, category, source_name: '合成' + (category === '公司公告' ? '公告来源' : category === '海外' ? '海外来源' : '财经来源'), publisher: '合成测试发行方' + (index % 3), family: category === '公司公告' ? 'cninfo_announcements' : category === '海外' ? 'ecb_news' : 'chinanews_finance', source_url: 'https://example.com/synthetic-news/' + index, published_at: category === '公司公告' ? null : '2026-10-24T' + String(14 - index).padStart(2, '0') + ':30:00+08:00', published_day: category === '公司公告' ? '2026-10-24' : null, received_at: '2026-10-24T18:00:00+08:00', content_hash: String(index).repeat(64), is_read: false, is_saved: false, is_following: false, related_sources: index === 1 ? [{ name: '合成关联出处', url: 'https://example.com/synthetic-reference' }] : [], late: index === 5,
    summary_input_available: Boolean(summary && summary.trim() !== title.trim() && Array.from(summary.trim()).length >= 20),
    summary_input_reason: summary ? null : '本条资讯只有标题，未保存正文或原生摘要。' };
}
const fixtureArticles = [
  article(1, '政策', '合成资讯：机器人示范方案更新，关注应用范围与申报条件', '这是一段独立合成的政策摘要，仅用于核对报刊版式、来源标注与阅读路径，不是真实市场新闻。'),
  article(2, '产业', '合成资讯：算力设施发布招标信息，采购范围补充披露', '合成招标文件列出建设节点与采购要求，供应商名单仍待确认。'),
  article(3, '公司公告', '合成公告：样例公司披露设备合同，交付安排待补充', null),
  article(4, '风险澄清', '合成澄清：样例公司回应市场传闻，相关合作尚未达成', '保留早间传闻与后续澄清的明确出处，便于核对信息变化。'),
  article(5, '海外', '合成海外：货币政策与跨境金融合作发布观察资料', '这段合成来源摘要用于验证海外栏目与迟到资料标签。'),
  article(6, '财经', '合成财经：区域产业交流与基础设施建设新进展', '合成财经发布只用于界面测试，不包含投资建议。'),
];
function edition(period: NewsPeriod, day: string, index: number, partial = false): EditionSummary {
  return { id: uuid(500 + index), period, day, issue_number: index + 1, headline: fixtureArticles[index % fixtureArticles.length].title,
    item_count: fixtureArticles.length, source_count: 3, reading_minutes: 3, window_start: '2026-10-24T08:00:00+08:00', window_end: '2026-10-25T08:00:00+08:00', generated_at: '2026-10-25T08:00:01+08:00', coverage_status: partial ? 'partial' : 'complete', coverage_note: partial ? '合成测试：启用前资料未补造，本期覆盖有限。' : '', ai_state: 'unconfigured' };
}
const fixtureEditions = [edition('day', '2026-10-25', 4), edition('day', '2026-10-24', 3), edition('day', '2026-10-23', 2), edition('day', '2026-10-22', 1), edition('day', '2026-10-21', 0), edition('week', '2026-10-19', 8, true), edition('month', '2026-10-01', 9, true)];
type SummaryRequest = { feature: string; news_id: string; regenerate: boolean };
type SyntheticSummary = { id: string; job_id: string; feature: string; state: string; stage: string; model: string; created_at: string; completed_at: string | null; error_code: null; limitations: string[];
  evidence_refs: { id: string; title: string; url: string; source_name: string }[];
  result: null | { summary: string; items: { title: string; text: string; evidence_refs: string[] }[] } };
function summaryRun(article: NewsArticle, index: number, complete = false): SyntheticSummary {
  return { id: uuid(3000 + index), job_id: uuid(4000 + index), feature: 'news_summary', state: complete ? 'succeeded' : 'queued', stage: complete ? '生成完成' : '等待 AI 执行器', model: 'synthetic-summary-model', created_at: meta.generated_at, completed_at: complete ? meta.generated_at : null, error_code: null,
    limitations: ['仅为合成浏览器验收，不是真实模型调用'], evidence_refs: [{ id: article.evidence_id, title: article.title, url: article.source_url, source_name: article.source_name }],
    result: complete ? { summary: '合成单篇摘要：仅依据选中的证据版本。', items: [{ title: '合成要点', text: '这是明确标识的合成结果，原始出处保留。', evidence_refs: [article.evidence_id] }] } : null };
}
type Options = { empty?: boolean; failFeed?: boolean; failWrite?: boolean; pageSize?: number; editionPageSize?: number; editions?: EditionSummary[]; ai?: boolean;
  role?: 'admin' | 'researcher' | 'viewer'; modelEnabled?: boolean; savedSummary?: boolean };
async function workspace(page: Page, options: Options = {}) {
  const state = { articles: structuredClone(fixtureArticles), queries: [] as URLSearchParams[], writes: [] as { id: string; body: ArticleState; csrf: string | undefined; key: string | undefined }[], requests: [] as string[], errors: [] as string[], summaryReads: [] as string[], summaryWrites: [] as { body: SummaryRequest; csrf?: string; key?: string }[], summaries: new Map<string, SyntheticSummary>() };
  page.on('pageerror', error => state.errors.push(error.message));
  page.on('request', request => state.requests.push(request.method() + ' ' + new URL(request.url()).pathname));
  await page.route('https://**/*', route => route.abort('blockedbyclient'));
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const url = new URL(request.url());
    const ok = (data: unknown) => route.fulfill({ json: { meta, data } });
    if (url.pathname.endsWith('/session')) return ok({ user: { id: uuid(1), username: '合成测试账号', role: options.role ?? 'viewer' }, csrf_token: 'synthetic-news-csrf' });
    if (url.pathname === '/api/v1/ai/settings') return ok({ enabled: options.modelEnabled ?? false, features: [{ id: 'news_summary', implemented: true, enabled: options.modelEnabled ?? false, effective_connection_id: uuid(2900) }] });
    if (url.pathname === '/api/v1/ai/runs') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON() as SummaryRequest;
        state.summaryWrites.push({ body, csrf: request.headers()['x-csrf-token'], key: request.headers()['idempotency-key'] });
        const selected = [...state.articles, ...fixtureArticles].find(item => item.evidence_id === body.news_id);
        if (!selected || body.feature !== 'news_summary') return route.fulfill({ status: 422, json: { error: { message: '合成测试：需要实际新闻证据版本' } } });
        const result = summaryRun(selected, state.summaryWrites.length);
        state.summaries.set(body.news_id, result);
        return ok(result);
      }
      const evidence = url.searchParams.get('news_id') ?? '';
      state.summaryReads.push(evidence);
      const selected = [...state.articles, ...fixtureArticles].find(item => item.evidence_id === evidence);
      if (options.savedSummary && selected && !state.summaries.has(evidence)) state.summaries.set(evidence, summaryRun(selected, 0, true));
      return ok({ items: state.summaries.has(evidence) ? [state.summaries.get(evidence)] : [] });
    }
    if (url.pathname.startsWith('/api/v1/ai/runs/')) {
      const stored = [...state.summaries.entries()].find(([, item]) => item.id === url.pathname.split('/').pop());
      const selected = stored && [...state.articles, ...fixtureArticles].find(item => item.evidence_id === stored[0]);
      if (!stored || !selected) return route.fulfill({ status: 404, json: { error: { message: '合成测试：生成记录不存在' } } });
      const completed = { ...summaryRun(selected, 0, true), id: stored[1].id, job_id: stored[1].job_id };
      state.summaries.set(stored[0], completed);
      return ok(completed);
    }
    if (url.pathname === '/api/v1/news/items') {
      state.queries.push(url.searchParams);
      if (options.failFeed) return route.fulfill({ status: 503, json: { error: { message: '合成测试：已存资讯暂时无法读取' } } });
      const query = url.searchParams;
      let items = options.empty ? [] : state.articles.filter(item => {
        const category = query.get('category');
        if (category && !['全部', '精选'].includes(category) && item.category !== category) return false;
        if (query.get('q') && ![item.title, item.summary, item.source_name].some(value => value?.includes(query.get('q')!))) return false;
        if (query.get('family') && item.family !== query.get('family')) return false;
        if (query.get('day') && (item.published_day ?? item.published_at?.slice(0, 10)) !== query.get('day')) return false;
        if (query.get('saved') === 'true' && !item.is_saved) return false;
        if (query.get('unread') === 'true' && item.is_read) return false;
        if (query.get('following') === 'true' && !item.is_following) return false;
        return true;
      });
      if (query.get('order') === 'earliest') items = [...items].reverse();
      const offset = Number(query.get('cursor')?.replace('opaque-', '') ?? '0');
      const limit = options.pageSize ?? 30;
      return ok({ items: items.slice(offset, offset + limit), total: items.length, next_cursor: offset + limit < items.length ? 'opaque-' + (offset + limit) : null, focus: items[0] ?? null,
        latest_edition: options.empty ? null : (options.editions ?? fixtureEditions).find(item => item.period === 'day') ?? null,
        sources: [{ family: 'chinanews_finance', name: '合成财经来源', publisher: '合成财经发行方', status: 'complete', last_success_at: '2026-10-24T18:00:00+08:00', latest_published_at: '2026-10-24T17:30:00+08:00', cached_count: options.empty ? 0 : 4, coverage: '仅为合成测试资料' }, { family: 'cninfo_announcements', name: '合成公告来源', publisher: '合成公告发行方', status: 'partial', last_success_at: '2026-10-24T18:00:00+08:00', latest_published_at: null, cached_count: options.empty ? 0 : 1, coverage: '合成覆盖说明：本轮达到分页上限，覆盖不完整。' }, { family: 'ecb_news', name: '合成海外来源', publisher: '合成海外发行方', status: 'error', last_success_at: null, latest_published_at: null, cached_count: options.empty ? 0 : 1, coverage: '合成来源暂时失败，保留已存内容。' }], coverage_start: '2026-10-21T08:00:00+08:00', ai_status: { state: 'unconfigured', connection_id: null, model: null, reason: '未配置模型连接' } });
    }
    const articleMatch = url.pathname.match(/^\/api\/v1\/news\/items\/([^/]+)(\/state)?$/);
    if (articleMatch) {
      const item = state.articles.find(item => item.id === articleMatch[1]);
      if (!item) return route.fulfill({ status: 404, json: { error: { message: '合成测试：资讯不存在' } } });
      if (articleMatch[2]) {
        const body = request.postDataJSON() as ArticleState & { evidence_id?: string };
        state.writes.push({ id: item.id, body, csrf: request.headers()['x-csrf-token'], key: request.headers()['idempotency-key'] });
        if (options.failWrite) return route.fulfill({ status: 503, json: { error: { message: '合成测试：阅读状态暂未保存' } } });
        if (body.read !== undefined && (!body.evidence_id || body.evidence_id === item.evidence_id)) item.is_read = body.read;
        if (body.saved !== undefined) item.is_saved = body.saved;
        if (body.following !== undefined) item.is_following = body.following;
        if (body.read !== undefined && body.evidence_id && body.evidence_id !== item.evidence_id) {
          const original = fixtureArticles.find(original => original.id === item.id && original.evidence_id === body.evidence_id);
          if (original) return ok({ ...original, is_read: Boolean(body.read), is_saved: item.is_saved, is_following: item.is_following });
        }
      }
      return ok(item);
    }
    if (url.pathname === '/api/v1/news/editions') {
      const rows = options.empty ? [] : (options.editions ?? fixtureEditions).filter(item => item.period === url.searchParams.get('period'));
      const offset = Number(url.searchParams.get('cursor')?.replace('archive-', '') ?? '0');
      const size = options.editionPageSize ?? 100;
      return ok({ items: rows.slice(offset, offset + size), next_cursor: offset + size < rows.length ? 'archive-' + (offset + size) : null });
    }
    if (url.pathname.startsWith('/api/v1/news/editions/')) {
      const found = (options.editions ?? fixtureEditions).find(item => item.id === url.pathname.split('/').pop());
      if (!found) return route.fulfill({ status: 404, json: { error: { message: '合成测试：刊期不存在' } } });
      const editionItems = fixtureArticles.map(original => {
        const current = state.articles.find(item => item.id === original.id)!;
        return { ...original, is_read: current.evidence_id === original.evidence_id ? current.is_read : false, is_saved: current.is_saved, is_following: current.is_following };
      });
      return ok({ edition: found, items: editionItems, ai: options.ai ? { id: uuid(900), model: 'synthetic-model', prompt_version: 'synthetic-v1', generated_at: '2026-10-25T08:02:00+08:00', lead: '这是一段明确标记的合成 AI 导读。', items: editionItems.map(item => ({ article_id: item.id, evidence_id: item.evidence_id, summary: '合成 AI 摘要：用于验证增强内容独立标识。', category: item.category, selected: true })), usage: null } : null });
    }
    return ok({ items: [] });
  });
  return state;
}
const row = (page: Page, title: string) => page.locator('.nc-news-row').filter({ has: page.getByRole('button', { name: title, exact: true }) });
const dialog = (page: Page) => page.getByRole('dialog', { name: '资讯详情', exact: true });
const tabs = (page: Page) => page.getByRole('navigation', { name: '资讯中心页面', exact: true });

test('news home uses server search and category/date/source filters, with an honest source directory', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-25T10:00:00+08:00') });
  const state = await workspace(page);
  state.articles[4].published_at = '2026-10-25T09:00:00+08:00';
  state.articles[4].received_at = '2026-10-25T09:01:00+08:00';
  state.articles[5].published_at = '2025-12-31T01:30:00Z';
  await page.goto('/news');
  await expect(page.getByRole('heading', { name: '资讯中心', exact: true })).toBeVisible();
  await expect(page.locator('.ty-sidebar a[aria-current="page"]')).toHaveText('资讯中心');
  await expect(page.locator('.nc-focus h3')).toHaveText(fixtureArticles[0].title);
  await expect(page.locator('.nc-focus').getByRole('link', { name: '查看原文' })).toHaveAttribute('href', fixtureArticles[0].source_url);
  await expect(page.locator('.nc-update')).toContainText('演示数据');
  await expect(page.locator('.nc-brief-count')).toHaveText('6 条要闻 · 约3分钟');
  await expect(row(page, fixtureArticles[1].title).locator(':scope > time')).toHaveText('10.24');
  await expect(row(page, fixtureArticles[1].title).locator('.nc-row-meta')).toContainText('2026/10/24 12:30');
  await expect(row(page, fixtureArticles[4].title).locator(':scope > time')).toHaveText('09:00');
  await expect(row(page, fixtureArticles[5].title).locator(':scope > time')).toHaveText('202512.31');
  await expect(row(page, fixtureArticles[5].title).locator('.nc-row-meta')).toContainText('2025/12/31 09:30');
  await page.getByRole('navigation', { name: '资讯分类' }).getByRole('button', { name: '产业', exact: true }).click();
  await expect(page.locator('.nc-news-row')).toHaveCount(1);
  await expect.poll(() => state.queries.some(query => query.get('category') === '产业')).toBe(true);
  await page.getByLabel('搜索资讯、题材、公司').fill('区域产业');
  await page.getByRole('button', { name: '搜索资讯', exact: true }).click();
  await expect(page.locator('.nc-news-row')).toHaveCount(1);
  await expect(page.locator('.nc-news-row')).toContainText(fixtureArticles[5].title);
  expect(state.queries.some(query => query.get('q') === '区域产业')).toBe(true);
  await page.reload();
  await expect(page.getByLabel('搜索资讯、题材、公司')).toHaveValue('区域产业');
  await page.getByRole('button', { name: '清空资讯搜索' }).click();
  await expect(page.locator('.nc-news-row')).toHaveCount(6);
  await page.locator('.nc-filter-tools details>summary').click();
  await page.getByRole('combobox', { name: '资讯来源' }).selectOption('cninfo_announcements');
  await expect(page.locator('.nc-news-row')).toHaveCount(1);
  await page.getByLabel('资讯日期', { exact: true }).fill('2026-10-24');
  await expect(page).toHaveURL(/family=cninfo_announcements/);
  await expect(page).toHaveURL(/day=2026-10-24/);
  await expect(page.locator('.nc-news-row')).toContainText('仅日期');
  await page.getByRole('button', { name: fixtureArticles[2].title, exact: true }).click();
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page)).toContainText('来源未提供摘要');
  await expect(dialog(page)).not.toContainText(fixtureArticles[0].summary!);
  await page.getByRole('button', { name: '关闭资讯详情' }).click();
  await page.locator('.nc-source-status>summary').click();
  await expect(page.locator('.nc-source-status')).toContainText('覆盖不完整');
  await expect(page.locator('.nc-source-status')).toContainText('采集暂时失败');
  await expect(page.locator('.nc-source-status')).toContainText('保留已存内容');
  expect(state.errors).toEqual([]);
  expect(state.requests.filter(value => value.includes('/api/')).some(value => /model|collect|refresh|chat\/completions/.test(value))).toBe(false);
});

test('saved, unread and following states persist through the account API and refresh', async ({ page }) => {
  const state = await workspace(page);
  await page.goto('/news?category=全部');
  await row(page, fixtureArticles[1].title).getByRole('button', { name: '收藏：' + fixtureArticles[1].title, exact: true }).click();
  await expect(row(page, fixtureArticles[1].title).getByRole('button', { name: '取消收藏：' + fixtureArticles[1].title, exact: true })).toBeVisible();
  await page.getByRole('link', { name: '查看我的收藏' }).click();
  await expect(page.locator('.nc-news-row')).toHaveCount(1);
  await page.getByRole('button', { name: fixtureArticles[1].title, exact: true }).click();
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).getByRole('button', { name: '标为未读', exact: true })).toBeEnabled();
  await dialog(page).getByRole('button', { name: '加入待跟进', exact: true }).click();
  await expect(dialog(page).getByRole('button', { name: '已列入待跟进', exact: true })).toBeEnabled();
  await dialog(page).getByRole('button', { name: '标为未读', exact: true }).click();
  await expect(dialog(page).getByRole('button', { name: '标为已读', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '关闭资讯详情' }).click();
  await page.reload();
  await expect(row(page, fixtureArticles[1].title)).toContainText('待跟进');
  await page.getByRole('checkbox', { name: '只看未读', exact: true }).check();
  await expect(page.locator('.nc-news-row')).toHaveCount(1);
  await expect(page).toHaveURL(/unread=true/);
  await page.getByRole('button', { name: fixtureArticles[1].title, exact: true }).click();
  await expect(dialog(page).getByRole('button', { name: '标为未读', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '关闭资讯详情' }).click();
  await expect(page.locator('.nc-news-row')).toHaveCount(0);
  expect(state.writes.length).toBeGreaterThanOrEqual(5);
  expect(state.writes.every(write => write.csrf === 'synthetic-news-csrf' && /^[\da-f-]{36}$/.test(write.key ?? ''))).toBe(true);
  expect(new Set(state.writes.map(write => write.key)).size).toBe(state.writes.length);
  expect(state.errors).toEqual([]);
});

test('daily, weekly and monthly editions use real summaries and retain calendar and URL selection', async ({ page }) => {
  const state = await workspace(page);
  await page.goto('/news?view=daily&period=day');
  await expect(page.getByRole('heading', { name: '天衍日报', exact: true })).toBeVisible();
  await expect(page.getByLabel('本期统计')).toContainText('6 条要闻');
  await expect(page.getByLabel('本期统计')).toContainText('3 个来源');
  await expect(page.getByLabel('本期统计')).toContainText('005');
  await expect(page.locator('.nc-masthead-meta')).toContainText('自动汇编');
  await expect(page.locator('.nc-paper-lead .nc-paper-byline')).toContainText('2026/10/24 13:30');
  await expect(page.locator('.nc-ai-guide')).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: '本期目录' })).toBeVisible();
  await page.getByRole('button', { name: '2026-10-23 阅读日报', exact: true }).click();
  await expect(page).toHaveURL(/date=2026-10-23/);
  await expect(page.getByLabel('本期统计')).toContainText('003');
  await page.reload();
  await expect(page.getByLabel('报刊日期', { exact: true })).toHaveValue('2026-10-23');
  await expect(page.getByRole('button', { name: '2026-10-23 阅读日报', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await tabs(page).getByRole('link', { name: '周报', exact: true }).click();
  await expect(page.getByRole('heading', { name: '天衍周报', exact: true })).toBeVisible();
  await expect(page.locator('.nc-coverage-note')).toContainText('覆盖有限');
  await expect(page).toHaveURL(/period=week/);
  await tabs(page).getByRole('link', { name: '月报', exact: true }).click();
  await expect(page.getByRole('heading', { name: '天衍月报', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/period=month/);
  await page.getByLabel('报刊日期', { exact: true }).fill('2026-10-02');
  await expect(page.getByRole('heading', { name: '这个日期尚未出刊', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '天衍月报', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '阅读最近一期', exact: true }).click();
  await expect(page).toHaveURL(/date=2026-10-01/);
  await expect(page.getByRole('heading', { name: '天衍月报', exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('heading', { name: '这个日期尚未出刊', exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('archive pagination finds a deep link and edition details keep their snapshot evidence', async ({ page }) => {
  const state = await workspace(page, { editionPageSize: 2 });
  await page.goto('/news?view=daily&period=day&date=2026-10-21');
  await expect(page.getByLabel('本期统计')).toContainText('001');
  await expect(page.locator('.nc-paper-section').getByRole('button', { name: fixtureArticles[2].title, exact: true })).toBeVisible();
  const current = state.articles.find(item => item.id === fixtureArticles[2].id)!;
  current.evidence_id = uuid(999);
  current.title = '合成更正稿：同一公告后续更正';
  await page.locator('.nc-paper-section').getByRole('button', { name: fixtureArticles[2].title, exact: true }).click();
  await expect(dialog(page)).toContainText('刊期证据版本');
  await dialog(page).getByText('来源与证据版本', { exact: true }).click();
  await expect(dialog(page)).toContainText(fixtureArticles[2].evidence_id);
  await expect(dialog(page)).toContainText('仅日期');
  await expect(dialog(page).getByRole('link', { name: '查看原文' })).toHaveAttribute('href', fixtureArticles[2].source_url);
  await expect(dialog(page).getByRole('button', { name: '标为未读', exact: true })).toBeEnabled();
  await expect(dialog(page)).toContainText(fixtureArticles[2].title);
  await expect(dialog(page)).not.toContainText(current.title);
  expect(state.writes.find(write => write.id === fixtureArticles[2].id && write.body.read === true)?.body).toEqual(expect.objectContaining({ evidence_id: fixtureArticles[2].evidence_id }));
  current.is_read = false;
  await dialog(page).getByRole('button', { name: '收藏', exact: true }).click();
  await expect(dialog(page).getByRole('button', { name: '已收藏', exact: true })).toBeEnabled();
  await expect(dialog(page).getByRole('button', { name: '标为未读', exact: true })).toBeEnabled();
  await page.reload();
  await expect(dialog(page).getByRole('heading', { name: fixtureArticles[2].title, exact: true })).toBeVisible();
  await expect(dialog(page)).not.toContainText(current.title);
  await page.getByRole('button', { name: '关闭资讯详情' }).click();
  expect(state.requests.some(value => value === 'GET /api/v1/news/items/' + fixtureArticles[2].id)).toBe(false);
  await page.goto('/news?view=daily&period=day&date=2026-10-21&article=' + uuid(9999));
  await expect(page.getByRole('alert')).toContainText('当前不在本期可展示的资料中');
  expect(state.requests.some(value => value === 'GET /api/v1/news/items/' + uuid(9999))).toBe(false);
  expect(state.errors).toEqual([]);
});

test('feed pagination and failed reads/writes recover without fabricated success', async ({ page }) => {
  const options: Options = { pageSize: 3, failFeed: true, failWrite: true };
  const state = await workspace(page, options);
  await page.goto('/news?category=全部');
  await page.getByRole('button', { name: /^消息通知/ }).click();
  const readFailure = page.getByRole('dialog', { name: '消息通知' }).locator('li').filter({ hasText: '已存资讯暂时无法读取' });
  await expect(readFailure).toBeVisible();
  await expect(page.locator('.nc-source-status>summary')).toContainText('暂无法读取');
  // Do not restore the source before the manual retry: a background read can
  // otherwise recover first and remove the notification during its click.
  let releaseRetry!: () => void, retryStarted = false;
  const retryGate = new Promise<void>(resolve => { releaseRetry = resolve; });
  const holdRetry = async (route: Route) => { retryStarted = true; await retryGate; await route.fallback(); };
  await page.route('**/api/v1/news/items?**', holdRetry);
  try {
    await readFailure.getByRole('button', { name: /^(重试|重新读取|重试更新)$/ }).click();
    await expect.poll(() => retryStarted).toBe(true);
    await expect(page.locator('.nc-news-row')).toHaveCount(0);
    options.failFeed = false;
  } finally {
    releaseRetry();
  }
  await expect(page.locator('.nc-news-row')).toHaveCount(3);
  await page.unroute('**/api/v1/news/items?**', holdRetry);
  await expect(readFailure).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await page.getByRole('button', { name: '继续阅读更多资讯' }).click();
  await expect(page.locator('.nc-news-row')).toHaveCount(6);
  await expect(page.getByRole('button', { name: '继续阅读更多资讯' })).toHaveCount(0);
  await row(page, fixtureArticles[0].title).getByRole('button', { name: '收藏：' + fixtureArticles[0].title, exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('阅读状态暂未保存');
  await expect(row(page, fixtureArticles[0].title).getByRole('button', { name: '收藏：' + fixtureArticles[0].title, exact: true })).toBeVisible();
  options.failWrite = false;
  await row(page, fixtureArticles[0].title).getByRole('button', { name: '收藏：' + fixtureArticles[0].title, exact: true }).click();
  await expect(row(page, fixtureArticles[0].title).getByRole('button', { name: '取消收藏：' + fixtureArticles[0].title, exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: '资讯排序', exact: true }).selectOption('earliest');
  await expect(page.locator('.nc-news-row').first()).toContainText(fixtureArticles[5].title);
  expect(state.queries.at(-1)!.get('order')).toBe('earliest');
  expect(state.errors).toEqual([]);
});

test('a source with no available articles never presents a fabricated paper or AI guide', async ({ page }) => {
  const state = await workspace(page, { empty: true });
  await page.goto('/news');
  await expect(page.getByRole('heading', { name: '暂无匹配的已存资讯', exact: true })).toBeVisible();
  await expect(page.locator('.nc-brief')).toContainText('尚未生成可阅读的刊期');
  await tabs(page).getByRole('link', { name: '日报', exact: true }).click();
  await expect(page.getByRole('heading', { name: '天衍日报尚未出刊', exact: true })).toBeVisible();
  await expect(page.locator('.nc-masthead')).toHaveCount(0);
  await expect(page.locator('.nc-paper-statistics')).toHaveCount(0);
  await expect(page.locator('.nc-ai-guide')).toHaveCount(0);
  expect(state.writes).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('an independently identified AI version is visibly labelled', async ({ page }) => {
  await workspace(page, { ai: true });
  await page.goto('/news?view=daily');
  await expect(page.locator('.nc-masthead-meta')).toContainText('AI 增强版');
  await expect(page.locator('.nc-ai-guide')).toContainText('AI 导读');
  await expect(page.locator('.nc-ai-guide')).toContainText('合成 AI 导读');
  await expect(page.locator('.nc-ai-summary')).toHaveCount(fixtureArticles.length);
  await expect(page.locator('.nc-paper-summary')).toContainText(fixtureArticles[0].summary!);
  await expect(page.locator('.nc-masthead-meta')).toContainText('历史 AI 增强版');
  await expect(page.locator('.nc-colophon')).toContainText('AI 增强业务待接入');
});

test('a manual single-article summary uses the evidence version and only generates after a click', async ({ page }) => {
  const state = await workspace(page, { role: 'researcher', modelEnabled: true });
  await page.goto('/news');
  await expect(page.locator('.nc-ai-caption')).toContainText('单篇新闻摘要可在详情中手动生成');
  await expect(page.locator('.nc-ai-caption')).toContainText('AI 增强业务待接入');
  await page.getByRole('button', { name: fixtureArticles[0].title, exact: true }).click();
  const action = dialog(page).getByRole('region', { name: '新闻摘要', exact: true });
  const generate = action.getByRole('button', { name: '生成新闻摘要', exact: true });
  await expect(generate).toBeEnabled();
  expect(state.summaryWrites).toEqual([]);
  expect(state.summaryReads).toContain(fixtureArticles[0].evidence_id);
  expect(state.summaryReads).not.toContain(fixtureArticles[0].id);
  await generate.click();
  await expect(action).toContainText('合成单篇摘要：仅依据选中的证据版本。');
  expect(state.summaryWrites).toHaveLength(1);
  expect(state.summaryWrites[0].body).toEqual({ feature: 'news_summary', regenerate: false, news_id: fixtureArticles[0].evidence_id });
  expect(state.summaryWrites[0].csrf).toBe('synthetic-news-csrf');
  expect(state.summaryWrites[0].key).toMatch(/^[\da-f-]{36}$/);
  await action.getByText('依据、范围与生成记录', { exact: true }).click();
  await expect(action.getByRole('link', { name: fixtureArticles[0].title, exact: true })).toHaveAttribute('href', fixtureArticles[0].source_url);
  await page.reload();
  await expect(dialog(page).getByRole('region', { name: '新闻摘要', exact: true })).toContainText('合成单篇摘要：仅依据选中的证据版本。');
  expect(state.summaryWrites).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

test('title-only input stays disabled while server-confirmed saved body remains usable', async ({ page }) => {
  const state = await workspace(page, { role: 'researcher', modelEnabled: true });
  state.articles[1] = { ...state.articles[1], summary: null, summary_input_available: true, summary_input_reason: null };
  await page.goto('/news?category=全部');
  await page.getByRole('button', { name: fixtureArticles[2].title, exact: true }).click();
  await expect(dialog(page).getByRole('button', { name: '生成新闻摘要', exact: true })).toBeDisabled();
  await expect(dialog(page)).toContainText('当前条目缺少正文或原生摘要，资料不足。');
  await expect(dialog(page)).toContainText('本条资讯只有标题，未保存正文或原生摘要。');
  expect(state.summaryWrites).toEqual([]);
  await page.getByRole('button', { name: '关闭资讯详情', exact: true }).click();
  await page.getByRole('button', { name: fixtureArticles[1].title, exact: true }).click();
  await expect(dialog(page).getByRole('button', { name: '生成新闻摘要', exact: true })).toBeEnabled();
  expect(state.summaryWrites).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('edition summary keeps its snapshot evidence even when the current article has changed', async ({ page }) => {
  const state = await workspace(page, { role: 'researcher', modelEnabled: true, savedSummary: true });
  state.articles[0] = { ...state.articles[0], evidence_id: uuid(9090), title: '当前更新版本，不替换刊期原始证据' };
  await page.goto('/news?view=daily&period=day');
  await page.locator('.nc-paper-lead h4 button').click();
  await expect(dialog(page)).toContainText('刊期证据版本');
  await expect(dialog(page).getByRole('region', { name: '新闻摘要', exact: true })).toContainText('合成单篇摘要：仅依据选中的证据版本。');
  expect(state.summaryReads).toContain(fixtureArticles[0].evidence_id);
  expect(state.summaryReads).not.toContain(uuid(9090));
  expect(state.summaryWrites).toEqual([]);
  expect(state.requests.some(value => value.startsWith('POST /api/v1/news/editions'))).toBe(false);
  expect(state.errors).toEqual([]);
});

test('black gold home and editions fit desktop and phones, with keyboard access and no horizontal overflow', async ({ page }, info) => {
  const state = await workspace(page);
  const output = process.env.NEWS_CENTER_QA_DIR ?? info.outputPath('news-center');
  await mkdir(output, { recursive: true });
  for (const view of ['feed', 'daily']) {
    await page.goto('/news?view=' + view);
    await expect(page.getByRole('heading', { name: view === 'feed' ? /今日焦点|最近焦点/ : '天衍日报', exact: true })).toBeVisible();
    for (const width of [1920, 1440, 1000, 768, 390, 320]) {
      await page.setViewportSize({ width, height: width < 680 ? 844 : 1080 });
      await page.waitForLoadState('networkidle');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), view + ' width ' + width).toBe(true);
      if ([1920, 1440, 390].includes(width)) await page.screenshot({ path: resolve(output, `news-center-${view}-${width}.png`), fullPage: true, animations: 'disabled' });
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('.nc-paper-lead h4 button').focus();
  await page.keyboard.press('Enter');
  await expect(dialog(page)).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog(page)).toHaveCount(0);
  await expect(page.locator('.nc-paper-lead h4 button')).toBeFocused();
  expect(state.errors).toEqual([]);
});
