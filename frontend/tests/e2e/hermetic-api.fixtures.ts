import type { Page } from '@playwright/test';

/**
 * Lowest-priority API boundary for page-specific synthetic fixtures. Context
 * routes run after page routes, so a test's precise data/error/revocation mocks
 * keep precedence regardless of when this boundary is installed. Unconfigured
 * requests never reach a real backend or masquerade as successful empty data.
 */
export async function routeHermeticApi(page: Page, session: unknown) {
  const state = { signedIn: false };
  const error = (code: string, message: string) => ({
    error: { code, message, details: [] }, request_id: '00000000-0000-4000-8000-0000000000ab',
  });
  await page.context().route('**/api/v1/**', route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === '/api/v1/session') {
      if (method === 'POST') state.signedIn = true;
      else if (method === 'DELETE') { state.signedIn = false; return route.fulfill({ status: 204 }); }
      else if (method !== 'GET') return route.fulfill({ status: 405, json: error('fixture_method_not_configured', '合成夹具未配置此会话操作。') });
      return state.signedIn ? route.fulfill({ json: session })
        : route.fulfill({ status: 401, json: error('unauthorized', '需要登录。') });
    }
    if (!state.signedIn) return route.fulfill({ status: 401, json: error('unauthorized', '需要登录。') });
    return method === 'GET'
      ? route.fulfill({ status: 503, json: error('fixture_read_not_configured', `合成夹具未配置此读取接口：${path}`) })
      : route.fulfill({ status: 405, json: error('fixture_write_not_configured', `合成夹具拒绝未配置的写入：${method} ${path}`) });
  });
  return state;
}
