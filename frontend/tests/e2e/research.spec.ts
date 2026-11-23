import { expect, test } from '@playwright/test';

for (const [path, title, section] of [
  ['/research/reviews', '每日复盘', '市场环境与情绪判断'],
  ['/research/directions', '板块轮动', '市场主线结构'],
  ['/research/plans', '明日预案', '观察对象与验证条件'],
]) {
  test(`${title}: date, unavailable actions and responsive reference layout`, async ({ page }, info) => {
    await page.goto(path + (path.endsWith('reviews') ? '?legacy=1' : ''));
    await page.getByLabel('账号', { exact: true }).fill(process.env.JL_E2E_USERNAME ?? 'e2e');
    await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
    await page.getByRole('button', { name: '进入工作台' }).click();
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
    if (path.endsWith('reviews')) await page.getByRole('radiogroup', { name: '每日复盘工作区' }).getByRole('radio', { name: '人工研究', exact: true }).click();
    await expect(page.getByRole('heading', { name: section, exact: true })).toBeVisible();
    await expect(page.getByText('人工研究支持保存、固定版本发布与历史回查；行情与 AI 未接入。', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '分享', exact: true })).toBeDisabled();
    await page.getByLabel('研究日期', { exact: true }).fill('2026-10-16');
    await page.reload();
    await expect(page.getByLabel('研究日期', { exact: true })).toHaveValue('2026-10-16');
    for (const width of [1920, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: info.outputPath(`${path.split('/').at(-1)}-${width}.png`), fullPage: true });
    }
    await page.getByRole('link', { name: '查看当日记录', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#research-records')).toBeFocused();
    await expect(page.locator('#research-records')).toBeInViewport();
    expect(new URL(page.url()).searchParams.get('date')).toBe('2026-10-16');
    if (path.endsWith('reviews')) {
      const choices = page.getByRole('radiogroup', { name: '情绪判断来源' });
      await choices.getByRole('radio', { name: 'AI 参考', exact: true }).click();
      await expect(page.getByText('AI 未接入，不生成默认分析。', { exact: true })).toBeVisible();
    } else if (path.endsWith('directions')) {
      await page.getByRole('radiogroup', { name: '方向判断来源' }).getByRole('radio', { name: 'AI 参考', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'AI 参考判断', exact: true })).toBeVisible();
    } else {
      await page.getByRole('radiogroup', { name: '预案观察分类' }).getByRole('radio', { name: '潜在机会', exact: true }).click();
      await expect(page.getByRole('radio', { name: '潜在机会', exact: true })).toBeChecked();
      await expect(page.getByText('监控未启用', { exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '新建条件预案', exact: true }).first()).toBeEnabled();
    }
    await page.getByRole('navigation', { name: '复盘研究子页面' }).getByRole('link', { name: '板块轮动', exact: true }).click();
    await expect(page.getByRole('heading', { name: '板块轮动', exact: true })).toBeVisible();
    await expect(page.getByLabel('研究日期', { exact: true })).toHaveValue('2026-10-16');
  });
}
