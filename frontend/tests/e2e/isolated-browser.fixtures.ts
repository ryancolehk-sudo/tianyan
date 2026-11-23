import { test as base } from '@playwright/test';
import type { Page } from '@playwright/test';

export async function openNotifications(page: Page) {
  const panel = page.getByRole('dialog', { name: '消息通知', exact: true });
  if (!await panel.isVisible()) await page.getByRole('button', { name: /^消息通知/ }).click();
  return panel;
}

// Page-level scenario routes take precedence. Anything they do not configure
// must remain inside the static test origin and cannot reach a real API.
export const test = base.extend({
  context: async ({ context, baseURL }, use) => {
    const origin = new URL(baseURL!).origin;
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort('blockedbyclient');
      if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/health/')) return route.fulfill({ status: 503,
        json: { error: { message: '隔离测试未配置此读取或操作' } } });
      return route.continue();
    });
    await use(context);
  },
});
