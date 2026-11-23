import { readFile } from 'node:fs/promises';
import { resolve, sep, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Page } from '@playwright/test';

const output = fileURLToPath(new URL('../test-fixtures/', import.meta.url));
const contentTypes: Record<string, string> = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml' };
export const chartFixturePath = '/__trading-fixture/tests/fixtures/trading-chart-host.html';
export const widgetsFixturePath = '/__trading-fixture/tests/fixtures/trading-widgets-host.html';

/** Serve compiled test-only entries on the current test origin, including when
 * baseURL points at production nginx. No test fixture is installed in nginx. */
export async function serveTradingFixture(page: Page) {
  // Fail promptly when a local caller forgot to build the test entries.
  await readFile(resolve(output, 'tests/fixtures/trading-chart-host.html'));
  await page.route('**/__trading-fixture/**', async route => {
    let pathname: string;
    try { pathname = decodeURIComponent(new URL(route.request().url()).pathname).slice('/__trading-fixture/'.length); }
    catch { return route.fulfill({ status: 400, body: 'Invalid test fixture path' }); }
    const path = resolve(output, pathname);
    if (!path.startsWith(output.endsWith(sep) ? output : output + sep)) return route.fulfill({ status: 403, body: 'Invalid test fixture path' });
    try { return await route.fulfill({ contentType: contentTypes[extname(path)] ?? 'application/octet-stream', body: await readFile(path) }); }
    catch { return route.fulfill({ status: 404, body: 'Test fixture asset not found' }); }
  });
}
