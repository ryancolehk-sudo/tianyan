import { createContext, useContext, useEffect, useRef, useState } from 'react';
import type { PropsWithChildren, ReactNode } from 'react';
import { api, ApiError } from '../../shared/api';
import { Icon } from '../../shared/icons';
import { useRead } from '../../shared/useRead';
import { useCallback } from 'react';
import { amount, isIntradaySnapshot, metricLabel, number, pct, snapshotTime, text, tone } from './model';
import type { Coverage, Day, Metrics, Ref, Row } from './model';
import { getDistribution, histogramColor } from './distribution';
import { notifiedReadError, useBackgroundRefresh } from '../../shared/BackgroundRefresh';
import { monitorHref } from '../../shared/dataMonitorLink';

export const ReviewRefresh = createContext(0);
export function useBoundRead<T>(request: string, load: () => Promise<T>) {
  const epoch = useContext(ReviewRefresh);
  const key = `${request}:refresh:${epoch}`;
  const retained = useRef<{ request: string; value: T } | null>(null);
  const read = useRead(useCallback(async () => ({ key, request, value: await load(), error: null as Error | null }), [key]));
  const matches = read.data?.key === key;
  if (read.data?.request === request && read.data.value !== null && !read.data.error) retained.current = { request, value: read.data.value };
  const forbidden = read.error instanceof ApiError && [401,403].includes(read.error.status) || read.data?.error instanceof ApiError && [401,403].includes(read.data.error.status);
  if (forbidden) retained.current = null;
  const saved = retained.current?.request === request ? retained.current.value : null;
  const data = matches ? read.data!.value ?? saved : saved;
  const error = read.error ?? (matches ? read.data!.error : null);
  const waiting = read.busy || read.refreshing || !matches && !error;
  useBackgroundRefresh(data != null && waiting && !error && !read.data, request);
  return { data, error, busy: data == null && waiting, reload: read.reload };
}
export function Card({ title, number: label, action, className = '', children }: PropsWithChildren<{ title: string; number?: string; action?: ReactNode; className?: string }>) {
  return <section className={`dr-card ${className}`}><header><h2>{label && <span>{label}</span>}{title}</h2>{action}</header>{children}</section>;
}
export function Empty({ children = '所选日期暂无已入库资料。' }: PropsWithChildren) { return <p className="dr-empty">{children}</p>; }
export function Loading({ text: label = '正在读取已保存资料…' }: { text?: string }) { return <p className="dr-loading" role="status"><span/>{label}</p>; }
export function Failure({ error, retry }: { error: Error | null; retry: () => void }) { return error && !notifiedReadError(error) ? <div className="dr-error" role="alert">{error.message}<button onClick={retry}>重试</button><a className="text-link" href={monitorHref({ context: 'current', returnTo: location.pathname + location.search + location.hash })}>查看当前运行 · 全部来源</a></div> : null; }
export function Table({ label, className = '', children }: PropsWithChildren<{ label: string; className?: string }>) { return <div className={`dr-table-scroll ${className}`} role="region" aria-label={label} tabIndex={0}><table className="dr-table">{children}</table></div>; }
export function Value({ value, percentage = false }: { value: unknown; percentage?: boolean }) { return <span className={tone(value)}>{percentage ? pct(value) : text(value)}</span>; }
export function Money({ value }: { value: unknown }) { return <span className={tone(value)}>{amount(value)}</span>; }
export function RefButton({ row, onOpen, label = '查看原始证据' }: { row?: Row | null; onOpen: (ref: Ref) => void; label?: string }) { return <button className="dr-icon" title={label} aria-label={label} disabled={!row?.row_reference} onClick={() => row?.row_reference && onOpen(row.row_reference)}><Icon name="news"/></button>; }
export function Dialog({ title, close, children }: PropsWithChildren<{ title: string; close: () => void }>) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const opener = document.activeElement as HTMLElement | null; const previous = document.body.style.overflow; document.body.style.overflow = 'hidden'; ref.current?.showModal(); return () => { document.body.style.overflow = previous; if (opener?.isConnected) opener.focus(); }; }, []);
  return <dialog ref={ref} className="dr-dialog" aria-label={title} onClose={close}><header><h2>{title}</h2><button aria-label="关闭弹窗" onClick={() => ref.current?.close()}><Icon name="close"/></button></header><div className="dr-dialog-body">{children}</div></dialog>;
}
export const stateLabel = (state: string) => ({ complete: '已入库', partial: '部分可用', missing: '未入库', unavailable: '不可用', broken_lineage: '证据链待核', permission_denied: '当前无权', empty: '无披露', ambiguous_duplicate_event: '披露歧义', ambiguous_disclosure: '披露歧义', not_loaded: '未载入' } as Record<string, string>)[state] ?? '待核';
const coverageReason = (reason: string) => ({ daily_archive_pending: '完整日频尚未归档，暂展示已保存的盘中样本', daily_data_not_published: '来源日频资料尚未发布', permission_denied: '当前没有读取权限', source_disabled: '来源未启用', schema_changed: '来源响应有变化，待核实', source_date_mismatch: '来源日期与所选日期不一致' } as Record<string, string>)[reason] ?? (/[^\x00-\x7f]/.test(reason) ? reason : '该项资料尚未完整取得');
export function Evidence({ reference, close }: { reference: Ref; close: () => void }) {
  const [rowIndex, setRowIndex] = useState<number | null>(reference.source_row ?? null);
  const read = useBoundRead(reference.id, () => api.evidenceVersion(reference.id));
  const record = read.data?.data;
  const payload = record?.payload as unknown as { rows?: Row[]; columns?: Array<{ key: string; label: string; unit?: string }>; text?: string; source_url?: string; title?: string } | undefined;
  const row = rowIndex === null ? null : payload?.rows?.[rowIndex];
  return <Dialog title="原始证据" close={close}>{read.busy ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : record && <><p className="dr-note">原件只读 · 数据日与实际接收时点分别保留</p><dl className="dr-meta"><dt>证据编号</dt><dd>{record.id}</dd><dt>接收时间</dt><dd>{new Date(record.received_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</dd><dt>版本</dt><dd>{record.version_number}</dd><dt>内容校验</dt><dd>{record.content_hash}</dd><dt>源页内行号</dt><dd>{reference.source_row === undefined ? '整件' : `${reference.source_row}（从0开始）`}</dd></dl>{payload?.rows && reference.source_row === undefined && <label>原页行号（从0开始，共{payload.rows.length}行）<input type="number" aria-label="原页行号" min={0} max={Math.max(0, payload.rows.length - 1)} step={1} value={rowIndex ?? ''} onChange={e => setRowIndex(e.target.value === '' ? null : Math.max(0, Math.min(payload.rows!.length - 1, Number(e.target.value))))}/></label>}{row ? <Table label="原始字段"><tbody>{Object.entries(row).map(([key, value]) => <tr key={key}><th>{key}</th><td>{text(value)}</td><td>{payload?.columns?.find(c => c.key === key)?.unit ?? ''}</td></tr>)}</tbody></Table> : payload?.text ? <pre>{payload.text}</pre> : <p>此引用为完整资料页，包含 {payload?.rows?.length ?? 0} 条记录。</p>}{payload?.source_url && <a href={payload.source_url} target="_blank" rel="noreferrer">打开来源说明 ↗</a>}</>}</Dialog>;
}
function CoverageMonitorLink({ entry }: { entry: Coverage }) {
  if (entry.state === 'complete') return null;
  const sourceIds = [...new Set(entry.evidence_refs?.map(ref => ref.source_id).filter((id): id is string => !!id) ?? [])];
  const returnTo = location.pathname + location.search + location.hash;
  const asOfTime = entry.trade_day ? new URLSearchParams(location.search).get('as_of_time') : null;
  return sourceIds.length ? <>{sourceIds.map((sourceId, index) => <a className="text-link" key={sourceId} aria-label={`查看来源 ${sourceId} 的监控`} href={monitorHref({ sourceId, tradeDay: entry.trade_day, asOfTime, returnTo, context: entry.trade_day ? 'history' : 'current' })}>查看{sourceIds.length > 1 ? `来源${index + 1}` : '该来源'}监控</a>)}</>
    : <a className="text-link" href={monitorHref({ context: 'current', returnTo })}>当前运行 · 全部来源</a>;
}
export function CoveragePanel({ entries, flags, open, close }: { entries: Coverage[]; flags: Metrics['quality_flags']; open: (ref: Ref) => void; close: () => void }) {
  const names: Record<string, string> = { 'tushare.ths_index': '同花顺分类目录', 'tushare.ths_member': '原日同花顺分类成员', 'tushare.ths_member.manifest': '同花顺分类采集批次', 'market.auction.book': '09:25竞价盘口', 'market.duanxianxia.auction.summary': '竞价封单汇总', emotion_history: '开盘啦历史情绪', stock_funds: '个股主力资金', market_funds: '沪深大盘主力资金', group_basics: '自由流通股本与换手', daily: '股票日线', limits: '限制价', pools: '涨跌停池', members: '原日题材成员', themes: '题材行情', theme_funds: '板块资金', lhb: '龙虎榜', seats: '席位', indices: '指数日线', news: '电报窗口', tencent_minutes: '腾讯原生分钟', sina_minutes: '新浪原生分钟' };
  const flagNames: Record<string, string> = { intraday_snapshot_not_close: '使用已保存的盘中样本，不是收盘结果', invalid_value: '存在异常数值', missing_value: '字段缺失', seal_amount_unit_unverified: '封单单位与收盘时点未核', supplier_price_disagreement: '供应商池与行情核验不一致', previous_pool_price_conflict: '昨日池与收盘核验冲突', cohort_missing_quote: '跨日样本缺报价', previous_trading_unverified: '昨日成交证据不足', cohort_price_unverified: '跨日限制价待核', previous_quote_missing: '昨日报价缺失', previous_pool_membership_difference: '供应商池范围变化', limit_pct_direction_mismatch: '封板方向与涨跌幅不一致' };
  return <Dialog title="数据范围与缺口" close={close}><p>广度、成交额优先使用沪深日线实际样本；日频未到时，已保存的同日行情会明确标为盘中样本并显示截至时点。短线池为供应商披露范围，排除ST。完整分页与全市场覆盖分别判断。</p><Table label="复盘资料覆盖"><thead><tr><th>资料</th><th>日期</th><th>状态与缺口</th><th>原行数</th><th>实际接收</th><th>证据</th></tr></thead><tbody>{entries.map((e, i) => <tr key={i}><td>{names[e.dataset] ?? e.dataset}</td><td>{e.trade_day ?? '—'}</td><td title={e.scope}>{stateLabel(e.state)}{e.reason && <small className="dr-snapshot-badge">{coverageReason(e.reason)}</small>}</td><td>{text(e.row_count)}</td><td>{e.received_at ? new Date(e.received_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'}</td><td><button disabled={!e.evidence_refs?.length} onClick={() => e.evidence_refs?.[0] && open(e.evidence_refs[0])}>原件</button><CoverageMonitorLink entry={e}/></td></tr>)}</tbody></Table>{flags?.length ? <ul>{flags.map((f, i) => <li key={i}>{flagNames[f.code] ?? '来源字段或样本待核'}{f.count ? ` · ${f.count}项` : ''}</li>)}</ul> : null}<p className="dr-note">历史补采不代表当时已知。分钟整源展示，缺点不拼接；电报仅当日最新最多20条窗口。原资料未开放导出时，导出按钮保持不可用。</p></Dialog>;
}

export function Distribution({ metrics }: { metrics: Metrics }) {
  const { bins, total, complete, detailed, note } = getDistribution(metrics);
  if (!complete) return <Empty>所选日期尚无足够的已存行情或日频资料，暂不能计算涨跌分布；缺失不计作0。</Empty>;
  if (!total) return <Empty>所选日期暂无有效涨跌幅，缺失不计作平盘。</Empty>;
  const largest = Math.max(1, ...bins.map(b => b.value!));
  const tick = Math.max(1, 10 ** Math.floor(Math.log10(largest)));
  const max = Math.ceil(largest / tick) * tick;
  const ticks = Array.from({ length: max / tick + 1 }, (_, i) => i * tick);
  const step = 590 / bins.length;
  return <>{isIntradaySnapshot(metrics) && <p className="dr-note gold">已存盘中样本 · 截至 {snapshotTime(metrics)} · 非收盘分布</p>}<svg className="dr-histogram" viewBox="0 0 650 254" role="img" aria-label={`沪深股票涨跌效应${detailed ? '十一' : '九'}区间分布`}>
    <text x="42" y="28">数量（只）</text><g className="dr-histogram-legend">{[['下跌', '#00D050'], ['平盘', '#88939A'], ['上涨', '#FF3B2F']].map(([label, color], i) => <g key={label} transform={`translate(${446 + i * 65}, 12)`}><rect width="8" height="8" rx="2" fill={color}/><text x="13" y="8">{label}</text></g>)}</g>
    {ticks.map(value => <g key={value}><line x1="42" x2="642" y1={208 - value / max * 160} y2={208 - value / max * 160} className="grid"/><text x="34" y={212 - value / max * 160} textAnchor="end">{value.toLocaleString()}</text></g>)}
    {bins.map((b, i) => { const center = 47 + (i + .5) * step; const width = step * .64; const match = b.label.match(/^(跌|涨)(.+)$/); const split = match && !b.key.startsWith('limit_'); return <g key={b.key} data-distribution-band={b.key}>
      <rect x={center - width / 2} y={208 - b.value! / max * 160} width={width} height={b.value! / max * 160} fill={histogramColor(i, bins.length)}><title>{b.title}：{text(b.value)}只{b.key.startsWith('limit_') ? metricLabel(metrics, '（收盘仍封板）') : ''}</title></rect>
      <text x={center} y={200 - b.value! / max * 160} textAnchor="middle" className="dr-chart-value">{b.value!.toLocaleString()}</text>
      <text x={center} y="226" textAnchor="middle">{split ? <><tspan x={center}>{match[1] === '跌' ? '下跌' : '上涨'}</tspan><tspan x={center} dy="15">{match[2]}</tspan></> : b.label}</text></g>; })}
    </svg><p className="dr-note">有效 {text(metrics.breadth?.valid_n)} · 缺失 {text(metrics.breadth?.missing_n)} · 上涨 <b className="rise">{text(metrics.breadth?.up)}</b> / 下跌 <b className="fall">{text(metrics.breadth?.down)}</b> / 平盘 {text(metrics.breadth?.flat)}。{note}</p></>;
}
function minuteIndex(slot: string) { const [h, m] = slot.split(':').map(Number); const n = h * 60 + m; return n <= 690 ? n - 571 : 120 + n - 781; }
export function IndexChart({ data }: { data: Day }) {
  const [cursor, setCursor] = useState(239);
  const colors = ['#ff4e5d', '#4da5ff', '#ffd23d'];
  const series = data.intraday.map(item => {
    const baseline = number(data.indices.find(i => i.ts_code === item.ts_code)?.record?.pre_close);
    return { ...item, segments: baseline && baseline > 0 ? item.segments.map(segment => segment.map(row => ({ x: minuteIndex(String(row.minute)), y: (Number(row.close) / baseline - 1) * 100, minute: String(row.minute) }))) : [] };
  });
  const points = series.flatMap(s => s.segments.flat());
  if (!points.length) return <Empty>所选日期尚无已保存的指数分时；上方仅显示已有指数资料，缺失分钟不补画。</Empty>;
  const bound = Math.max(.5, Math.ceil(Math.max(...points.map(p => Math.abs(p.y))) * 2) / 2);
  const x = (n: number) => 45 + n / 239 * 575;
  const y = (n: number) => 116 - n / bound * 85;
  return <><div className="dr-chart-legend">{series.map((s, i) => <span key={s.ts_code}><i style={{ background: colors[i] }}/>{s.name} <b className={tone(data.indices.find(n => n.ts_code === s.ts_code)?.record?.pct_chg)}>{pct(data.indices.find(n => n.ts_code === s.ts_code)?.record?.pct_chg)}</b></span>)}</div><svg className="dr-index-chart" viewBox="0 0 650 243" role="img" aria-label="三指数原生分钟走势，方向键查看分钟" tabIndex={0} onKeyDown={e => { const next = e.key === 'ArrowRight' ? cursor + 1 : e.key === 'ArrowLeft' ? cursor - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? 239 : null; if (next !== null) { e.preventDefault(); setCursor(Math.max(0, Math.min(239, next))); } }} onMouseMove={e => { const box = e.currentTarget.getBoundingClientRect(); setCursor(Math.max(0, Math.min(239, Math.round(((e.clientX - box.left) / box.width * 650 - 45) / 575 * 239)))); }}>
    {[-1, -.5, 0, .5, 1].map(r => <g key={r}><line x1="45" x2="620" y1={y(r * bound)} y2={y(r * bound)} className="grid"/><text x="36" y={y(r * bound) + 4} textAnchor="end">{(r * bound).toFixed(1)}%</text></g>)}{[0, 59, 119, 179, 239].map((n, i) => <g key={n}><line x1={x(n)} x2={x(n)} y1="30" y2="201" className="grid"/><text x={x(n)} y="225" textAnchor="middle">{['09:31', '10:30', '11:30/13:01', '14:00', '15:00'][i]}</text></g>)}<line x1={x(119.5)} x2={x(119.5)} y1="30" y2="201" className="dr-lunch"/>
    {series.map((s, i) => s.segments.map((segment, j) => <path key={`${i}-${j}`} d={segment.map((p, n) => `${n ? 'L' : 'M'}${x(p.x).toFixed(2)} ${y(p.y).toFixed(2)}`).join(' ')} fill="none" stroke={colors[i]} strokeWidth="2"/>))}<line x1={x(cursor)} x2={x(cursor)} y1="30" y2="201" className="dr-cursor"/>
  </svg><div className="dr-chart-readout" aria-live="polite">{series.map((s, i) => { const p = s.segments.flat().find(p => p.x === cursor); return <span key={i} style={{ color: colors[i] }}>{s.name} {p ? `${p.minute} ${pct(p.y)}` : '该分钟缺失'}</span>; })}</div><p className="dr-note">{series.map(s => `${s.name} ${s.selected_source === 'tencent_minutes' ? '腾讯' : s.selected_source === 'sina_minutes' ? '新浪' : '未载'} ${s.standard_point_count}/240`).join(' · ')}。午休断段，缺分钟不补。</p></>;
}
