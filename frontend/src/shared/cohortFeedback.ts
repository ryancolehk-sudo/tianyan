type Fields = Record<string, unknown>;

function finite(value: unknown): number | null {
  if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function cohortMean(group: Fields | undefined, session: 'open' | 'close') {
  const value = finite(group?.[session + '_mean_pct']);
  const valid = finite(group?.[session + '_valid_n']);
  const total = finite(group?.N);
  if (value !== null) return { value, note: '' };
  const known = finite(group?.['known_' + session + '_mean_pct']);
  if (known !== null && valid !== null && valid > 0 && (total === null || valid <= total)) {
    return { value: known, note: '已知 ' + valid + ' 只' };
  }
  return { value: null, note: total === 0 ? '无此类样本' : total === null ? '缺昨日样本池' : '缺有效报价' };
}

const reasons: Record<string, string> = {
  previous_quote_missing: '缺上日行情', previous_close_unusable: '缺有效收盘价',
  previous_trading_unverified: '没有有效成交记录', previous_price_limit_missing: '缺当日限制价',
  previous_upper_limit_unusable: '涨停价无效', previous_price_limit_not_applicable: '当日无涨跌幅限制',
  previous_close_above_limit: '收盘价高于涨停价', previous_price_limit_range_unusable: '限制价区间异常',
  previous_close_below_limit: '收盘价低于跌停价',
};

export function unclassifiedNote(group: Fields | undefined) {
  const count = finite(group?.unknown_n);
  if (count === null || count <= 0) return '';
  const samples = Array.isArray(group?.samples) ? group.samples : [];
  const labels = new Set<string>();
  for (const sample of samples) {
    if (sample?.classification !== 'unknown' || !Array.isArray(sample.reasons)) continue;
    for (const reason of sample.reasons) if (reasons[reason]) labels.add(reasons[reason]);
  }
  return '另有 ' + count + ' 只未能归类' + (labels.size ? '（' + [...labels].join('、') + '）' : '') + '，未计入断板样本';
}
