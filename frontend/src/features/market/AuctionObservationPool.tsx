import { useCallback, useEffect, useMemo, useState } from 'react';
import type { DatasetItem, RuntimeSource, RuntimeSourceListResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Empty, ErrorState, Segmented } from '../../shared/ui';
import { PlanChoice } from './AuctionNotebook';
import { compactMoney, decimalCompare, matchesStock, tableForDay, validDecimal } from './auctionWorkbenchModel';
import type { VendorBatch } from './auctionVendorBatches';
import { latestStage } from './auctionWorkbenchModel';
import { shiftDay, sourceStates, stamp } from './auctionFormat';
import { previousCollectedSession } from './auctionCalendar';

/** Confirm a previous market session, including closed days, rather than day - 1. */
export function usePreviousAuctionDay(day: string, index: RuntimeSourceListResponse | null) {
  return useRead(useCallback(async () => {
    async function resolvePrevious() {
    const versions = (await api.calendars()).data.items;
    const calendars = ['SSE', 'SZSE'].map(exchange => versions.filter(c => c.exchange === exchange && c.coverage_start <= day && c.coverage_end >= day)
      .sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at))[0]);
    if (calendars.some(c => !c)) return null;
    for (let offset = 0; offset <= 14; offset++) {
      const candidate = shiftDay(day, -offset);
      if (calendars.some(c => c.coverage_start > candidate)) return null;
      const answers = await Promise.all(calendars.map(c => api.calendarSession(c.exchange, c.version, candidate)));
      if (answers.some(a => !a.data.session || a.data.requested_day !== candidate)) return null;
      const open = answers.map(a => a.data.session!.is_open);
      if (open[0] !== open[1]) return null;
      if (!offset && !open[0]) return null;
      if (offset && open[0]) return candidate;
    }
    return null;
    }
    if (!index) return { day, previous: null };
    const manual = await resolvePrevious();
    if (manual) return { day, previous: manual };
    const collected = await api.calendarEvidence();
    return { day, previous: previousCollectedSession(day, collected.data.items, index.data.items) };
  }, [day, index]));
}

export function AuctionObservationPool({ day, items, batches, sources, query, select, previousDay, calendarBusy, refresh, errors }: {
  day: string; items: DatasetItem[]; batches: VendorBatch[]; sources: RuntimeSource[]; query: string;
  select: (code: string) => void; previousDay: string | null; calendarBusy: boolean; refresh: () => void; errors: string[];
}) {
  const [tab, setTab] = useState('昨日强势'), [filter, setFilter] = useState('涨停'), [sort, setSort] = useState('成交额');
  const [page, setPage] = useState(1), [plan, setPlan] = useState('');
  const revision = useRead(useCallback(() => plan ? api.researchRevision(plan) : Promise.resolve(null), [plan]));
  useEffect(() => { setPage(1); setPlan(''); }, [day]);
  useEffect(() => { setPage(1); }, [tab, filter, sort, query]);
  const family = tab === '成交核对' ? 'tushare_stk_auction' : 'tushare_limit_list_d';
  const status = sources.find(s => s.family === family);
  const record = tableForDay(items, family, family.replace('tushare_', 'tushare.'), tab === '成交核对' ? day : previousDay ?? '');
  const stockMap = useMemo(() => new Map(batches.filter(b => b.tradeDay === day && b.state === 'paired')
    .flatMap(b => b.stocks.map(s => [s.code, { stock: s, batch: b }] as const))), [batches, day]);
  const rows = (record?.payload.rows ?? []).filter(r => {
    const stock = stockMap.get(r.ts_code!.slice(0, 6))?.stock;
    const text = query.trim().toLowerCase();
    if (text && !(stock && matchesStock(stock, text)) && ![r.ts_code, r.name, r.industry].some(v => v?.toLowerCase().includes(text))) return false;
    return tab === '成交核对' || (filter === '连板' ? r.limit === 'U' && Number(r.limit_times) >= 2 : r.limit === (filter === '炸板' ? 'Z' : 'U'));
  }).sort((a, b) => {
    const key = tab === '成交核对' ? sort === '量比' ? 'volume_ratio' : sort === '换手率' ? 'turnover_rate' : 'amount' : 'limit_times';
    const x = a[key], y = b[key];
    return validDecimal(x) && validDecimal(y) ? decimalCompare(y, x) : validDecimal(x) ? -1 : validDecimal(y) ? 1 : 0;
  });
  const pages = Math.max(1, Math.ceil(rows.length / 25)), current = Math.min(page, pages);
  return <section className="card auction-observation-pool" aria-label="竞价观察池"><div className="card-heading"><h2>竞价观察池</h2><small>背景 → 观察 → 成交核对</small></div>
    <Segmented label="观察池分类" options={['昨日强势', '成交核对', '预案自选']} value={tab} onChange={setTab}/>
    {tab === '预案自选' ? <><PlanChoice key={day} day={day} value={plan} change={setPlan}/>{revision.busy && plan ? <p role="status">正在读取固定预案…</p> : revision.error ? <ErrorState error={revision.error} retry={revision.reload}/> : revision.data?.data ? <div className="auction-pool-plans"><h3>{revision.data.data.title} · 第 {revision.data.data.revision_number} 版</h3>{revision.data.data.scenarios.map(s => <div key={s.id}><strong>{s.label}</strong><p>{s.trigger_text}</p><small>原预案引用 {s.instrument_ids.length} 个标的；不自动核查条件</small><p><a href={`#auction-notebook`}>在下方记录与回查人工观察</a></p></div>)}</div> : null}</> : <>
      <div className="auction-pool-context">{tab === '昨日强势' ? <>背景交易日：{previousDay ?? (calendarBusy ? '正在核对日历…' : '日历覆盖不足，未知')}<Segmented label="昨日背景分类" options={['涨停', '连板', '炸板']} value={filter} onChange={setFilter}/></> : <><span>成交数据日：{day} · 9:26–9:29 开始可取，以实际返回为准</span><label>排序 <select aria-label="竞价成交排序" value={sort} onChange={e => setSort(e.target.value)}>{['成交额', '量比', '换手率'].map(s => <option key={s}>{s}</option>)}</select></label></>}</div>
      <p className="feature-note">{status ? `${status.provider.name} · ${sourceStates[status.collection_state] ?? status.collection_state}` : '来源尚未配置或状态未知'}{status?.last_attempt?.reason ? ` · ${status.last_attempt.reason}` : ''}</p>
      {errors.includes(family) && <p className="auction-notice" role="status">参考表缓存读取失败；如有已核权限的旧缓存则继续显示。</p>}
      {!record ? <Empty title={tab === '成交核对' ? '所选日暂无可核对的竞价成交' : '暂无可核对的昨日背景'}>{tab === '成交核对' ? '继续查看短线侠阶段封单；成交权限、范围和采集状态见数据中心。' : '只关联前一交易日原表，不拿最近一份或当前日名单代替昨日。'}</Empty>
        : <><div className="table-wrap" role="region" aria-label="竞价观察池条目" tabIndex={0}><table><thead><tr><th>标的 / 代码</th>{tab === '成交核对' ? <><th>竞价成交额（原单位）</th><th>量比 / 换手率</th></> : <><th>昨日背景</th><th>所选日封单</th></>}</tr></thead><tbody>{rows.slice((current - 1) * 25, current * 25).map(r => {
          const code = r.ts_code!.slice(0, 6), stock = stockMap.get(code), stage = stock ? latestStage(stock.batch) : null;
          return <tr key={r.ts_code}><td><button type="button" className="pool-stock" onClick={() => select(code)}><strong>{r.name ?? stock?.stock.name ?? code}</strong><small>{r.ts_code}</small></button></td>{tab === '成交核对' ? <><td>{r.amount ?? '—'}</td><td>{r.volume_ratio ?? '—'}<small>换手 {r.turnover_rate == null ? '—' : r.turnover_rate + "（原值）"}</small></td></> : <><td>{r.limit === 'Z' ? '炸板' : r.limit_times == null ? '连板未知' : `${r.limit_times}连板`}<small>开板 {r.open_times ?? '未知'} 次</small></td><td>{stock && stage ? compactMoney(stock.stock.amounts[stage]) : '未入来源表'}</td></>}</tr>;
        })}{!rows.length && <tr><td colSpan={3}>本次返回中无匹配条目；不代表市场为零</td></tr>}</tbody></table></div><div className="auction-pager"><span>第 {current} / {pages} 页 · {rows.length} 条</span><button disabled={current <= 1} onClick={() => setPage(current - 1)}>上一页</button><button disabled={current >= pages} onClick={() => setPage(current + 1)}>下一页</button></div><details className="auction-pool-provenance"><summary>来源、接收时间与范围</summary><p>{record.source_name} · 本机接收 {stamp(record.received_at)}</p><p>{record.payload.scope}</p><p>版本 {record.content_hash.slice(0, 16)}…；来源更新时间未知</p><a href={record.payload.source_url} target="_blank" rel="noreferrer">原始出处</a></details></>}
      <p className="feature-note">{tab === '昨日强势' ? '按供应商 U / Z / limit_times 分类，不含 ST 统计；仅当前返回范围。' : '成交额与封单分别展示；不计算抢筹、净额或撤单率。'}</p>
    </>}
    <div className="auction-pool-footer"><button type="button" onClick={refresh}>刷新观察池缓存</button><a href="/data?view=sources">来源状态 ›</a></div>
  </section>;
}
