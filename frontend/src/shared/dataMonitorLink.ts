export type MonitorLinkContext = {
  sourceId?: string | null;
  tradeDay?: string | null;
  asOfTime?: string | null;
  returnTo?: string | null;
  context?: 'current' | 'history';
};

const returnPaths = new Set(['/workbench', '/auction', '/news', '/data', '/system',
  '/research/reviews', '/research/directions', '/research/plans', '/research/validations', '/research/tomorrow']);
export function monitorReturnTo(value: string | null | undefined) {
  if (!value?.startsWith('/') || value.startsWith('//') || /[\\\u0000-\u001f]/.test(value)) return null;
  try {
    const target = new URL(value, 'https://workspace.invalid');
    return target.origin === 'https://workspace.invalid' && returnPaths.has(target.pathname)
      ? target.pathname + target.search + target.hash : null;
  } catch { return null; }
}

// Callers pass identities and dates from the actual response, never from a name or family.
export function monitorHref(value: MonitorLinkContext = {}) {
  const query = new URLSearchParams({ view: 'monitor', monitor_context: value.context ?? (value.asOfTime ? 'history' : 'current') });
  if (value.sourceId) query.set('source_id', value.sourceId);
  if (value.tradeDay) query.set('trade_day', value.tradeDay);
  if (value.asOfTime) query.set('as_of_time', value.asOfTime);
  const back = monitorReturnTo(value.returnTo);
  if (back) query.set('return_to', back);
  return '/data?' + query + '#data-recovery';
}

export function monitorContext(search: string) {
  const query = new URLSearchParams(search);
  const sourceId = query.get('source_id') ?? '';
  const tradeDay = query.get('trade_day') ?? '';
  const asOfTime = query.get('as_of_time') ?? '';
  const history = query.get('monitor_context') === 'history' || !!asOfTime;
  const errors: string[] = [];
  if (sourceId && !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(sourceId)) errors.push('来源编号无效，请从原数据区域重新打开，或清除来源筛选。');
  if (tradeDay && (!/^\d{4}-\d{2}-\d{2}$/.test(tradeDay) || !Number.isFinite(Date.parse(tradeDay + 'T00:00:00Z'))
    || new Date(tradeDay + 'T00:00:00Z').toISOString().slice(0, 10) !== tradeDay)) errors.push('资料日期无效，请重新选择日期。');
  if (asOfTime && (!/(?:Z|[+-]\d{2}:\d{2})$/.test(asOfTime) || !Number.isFinite(Date.parse(asOfTime)))) errors.push('原资料截止时间无效，请返回原页重新选择。');
  return { sourceId, tradeDay, asOfTime, history, returnTo: monitorReturnTo(query.get('return_to')), error: errors.join(' ') };
}
