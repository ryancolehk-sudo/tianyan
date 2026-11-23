import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../shared/icons';
import type { IconName } from '../../shared/icons';
import { dataCenterApi } from './api';
import { RecoveryPanel } from './RecoveryPanel';
import type { CallFilters, CallRecord, ConnectionView, Issue, Overview } from './api';
import { dateTime, duration, Empty, ErrorNotice, kindNames, Panel, reasonLabel, Status, Toggle, useResource } from './shared';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { monitorContext } from '../../shared/dataMonitorLink';

function safeDetails(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(safeDetails);
  if (value !== null && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, /api.?key|authorization|password|secret|(?:access|refresh|id).?token|code.?verifier|pkce.?verifier|(?:auth|oauth).?code|(?:^|[_-])token(?:$|[_-])/i.test(key) ? '已隐藏' : safeDetails(entry)]));
  return value;
}

function DetailsDrawer({ call, issue, close, configure }: { call?: CallRecord; issue?: Issue; close: () => void; configure: (id: string | null) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current; node?.showModal(); return () => node?.close(); }, []);
  return <dialog className="dc-drawer" ref={dialog} aria-labelledby="dc-detail-title" onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div className="dc-drawer-content"><div className="dc-drawer-head"><h2 id="dc-detail-title">{call ? '调用详情' : '异常详情'}</h2><button type="button" onClick={close} aria-label="关闭详情"><Icon name="close"/></button></div>
      {call ? <><h3>{call.task}</h3><Status value={call.status}/><dl className="dc-detail-grid"><dt>服务</dt><dd>{call.service_name}</dd><dt>开始时间</dt><dd>{dateTime(call.started_at)}</dd><dt>完成时间</dt><dd>{dateTime(call.completed_at)}</dd><dt>耗时</dt><dd>{duration(call.elapsed_ms)}</dd><dt>实际请求</dt><dd>{call.request_count} 次</dd><dt>输入 tokens</dt><dd>{call.input_tokens ?? '未知'}</dd><dt>输出 tokens</dt><dd>{call.output_tokens ?? '未知'}</dd><dt>费用</dt><dd>{call.cost_cny == null ? '未知（未返回用量或未配置价格）' : `¥ ${call.cost_cny}`}</dd><dt>错误原因</dt><dd>{call.error_code ?? '—'}</dd><dt>计划重试</dt><dd>{dateTime(call.retry_at)}</dd><dt>业务任务</dt><dd>{call.job_id ?? '—'}</dd><dt>调用编号</dt><dd>{call.id}</dd></dl><h3>脱敏记录</h3><pre className="dc-json">{JSON.stringify(safeDetails(call.details), null, 2)}</pre></>
        : issue && <><h3>{issue.title}</h3><p>{reasonLabel(issue.message)}</p><dl className="dc-detail-grid"><dt>业务任务</dt><dd>{issue.job_id ?? '—'}</dd><dt>计划重试</dt><dd>{dateTime(issue.retry_at)}</dd></dl><button type="button" className="dc-button-gold" onClick={() => { close(); configure(issue.connection_id); }}>查看接入配置</button></>}
      <p className="dc-muted">以上时间为北京时间。请求用量与费用只展示已记录的实际值。</p>
    </div>
  </dialog>;
}

function CallsList({ filters, refreshVersion }: { filters: CallFilters; refreshVersion: number }) {
  const loader = useCallback(() => dataCenterApi.calls(filters), [filters]);
  const read = useResource(loader);
  const [more, setMore] = useState<CallRecord[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null | undefined>(undefined);
  const [moreBusy, setMoreBusy] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [selected, setSelected] = useState<CallRecord | null>(null);
  const mounted = useRef(true);
  const paginationLock = useRef(false);
  const firstRefresh = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (firstRefresh.current) { firstRefresh.current = false; return; } read.reload(); }, [refreshVersion, read.reload]);
  const firstItems = read.data?.data.items ?? [];
  const seen = new Set(firstItems.map(item => item.id));
  const items = [...firstItems, ...more.filter(item => !seen.has(item.id))];
  const cursor = nextCursor === undefined ? read.data?.data.next_cursor : nextCursor;
  async function loadMore() {
    if (!cursor || paginationLock.current) return;
    paginationLock.current = true; setMoreBusy(true); setMoreError(null);
    try { const result = await dataCenterApi.calls({ ...filters, cursor }); if (mounted.current) { setMore(previous => [...previous, ...result.data.items]); setNextCursor(result.data.next_cursor); } }
    catch (reason) { if (mounted.current) setMoreError(reason); }
    finally { paginationLock.current = false; if (mounted.current) setMoreBusy(false); }
  }
  return <>
    {read.error !== null && <ErrorNotice error={read.error} retry={read.reload}/>}
    <div className="dc-table-wrap" role="region" aria-label="调用记录" tabIndex={0}><table className="dc-table dc-calls-table"><thead><tr>{['时间', '类型', '任务', '服务', '结果', '耗时', '详情'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>{items.map(item => <tr key={item.id} className={['error', 'failed', 'rate_limited', 'unknown'].includes(item.status) ? 'dc-row-warning' : undefined}><td title={dateTime(item.started_at)}>{dateTime(item.started_at, true)}<small className="dc-cell-date">{new Date(item.started_at).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' })}</small></td><td><span className="dc-tag">{item.kind === 'ai' ? 'AI' : '数据'}</span></td><td>{item.task}</td><td>{item.service_name}</td><td><Status value={item.status}/></td><td>{duration(item.elapsed_ms)}</td><td><button type="button" className="dc-link" onClick={() => setSelected(item)}>查看</button></td></tr>)}</tbody></table>
      {!read.data && read.busy ? <div className="dc-loading" role="status">正在读取实际调用记录…</div> : !items.length && !read.error && <Empty title="暂无符合条件的调用记录">保存配置、读取页面和缓存均不计为模型调用。</Empty>}
    </div>
    {moreError !== null && <ErrorNotice error={moreError} retry={() => { void loadMore(); }}/>}
    {cursor && <div className="dc-pagination"><button type="button" disabled={moreBusy} onClick={() => { void loadMore(); }}>{moreBusy ? '正在读取…' : '加载更早记录'}</button></div>}
    {selected && <DetailsDrawer call={selected} close={() => setSelected(null)} configure={() => undefined}/>}
  </>;
}

function CallLog({ connections, refreshVersion, updatedAt, sourceId = '' }: { connections: ConnectionView[]; refreshVersion: number; updatedAt?: string; sourceId?: string }) {
  const [kind, setKind] = useState('');
  const related = connections.find(item => item.source_ids.includes(sourceId));
  const [connection, setConnection] = useState(related?.id ?? '');
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [searchOpen, setSearchOpen] = useState(false);
  const filters = useRef<CallFilters>({});
  const filterKey = JSON.stringify({ kind, connection, errorsOnly, from, to });
  const previousFilterKey = useRef('');
  if (previousFilterKey.current !== filterKey) {
    previousFilterKey.current = filterKey;
    filters.current = { ...(kind ? { kind: kind as 'ai' | 'data' } : {}), ...(connection ? { connection_id: connection } : {}), errors_only: errorsOnly, ...(from ? { from_date: from } : {}), ...(to ? { to_date: to } : {}) };
  }
  const invalidDates = !!from && !!to && from > to;
  return <Panel title="调用记录" className="dc-call-panel" action={<div className="dc-call-filters"><select aria-label="调用类型" value={kind} onChange={event => setKind(event.target.value)}><option value="">全部类型</option><option value="ai">AI 模型</option><option value="data">行情 / 资讯</option></select><select aria-label="调用服务" value={connection} onChange={event => setConnection(event.target.value)}><option value="">全部服务</option>{connections.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><label className="dc-switch-label"><Toggle label="只看异常调用" checked={errorsOnly} onChange={() => setErrorsOnly(!errorsOnly)}/><span>只看异常</span></label><button type="button" aria-label="筛选调用时间" aria-expanded={searchOpen} onClick={() => setSearchOpen(!searchOpen)}><Icon name="search"/></button></div>}>
    {searchOpen && <div className="dc-date-filters"><label>开始日期<input type="date" value={from} max={to || undefined} onChange={event => setFrom(event.target.value)}/></label><span>至</span><label>结束日期<input type="date" value={to} min={from || undefined} onChange={event => setTo(event.target.value)}/></label><button type="button" onClick={() => { setFrom(''); setTo(''); }}>清除日期</button><small>北京时间</small></div>}
    {invalidDates ? <ErrorNotice error={new Error('开始日期不能晚于结束日期。')}/> : <CallsList key={filterKey} filters={filters.current} refreshVersion={refreshVersion}/>}
    <p className="dc-panel-note">状态更新于 {dateTime(updatedAt)} · 调用时间与资料日期分别记录。{sourceId ? related ? '已定位关联服务；该服务可能包含其他数据能力。' : '关联服务尚未核实，调用列表未按来源筛选。' : '记录来自本机实际请求。'}</p>
  </Panel>;
}

export function Monitor({ overview, connections, loading, error, refresh, configure, refreshVersion }: {
  overview: Overview | undefined; connections: ConnectionView[]; loading: boolean; error: unknown;
  refresh: () => void; configure: (id: string | null) => void; refreshVersion: number;
}) {
  const navigation = useNavigation();
  const focus = monitorContext(navigation.search);
  const source = connections.find(item => item.source_ids.includes(focus.sourceId));
  const capability = source?.capabilities?.find(item => item.source_id === focus.sourceId);
  function clearFocus() {
    const url = new URL(location.href);
    for (const key of ['source_id', 'trade_day', 'as_of_time', 'monitor_context']) url.searchParams.delete(key);
    url.hash = '';
    updateLocation(url);
  }
  useEffect(() => {
    if (navigation.action !== 'query' && navigation.hash === '#data-recovery') {
      const target = document.getElementById('data-recovery');
      target?.focus({ preventScroll: true }); target?.scrollIntoView({ block: 'start' });
    }
  }, [navigation.search, navigation.hash]);
  const [issue, setIssue] = useState<Issue | null>(null);
  const metrics: { label: string; value: number | undefined; unit: string; note: string; icon: IconName; tone: string }[] = [
    { label: '接入服务', value: overview?.services_count, unit: '个', note: '模型与数据来源', icon: 'data', tone: 'gold' },
    { label: '运行任务', value: overview?.running_tasks, unit: '个', note: overview?.tasks.find(item => ['running', 'queued', 'started'].includes(item.state))?.stage || '当前执行与排队任务', icon: 'system', tone: 'blue' },
    { label: '今日调用', value: overview?.today_requests, unit: '次', note: '模型与数据实际请求', icon: 'news', tone: 'green' },
    { label: '待处理', value: overview?.pending_issues, unit: '项', note: overview?.issues[0]?.title ?? '当前待处理异常', icon: 'shield', tone: 'gold' },
  ];
  return <div className="dc-monitor">
    <section className="dc-monitor-context" aria-label="监控定位">
      <div><strong>{focus.history ? '历史资料定位' : '当前运行定位'}</strong><span>{focus.sourceId ? `${source?.name ?? '来源名称待核'}${capability ? ' · ' + capability.name : ''}` : '全部来源'}{focus.tradeDay ? ` · 资料日 ${focus.tradeDay}` : ' · 当前核验'}</span>
        {focus.sourceId && <details><summary>来源编号</summary><code>{focus.sourceId}</code></details>}
        {focus.asOfTime && <span>原页资料截止：{dateTime(focus.asOfTime)}</span>}
        <small>{focus.history ? '核验记录按所选来源与资料日读取；下方服务和任务反映当前运行，不重建当时的运行状态。' : '这里只读取已存核验记录；打开或刷新不启动采集。'}</small>
      </div><div className="dc-monitor-context-actions">{focus.returnTo && <a href={focus.returnTo}>返回原页面</a>}{(focus.sourceId || focus.tradeDay || focus.asOfTime || focus.error) && <button type="button" onClick={clearFocus}>清除定位</button>}</div>
    </section>
    <p className="dc-panel-note dc-current-runtime-label">当前运行 · 服务、任务与调用状态独立于历史资料日</p>
    {error !== null && <ErrorNotice error={error} retry={refresh}/>}
    <div className="dc-metrics">{metrics.map(item => <article className={'dc-metric dc-metric-' + item.tone} key={item.label}><span className="dc-metric-icon"><Icon name={item.icon}/></span><div><h3>{item.label}</h3><strong>{item.value ?? '—'} <small>{item.unit}</small></strong><p>{item.note}</p></div></article>)}</div>
    <div className="dc-monitor-top"><Panel title="服务运行状态" className="dc-service-status"><div className="dc-table-wrap" role="region" aria-label="服务运行状态" tabIndex={0}><table className="dc-table"><thead><tr>{['服务', '当前状态', '最新数据', '最近成功', '影响模块'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>{overview?.services.map(service => <tr key={service.id} className={['rate_limited', 'error', 'failed'].includes(service.status) ? 'dc-row-warning' : undefined}><td><strong>{service.name}</strong><small className="dc-cell-note">{kindNames[service.kind]}</small></td><td><Status value={service.status}/>{service.error_code && <small className="dc-cell-note" title={service.error_code}>{reasonLabel(service.error_code)}</small>}</td><td title={dateTime(service.latest_data_at)}>{dateTime(service.latest_data_at, true)}</td><td title={dateTime(service.last_success_at)}>{dateTime(service.last_success_at, true)}<small className="dc-cell-note">最近尝试 {dateTime(service.last_attempt_at, true)}</small></td><td>{service.impact_modules.join(' / ') || '—'}</td></tr>)}</tbody></table>{!overview && loading ? <div role="status" className="dc-loading">正在读取服务实际状态…</div> : overview && !overview.services.length && <Empty title="还没有接入服务">在接入管理中添加模型或数据来源。</Empty>}</div><p className="dc-panel-note">数据本身的时间与实际请求时间分别记录。状态反映最近结果。</p></Panel>
      <Panel title="当前任务与异常" className="dc-current-tasks"><div className="dc-issue-list">{overview?.issues.map(item => <article className="dc-issue" key={item.id}><Icon name="shield"/><div><h3>{item.title}</h3><p>{reasonLabel(item.message)}</p>{item.retry_at && <small>计划重试：{dateTime(item.retry_at)}</small>}</div><div className="dc-issue-actions"><button type="button" className="dc-button-gold" onClick={() => setIssue(item)}>查看原因</button><button type="button" onClick={() => configure(item.connection_id)}>接入配置</button></div></article>)}{overview && !overview.issues.length && <div className="dc-no-issues"><Status value="success" label="当前没有待处理异常"/></div>}</div>
        <div className="dc-task-list">{overview?.tasks.map(task => <article className="dc-task" key={task.id}><span className={'dc-task-mark' + (['running', 'started', 'generating'].includes(task.state) ? ' is-running' : '')}><Icon name="system"/></span><div><h3>{task.service_name || task.feature || '后台任务'} · <Status value={task.state}/></h3><p>当前阶段：{task.stage || '等待执行'}</p>{task.error_code && <small className="dc-bad">{task.error_code}</small>}{['running', 'started', 'generating'].includes(task.state) && <div className="dc-progress" role="progressbar" aria-label="任务执行中"><span/></div>}<small>开始于 {dateTime(task.started_at || task.created_at)}</small></div></article>)}{overview && !overview.tasks.length && <div className="dc-idle-task"><Icon name="system"/><span>当前没有运行中的任务<small>任务执行时展示实际阶段，不估算完成比例。</small></span></div>}</div>
      </Panel></div>
    <RecoveryPanel connections={connections} refreshVersion={refreshVersion}/>
    <CallLog key={focus.sourceId + ':' + source?.id} connections={connections} sourceId={focus.error ? '' : focus.sourceId} refreshVersion={refreshVersion} updatedAt={overview?.updated_at}/>
    {issue && <DetailsDrawer issue={issue} close={() => setIssue(null)} configure={configure}/>}
  </div>;
}
