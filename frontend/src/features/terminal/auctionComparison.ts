import type { Stock } from './types';
export const auctionNodes = ['09:15','09:20','09:25'] as const;
export type AuctionNode = typeof auctionNodes[number];
export type AuctionSort = 'auction' | 'amount' | 'seal' | 'current';
export function nodeDelta(stock: Stock, node: AuctionNode) {
  const previous = auctionNodes[auctionNodes.indexOf(node)-1];
  const current = stock.node_amounts?.[node], before = previous ? stock.node_amounts?.[previous] : null;
  // The API's node triplet is projected atomically from one source row, never
  // from rolling seals or an adjacent trading day. Unknown origin is ineligible.
  if (!stock.node_source || !stock.node_received_at || current == null || before == null ||
      !Number.isFinite(current) || !Number.isFinite(before) || current < 0 || before < 0) return null;
  return {amount:current-before, pct:before === 0 ? null : (current-before)/before*100, previous};
}
export function rankAuction(items: Stock[], sort: AuctionSort, node: AuctionNode) {
  const value = (row:Stock) => sort==='auction' ? row.auction_indicative_pct ?? row.auction_pct : sort==='amount' ? row.auction_amount_cny : sort==='current' ? row.auction_current_seal_cny : row.node_amounts?.[node];
  return [...items].sort((a,b)=>(value(b)??-Infinity)-(value(a)??-Infinity)||a.code.localeCompare(b.code));
}
