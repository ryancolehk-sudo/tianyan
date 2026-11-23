import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: '.', testMatch: 'vertical.spec.ts', workers: 1, retries: 0, timeout: 180_000,
  use: { baseURL: process.env.JL_E2E_BASE_URL ?? 'http://127.0.0.1:8080', browserName: 'chromium',
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai', viewport: { width: 1440, height: 1000 }, trace: 'off', screenshot: 'only-on-failure' },
  reporter: 'list',
});
