export type TerminalView = 'overview' | 'auction' | 'ladder' | 'funds';
export const terminalTabs = [
  ['overview', '综合盯盘'], ['auction', '竞价观察'], ['ladder', '连板观察'], ['funds', '资金动向'],
] as const;
export function terminalView(path: string, search = location.search): TerminalView {
  if (path === '/auction') return 'auction';
  const value = new URLSearchParams(search).get('view');
  return terminalTabs.some(([key]) => key === value) ? value as TerminalView : 'overview';
}
export function terminalHref(view: TerminalView, search = location.search) {
  const query = new URLSearchParams(search);
  query.set('view', view);
  return '/workbench?' + query;
}
