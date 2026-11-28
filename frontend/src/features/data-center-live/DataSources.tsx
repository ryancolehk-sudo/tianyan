import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { Icon } from '../../shared/icons';
import { ApiError, request } from '../../shared/api';
import { collectionParams, goToDataTopic } from '../../shared/dataNavigation';
import { useNavigation } from '../../shared/navigation';
import { CollectionTools } from '../data/CollectionTools';
import { connectionInput, dataCenterApi } from './api';
import type { AiSettings, ConnectionIndex, ConnectionView, Envelope } from './api';
import { capabilityApi } from './capabilityApi';
import type { CapabilityMatrix, CapabilityOption, PortResult, PortScope, RouteCapability, RouteChange, Supplier } from './capabilityApi';
import { dateTime, duration, ErrorNotice, Panel, reasonLabel, Status, statusLabel, useResource } from './shared';
import { FullMinuteJob } from './FullMinuteJob';
import { FunctionCoverage } from './FunctionCoverage';
import type { Archive } from './featureCoverage';

const readAnomalyArchive = () => request<Envelope<Archive | null>>('/api/v1/review-data/anomaly');

const permission = { verified: '已开通', locked: '需解锁', unknown: '尚无权限检测结果' };
const coreProviders = ['fuyao', 'tdx_data', 'tushare', 'tickflow'];
function coreOrder(provider: Supplier) { return coreProviders.indexOf(coreProviders.includes(provider.id) ? provider.id : provider.template); }
function usable(option: CapabilityOption) { return option.implemented && option.entitlement === 'verified' && !['disabled', 'paused', 'not_configured', 'points_not_enabled', 'permission_missing', 'credentials_missing', 'unavailable', 'error', 'failed', 'empty', 'time_unverified'].includes(option.state); }
function orderedSources(row: RouteCapability) { return row.current_provider_id ? [row.current_provider_id, ...(row.fallback_provider_ids ?? [])] : []; }
const portHelp: Record<string, string> = {
  verification_queued: '已安排免费检测。取得有效数据后会自动加入来源列表，已有首选继续优先。',
  not_adapted: '此接口尚未接入系统，当前可使用其他来源。',
  credentials_missing: '先在连接设置填写此来源的 Key。',
  not_configured: '在来源的采集参数中登记范围后，可检测此端口。',
  disabled: '采集已关闭，可在此来源的采集参数中启用。',
  paused: '此端口已暂停。请在采集参数中核对暂停原因并恢复。',
  connection_paused: '此来源已暂停，可在连接设置中恢复采集。',
  permission_missing: '当前账号缺少此接口权限，开通后再检测。',
  points_consent: '通达信按积分计费，请单独授权并检测此端口。',
  points_not_enabled: '此端口按积分计费，请先允许本次检测消耗积分。',
  time_unverified: '返回的报价缺少可核验的行情时点，供应商补齐后可重新检测。',
  empty: '接口未返回数据，请核对标的和采集时段后检测。',
  unavailable: '来源暂不可用，请检查连接或选用其他来源。',
  error: '最近取数失败，请检查连接设置后检测。',
  failed: '最近检测失败，请核对配置后重新检测。',
  not_checked: '此端口还没有通过单项取数检测。',
};
function optionBlocker(option: CapabilityOption, supplier?: Supplier) {
  if (!option.implemented) return 'not_adapted';
  if (supplier?.credential_required && !supplier.credential_configured) return 'credentials_missing';
  if (supplier?.id === 'tdx_data' && !supplier.enabled && option.billing === 'points' && option.entitlement !== 'verified' && option.entitlement !== 'locked') return 'points_consent';
  if (option.entitlement === 'locked') return 'permission_missing';
  return usable(option) ? null : option.state in portHelp ? option.state : 'not_checked';
}
function safeReference(value: string) { try { return new URL(value).protocol === 'https:'; } catch { return false; } }
function optionState(option: CapabilityOption, compact = false) {
  if (!option.implemented) return '待接入';
  if (option.entitlement === 'locked') return '需解锁';
  if (option.state === 'disabled') return '采集已关闭';
  if (option.state === 'paused') return '采集已暂停';
  if (option.state === 'points_not_enabled') return '待允许积分';
  if (['error', 'failed'].includes(option.state)) return '检测失败';
  if (option.state === 'empty') return '未返回数据';
  if (option.state === 'unavailable') return '暂不可用';
  if (option.state === 'credentials_missing') return '待填写 Key';
  if (option.state === 'not_configured') return '待配置';
  if (option.state === 'time_unverified') return compact ? '时点待核' : '行情时点待核对';
  if (option.state === 'permission_missing') return '接口权限不足';
  if (option.entitlement !== 'verified') return option.reason ? reasonLabel(option.reason) : '尚无权限检测结果';
  if (option.state !== 'ready') {
    const label = statusLabel(option.state);
    return label === option.state ? (option.reason ? reasonLabel(option.reason) : '待核验') : label;
  }
  if (option.selected) return '当前使用';
  return '可替换';
}
function Billing({ option }: { option: CapabilityOption }) {
  return <span className={'dc-billing dc-billing-' + option.billing}>{option.billing_label}</span>;
}
function storedFamily(option: CapabilityOption) {
  const families: Record<string, string> = { 'tdx_data:depth5': 'tdx_depth5', 'fuyao:adjustment': 'fuyao_adj_factor', 'fuyao:financial': 'fuyao_financial' };
  return option.implemented ? families[option.provider_id + ':' + option.capability_id] : undefined;
}
function OptionEvidence({ option }: { option: CapabilityOption }) {
  const family = storedFamily(option);
  return <><p>{option.scope}</p><p className="dc-option-evidence">{option.billing_note || '费用以供应商实际权益为准。'}{safeReference(option.reference_url) && <> · <a href={option.reference_url} target="_blank" rel="noopener noreferrer">官方费用依据 ↗</a></>}<span>核验：{option.checked_at ? dateTime(option.checked_at) : '尚未核验'}</span></p><p className="dc-option-evidence">{option.last_success_at ? `最近成功：${dateTime(option.last_success_at)}` : '尚无成功取数记录'}{option.reason && <> · {reasonLabel(option.reason)}</>}{family && <span><a aria-label={option.name + '查看已存数据'} href={'/data?view=datasets&family=' + encodeURIComponent(family)}>查看已存数据 →</a></span>}</p></>;
}

function SupplierCard({ item, selected, admin, busy, configuring, canApply, select, choose, apply, auto }: {
  item: Supplier; selected: boolean; admin: boolean; busy: boolean; canApply: boolean;
  configuring: boolean; select: () => void; choose: (option: CapabilityOption) => void; apply: () => void; auto: () => void;
}) {
  const options = [...item.capabilities].sort((a, b) => Number(b.selected) - Number(a.selected));
  const quoteReceived = item.template === 'tdx_data' && options.some(o => o.capability_id === 'stock_quote' && o.entitlement === 'verified' && o.last_success_at && o.state === 'time_unverified');
  const chip = (option: CapabilityOption) => {
    const locked = option.entitlement === 'locked' || option.provider_id === 'tickflow' && option.billing === 'subscription' && option.entitlement !== 'verified';
    const ready = option.implemented && option.entitlement === 'verified' && option.state === 'ready';
    return <button type="button" key={option.capability_id} className={'dc-capability-chip' + (option.selected ? ' serving' : '') + (!option.implemented ? ' unimplemented' : '')} aria-label={`${item.name} ${option.name} ${option.billing_label} ${optionState(option)}`} onClick={() => choose(option)}><span>{locked ? '🔒 ' : option.selected ? '✓ ' : ''}{option.name}</span>{!locked && <Billing option={option}/>}
      {!ready && <small className="dc-capability-state">{optionState(option, true)}</small>}</button>;
  };
  return <article className={'dc-supplier-card' + (selected ? ' selected' : '')}><div className="dc-supplier-card-heading"><h3><button type="button" aria-expanded={selected} aria-controls="dc-supplier-detail" onClick={select}><i className={item.enabled ? 'is-on' : ''}/>{item.name}</button></h3>
    <button type="button" className="dc-configure-supplier" title={'配置' + item.name + '的端口'} disabled={!admin || busy || !options.length} onClick={auto}>{configuring ? '配置中…' : '一键配置'}</button>
    <span className="dc-tag">{item.connection_id ? item.credential_required && !item.credential_configured ? '待填 Key' : item.enabled ? '已启用' : quoteReceived ? '报价已收到 · 自动采集未启用' : item.credential_configured ? 'Key 已存 · 未启用' : '已配置' : '未配置'}</span></div><details className="dc-more-capabilities"><summary>数据项与费用（{options.length}）<Icon name="chevron"/></summary><div className="dc-capability-chips">{options.map(chip)}</div></details><div className="dc-supplier-card-footer"><span>已选用于 <strong>{item.serving_count}</strong> 项数据</span><div><button type="button" onClick={select}>连接设置</button><button type="button" className="dc-link" disabled={!admin || busy || !canApply} onClick={apply}>分配数据项</button></div></div></article>;
}

function SourceSelection({ matrix, provider, capability, busy, error, close, apply, manage, editable }: {
  matrix: CapabilityMatrix; provider?: Supplier; capability?: RouteCapability; busy: boolean; error: unknown; editable: boolean;
  close: () => void; apply: (changes: RouteChange[]) => void; manage: (option: CapabilityOption) => void;
}) {
  const ref = useRef<HTMLElement>(null);
  const rows = capability ? [capability] : matrix.capabilities.filter(row => row.routable !== false && row.options.some(opt => opt.provider_id === provider?.id));
  const [drafts, setDrafts] = useState<Record<string, string[]>>(() => Object.fromEntries(rows.map(row => {
    if (!provider) return [row.id, orderedSources(row)];
    const opt = row.options.find(item => item.provider_id === provider.id)!;
    return [row.id, usable(opt) && opt.billing !== 'points' && row.current_provider_id !== provider.id ? [provider.id] : []];
  })));
  const [points, setPoints] = useState<Record<string, boolean>>(() => Object.fromEntries(rows.map(row => [
    row.id, row.options.some(opt => opt.billing === 'points' && orderedSources(row).includes(opt.provider_id)),
  ])));
  useEffect(() => { ref.current?.focus(); }, []);
  useEffect(() => {
    const cancel = (event: KeyboardEvent) => { if (event.key === 'Escape' && !busy) close(); };
    window.addEventListener('keydown', cancel);
    return () => window.removeEventListener('keydown', cancel);
  }, [busy, close]);
  function toggle(id: string, key: string, checked: boolean) {
    setDrafts(old => ({ ...old, [id]: checked ? [...old[id], key] : old[id].filter(value => value !== key) }));
  }
  const changes: RouteChange[] = [];
  let needsPoints = false;
  for (const row of rows) {
    const draft = drafts[row.id] ?? [];
    if (!draft.length) continue;
    const previous = orderedSources(row);
    const order = provider ? [...draft, ...previous.filter(key => !draft.includes(key))] : draft;
    if (order.length === previous.length && order.every((key, index) => key === previous[index])) continue;
    const paid = row.options.some(opt => opt.billing === 'points' && order.includes(opt.provider_id));
    if (paid && !points[row.id]) needsPoints = true;
    changes.push({ capability_id: row.id, provider_id: order[0], ...(order.length > 1 ? { fallback_provider_ids: order.slice(1) } : {}), ...(paid && points[row.id] ? { allow_points: true } : {}) });
  }
  const supplierName = (id: string) => matrix.providers.find(item => item.id === id)?.name ?? id;
  return <section ref={ref} tabIndex={-1} className="dc-source-selection" aria-labelledby="dc-routing-title">
    <div className="dc-routing-dialog-head"><h2 id="dc-routing-title">{capability ? capability.name + ' · 可替换来源' : '选择由 ' + provider?.name + ' 提供的数据项'}</h2><button type="button" aria-label="关闭来源选择" disabled={busy} onClick={close}><Icon name="close"/></button></div>
    <p className="dc-routing-intro">{!editable ? '修改来源需要管理员角色。' : provider ? '勾选要交给此来源的数据项，原来源保留为备用。' : '可以多选。首选无法提供合格数据时，按备用顺序取数。'}</p>
    <div className="dc-routing-options">{rows.map(row => <section key={row.id} className="dc-routing-row">
      <h3>{row.name}<small>当前首选 · {row.current_provider_id ? supplierName(row.current_provider_id) : '自动选择'}</small></h3>
      {row.options.filter(opt => !provider || opt.provider_id === provider.id).map(option => {
        const key = option.provider_id;
        const draft = drafts[row.id] ?? [];
        const rank = draft.indexOf(key);
        const selected = rank >= 0;
        const reason = optionBlocker(option, matrix.providers.find(item => item.id === key));
        const paidReady = reason === 'points_not_enabled' && option.entitlement === 'verified' && option.billing === 'points';
        const name = supplierName(key);
        return <div className={'dc-route-choice' + (selected ? ' selected' : '')} key={key}>
          <div className="dc-choice-head"><label><input type="checkbox" aria-label={row.name + '使用' + name} checked={selected}
            disabled={!editable || busy || (!selected && !!reason && !paidReady) || (!!provider && row.current_provider_id === key)}
            onChange={event => toggle(row.id, key, event.target.checked)}/><strong>{name}</strong><Billing option={option}/></label>
            {selected && !provider ? <span className="dc-source-rank">{rank === 0 ? '首选' : '备用 ' + rank}</span> : <span className="dc-muted">{optionState(option)}</span>}
            {rank > 0 && !provider && !reason && <button type="button" className="dc-link" disabled={busy || !editable} onClick={() => setDrafts(old => ({ ...old, [row.id]: [key, ...old[row.id].filter(id => id !== key)] }))}>设为首选</button>}
          </div>
          {reason && <p className="dc-block-reason">{portHelp[reason]} {option.implemented && <button type="button" className="dc-link" disabled={busy} onClick={() => manage(option)}>{reason === 'points_consent' ? '去授权本次检测' : '处理此端口'}</button>}</p>}
          {option.billing === 'points' && reason !== 'points_consent' && <label className="dc-points-consent"><input type="checkbox" checked={points[row.id] ?? false} disabled={!editable || busy || !selected} onChange={event => setPoints({ ...points, [row.id]: event.target.checked })}/>允许此能力消耗通达信积分</label>}
          <details className="dc-choice-evidence"><summary>覆盖范围与检测记录</summary><OptionEvidence option={option}/></details>
        </div>;
      })}
    </section>)}</div>
    {error !== null && <ErrorNotice error={error}/>}<div className="dc-dialog-actions"><span>{capability ? (drafts[capability.id]?.length ? '已选 ' + drafts[capability.id].length + ' 个来源' : '请至少选择一个来源') : '将修改 ' + changes.length + ' 项能力'}</span><button type="button" disabled={busy} onClick={close}>取消</button><button type="button" className="dc-primary" disabled={!editable || busy || !changes.length || needsPoints} onClick={() => apply(changes)}>{busy ? '正在应用…' : '应用选中能力'}</button></div>
  </section>;
}

function PortReport({ title, items, matrix, manage }: {
  title: string; items: PortResult[]; matrix: CapabilityMatrix; manage: (option: CapabilityOption) => void;
}) {
  const labels = { configured: '本次配置', existing: '已有配置', attention: '待处理' };
  items = items.map(item => {
    if (item.reason !== 'verification_queued') return item;
    const supplier = matrix.providers.find(p => p.id === item.provider_id);
    const port = supplier?.capabilities.find(o => o.capability_id === item.capability_id);
    if (port?.selected && usable(port)) return { ...item, state: 'configured', reason: 'route_saved' };
    const blocker = port && optionBlocker(port, supplier);
    return blocker && blocker !== 'not_checked' ? { ...item, reason: blocker } : item;
  });
  const tdx = matrix.providers.find(p => p.template === 'tdx_data' && p.credential_configured);
  const consent = tdx?.capabilities.find(o => items.some(i => i.provider_id === tdx.id && i.capability_id === o.capability_id && i.reason === 'points_consent'));
  const queued = items.filter(item => item.reason === 'verification_queued').length;
  return <><div role="status"><strong>{title}</strong> · {Object.entries(labels).map(([key, name]) => name + ' ' + items.filter(item => item.state === key && item.reason !== 'verification_queued').length).join('，')}{queued > 0 && '，等待检测 ' + queued}</div>
    {consent && <button type="button" className="dc-link" onClick={() => manage(consent)}>配置通达信积分端口</button>}
    <p>端口配置用于选择采集来源。历史复盘按交易日归档，可在运行监控查看采集结果。</p>
    {!!items.length && <details><summary>查看端口结果</summary><ul>{items.map(item => {
      const provider = matrix.providers.find(entry => entry.id === item.provider_id);
      const option = provider?.capabilities.find(entry => entry.capability_id === item.capability_id);
      return <li key={item.provider_id + '/' + item.capability_id}><span><strong>{provider?.name} · {option?.name}</strong><small>{portHelp[item.reason] ?? labels[item.state]}</small></span>
        {item.state === 'attention' && item.reason !== 'verification_queued' && option?.implemented && <button type="button" className="dc-link" onClick={() => manage(option)}>{item.reason === 'points_consent' ? '去授权本次检测' : '处理此端口'}</button>}</li>;
    })}</ul></details>}</>;
}

function PointsSetup({ provider, initialPort, session, close, changed }: {
  provider: Supplier; initialPort: string; session: SessionResponse; close: () => void; changed: () => void;
}) {
  const index = useResource(dataCenterApi.integrations);
  const options = provider.capabilities.filter(o => o.implemented && o.billing === 'points' && o.source_ids.length);
  const [port, setPort] = useState(initialPort);
  const [sending, setSending] = useState(false);
  const [submitted, setSubmitted] = useState('');
  const [error, setError] = useState<unknown>(null);
  const lock = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, []);
  const option = options.find(o => o.capability_id === port) ?? options[0];
  const source = option?.source_ids[0];
  const connection = index.data?.data.items.find(c => c.id === provider.connection_id);
  const last = connection?.last_test;
  const receipt = last && last.source_id === source && last.revision === connection?.revision && (!submitted || last.job_id === submitted) ? last : null;
  const pending = last?.status === 'queued' || last?.status === 'running' || !!submitted && !receipt;
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => { if (!document.hidden) index.reload(); }, 3000);
    return () => window.clearInterval(timer);
  }, [pending, index.reload]);
  async function act(activate: boolean) {
    if (!connection || !option || !source || pending || lock.current) return;
    lock.current = true; setSending(true); setError(null);
    try {
      const csrf = session.data.csrf_token;
      if (activate) {
        const snapshot = (await capabilityApi.read()).data;
        const cap = snapshot.capabilities.find(c => c.id === option.capability_id)!;
        const current = cap.options.find(o => o.provider_id === provider.id);
        if (!current || current.entitlement !== 'verified' || !(usable(current) || current.state === 'points_not_enabled')) throw new Error('这项数据尚未通过核验，请查看检测结果。');
        const order = [...new Set([...orderedSources(cap), provider.id])];
        await capabilityApi.routes(snapshot.revision, [{capability_id:cap.id, provider_id:order[0], fallback_provider_ids:order.slice(1), allow_points:true}],csrf);
        changed(); close();
      } else {
        await capabilityApi.connect(provider.id, '', csrf, source);
        const result = await dataCenterApi.probe({...connectionInput(connection),id:connection.id,source_id:source},csrf);
        setSubmitted(result.data.job_id ?? ''); index.reload();
      }
    } catch (reason) { setError(reason); index.reload(); }
    finally { lock.current=false; setSending(false); }
  }
  const missingQuoteDate = option?.capability_id === 'stock_quote' && (receipt?.error_code === 'quote_time_unknown'
    || !receipt && option.state === 'time_unverified' && option.entitlement === 'verified' && !!option.last_success_at);
  const verified = receipt?.status === 'success' || !receipt && option?.entitlement === 'verified' && (usable(option) || option.state === 'points_not_enabled');
  return <section className="dc-source-selection" aria-labelledby="tdx-setup-title">
    <div className="dc-routing-dialog-head"><h2 ref={heading} tabIndex={-1} id="tdx-setup-title">配置通达信端口</h2><button type="button" disabled={sending} onClick={close}>关闭</button></div>
    <p>Key 已保存。选择一个端口进行积分检测，通过后可将它加入数据来源。</p>
    <label>本次端口 <select value={option?.capability_id ?? ''} disabled={sending || pending} onChange={e => { setPort(e.target.value); setSubmitted(''); setError(null); }}>
      {options.map(o => <option key={o.capability_id} value={o.capability_id}>{o.name}</option>)}</select></label>
    {index.error != null && <ErrorNotice error={index.error} retry={index.reload}/>}
    {!connection && !index.busy && index.error == null && <p>尚未读到已保存的连接。<button type="button" onClick={index.reload}>重新读取</button></p>}
    <p role="status">{pending ? '检测已提交，正在等待实际取数回执…' : missingQuoteDate ? '已收到股票报价，Key 可用。通达信快照提供时分秒，这条报价缺少可核验的交易日期，暂不能用作实时行情来源。' : receipt?.error_code === 'tdx_sdk_unavailable' ? '上次检测未能通过本机通达信组件取得行情。可以重新授权检测一次。' : receipt?.status === 'error' ? '上次检测未通过。' + (receipt.error_code ? reasonLabel(receipt.error_code) : receipt.message ?? '') : verified ? '该端口已通过检测。' : '等待授权本次检测。'}</p>
    {error != null && <ErrorNotice error={error}/>}
    {missingQuoteDate && !pending ? <p>这项检测已完成，无需重复消耗积分。<a href={'/data?view=monitor&source_id=' + encodeURIComponent(source ?? '') + '#data-calls'}>查看采集记录</a></p> : option?.selected && provider.enabled ? <strong>此端口已选用</strong> : verified ?
      <button type="button" className="dc-primary" disabled={!connection || sending || pending} onClick={() => void act(true)}>启用此端口并允许后续采集消耗积分</button> :
      <button type="button" className="dc-primary" disabled={!connection || !source || sending || pending} onClick={() => void act(false)}>允许本次消耗积分并检测</button>}
  </section>;
}

function SupplierDetail({ provider, initialSource, index, session, busy, connect, run, configure, openCollection, auto }: {
  provider: Supplier; index: ConnectionIndex | null; session: SessionResponse; busy: boolean;
  connect: (key: string) => Promise<void>; run: (action: () => Promise<unknown>, message?: string) => Promise<void>;
  initialSource: string; configure: (id: string | null) => void; auto: (option: CapabilityOption) => void;
  openCollection: (sourceId?: string) => void;
}) {
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [sourceId, setSourceId] = useState(initialSource);
  const [probeMessage, setProbeMessage] = useState('');
  const [allowPointsTest, setAllowPointsTest] = useState(false);
  const admin = session.data.user.role === 'admin';
  const connection = index?.items.find(item => item.id === provider.connection_id);
  const testable = provider.capabilities.filter(option => option.implemented && option.source_ids.length);
  const sources = [...new Map(testable.flatMap(option => option.source_ids.map(id => [id, option.name] as const))).entries()];
  const actualSource = sourceId.startsWith('port:') ? testable.find(option => option.capability_id === sourceId.slice(5))?.source_ids[0] ?? ''
    : sources.some(([id]) => id === sourceId) ? sourceId : sources[0]?.[0] ?? '';
  const testUsesPoints = testable.some(option => option.billing === 'points' && option.source_ids.includes(actualSource));
  async function save(event: FormEvent) { event.preventDefault(); try { await connect(apiKey); setApiKey(''); setShowKey(false); } catch { /* The containing page displays the saved error. */ } }
  async function test(item: ConnectionView) {
    setProbeMessage('');
    await run(async () => {
      let current = item;
      if (testUsesPoints) {
        if (!allowPointsTest) return;
        await capabilityApi.connect(provider.id, '', session.data.csrf_token, actualSource);
        const integrations = await dataCenterApi.integrations();
        current = integrations.data.items.find(row => row.id === item.id) ?? item;
      }
      const response = await dataCenterApi.probe({ ...connectionInput(current), id: current.id, source_id: actualSource }, session.data.csrf_token);
      setAllowPointsTest(false);
      setProbeMessage(response.data.status === 'queued' ? '这一项能力已排队检测。实际取数结果见运行监控，其他能力仍保留各自状态。' : `${response.data.message} · ${duration(response.data.elapsed_ms)}`);
    });
  }
  return <Panel title={provider.name} className="dc-supplier-detail" action={<Status value={provider.enabled ? 'ready' : provider.connection_id ? 'disabled' : 'not_checked'} label={provider.enabled ? '连接已启用' : provider.connection_id ? '连接未启用' : '尚未配置'}/>}>
    <div className="dc-supplier-detail-grid"><div className="dc-provider-credentials"><p className="dc-muted">{provider.endpoint}</p>{provider.template === 'tdx' || provider.id.toLowerCase().includes('tdx') ? <p className="dc-permission-note">MCP 授权与行情数据服务权益分别核验。五档、分钟和全量分钟使用数据服务；MCP Key 已保存不代表这些能力已开通。</p> : null}
      <form onSubmit={save} autoComplete="off"><fieldset disabled={!admin || busy || !index?.credential_store_ready && provider.credential_required} className="dc-form-fields">{provider.credential_required && <label>{provider.id.toLowerCase().includes('tdx') ? '通达信数据服务 / MCP Key' : 'API Key'}<span className="dc-key-input"><input aria-label={`${provider.name} API Key`} type={showKey ? 'text' : 'password'} value={apiKey} onChange={event => setApiKey(event.target.value)} autoComplete="new-password" maxLength={4096} spellCheck={false} placeholder={provider.credential_configured ? '已安全保存；留空保留原 Key' : '粘贴供应商提供的 Key'} required={!provider.credential_configured}/><button type="button" aria-label={showKey ? '隐藏新 Key' : '显示新 Key'} onClick={() => setShowKey(!showKey)}>{showKey ? '隐藏' : '显示'}</button></span><small>{provider.credential_configured ? '凭据已加密保存，不回显。' : '仅保存到本机加密凭据库。'}</small></label>}<button className="dc-primary" type="submit">{busy ? '正在保存…' : provider.connection_id ? '保存配置' : '连接并保存'}</button></fieldset></form>
      {!index?.credential_store_ready && provider.credential_required && <p className="dc-bad">本机凭据库尚未就绪，暂时无法保存新 Key。</p>}
      <p className="dc-form-footnote">保存配置不会启动自动采集。免费、已开通和取数成功分别显示。</p>
      {connection && <div className="dc-provider-test"><label>本次检测能力<select aria-label={`${provider.name}本次检测能力`} value={actualSource} onChange={event => { setSourceId(event.target.value); setAllowPointsTest(false); }}>{!actualSource && <option value="">请先配置此端口的采集范围</option>}{sources.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>{testUsesPoints && <label className="dc-points-consent"><input type="checkbox" checked={allowPointsTest} disabled={!admin || busy} onChange={event => setAllowPointsTest(event.target.checked)}/>允许本次检测消耗积分</label>}<div className="dc-provider-actions"><button type="button" disabled={!admin || busy || !actualSource || testUsesPoints && !allowPointsTest} onClick={() => { void test(connection); }}>检测单项能力</button><button type="button" disabled={!admin || busy} onClick={() => { void run(() => dataCenterApi.save({ ...connectionInput(connection), enabled: !connection.enabled }, session.data.csrf_token, connection.id), connection.enabled ? '此来源采集已暂停，已保存资料仍保留。' : '此来源已恢复，按已配置能力和交易时段运行。'); }}>{connection.enabled ? '暂停此来源采集' : '恢复此来源采集'}</button><button type="button" onClick={() => openCollection(actualSource)}>采集参数</button><button type="button" disabled={busy} onClick={() => configure(connection.id)}>高级连接配置</button></div>{probeMessage && <p className="dc-feedback" role="status">{probeMessage}</p>}</div>}
      {!connection && <button type="button" onClick={() => openCollection()}>采集参数</button>}
      {provider.capabilities.some(option => option.capability_id === 'full_minute') && <FullMinuteJob session={session} ready={provider.enabled && provider.capabilities.some(option => option.capability_id === 'full_minute' && option.implemented && option.selected && option.entitlement === 'verified')}/>}
    </div><details className="dc-provider-evidence"><summary>数据项与费用明细</summary>{provider.capabilities.map(option => <article key={option.capability_id}><div><strong>{option.name}</strong><Billing option={option}/><span className="dc-muted">{optionState(option)} · {permission[option.entitlement]}</span><button type="button" disabled={!admin || busy} onClick={() => auto(option)}>一键配置此端口</button></div><OptionEvidence option={option}/></article>)}</details></div>
  </Panel>;
}

export function DataSources({ session, index, ai, refreshVersion, refresh, configure, active = true }: {
  session: SessionResponse; index: ConnectionIndex | null; ai?: AiSettings; refreshVersion: number; refresh: () => void;
  configure: (id: string | null, sourceId?: string) => void; active?: boolean;
}) {
  const read = useResource(capabilityApi.read, active);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pointsSetup, setPointsSetup] = useState<{provider:string; port:string} | null>(null);
  const [detailSource, setDetailSource] = useState('');
  const [dialog, setDialog] = useState<{ provider?: string; capability?: string; snapshot: CapabilityMatrix } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [autoScope, setAutoScope] = useState<PortScope | null>(null);
  const [report, setReport] = useState<{ title: string; items: PortResult[] } | null>(null);
  const reportRef = useRef<HTMLDivElement>(null);
  const [othersOpen, setOthersOpen] = useState(false);
  const lock = useRef(false);
  const selectionTrigger = useRef<HTMLElement | null>(null);
  const detail = useRef<HTMLDivElement>(null);
  const navigation = useNavigation();
  const params = new URLSearchParams(navigation.search);
  const managing = params.get('source_view') === 'providers';
  const collectionOpen = active && (navigation.hash === '#data-sources' || params.get('view') === 'sources');
  const archive = useResource(readAnomalyArchive, active && !managing && !collectionOpen);
  const requestedProvider = params.get('collection_provider');
  const requestedSource = params.get('collection_source') ?? '';
  const collectionKey = JSON.stringify([requestedProvider, requestedSource]);
  const [visitedCollection, setVisitedCollection] = useState<string | null>(null);
  useEffect(() => { if (collectionOpen) setVisitedCollection(collectionKey); }, [collectionOpen, collectionKey]);
  const collectionTrigger = useRef<HTMLElement | null>(null);
  useEffect(() => { if (active && refreshVersion) read.reload(); }, [active, refreshVersion, read.reload]);
  useEffect(() => { if (!active) { setDialog(null); setError(null); } }, [active]);
  const matrix = read.data?.data;
  const watching = report?.items.some(item => item.reason === 'verification_queued' && matrix?.providers
    .find(p => p.id === item.provider_id)?.capabilities.some(o => o.capability_id === item.capability_id && !o.selected && o.state === 'not_checked'));
  useEffect(() => {
    if (!active || !watching) return;
    const timer = window.setInterval(() => { if (!document.hidden) read.reload(); }, 15000);
    return () => window.clearInterval(timer);
  }, [active, watching, read.reload]);
  const provider = matrix?.providers.find(item => item.id === selectedId);
  const collectionProvider = matrix?.providers.find(item => item.id === requestedProvider);
  const collectionConnection = index?.items.find(item => item.id === collectionProvider?.connection_id);
  const scope = collectionProvider && {
    sourceIds: [...(collectionConnection?.source_ids ?? []), ...collectionProvider.capabilities.flatMap(option => option.source_ids)],
    families: index?.templates.find(item => item.id === collectionProvider.template)?.source_families ?? [],
  };
  const admin = session.data.user.role === 'admin';
  function changeSourceView(open: boolean) {
    const next = collectionParams(navigation.search);
    if (open) next.set('source_view', 'providers');
    else next.delete('source_view');
    goToDataTopic('connections', next);
  }
  function openCollection(providerId?: string, sourceId?: string) {
    collectionTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    goToDataTopic('sources', collectionParams(navigation.search, sourceId, providerId));
  }
  function closeCollection() {
    goToDataTopic('connections', collectionParams(navigation.search));
    requestAnimationFrame(() => {
      const trigger = collectionTrigger.current;
      if (trigger?.isConnected) trigger.focus();
      else document.querySelector<HTMLButtonElement>('#data-sources button')?.focus();
    });
  }
  async function run(action: () => Promise<unknown>, message = '') {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(null); setNotice('');
    try { await action(); setNotice(message); refresh(); }
    catch (reason) { setError(reason); if (reason instanceof ApiError && reason.status === 409) read.reload(); throw reason; }
    finally { lock.current = false; setBusy(false); }
  }
  function background(action: () => Promise<unknown>, message = '') { return run(action, message).catch(() => undefined); }
  function select(id: string, source = '') {
    if (collectionOpen || !managing) changeSourceView(true);
    setDialog(null); setDetailSource(source); setSelectedId(id); setError(null);
    if (matrix?.providers.some(item => item.id === id && coreOrder(item) < 0)) setOthersOpen(true);
    window.setTimeout(() => detail.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0);
  }
  function showDialog(value: { provider?: string; capability?: string }) {
    if (!matrix) return;
    selectionTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setError(null); setDialog({ ...value, snapshot: matrix });
  }
  function closeSelection() {
    setDialog(null);
    setError(null);
    requestAnimationFrame(() => selectionTrigger.current?.focus());
  }
  async function apply(changes: RouteChange[]) {
    if (!dialog) return;
    await background(async () => { const response = await capabilityApi.routes(dialog.snapshot.revision, changes, session.data.csrf_token); read.setData(response); setReport(null); closeSelection(); }, '首选和备用来源已保存。');
  }
  async function auto(scope: PortScope) {
    if (!matrix || !admin || lock.current) return;
    setAutoScope(scope); setReport(null);
    const vendor = matrix.providers.find(item => item.id === scope.provider_id)?.name;
    const port = matrix.capabilities.find(item => item.id === scope.capability_id)?.name;
    try {
      await background(async () => {
        const result = await capabilityApi.configure(matrix.revision, scope, session.data.csrf_token);
        read.setData({ ...result, data: result.data.matrix });
        setReport({ title: [vendor, port].filter(Boolean).join(' · ') || '全部来源', items: result.data.items });
        const points = result.data.matrix.providers.find(p => p.template === 'tdx_data' && p.connection_id && p.credential_configured);
        if (points && scope.provider_id === points.id && result.data.items.some(i => i.provider_id === points.id && i.state === 'attention')) {
          setPointsSetup({provider:points.id, port:scope.capability_id ?? 'stock_quote'});
          return;
        }
        requestAnimationFrame(() => reportRef.current?.scrollIntoView({ block: 'nearest' }));
      });
    } finally { setAutoScope(null); }
  }
  function manage(option: CapabilityOption) {
    const supplier = matrix?.providers.find(p => p.id === option.provider_id);
    if (admin && option.billing === 'points' && supplier?.template === 'tdx_data' && supplier.credential_configured) {
      setDialog(null); setPointsSetup({provider:supplier.id,port:option.capability_id}); return;
    }
    select(option.provider_id, option.source_ids[0] ?? 'port:' + option.capability_id);
  }
  const groups = [...new Set(matrix?.capabilities.map(item => item.group) ?? [])];
  const primary = [...(matrix?.providers ?? [])].filter(item => coreOrder(item) >= 0).sort((a, b) => coreOrder(a) - coreOrder(b));
  const others = matrix?.providers.filter(item => coreOrder(item) < 0) ?? [];
  const card = (item: Supplier) => <SupplierCard key={item.id} item={item} selected={selectedId === item.id} admin={admin} busy={busy} configuring={autoScope?.provider_id === item.id && !autoScope.capability_id} auto={() => { void auto({ provider_id: item.id }); }} canApply={item.capabilities.some(option => usable(option) && matrix?.capabilities.find(row => row.id === option.capability_id)?.routable !== false)} select={() => select(item.id)} choose={option => { const route = matrix?.capabilities.find(row => row.id === option.capability_id); if (route && route.routable !== false) showDialog({ capability: route.id }); else manage(option); }} apply={() => showDialog({ provider: item.id })}/>;
  const routeCard = (item: RouteCapability) => {
    const current = matrix?.providers.find(row => row.id === item.current_provider_id);
    const effective = matrix?.providers.find(row => row.id === item.effective_provider_id);
    const activeOption = item.options.find(option => option.provider_id === item.current_provider_id);
    return <article key={item.id} className="dc-route-card"><h3>{item.name}</h3><p>{item.description}</p><div className="dc-route-current"><small>首选</small><strong>{current?.name ?? '自动 · 已购权益优先'}</strong>{activeOption && <Billing option={activeOption}/>}</div>{!!item.fallback_provider_ids?.length && <small className="dc-effective-source">备用 · {item.fallback_provider_ids.map(id => matrix?.providers.find(supplier => supplier.id === id)?.name ?? id).join(" → ")}</small>}<small className="dc-effective-source">实际供数：{effective?.name ?? '暂无满足要求的来源'}</small><div className="dc-route-providers">{item.options.map(option => <span key={option.provider_id} title={option.reason ?? option.scope}>{matrix?.providers.find(row => row.id === option.provider_id)?.name ?? option.provider_id}<Billing option={option}/></span>)}</div>{item.routable !== false ? <button type="button" disabled={!admin || busy} onClick={() => showDialog({ capability: item.id })}>选择来源 <Icon name="arrow"/></button> : <small className="dc-muted">沿用现有采集配置，可在来源详情管理</small>}</article>;
  };
  return <>
    {active && admin && pointsSetup && matrix?.providers.some(p => p.id === pointsSetup.provider) &&
      <PointsSetup provider={matrix.providers.find(p => p.id === pointsSetup.provider)!} initialPort={pointsSetup.port} session={session}
        close={() => setPointsSetup(null)} changed={() => { read.reload(); refresh(); setReport(null); setNotice('已启用所选通达信端口。'); }}/>}
    {dialog && <SourceSelection matrix={dialog.snapshot} provider={dialog.snapshot.providers.find(item => item.id === dialog.provider)} capability={dialog.snapshot.capabilities.find(item => item.id === dialog.capability)} busy={busy} error={error} editable={admin} close={closeSelection} manage={manage} apply={changes => { void apply(changes); }}/>}
    <div hidden={!!dialog || !!pointsSetup && active && admin}><div className="dc-sources-page">
    {error !== null && !dialog && <ErrorNotice error={error}/>} {notice && <div className="dc-feedback" role="status">{notice}</div>}
    {read.error !== null && <ErrorNotice error={read.error} retry={read.reload} inline={!matrix}/>}
    <div hidden={managing || collectionOpen}><FunctionCoverage input={{ matrix: read.error ? undefined : matrix,
      index: index ?? undefined, ai, archive: archive.error ? undefined : archive.data?.data }}
      account={session.data.user.id} csrf={admin ? session.data.csrf_token : undefined} active={active && !managing && !collectionOpen && !dialog}
      manageSources={() => changeSourceView(true)} choose={id => showDialog({ capability: id })}
      manage={row => select(row.providerId, row.sourceId ?? (row.option ? 'port:' + row.option.capability_id : ''))}
      refresh={() => { refresh(); archive.reload(); }} busy={read.busy || busy}/></div>
    <div hidden={!managing || collectionOpen}>
    <button type="button" className="dc-text-button fc-back" onClick={() => changeSourceView(false)}>返回功能覆盖</button>
    <div className="data-provider-layout">
    <section className="dc-suppliers-section"><div className="dc-section-heading"><h2>数据来源</h2><div className="dc-port-actions"><button type="button" className="dc-primary" disabled={!admin || busy || !matrix} onClick={() => { void auto({}); }}>{autoScope && !autoScope.provider_id && !autoScope.capability_id ? '正在配置…' : '一键配置所有端口'}</button><button type="button" disabled={read.busy || busy} onClick={read.reload}><Icon name="refresh"/>刷新</button></div></div>
      {report && matrix && <div className="dc-port-report" ref={reportRef}><PortReport title={report.title} items={report.items} matrix={matrix} manage={manage}/></div>}
      {!matrix && read.busy ? <p className="dc-loading" role="status">正在读取来源与费用…</p> : matrix && !matrix.providers.length ? <p className="data-field-hint">还没有已登记的数据来源。</p> : matrix && <><div className="dc-supplier-grid dc-core-suppliers">{primary.map(card)}</div>{!!others.length && <details className="dc-other-suppliers" open={othersOpen} onToggle={event => setOthersOpen(event.currentTarget.open)}><summary>其他已登记来源（{others.length}）<Icon name="chevron"/></summary><div className="dc-supplier-grid">{others.map(card)}</div></details>}</>}
    </section>
    {matrix && groups.length > 0 && <section className="dc-route-section"><div className="dc-section-heading"><div><h2>按数据项选择来源</h2><p>手动调整首选与备用来源，积分端口需单独授权。</p></div></div>{groups.map(group => {
      const items = matrix.capabilities.filter(item => item.group === group);
      const selected = items.filter(item => item.routable !== false && item.current_provider_id);
      const remaining = items.filter(item => item.routable === false || !item.current_provider_id);
      return <details key={group} className="dc-route-group"><summary>{group}<small>{items.length} 项能力</small><Icon name="chevron"/></summary>{selected.length ? <><div className="dc-route-grid">{selected.map(routeCard)}</div>{remaining.length > 0 && <details className="dc-route-optional"><summary>更多接口与能力（{remaining.length}）<Icon name="chevron"/></summary><div className="dc-route-grid">{remaining.map(routeCard)}</div></details>}</> : <div className="dc-route-grid">{items.map(routeCard)}</div>}</details>;
    })}</section>}
    </div>
    <div ref={detail} id="dc-supplier-detail">{provider && <><button type="button" onClick={() => setSelectedId(null)}>收起连接设置</button><SupplierDetail key={provider.id + '/' + detailSource} initialSource={detailSource} provider={provider} index={index} session={session} busy={busy} auto={option => { void auto({ provider_id: provider.id, capability_id: option.capability_id }); }} connect={async key => {
      let saved = false; await run(async () => { const response = await capabilityApi.connect(provider.id, key, session.data.csrf_token); read.setData(response); saved = true; }, '配置已加密保存。可按能力检测，再选择需要应用的来源。'); if (!saved) throw new Error('保存未完成');
      if (provider.template === 'tdx_data') setPointsSetup({provider:provider.id, port:'stock_quote'});
    }} run={background} configure={configure} openCollection={id => openCollection(provider.id, id)}/></>}</div>
    </div>
    <div id="data-sources" tabIndex={-1} className="dc-collection-inline">
      {!collectionOpen && managing && !provider && <button type="button" className="dc-text-button" onClick={() => openCollection()}><Icon name="search"/>按接口查找采集参数</button>}
      {(collectionOpen || visitedCollection === collectionKey) && <div hidden={!collectionOpen}>
        <div className="dc-collection-heading"><h3>{collectionProvider ? collectionProvider.name + ' · 采集参数' : '采集参数'}</h3>
          <button type="button" onClick={closeCollection} aria-label="收起采集参数">收起</button></div>
        {requestedProvider && !collectionProvider
          ? read.error ? <ErrorNotice error={read.error} retry={read.reload} inline/>
            : <p role="status">{matrix ? '找不到指定的数据来源。' : '正在读取数据来源…'}</p>
          : <div className="dc-legacy-tools"><CollectionTools key={requestedProvider ?? ''} session={session} scope={scope}
              requestedSourceId={requestedSource} onChanged={refresh} connectionAction={sourceId => {
                const linked = index?.items.find(item => item.kind !== 'model' && item.source_ids.includes(sourceId));
                return admin && linked ? <button type="button" onClick={() => configure(linked.id, sourceId)}>查看「{linked.name}」配置</button> : null;
              }}/></div>}
      </div>}
    </div>

  </div></div></>;
}
