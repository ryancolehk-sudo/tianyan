import { Change, Empty, Panel } from './Components';
import type { PageProps } from './Components';
import { dateTime } from './data';
import { useNavigation } from '../../shared/navigation';

type LadderRow = {
  code: string; name: string; height: number | null; status: string;
  pct_change: number | null; source_time?: string | null; source_name?: string | null;
};

export function OverviewLadder({ data, selected, selectStock, asOfTime }: PageProps) {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  const live = !asOfTime && data.intraday_ladder?.trade_day === data.trade_day ? data.intraday_ladder : null;
  const archived = data.stocks.filter(stock => stock.daily_pool?.trade_day === data.trade_day
    && (stock.daily_pool.state === 'U' || stock.daily_pool.state === 'Z'));
  const daily = archived.length > 0 && (!live || live.phase === 'closed');
  const rows: LadderRow[] = daily ? archived.map(stock => ({
    code: stock.code, name: stock.name, height: stock.daily_pool!.board_height,
    status: stock.daily_pool!.state === 'U' ? 'closed' : 'opened',
    pct_change: stock.pct_change, source_time: stock.source_time, source_name: stock.daily_pool!.source_name,
  })) : (live?.rows ?? []).filter(row => ['sealed', 'touched', 'opened'].includes(row.status)).map(row => ({
    ...row, height: row.target_height ?? null, pct_change: row.pct_change ?? null,
    status: row.freshness === 'current' ? row.status : 'stale',
  }));
  const groupKey = (row: LadderRow) => row.status === 'opened' ? '开板' : row.status === 'stale' ? '待更新'
    : row.height != null ? String(row.height) : '板数待核';
  const groups = [...new Set(rows.map(groupKey))].sort((a, b) =>
    (Number.isFinite(Number(a)) ? 0 : 1) - (Number.isFinite(Number(b)) ? 0 : 1)
    || Number(b) - Number(a) || a.localeCompare(b, 'zh-CN'));
  const statusLabel: Record<string, string> = {
    sealed: '已封住', touched: '触板待核', opened: '开板', stale: '行情待更新', closed: '收盘涨停',
  };
  const full = new URLSearchParams({ view: 'ladder',
    ...(query.get('trade_day') ? { trade_day: query.get('trade_day')! } : {}),
    ...(selected ? { stock: selected.code } : {}), ...(asOfTime ? { as_of_time: asOfTime } : {}) });
  const stamp = daily ? '收盘' : live?.phase === 'closed' ? '盘中末次'
    : live?.phase === 'break' ? '午休' : '盘中';
  return <Panel title="连板梯队" subtitle={data.trade_day + ' ' + stamp}
    action={<a className="ty-link" href={'/workbench?' + full}>完整梯队</a>} className="ty-mini-ladder">
    <div className="ty-mini-ladder-body" aria-label="当日连板梯队">
      {groups.map(key => {
        const group = rows.filter(row => groupKey(row) === key).sort((a, b) =>
          (b.pct_change ?? -Infinity) - (a.pct_change ?? -Infinity) || a.code.localeCompare(b.code));
        return <div className="ty-mini-ladder-row" key={key}>
          <div><strong>{/^\d+$/.test(key) ? key + '板' : key}</strong><small>{group.length}只</small></div>
          <div className="ty-mini-ladder-stocks">{group.map(stock => <button key={stock.code} type="button"
            className={selected?.code === stock.code ? 'is-selected' : ''} aria-pressed={selected?.code === stock.code}
            data-ladder-status={stock.status} onClick={() => selectStock(stock.code)}
            title={`${stock.name} ${stock.code} · ${statusLabel[stock.status]} · ${stock.height == null ? '板数待核' : stock.height + '板'}\n${stock.source_name ?? ''} · 行情 ${dateTime(stock.source_time)}`}>
            <span className="ty-ladder-chip-name">{stock.name}</span><Change value={stock.pct_change}/>
            {stock.status === 'touched' && <small>触板</small>}
          </button>)}</div>
        </div>;
      })}
      {!groups.length && <Empty title="等待当日连板数据"/>}
    </div>
  </Panel>;
}
