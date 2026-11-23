import type { DatasetItem } from '../../generated/schema';

const time = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });

export function BreadthDailyReference({ items, day, busy, failed }: {
  items: DatasetItem[]; day: string; busy: boolean; failed: boolean;
}) {
  // The cache endpoint rechecks display/cache rights on every read. Do not
  // merge providers, dates, revisions, or substitute a previous nonempty table.
  const record = items.filter(item => item.family === 'tushare_limit_list_d' && item.payload.dataset === 'tushare.limit_list_d')
    .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at))[0];
  const rows = record?.payload.rows ?? [];
  const received = record ? Date.parse(record.received_at) : NaN;
  const close = Date.parse(day + 'T15:00:00+08:00');
  const codes = rows.map(row => row.ts_code);
  const valid = !failed && rows.length > 0 && Number.isFinite(received) && received >= close && received <= Date.now()
    && rows.every(row => row.trade_date === day.replaceAll('-', '') && /^(?:U|D|Z)$/.test(row.limit ?? '')
      && /^\d{6}\.(?:SH|SZ|BJ)$/.test(row.ts_code ?? ''))
    && new Set(codes).size === rows.length;
  const count = (category: string) => {
    const amount = rows.filter(row => row.limit === category).length;
    // No category rows does not establish a zero: upstream coverage and
    // pagination are not yet proven by this reference-table contract.
    return amount ? `${amount} 条` : '未返回';
  };
  return <section className="breadth-daily-reference" aria-label="涨跌停盘后日榜参考" aria-busy={busy}>
    <div className="breadth-daily-heading"><strong>盘后日榜参考 · {day}</strong><a href="/data?view=sources">查看日榜采集状态</a></div>
    {valid && record ? <>
      <div className="breadth-daily-counts"><span>日榜返回涨停 <b className="breadth-daily-up" data-testid="breadth-daily-up">{count('U')}</b></span><span>日榜返回跌停 <b className="breadth-daily-down" data-testid="breadth-daily-down">{count('D')}</b></span></div>
      <p>{record.source_name} · 接收于 {time.format(new Date(received))}（北京时间）</p>
      <p>按日榜 U / D 分类计数，不含 ST；仅统计当前返回条目，完整性尚未确认。盘中曲线仍显示实际采集记录，日榜不补画缺失时段。</p>
    </> : <p role="status">{failed ? '日榜缓存读取失败；盘中曲线仍可查看。' : busy ? '正在核对所选日期的日榜…' : '所选日期暂无可核对的盘后日榜；盘中最后采集值不代表收盘数量。'}</p>}
  </section>;
}
