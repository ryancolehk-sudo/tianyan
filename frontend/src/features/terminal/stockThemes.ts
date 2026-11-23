import type { Stock } from './types';

// A saved DC membership and a vendor auction tag are separate classifications.
// Equal display names from different sources never silently join a cohort.
export function stockThemes(stock: Stock | null | undefined) {
  if (!stock) return [];
  const labels = [...new Set((stock.auction_tags ?? []).filter(tag => tag !== stock.auction_board
    && !/^(?:昨)?(?:首板|\d+板|[一二三四五六七八九十]+板)$/.test(tag)))];
  return [
    ...(stock.theme ? [{ key: `dc:${stock.theme}`, label: stock.theme, source: '已存板块成分' }] : []),
    ...labels.map(label => ({ key: `auction:${label}`, label, source: '供应商竞价标签' })),
  ];
}

export function sharesTheme(stock: Stock, selected: Stock | null) {
  const keys = new Set(stockThemes(selected).map(item => item.key));
  return stockThemes(stock).some(item => keys.has(item.key));
}
