import type { Page } from '@playwright/test';

/** These regression cases deliberately exercise preserved legacy components.
 * Adjust only their test navigation target; production links continue to open
 * the approved Terminal interface, covered separately by terminal.spec.ts and
 * the default-entry shell/navigation cases. WorkbenchV2 remains archived. */
export async function openLegacyWorkbench(page: Page, name = '盘中工作台') {
  const link = page.getByRole('navigation', { name: '主导航', exact: true })
    .getByRole('link', { name: name === '盘中工作台' ? /^(?:盘中)?工作台$/ : name, exact: true });
  await link.evaluate(anchor => {
    const url = new URL((anchor as HTMLAnchorElement).href);
    if (!['/workbench', '/auction'].includes(url.pathname)) throw new Error('Expected a legacy workbench target');
    url.searchParams.set('legacy', '1');
    anchor.setAttribute('href', url.pathname + url.search + url.hash);
  });
  await link.click();
}
