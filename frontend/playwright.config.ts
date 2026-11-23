import { defineConfig } from '@playwright/test';

// Terminal is the approved default four-page interface (terminal.spec.ts).
// Old workbench/auction/market specs run through explicit ?legacy=1 deep links;
// their original UI and research regressions remain part of the active suite.
// The separately retained WorkbenchV2 proposal has a different view contract
// and is not mounted as the default product. Its seven UI cases are archived
// until that alternative is explicitly mounted; this does not skip Terminal.

export default defineConfig({
  testDir: './tests/e2e', workers: 1, retries: 0, timeout: 30_000,
  testIgnore: ['**/workbench-v2.spec.ts'],
  use: { baseURL: process.env.JL_E2E_BASE_URL ?? 'http://127.0.0.1:8088',
    browserName: 'chromium', locale: 'zh-CN', timezoneId: 'Asia/Shanghai', viewport: { width: 1440, height: 1000 },
    trace: 'off', screenshot: 'only-on-failure' },
  reporter: 'list',
});
