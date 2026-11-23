import { test, expect } from '@playwright/test';
import capture from '../../../scripts/capture_workbench_values.cjs';

const { visibleQuoteMatches, expectedQuote, quoteIdentity } = capture;
const format = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const stock = { code: '999991.SZ', name: '合成验收对象', price: 12.35, price_basis: 'intraday',
  source_family: 'synthetic_fixture', source_name: '合成来源',
  source_time: '2026-10-29T09:30:00+08:00', source_received_at: '2026-10-29T09:30:01+08:00' };

test('a changed clock cannot pass until the real price and both source clocks are visible', async ({ page }) => {
  const expected = expectedQuote(stock, format);
  const render = async (price: string, hint: string, name = stock.name) => page.setContent(`<section class="ty-ai-follow">
    <div class="ty-ai-subject"><strong>${name}</strong></div><p class="ty-ai-follow-hint">${hint}</p>
    <div class="ty-ai-timeline"><div class="ty-ai-item"><p><b>${price}</b></p></div></div></section>`);
  const source = '实际行情来源：合成来源 · ';
  const both = source + '行情时点 2026-10-29 09:30:00 · 接收 2026-10-29 09:30:01';
  await render('11.00', both);
  expect(await page.evaluate(visibleQuoteMatches, expected)).toBe(false);
  await render('12.35', source + '行情时点 2026-10-29 09:29:00 · 接收 2026-10-29 09:30:01');
  expect(await page.evaluate(visibleQuoteMatches, expected)).toBe(false);
  await render('12.35', source + '行情时点 2026-10-29 09:30:00');
  expect(await page.evaluate(visibleQuoteMatches, expected)).toBe(false);
  await render('12.35', both, '另一个合成对象');
  expect(await page.evaluate(visibleQuoteMatches, expected)).toBe(false);
  await render('12.35', both.replace('合成来源', '另一个来源'));
  expect(await page.evaluate(visibleQuoteMatches, expected)).toBe(false);
  await render('12.35', both.replace('行情时点', '服务器快照'));
  expect(await page.evaluate(visibleQuoteMatches, expected)).toBe(false);
  await render('12.35', both);
  expect(await page.evaluate(visibleQuoteMatches, expected)).toBe(true);
});

test('duplicate responses do not count, while an unchanged price with a new native clock does', () => {
  const identity = quoteIdentity(stock, '2026-10-29');
  expect(quoteIdentity({ ...stock, source_received_at: '2026-10-29T09:31:01+08:00' }, '2026-10-29')).toBe(identity);
  expect(quoteIdentity({ ...stock, source_time: '2026-10-29T09:31:00+08:00' }, '2026-10-29')).not.toBe(identity);
  expect(quoteIdentity({ ...stock, source_family: 'another_fixture' }, '2026-10-29')).not.toBe(identity);
  expect(expectedQuote({ ...stock, source_time: null }, format)).toBeNull();
  expect(expectedQuote({ ...stock, source_received_at: null }, format)).toBeNull();
  expect(expectedQuote({ ...stock, source_family: null }, format)).toBeNull();
  expect(expectedQuote({ ...stock, source_name: null }, format)).toBeNull();
  expect(expectedQuote({ ...stock, price_basis: 'unknown' }, format)).toBeNull();
});
