import { useCallback, useEffect, useRef, useState } from 'react';
import { request } from '../../shared/api';
import type { ConnectionView, Envelope } from './api';
import { dateTime, Empty, ErrorNotice, Panel, reasonLabel, Status, useResource } from './shared';
import { useRecoveryRead } from '../../shared/RecoveryHealth';
import { checkedHealth, healthActions, healthLabels, healthState, recoveryDay } from '../../shared/dataRecovery';
import type { DataHealth, RecoveryItem, RecoveryStatus } from '../../shared/dataRecovery';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { monitorContext } from '../../shared/dataMonitorLink';
export type { RecoveryItem, RecoveryStatus } from '../../shared/dataRecovery';

const states: Record<string, { label: string; tone: string }> = {
  missing: { label: '缺口待核', tone: 'partial' }, partial: { label: '部分已存', tone: 'partial' },
  complete: { label: '已补齐', tone: 'complete' }, queued: { label: '恢复中 · 排队', tone: 'queued' },
  running: { label: '恢复中', tone: 'running' }, waiting: { label: '等待重试', tone: 'retry_wait' },
  blocked: { label: '恢复受阻', tone: 'error' }, unsupported: { label: '待源支持', tone: 'not_checked' },
};
const datasets: Record<string, string> = {
  'review.daily': '股票日线', 'review.indices': '指数日线', 'review.limits': '每日涨跌停价',
  'review.pools': '涨跌停与炸板日榜', 'review.themes': '概念板块日榜', 'review.members': '板块成员',
  'review.theme_funds': '概念板块资金日榜', 'review.industry_themes': '行业板块日榜',
  'review.industry_funds': '行业板块资金日榜', 'review.lhb': '龙虎榜', 'review.seats': '龙虎榜席位',
  'review.calendar_sse': '上交所交易日历', 'review.calendar_szse': '深交所交易日历',
  'minute.bars': '分钟行情', 'funds.minute_history': '板块资金分钟历史',
  'funds.scope': '资金历史采样范围',
  'breadth.minute_history': '涨跌停家数分钟历史', 'direction.daily_history': '板块前十日榜',
  'realtime.snapshots': '实时采样记录',
};
const reasons: Record<string, string> = {
  historical_window_expired: '已超出该来源支持的历史回补期限',
  local_history_window_expired: '已超出本版本支持的历史回补范围',
  calendar_coverage_missing: '交易日历尚不完整，补齐后继续核对对应日期',
  historical_scope_unverified: '该日的采样范围尚未保存，暂时无法核验分钟覆盖；数量保持未知',
  historical_window_unverified: '该日期的历史回补能力尚未验证',
  nominal_slots_missing: '部分常规交易时段的样本时点尚未保存，等待核验或回补',
  history_not_complete: '历史样本尚未齐备', empty_not_confirmed: '返回空表，尚未确认该日确实无数据',
  session_not_started: '尚未到本项数据的采样时段',
  snapshot_replay_not_verified: '历史快照回放能力尚未验证；现有样本仍保留，不代表数据全部丢失',
  emotion_snapshot_current_only: '后台自动续采，来源只返回最新值，已错过的时点无法回填',
  emotion_snapshot_slots_observed: '已检查的采样时段均有记录',
  snapshot_scope_not_verified: '采样时点已有记录，实际返回范围仍待核验',
  missing: '尚未找到该日期和范围的已存记录', partial: '已存记录仍有待核验部分',
  recovery_unavailable: '暂时无法恢复，等待后台重试', source_paused: '来源已暂停',
  source_route_not_selected: '此能力当前未选择该来源',
  upstream_redirect: '来源地址发生跳转，等待核验', unsafe_upstream_response: '来源返回未通过核验',
  endpoint_not_allowed: '来源地址尚未通过核验',
};
const scopes: Record<string, string> = {
  all: '全部请求范围', top10: '前 10 名', configured_source: '当前来源配置范围',
  source_universe: '来源统计范围（未独立核验）', unadjusted: '未复权',
  concept: '概念板块', industry: '行业板块', SH: '上交所', SZ: '深交所', BJ: '北交所', stock: '股票', index: '指数',
  SSE: '上交所', SZSE: '深交所', before_local_calendar_window: '本版本回补范围之前',
};
const scopeKeys: Record<string, string> = { scope: '范围', instrument: '标的', adjustment: '复权', category: '板块类别', code: '代码', exchange: '交易所', month: '月份', start_day: '起始日' };
const roles: Record<string, string> = { scheduler: '采集调度', realtime: '实时采集', recovery: '历史补漏', general: '通用任务', all: '综合执行' };
const groups = [
  { label: '缺口 / 待核', states: ['missing', 'partial'], tone: 'partial' },
  { label: '恢复中', states: ['queued', 'running'], tone: 'running' },
  { label: '等待重试', states: ['waiting'], tone: 'retry_wait' },
  { label: '已补齐', states: ['complete'], tone: 'complete' },
  { label: '待源支持', states: ['unsupported'], tone: 'not_checked' },
  { label: '恢复受阻', states: ['blocked'], tone: 'error' },
];
function scopeValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '待核';
  if (Array.isArray(value)) return value.map(scopeValue).join(' / ');
  if (typeof value === 'object') return Object.values(value).map(scopeValue).join(' · ');
  return scopes[String(value)] ?? String(value);
}
function scopeLabel(scope: Record<string, unknown>) {
  return Object.entries(scope).map(([key, value]) => `${scopeKeys[key] ?? key}：${scopeValue(value)}`).join('；') || '范围尚未提供';
}
function intervalLabel(value: string) {
  return value === 'day' ? '日频' : value === 'sample' ? '按配置采样' : /^\d+m$/.test(value) ? `${value.slice(0, -1)} 分钟` : value;
}
function sourceLabel(id: string, connections: ConnectionView[]) {
  for (const connection of connections) {
    const capability = connection.capabilities?.find(item => item.source_id === id);
    if (capability) return `${connection.name} · ${capability.name}`;
    if (connection.source_ids.includes(id)) return connection.name;
  }
  return `来源 ${id.slice(0, 8)}`;
}

const healthTones = { normal: 'normal', fallback: 'partial', recovering: 'running', attention: 'error' };
const stageNames: Record<string, string> = { source: '来源核验', queue: '任务队列', projection: '页面资料更新', history: '历史补漏', routing: '来源切换', recovery: '恢复核验', monitor: '运行状态核验' };
const capabilityNames: Record<string, string> = { stock_quote: '股票行情', index_quote: '指数行情', read_projection: '页面已存资料',
  workbench_projection: '工作台资料', collection_queue: '采集任务队列', historical_coverage: '历史资料覆盖' };
function capabilityName(item: DataHealth, connections: ConnectionView[]) {
  if (capabilityNames[item.capability]) return capabilityNames[item.capability];
  const minute = /^stock_minute_(\d+)m$/.exec(item.capability);
  if (minute) return `${minute[1]} 分钟股票行情`;
  if (item.primary_source_id) {
    const capability = connections.flatMap(connection => connection.capabilities ?? []).find(row => row.source_id === item.primary_source_id);
    if (capability) return capability.name;
  }
  return item.capability.startsWith('source:') ? '来源资料核验' : '数据运行核验';
}
const healthReasons: Record<string, string> = { no_valid_sample: '尚无有效采样', source_time_stale: '来源数据时点尚未推进',
    source_time_missing: '来源时点待核', source_day_mismatch: '来源资料日期与核验日期不一致',
    snapshot_incomplete: '当前样本尚不完整', projection_stale: '页面资料等待更新', queue_stalled: '任务队列等待恢复',
    primary_failed: '主来源本次未通过核验', primary_recovered: '主来源已恢复核验', fallback_active: '当前使用已配置备用来源',
    source_not_verified: '来源尚未通过核验', source_verification_outdated: '来源核验结果需要更新',
    current_day_not_observed: '该交易日尚无有效观测', source_stale: '来源数据时点已过期',
    quote_missing_or_stale: '行情缺失或时点已过期', minute_scope_missing_or_stale: '分钟数据范围缺失或时点已过期',
    minute_coverage_pending: '分钟资料覆盖仍待核验', depth_missing_or_time_unverified: '五档数据缺失或来源时点尚未核实',
    source_time_unverified: '来源时点尚未核实', coverage_partial: '资料覆盖尚不完整',
    market_scope_changed: '市场样本范围发生变化，等待重新核验', primary_recovery_check: '正在核验主来源恢复情况',
    collection_queue_overdue: '采集任务队列超过预期等待时间', projection_overdue: '页面资料更新超过预期等待时间',
    health_check_overdue: '运行状态未按时完成核验', local_check_failed: '该来源的本机检查异常，其他来源继续运行' };
function healthReason(item: DataHealth) {
  if (!item.reason) return item.state === 'normal' ? '本项已通过当前核验' : '后台按核验结果继续处理';
  const label = healthReasons[item.reason] ?? reasonLabel(item.reason);
  return label !== item.reason || /[\u3400-\u9fff]/.test(label) ? label : '后台已记录待核原因';
}
function HealthRecords({ data, day, busy, error, connections }: { data?: RecoveryStatus; day: string; busy: boolean; error: Error | null; connections: ConnectionView[] }) {
  const state = !error && !busy ? checkedHealth(data, day) : null;
  const label = error ? '状态待核' : busy && data ? '正在核对' : state ? healthLabels[state] : '尚未检查';
  const target = day || recoveryDay(data?.server_time);
  return <section className="dc-health-records" aria-label="数据运行与恢复">
    <div className="dc-health-summary"><strong>{day ? '所选日' : '今日'}运行状态{target && ` · ${target}`}</strong><Status value={state ? healthTones[state] : 'not_checked'} label={label}/></div>
    {data?.health?.length ? <details><summary>查看核验与恢复记录（{data.health.length} 项）</summary><div className="dc-table-wrap" role="region" aria-label="数据运行核验记录" tabIndex={0}><table className="dc-table dc-health-table"><thead><tr>{['资料日与能力', '状态与影响页面', '核验范围', '阶段与原因', '主来源与当前来源', '数据与核验时点', '后续安排'].map(value => <th scope="col" key={value}>{value}</th>)}</tr></thead><tbody>{data.health.map(item => {
      const dateValid = item.trade_day <= recoveryDay(data.server_time);
      const checked = dateValid ? healthState(item.state) : null, current = !error && !busy && dateValid;
      const diagnosticReason = item.reason && !healthReasons[item.reason] && reasonLabel(item.reason) === item.reason && !/[\u3400-\u9fff]/.test(item.reason);
      const diagnosticStage = item.stage && !stageNames[item.stage];
      return <tr key={item.id} data-health-id={item.id}><td><strong>{capabilityName(item, connections)}</strong><small className="dc-cell-note">{item.trade_day}{item.trade_day < recoveryDay(data.server_time) ? ' · 历史资料' : item.trade_day === recoveryDay(data.server_time) ? ' · 今日资料' : ' · 日期待核'}</small></td>
        <td><Status value={current && checked ? healthTones[checked] : 'not_checked'} label={checked ? current ? healthLabels[checked] : `上次：${healthLabels[checked]}` : '尚未检查'}/><small className="dc-cell-note">{item.impact_modules?.join(' / ') || '影响页面待核'}</small></td>
        <td>{scopeLabel(item.scope)}</td><td>{stageNames[item.stage] ?? '阶段待核'}<small className="dc-cell-note">{healthReason(item)}</small><small className="dc-cell-note">{item.missing_count == null ? '缺口数量待核' : `待核缺口 ${item.missing_count}`}</small>
          {(diagnosticReason || diagnosticStage) && <details className="dc-recovery-ranges"><summary>诊断信息</summary>{diagnosticReason && <p>原因代码：<code>{item.reason}</code></p>}{diagnosticStage && <p>阶段代码：<code>{item.stage}</code></p>}</details>}</td>
        <td>主来源：{item.primary_source_id ? sourceLabel(item.primary_source_id, connections) : '—'}<small className="dc-cell-note">当前来源：{item.active_source_id ? sourceLabel(item.active_source_id, connections) : '—'}</small></td>
        <td>来源时点 {dateTime(item.last_source_time)}<small className="dc-cell-note">有效资料接收 {dateTime(item.last_valid_at)}</small><small className="dc-cell-note">核验于 {dateTime(item.updated_at)}</small></td>
        <td>{item.next_action ? healthActions[item.next_action] ?? '等待后台进一步核验' : '暂无后续安排'}<small className="dc-cell-note">{item.next_action_at ? `计划于 ${dateTime(item.next_action_at)}` : '后续时间待定'}</small></td></tr>;
    })}</tbody></table></div></details> : <p className="dc-panel-note">此日尚无运行核验记录，暂无记录不代表正常。</p>}
  </section>;
}

function Samples({ item }: { item: RecoveryItem }) {
  return <>
    <span>{item.actual_count === null ? '已存数量待核' : `已存 ${item.actual_count.toLocaleString('zh-CN')}`}</span>
    <small className="dc-cell-note">{item.expected_count === null ? '基准总量待核' : `核验基准 ${item.expected_count.toLocaleString('zh-CN')} 个${item.interval === 'day' ? '记录' : '时点'}`}</small>
    {item.missing_ranges.length ? <details className="dc-recovery-ranges"><summary>待核 {item.missing_ranges.reduce((total, range) => total + range.count, 0).toLocaleString('zh-CN')} 个时点 · {item.missing_ranges.length} 段</summary><ul>{item.missing_ranges.map((range, index) => <li key={`${range.start}-${index}`} title={`${dateTime(range.start)} — ${dateTime(range.end)}`}>{dateTime(range.start, true)}{range.end !== range.start && ` — ${dateTime(range.end, true)}`} · {range.count} 个</li>)}</ul></details>
      : <small className="dc-cell-note">{item.state === 'complete' ? '本项归档核验通过' : '未列出待核时段'}</small>}
  </>;
}

export function RecoveryPanel({ connections, refreshVersion }: { connections: ConnectionView[]; refreshVersion: number }) {
  const navigation = useNavigation();
  const focus = monitorContext(navigation.search);
  const day = focus.tradeDay, sourceId = focus.sourceId;
  const scope = JSON.stringify([day, sourceId, focus.error]);
  const [page, setPage] = useState({ scope, offset: 0 });
  const offset = page.scope === scope ? page.offset : 0;
  useEffect(() => { setPage(previous => previous.scope === scope ? previous : { scope, offset: 0 }); }, [scope]);
  function setOffset(value: number | ((previous: number) => number)) { setPage({ scope, offset: typeof value === 'function' ? value(offset) : value }); }
  const shared = useRecoveryRead();
  const separate = !!day || !!sourceId || !!focus.error || !!offset || !shared;
  const load = useCallback(async () => {
    if (focus.error) return null;
    const query = new URLSearchParams({ limit: '100' });
    if (day) query.set('day', day);
    if (sourceId) query.set('source_id', sourceId);
    if (offset) query.set('offset', String(offset));
    const response = separate ? await request<Envelope<RecoveryStatus>>('/api/v1/data-recovery?' + query) : null;
    if (response && (response.data.items.some(item => sourceId && item.source_id !== sourceId || day && item.trade_day !== day)
      || response.data.health?.some(item => sourceId && item.primary_source_id !== sourceId && item.active_source_id !== sourceId || day && item.trade_day !== day))) {
      throw new Error('返回的来源或资料日期与当前定位不一致，未作为所选范围显示。请重新读取或检查本机程序版本。');
    }
    return response;
  }, [day, sourceId, focus.error, offset, separate]);
  const filtered = useResource(load);
  const read = separate ? filtered : shared!;
  const data = focus.error ? undefined : read.data?.data;
  const previousRefresh = useRef(refreshVersion);
  useEffect(() => {
    // Default monitoring shares the header's 30-second read. Only an explicit
    // date or another page needs a separate filtered request.
    if (previousRefresh.current !== refreshVersion) { previousRefresh.current = refreshVersion; if (separate) read.reload(); }
  }, [refreshVersion, read.reload, separate]);
  function selectDay(value: string) {
    const url = new URL(location.href);
    if (value) url.searchParams.set('trade_day', value); else url.searchParams.delete('trade_day');
    updateLocation(url);
    if (!value && !sourceId) shared?.reload();
  }
  const items = data?.items ?? [];
  const counts = data?.counts ?? {};
  const workers = data?.workers ?? [];
  const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
  return <section id="data-recovery" tabIndex={-1}><Panel title="数据完整性与补漏" className="dc-recovery-panel" action={<div className="dc-date-filters dc-recovery-filters"><label>数据日期<input aria-label="补漏数据日期" type="date" value={focus.error && !/^\d{4}-\d{2}-\d{2}$/.test(day) ? '' : day} onChange={event => selectDay(event.target.value)}/></label><button type="button" onClick={() => selectDay('')} disabled={!day}>全部日期</button><button type="button" onClick={read.reload} disabled={read.busy || !!focus.error}>{read.busy ? '读取中…' : '刷新补漏状态'}</button></div>}>
    {focus.error && <p className="dc-alert dc-alert-error" role="alert">{focus.error}</p>}
    {read.error !== null && <><ErrorNotice error={read.error} retry={read.reload}/>{data && <p className="dc-panel-note" role="status">本次读取失败，保留上次结果（{dateTime(data.server_time)}）。</p>}</>}
    {!focus.error && <HealthRecords data={data} day={day} busy={read.busy} error={read.error} connections={connections}/>}
    {data && <div className="dc-recovery-summary" aria-label="当前筛选的补漏统计">{groups.map(group => <Status key={group.label} value={group.tone} label={`${group.label} ${group.states.reduce((sum, state) => sum + (counts[state] ?? 0), 0)}`}/>)}<small>当前筛选共 {total} 项</small></div>}
    <div className="dc-table-wrap" role="region" aria-label="数据完整性与补漏记录" tabIndex={0}><table className="dc-table dc-recovery-table"><thead><tr>{['数据日期', '来源与数据集', '核验范围', '状态', '已存与待核', '原因与重试'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>{items.map(item => {
      const state = states[item.state] ?? { label: '状态待核', tone: 'not_checked' };
      return <tr key={item.id} className={item.state === 'blocked' ? 'dc-row-warning' : undefined}><td>{item.trade_day}<small className="dc-cell-note">{intervalLabel(item.interval)}</small></td><td><strong>{datasets[item.dataset] ?? item.dataset}</strong><small className="dc-cell-note" title={item.source_id}>{sourceLabel(item.source_id, connections)}</small></td><td>{scopeLabel(item.scope)}</td><td><Status value={state.tone} label={state.label}/></td><td><Samples item={item}/></td><td>{item.reason ? reasons[item.reason] ?? reasonLabel(item.reason) : item.state === 'complete' ? '当前日期和范围的归档已通过核验' : '后台按当前状态继续处理'}<small className="dc-cell-note">核验于 {dateTime(item.checked_at)}</small><small className="dc-cell-note">{item.next_retry_at ? `下次检查 / 重试 ${dateTime(item.next_retry_at)}` : '暂未安排重试'}</small>{item.job_id && <small className="dc-cell-note" title={item.job_id}>已关联后台任务</small>}</td></tr>;
    })}</tbody></table>
      {!data && read.busy ? <div className="dc-loading" role="status">正在读取数据完整性记录…</div> : data && !items.length && <Empty title="当前日期范围暂无核验记录">后台完成核验后会在这里显示；暂无记录不代表数据已经完整。</Empty>}
    </div>
    {data && (offset > 0 || data.has_more) && <div className="dc-pagination"><span>{items.length ? `第 ${offset + 1}—${offset + items.length} 项` : '当前页暂无记录'}{data.has_more ? ' · 后面还有记录' : ''}</span><button type="button" onClick={() => setOffset(value => Math.max(0, value - 100))} disabled={!offset || read.busy}>上一页</button><button type="button" onClick={() => setOffset(value => value + 100)} disabled={!data.has_more || read.busy}>下一页</button></div>}
    {!!workers.length && <details className="dc-recovery-workers"><summary>后台执行记录（{workers.length}）</summary><div className="dc-table-wrap" role="region" aria-label="补漏后台执行记录" tabIndex={0}><table className="dc-table"><thead><tr>{['分工', '最近联系', '最近进展', '任务'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>{workers.map((worker, index) => <tr key={`${worker.role}-${index}`}><td>{roles[worker.role] ?? worker.role}</td><td>{dateTime(worker.seen_at)}</td><td>{dateTime(worker.progress_at)}</td><td title={worker.job_id ?? undefined}>{worker.job_id ? '已报告任务' : '未报告任务'}</td></tr>)}</tbody></table></div><p className="dc-panel-note">最近联系与实际进展分别记录，联系正常不代表补漏已经完成。</p></details>}
    <p className="dc-panel-note">数量仅对应列明的日期、来源和范围；分钟时点齐备不等于成交完整。每 30 秒读取已存状态，补采由后台独立执行。状态读取于 {dateTime(data?.server_time)}（北京时间）。</p>
  </Panel></section>;
}
