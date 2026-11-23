import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { Icon } from '../../shared/icons';
import { connectionInput, dataCenterApi } from './api';
import type { ConnectionIndex, ConnectionView } from './api';
import { capabilityApi } from './capabilityApi';
import type { CapabilityMatrix, CapabilityOption, RouteCapability, RouteChange, Supplier } from './capabilityApi';
import { dateTime, duration, Empty, ErrorNotice, Panel, reasonLabel, Status, statusLabel, useResource } from './shared';
import { FullMinuteJob } from './FullMinuteJob';

const permission = { verified: '已开通', locked: '需解锁', unknown: '待核验' };
const coreProviders = ['fuyao', 'tdx_data', 'tushare', 'tickflow'];
function coreOrder(provider: Supplier) { return coreProviders.indexOf(coreProviders.includes(provider.id) ? provider.id : provider.template); }
function usable(option: CapabilityOption) { return option.implemented && option.entitlement === 'verified' && !['disabled', 'points_not_enabled', 'permission_missing', 'credentials_missing', 'unavailable', 'error', 'failed', 'empty', 'time_unverified'].includes(option.state); }
function safeReference(value: string) { try { return new URL(value).protocol === 'https:'; } catch { return false; } }
function optionState(option: CapabilityOption, compact = false) {
  if (!option.implemented) return '待接入';
  if (option.entitlement === 'locked') return '需解锁';
  if (option.state === 'disabled') return '采集已关闭';
  if (option.state === 'points_not_enabled') return '待允许积分';
  if (['error', 'failed'].includes(option.state)) return '检测失败';
  if (option.state === 'empty') return '未返回数据';
  if (option.state === 'unavailable') return '暂不可用';
  if (option.state === 'credentials_missing') return '待填写 Key';
  if (option.state === 'not_configured') return '待配置';
  if (option.state === 'time_unverified') return compact ? '时点待核' : '行情时点待核对';
  if (option.entitlement !== 'verified') return '待核验';
  if (option.state !== 'ready') {
    const label = statusLabel(option.state);
    return label === option.state ? '待核验' : label;
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

function SupplierCard({ item, selected, admin, busy, canApply, select, choose, apply }: {
  item: Supplier; selected: boolean; admin: boolean; busy: boolean; canApply: boolean;
  select: () => void; choose: (option: CapabilityOption) => void; apply: () => void;
}) {
  const options = [...item.capabilities].sort((a, b) => Number(b.selected) - Number(a.selected));
  const chip = (option: CapabilityOption) => {
    const locked = option.entitlement === 'locked' || option.provider_id === 'tickflow' && option.billing === 'subscription' && option.entitlement !== 'verified';
    const ready = option.implemented && option.entitlement === 'verified' && option.state === 'ready';
    return <button type="button" key={option.capability_id} className={'dc-capability-chip' + (option.selected ? ' serving' : '') + (!option.implemented ? ' unimplemented' : '')} aria-label={`${item.name} ${option.name} ${option.billing_label} ${optionState(option)}`} onClick={() => choose(option)}><span>{locked ? '🔒 ' : option.selected ? '✓ ' : ''}{option.name}</span>{!locked && <Billing option={option}/>}
      {!ready && <small className="dc-capability-state">{optionState(option, true)}</small>}</button>;
  };
  return <article className={'dc-supplier-card' + (selected ? ' selected' : '')}><div className="dc-supplier-card-heading"><h3><button type="button" aria-expanded={selected} aria-controls="dc-supplier-detail" onClick={select}><i className={item.enabled ? 'is-on' : ''}/>{item.name}</button></h3><span className="dc-tag">{item.connection_id ? item.credential_required && !item.credential_configured ? '待填 Key' : item.enabled ? '已启用' : '已配置' : '未配置'}</span></div><div className="dc-capability-chips">{options.slice(0, 6).map(chip)}</div>{options.length > 6 && <details className="dc-more-capabilities"><summary>查看其余 {options.length - 6} 项<Icon name="chevron"/></summary><div className="dc-capability-chips">{options.slice(6).map(chip)}</div></details>}<div className="dc-supplier-card-footer"><span>当前承担 <strong>{item.serving_count}</strong> 项能力</span><div><button type="button" onClick={select}>配置</button><button type="button" className="dc-link" disabled={!admin || busy || !canApply} onClick={apply}>套用</button></div></div></article>;
}

function ApplyDialog({ matrix, provider, capability, busy, error, close, apply, editable }: {
  matrix: CapabilityMatrix; provider?: Supplier; capability?: RouteCapability; busy: boolean; error: unknown; editable: boolean;
  close: () => void; apply: (changes: RouteChange[]) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [chosen, setChosen] = useState<Record<string, string>>(() => provider ? Object.fromEntries(matrix.capabilities.flatMap(item => {
    const option = item.options.find(row => row.provider_id === provider.id);
    return item.routable !== false && option && usable(option) && option.billing !== 'points' && item.current_provider_id !== provider.id ? [[item.id, provider.id]] : [];
  })) : {});
  const [points, setPoints] = useState<Record<string, boolean>>({});
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => { dialog?.close(); }; }, []);
  const rows = capability ? [capability] : matrix.capabilities.filter(item => item.routable !== false && item.options.some(row => row.provider_id === provider?.id));
  const changes = Object.entries(chosen).filter(([, value]) => value).map(([id, value]) => ({ capability_id: id, provider_id: value, ...(points[id] ? { allow_points: true } : {}) }));
  const blocked = changes.some(change => rows.find(row => row.id === change.capability_id)?.options.find(option => option.provider_id === change.provider_id)?.billing === 'points' && !points[change.capability_id]);
  return <dialog ref={ref} className="dc-routing-dialog" aria-labelledby="dc-routing-title" onCancel={event => { if (busy) event.preventDefault(); else close(); }}>
    <div className="dc-routing-dialog-head"><div><small>按能力选择数据来源</small><h2 id="dc-routing-title">{capability ? `${capability.name} · 可替换来源` : `套用 ${provider?.name}`}</h2></div><button type="button" aria-label="关闭来源选择" disabled={busy} onClick={close}><Icon name="close"/></button></div>
    <p className="dc-routing-intro">{editable ? '只修改勾选的能力。未勾选项保持当前来源；积分能力需要逐项明确启用。' : '当前账号可查看替代关系；修改来源需要管理员角色。'}</p>
    <div className="dc-routing-options">{rows.map(row => <section key={row.id} className="dc-routing-row"><h3>{row.name}<small>当前：{matrix.providers.find(item => item.id === row.current_provider_id)?.name ?? '自动选择'}</small></h3><p>{row.description}</p>{row.options.filter(option => !provider || option.provider_id === provider.id).map(option => {
      const selected = chosen[row.id] === option.provider_id;
      const same = row.current_provider_id === option.provider_id;
      return <div className={'dc-route-choice' + (selected ? ' selected' : '')} key={option.provider_id}><label><input type="checkbox" aria-label={`${row.name}使用${matrix.providers.find(item => item.id === option.provider_id)?.name ?? option.provider_id}`} checked={selected} disabled={!editable || busy || !usable(option) || same} onChange={event => setChosen({ ...chosen, [row.id]: event.target.checked ? option.provider_id : '' })}/><strong>{matrix.providers.find(item => item.id === option.provider_id)?.name ?? option.provider_id}</strong><Billing option={option}/><span className="dc-muted">{same ? usable(option) ? '当前使用' : '当前选择 · ' + optionState(option) : optionState(option)} · {permission[option.entitlement]}</span></label><OptionEvidence option={option}/>{option.billing === 'points' && <label className="dc-points-consent"><input type="checkbox" checked={points[row.id] ?? false} disabled={!editable || busy || !selected} onChange={event => setPoints({ ...points, [row.id]: event.target.checked })}/>允许此能力消耗通达信积分</label>}</div>;
    })}</section>)}</div>
    {error !== null && <ErrorNotice error={error}/>}<div className="dc-dialog-actions"><span>将修改 {changes.length} 项能力</span><button type="button" disabled={busy} onClick={close}>取消</button><button type="button" className="dc-primary" disabled={!editable || busy || !changes.length || blocked} onClick={() => apply(changes)}>{busy ? '正在应用…' : '应用选中能力'}</button></div>
  </dialog>;
}

function SupplierDetail({ provider, index, session, busy, connect, run, configure }: {
  provider: Supplier; index: ConnectionIndex | null; session: SessionResponse; busy: boolean;
  connect: (key: string) => Promise<void>; run: (action: () => Promise<unknown>, message?: string) => Promise<void>;
  configure: (id: string | null) => void;
}) {
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [sourceId, setSourceId] = useState('');
  const [probeMessage, setProbeMessage] = useState('');
  const [allowPointsTest, setAllowPointsTest] = useState(false);
  const admin = session.data.user.role === 'admin';
  const connection = index?.items.find(item => item.id === provider.connection_id);
  const testable = provider.capabilities.filter(option => option.implemented && option.source_ids.length);
  const sources = [...new Map(testable.flatMap(option => option.source_ids.map(id => [id, option.name] as const))).entries()];
  const actualSource = sources.some(([id]) => id === sourceId) ? sourceId : sources[0]?.[0] ?? '';
  const testUsesPoints = testable.some(option => option.billing === 'points' && option.source_ids.includes(actualSource));
  async function save(event: FormEvent) { event.preventDefault(); try { await connect(apiKey); setApiKey(''); setShowKey(false); } catch { /* The containing page displays the saved error. */ } }
  async function test(item: ConnectionView) {
    setProbeMessage('');
    await run(async () => {
      let current = item;
      if (testUsesPoints) {
        if (!allowPointsTest) return;
        await capabilityApi.connect(provider.id, '', session.data.csrf_token, true);
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
      {connection && <div className="dc-provider-test"><label>本次检测能力<select aria-label={`${provider.name}本次检测能力`} value={actualSource} onChange={event => { setSourceId(event.target.value); setAllowPointsTest(false); }}>{sources.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>{testUsesPoints && <label className="dc-points-consent"><input type="checkbox" checked={allowPointsTest} disabled={!admin || busy} onChange={event => setAllowPointsTest(event.target.checked)}/>允许本次检测消耗积分</label>}<div className="dc-provider-actions"><button type="button" disabled={!admin || busy || !actualSource || testUsesPoints && !allowPointsTest} onClick={() => { void test(connection); }}>检测单项能力</button><button type="button" disabled={!admin || busy} onClick={() => { void run(() => dataCenterApi.save({ ...connectionInput(connection), enabled: !connection.enabled }, session.data.csrf_token, connection.id), connection.enabled ? '此来源采集已暂停，已保存资料仍保留。' : '此来源已恢复，按已配置能力和交易时段运行。'); }}>{connection.enabled ? '暂停此来源采集' : '恢复此来源采集'}</button><button type="button" disabled={busy} onClick={() => configure(connection.id)}>高级采集配置</button></div>{probeMessage && <p className="dc-feedback" role="status">{probeMessage}</p>}</div>}
      {provider.capabilities.some(option => option.capability_id === 'full_minute') && <FullMinuteJob session={session} ready={provider.enabled && provider.capabilities.some(option => option.capability_id === 'full_minute' && option.implemented && option.selected && option.entitlement === 'verified')}/>}
    </div><div className="dc-provider-evidence"><h3>能力与费用明细</h3>{provider.capabilities.map(option => <article key={option.capability_id}><div><strong>{option.name}</strong><Billing option={option}/><span className="dc-muted">{optionState(option)} · {permission[option.entitlement]}</span></div><OptionEvidence option={option}/></article>)}</div></div>
  </Panel>;
}

export function DataSources({ session, index, refreshVersion, refresh, configure, openAi, active = true }: {
  session: SessionResponse; index: ConnectionIndex | null; refreshVersion: number; refresh: () => void;
  configure: (id: string | null) => void; openAi: () => void; active?: boolean;
}) {
  const read = useResource(capabilityApi.read);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ provider?: string; capability?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [guideStep, setGuideStep] = useState<number | null>(null);
  const [othersOpen, setOthersOpen] = useState(false);
  const lock = useRef(false);
  const suppliers = useRef<HTMLElement>(null);
  const capabilities = useRef<HTMLElement>(null);
  const detail = useRef<HTMLDivElement>(null);
  useEffect(() => { if (refreshVersion) read.reload(); }, [refreshVersion, read.reload]);
  useEffect(() => { if (!active) { setDialog(null); setError(null); } }, [active]);
  const matrix = read.data?.data;
  const provider = matrix?.providers.find(item => item.id === selectedId);
  const admin = session.data.user.role === 'admin';
  async function run(action: () => Promise<unknown>, message = '') {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(null); setNotice('');
    try { await action(); setNotice(message); read.reload(); refresh(); }
    catch (reason) { setError(reason); throw reason; }
    finally { lock.current = false; setBusy(false); }
  }
  function background(action: () => Promise<unknown>, message = '') { return run(action, message).catch(() => undefined); }
  function select(id: string) { setSelectedId(id); if (matrix?.providers.some(item => item.id === id && coreOrder(item) < 0)) setOthersOpen(true); setError(null); window.setTimeout(() => detail.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0); }
  function showDialog(value: { provider?: string; capability?: string }) { setError(null); setDialog(value); }
  async function apply(changes: RouteChange[]) {
    if (!matrix) return;
    await background(async () => { const response = await capabilityApi.routes(matrix.revision, changes, session.data.csrf_token); read.setData(response); setDialog(null); setGuideStep(3); const targets = [...new Set(changes.map(item => item.provider_id))]; if (targets.length === 1) { setSelectedId(targets[0]); if (response.data.providers.some(item => item.id === targets[0] && coreOrder(item) < 0)) setOthersOpen(true); } }, '所选能力的来源已更新。实际供数来源会随权限、覆盖和采集结果显示。');
  }
  const hasConnections = matrix?.providers.some(item => item.connection_id);
  const groups = [...new Set(matrix?.capabilities.map(item => item.group) ?? [])];
  const step = guideStep ?? (hasConnections ? 2 : 1);
  const primary = [...(matrix?.providers ?? [])].filter(item => coreOrder(item) >= 0).sort((a, b) => coreOrder(a) - coreOrder(b));
  const others = matrix?.providers.filter(item => coreOrder(item) < 0) ?? [];
  const card = (item: Supplier) => <SupplierCard key={item.id} item={item} selected={selectedId === item.id} admin={admin} busy={busy} canApply={item.capabilities.some(option => usable(option) && matrix?.capabilities.find(row => row.id === option.capability_id)?.routable !== false)} select={() => select(item.id)} choose={option => { const route = matrix?.capabilities.find(row => row.id === option.capability_id); if (route && route.routable !== false) showDialog({ capability: route.id }); else select(item.id); }} apply={() => showDialog({ provider: item.id })}/>;
  const routeCard = (item: RouteCapability) => {
    const current = matrix?.providers.find(row => row.id === item.current_provider_id);
    const effective = matrix?.providers.find(row => row.id === item.effective_provider_id);
    const activeOption = item.options.find(option => option.provider_id === item.current_provider_id);
    return <article key={item.id} className="dc-route-card"><h3>{item.name}</h3><p>{item.description}</p><div className="dc-route-current"><small>当前选择</small><strong>{current?.name ?? '自动 · 已购权益优先'}</strong>{activeOption && <Billing option={activeOption}/>}</div><small className="dc-effective-source">实际供数：{effective?.name ?? '暂无满足要求的来源'}</small><div className="dc-route-providers">{item.options.map(option => <span key={option.provider_id} title={option.reason ?? option.scope}>{matrix?.providers.find(row => row.id === option.provider_id)?.name ?? option.provider_id}<Billing option={option}/></span>)}</div>{item.routable !== false ? <button type="button" disabled={!admin || busy} onClick={() => showDialog({ capability: item.id })}>选择来源 <Icon name="arrow"/></button> : <small className="dc-muted">沿用现有采集配置，可在来源详情管理</small>}</article>;
  };
  return <div className="dc-sources-page">
    {error !== null && !dialog && <ErrorNotice error={error}/>} {notice && <div className="dc-feedback" role="status">{notice}</div>}
    <details className="dc-onboarding" open={!hasConnections || guideStep !== null}><summary>配置引导 <small>{hasConnections ? '已识别现有配置，可直接调整' : '三步完成数据来源配置'}</small></summary><div className="dc-guide-steps">{['连接来源', '查看可用能力与费用', '应用选择'].map((label, i) => <button key={label} type="button" className={step === i + 1 ? 'selected' : ''} onClick={() => { setGuideStep(i + 1); (i === 0 ? suppliers.current : capabilities.current)?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }}><span>{i + 1}</span>{label}</button>)}</div><div className="dc-guide-description"><p>{step === 1 ? '选择下方供应商，填入 Key 并保存。已有连接会被保留，无需重新填写。' : step === 2 ? '先看每项能力的费用、权限和取数记录，再点击能力查看可替换来源。' : '使用“套用”查看替换清单，仅应用你勾选的能力。AI 可以稍后配置。'}</p><button type="button" onClick={openAi}>配置 AI（可选）<Icon name="arrow"/></button></div></details>
    <section ref={suppliers} className="dc-suppliers-section"><div className="dc-section-heading"><div><h2>数据来源</h2><p>一张卡片看清费用、已开通能力和当前用途。</p></div><button type="button" disabled={read.busy || busy} onClick={read.reload}><Icon name="refresh"/>刷新</button></div>
      {read.error !== null && <ErrorNotice error={read.error} retry={read.reload}/>}
      {!matrix && read.busy ? <Empty title="正在读取来源与费用…"/> : matrix && <><div className="dc-supplier-grid dc-core-suppliers">{primary.map(card)}</div>{!!others.length && <details className="dc-other-suppliers" open={othersOpen} onToggle={event => setOthersOpen(event.currentTarget.open)}><summary>其他已登记来源（{others.length}）<Icon name="chevron"/></summary><div className="dc-supplier-grid">{others.map(card)}</div></details>}<button type="button" className="dc-add-supplier" onClick={() => configure(null)}><Icon name="plus"/>管理其他已登记来源</button></>}
      <p className="dc-source-legend"><span><i className="dc-legend-dot"/>高亮能力正在使用</span><span>费用与连接检测分别核验</span><span>未知费用按“费用待确认”显示</span></p>
    </section>
    <div ref={detail} id="dc-supplier-detail">{provider && <SupplierDetail key={provider.id} provider={provider} index={index} session={session} busy={busy} connect={async key => {
      let saved = false; await run(async () => { const response = await capabilityApi.connect(provider.id, key, session.data.csrf_token); read.setData(response); saved = true; setGuideStep(2); }, '配置已加密保存。可按能力检测，再选择需要应用的来源。'); if (!saved) throw new Error('保存未完成');
    }} run={background} configure={configure}/>}</div>
    {matrix && <section ref={capabilities} className="dc-route-section"><div className="dc-section-heading"><div><h2>能力路由</h2><p>满足覆盖与时效的免费来源优先，已购服务补缺。积分路径由你逐项启用。</p></div></div>{groups.map(group => {
      const items = matrix.capabilities.filter(item => item.group === group);
      const selected = items.filter(item => item.routable !== false && item.current_provider_id);
      const remaining = items.filter(item => item.routable === false || !item.current_provider_id);
      return <details key={group} className="dc-route-group" open={selected.length > 0}><summary>{group}<small>{items.length} 项能力</small><Icon name="chevron"/></summary>{selected.length ? <><div className="dc-route-grid">{selected.map(routeCard)}</div>{remaining.length > 0 && <details className="dc-route-optional"><summary>更多接口与能力（{remaining.length}）<Icon name="chevron"/></summary><div className="dc-route-grid">{remaining.map(routeCard)}</div></details>}</> : <div className="dc-route-grid">{items.map(routeCard)}</div>}</details>;
    })}</section>}
    {dialog && matrix && <ApplyDialog matrix={matrix} provider={matrix.providers.find(item => item.id === dialog.provider)} capability={matrix.capabilities.find(item => item.id === dialog.capability)} busy={busy} error={error} editable={admin} close={() => { setDialog(null); setError(null); }} apply={changes => { void apply(changes); }}/>}
  </div>;
}
