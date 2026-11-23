import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api, ApiError } from '../../shared/api';
import { Icon } from '../../shared/icons';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { clearPrivateDrafts } from '../../shared/privateDrafts';
import { connectionInput, credentialError, dataCenterApi } from './api';
import type { AiSettings, ConnectionView, FeatureBinding } from './api';
import { ConnectionEditor } from './ConnectionEditor';
import { DataSources } from './DataSources';
import { IfindResearch } from './IfindResearch';
import { Monitor } from './Monitor';
import { ChatGPTAccounts } from '../chatgpt/ChatGPTAccounts';
import { useRecoveryRead } from '../../shared/RecoveryHealth';
import { dateTime, duration, Empty, ErrorNotice, kindNames, messageOf, Panel, reasonLabel, Status, statusLabel, Toggle, useResource } from './shared';
import './dataCenterLive.css';

function tested(item: ConnectionView) {
  return !credentialError(item) && item.last_test?.status === 'success' && (item.tested_revision ?? item.last_test.revision) === item.revision;
}
function connectionState(item: ConnectionView) {
  const credentialIssue = credentialError(item);
  if (credentialIssue) return 'unavailable';
  if (!item.enabled) return 'disabled';
  if (item.kind !== 'model') {
    const enabled = (item.capabilities ?? []).filter(capability => capability.enabled);
    if (!enabled.length) return item.capabilities?.length ? 'disabled' : 'not_checked';
    if (enabled.every(capability => capability.collection_enabled === false)) return 'collection_disabled';
    if (enabled.every(capability => capability.state === 'ready')) return 'ready';
    const errors = enabled.filter(capability => ['error', 'unavailable', 'credentials_missing', 'permission_missing'].includes(capability.state));
    if (errors.length === enabled.length) return 'error';
    if (errors.length) return 'partial';
    const states = [...new Set(enabled.map(capability => capability.state))];
    return states.length === 1 ? states[0] : 'partial';
  }
  if (item.last_test && item.last_test.revision !== item.revision && item.kind === 'model') return 'stale';
  if (item.last_test?.status) return item.last_test.status;
  return 'not_checked';
}

function ServiceCard({ item, settings, busy, admin, configure, test, toggle, remove, setDefault }: {
  item: ConnectionView; settings: AiSettings | undefined; busy: boolean; admin: boolean;
  configure: () => void; test: () => void; toggle: () => void; remove: () => void; setDefault: () => void;
}) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const isDefault = settings?.default_connection_id === item.id;
  const isFallback = settings?.features.some(feature => feature.fallback_connection_id === item.id);
  const state = connectionState(item);
  const credentialIssue = credentialError(item);
  const partialErrors = item.kind !== 'model' && state === 'partial' && item.capabilities?.some(capability => capability.enabled && ['error', 'unavailable', 'credentials_missing', 'permission_missing'].includes(capability.state));
  return <article className="dc-service-card">
    <div className="dc-service-main"><span className={'dc-provider-icon dc-provider-' + item.kind}><Icon name={item.kind === 'model' ? 'data' : item.kind === 'market' ? 'chart' : 'news'}/></span><div className="dc-service-name"><div><h3>{item.name}</h3><span className="dc-tag">{kindNames[item.kind]}</span>{isDefault && <span className="dc-tag dc-tag-gold">全局默认</span>}{!isDefault && isFallback && <span className="dc-tag">备用</span>}</div><p title={item.kind === 'model' ? item.model ?? '' : item.capabilities?.map(capability => capability.name).join(' / ')}>{item.kind === 'model' ? item.model || '尚未指定模型' : item.capabilities?.map(capability => capability.name).join(' · ') || '尚未关联数据能力'}</p><Status value={state} label={credentialIssue ? '凭据不可用' : partialErrors ? '部分异常' : item.enabled && item.kind === 'model' && state === 'success' ? '调用通过' : undefined}/></div></div>
    {credentialIssue && <p className="dc-credential-error" role="status">{reasonLabel(credentialIssue)}。请重新填写有效 API Key。</p>}
    <div className="dc-service-actions"><button type="button" onClick={configure} disabled={busy}><Icon name="research"/>配置</button><button type="button" onClick={test} disabled={!admin || busy}><span className="dc-play"/>{item.kind === 'model' ? '测试' : '检测单项'}</button></div>
    <div className="dc-service-meta"><label className="dc-switch-label"><Toggle label={`${item.enabled ? '停用' : '启用'}${item.name}`} checked={item.enabled} disabled={!admin || busy || (!item.enabled && item.kind === 'model' && !tested(item))} onChange={toggle}/><span>{item.enabled ? '已启用' : '未启用'}</span></label>{item.kind === 'model' && !isDefault && <button type="button" className="dc-link" onClick={setDefault} disabled={!admin || busy || !item.enabled || !tested(item)}>☆ 设为默认</button>}<button type="button" className="dc-remove" onClick={() => setConfirmRemove(!confirmRemove)} disabled={!admin || busy} aria-label={`移除${item.name}`}>移除</button></div>
    {item.last_test && <p className="dc-card-test">最近检测 {dateTime(item.last_test.at, true)} · {duration(item.last_test.elapsed_ms)}{item.last_test.error_code ? ` · ${item.last_test.error_code}` : ''}{item.last_test.revision !== item.revision && ' · 旧配置结果'}</p>}
    {!!item.capabilities?.length && <details className="dc-capabilities"><summary><span>查看 {item.capabilities.length} 项数据能力</span>{item.last_test?.status === 'success' && item.last_test.revision === item.revision && <span className="dc-single-test">最近单项通过</span>}<Icon name="chevron"/></summary>{item.capabilities.map(capability => <div key={capability.source_id}><strong>{capability.name}</strong><Status value={capability.state}/><small>数据 {dateTime(capability.data_time)}<br/>成功 {dateTime(capability.last_success_at)} · 间隔 {capability.interval_seconds} 秒</small></div>)}</details>}
    {confirmRemove && <div className="dc-remove-confirm"><span>移除后停止后续调用，保留历史记录。</span><button type="button" disabled={busy} onClick={remove}>确认移除</button><button type="button" onClick={() => setConfirmRemove(false)}>取消</button></div>}
  </article>;
}

function FeatureBindings({ settings, models, personalModels, admin, busy, update }: {
  settings: AiSettings; models: ConnectionView[]; personalModels: ConnectionView[]; admin: boolean; busy: boolean;
  update: (feature: FeatureBinding, changes: Partial<Pick<FeatureBinding, 'enabled' | 'connection_id' | 'fallback_connection_id'>>) => void;
}) {
  const usable = (model: ConnectionView) => model.enabled && tested(model);
  const effectiveName = (feature: FeatureBinding) => {
    if (!feature.implemented) return '业务待接入';
    const effective = [...personalModels, ...models].find(model => model.id === feature.effective_connection_id);
    return effective ? `${effective.name}${effective.owner_id ? ` · ${effective.model || '模型待配置'}（本人计划）` : ''}`
      : settings.personal_connection_id && feature.implemented ? '本人 ChatGPT（模型或授权待确认）' : '尚未配置';
  };
  return <Panel title="AI 功能使用模型" className="dc-bindings" action={<span className="dc-muted">{settings.personal_connection_id ? "本人已选择 ChatGPT 计划；功能开关仍然生效。" : "默认跟随全局，可按功能调整。"}</span>}><div className="dc-table-wrap" role="region" tabIndex={0} aria-label="AI功能使用模型"><table className="dc-table"><thead><tr><th scope="col">所属模块</th><th scope="col">AI 功能</th><th scope="col">使用模型</th><th scope="col">启用</th></tr></thead><tbody>{settings.features.map(feature => <tr key={feature.id}><td>{feature.module}</td><td>{feature.name}{!feature.implemented && <small className="dc-cell-note">业务待接入</small>}</td><td><select aria-label={`${feature.name}使用模型`} value={feature.connection_id ?? ''} disabled={!admin || busy || !feature.implemented} onChange={event => update(feature, { connection_id: event.target.value || null })}><option value="">跟随默认</option>{models.map(model => <option value={model.id} key={model.id} disabled={!usable(model)}>{model.name} · {model.model}{!usable(model) ? '（未启用或未验证）' : ''}</option>)}</select><small className="dc-cell-note">实际：{effectiveName(feature)}{!settings.enabled && feature.implemented ? ' · 全局已关闭' : ''}</small></td><td><Toggle label={`${feature.name}启用开关`} checked={feature.implemented && feature.enabled} onChange={() => update(feature, { enabled: !feature.enabled })} disabled={!admin || busy || !feature.implemented}/></td></tr>)}</tbody></table></div><details className="dc-fallback"><summary>备用服务设置 · 默认关闭<Icon name="chevron"/></summary><p>仅为已实现的功能指定备用。失败处理遵循后台重试规则，结果不明时不自动重发。{settings.personal_connection_id && " 本人 ChatGPT 计划请求不使用此 API 备用配置，失败后不会自动切换计费路径。"}</p>{settings.features.filter(feature => feature.implemented).map(feature => <label key={feature.id}><span>{feature.name}</span><select aria-label={`${feature.name}备用服务`} value={feature.fallback_connection_id ?? ''} disabled={!admin || busy} onChange={event => update(feature, { fallback_connection_id: event.target.value || null })}><option value="">关闭备用</option>{models.filter(model => model.id !== feature.effective_connection_id).map(model => <option key={model.id} value={model.id} disabled={!usable(model)}>{model.name} · {model.model}</option>)}</select></label>)}</details></Panel>;
}

export function DataCenterLive({ session, legacyTools }: { session: SessionResponse; legacyTools: ReactNode }) {
  const recovery = useRecoveryRead();
  const navigation = useNavigation();
  const queryView = new URLSearchParams(navigation.search).get('view') ?? 'connections';
  const toolsOpen = !['connections', 'data-sources', 'ai', 'monitor'].includes(queryView);
  const view = queryView === 'monitor' ? 'monitor' : queryView === 'ai' ? 'ai' : 'connections';
  const connections = useResource(dataCenterApi.integrations);
  const settings = useResource(dataCenterApi.settings);
  const overview = useResource(dataCenterApi.overview);
  const [page, setPage] = useState(0);
  const [editing, setEditing] = useState<ConnectionView | null>(null);
  const [editorVersion, setEditorVersion] = useState(0);
  const [dataEditing, setDataEditing] = useState<ConnectionView | null>(null);
  const [dataEditorVersion, setDataEditorVersion] = useState(0);
  const [advancedDataOpen, setAdvancedDataOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [refreshVersion, setRefreshVersion] = useState(0);
  const mutationLock = useRef(false);
  const mounted = useRef(true);
  const editorTop = useRef<HTMLDivElement>(null);
  const dataEditorTop = useRef<HTMLDivElement>(null);
  const admin = session.data.user.role === 'admin';
  const index = connections.data?.data;
  const ai = settings.data?.data;
  const items = index?.items ?? [];
  const models = items.filter(item => item.kind === 'model' && !item.owner_id);
  const personalModels = items.filter(item => item.kind === 'model' && item.owner_id === session.data.user.id);
  const defaultModel = models.find(item => item.id === ai?.default_connection_id);
  const defaultCredentialIssue = defaultModel ? credentialError(defaultModel) : null;
  const filtered = models;
  const maxPage = Math.max(0, Math.ceil(filtered.length / 4) - 1);
  const visiblePage = Math.min(page, maxPage);
  const visible = filtered.slice(visiblePage * 4, (visiblePage + 1) * 4);
  const active = (overview.data?.data.running_tasks ?? 0) > 0
    || !!overview.data?.data.tasks.some(task => ['queued', 'running', 'retry_wait', 'started', 'generating'].includes(task.state))
    || items.some(item => ['queued', 'running'].includes(item.last_test?.status ?? ''));
  const refresh = useCallback(() => { connections.reload(); settings.reload(); overview.reload(); setRefreshVersion(version => version + 1); }, [connections.reload, settings.reload, overview.reload]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    let timer: number | undefined;
    function schedule() {
      window.clearTimeout(timer);
      if (document.visibilityState === 'hidden') return;
      timer = window.setTimeout(() => { refresh(); schedule(); }, active ? 5000 : 30000);
    }
    function visibility() { if (document.visibilityState === 'visible') refresh(); schedule(); }
    schedule(); document.addEventListener('visibilitychange', visibility);
    return () => { window.clearTimeout(timer); document.removeEventListener('visibilitychange', visibility); };
  }, [active, refresh]);
  function changeView(value: string) { const url = new URL(location.href); url.searchParams.set('view', value); updateLocation(url); }
  function configure(id: string | null, initialKind: 'market' | 'model' = 'model') {
    const item = items.find(row => row.id === id) ?? null;
    if (item && item.kind !== 'model' || !item && initialKind === 'market') {
      setDataEditing(item); setDataEditorVersion(version => version + 1); setAdvancedDataOpen(true); setError(null); setNotice('');
      if (view !== 'connections' || toolsOpen) changeView('connections');
      window.setTimeout(() => dataEditorTop.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
      return;
    }
    if (item?.owner_id) {
      setEditing(null); setEditorVersion(version => version + 1); setError(null);
      setNotice('该调用使用本人 ChatGPT 账号，请在“我的 ChatGPT”中检查授权与模型。');
      if (view !== 'ai' || toolsOpen) changeView('ai');
      window.setTimeout(() => document.querySelector<HTMLElement>('.cg-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
      return;
    }
    setEditing(item); setEditorVersion(version => version + 1); setError(null); setNotice('');
    if (view !== 'ai' || toolsOpen) changeView('ai');
    window.setTimeout(() => editorTop.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
  }
  async function act(action: () => Promise<void>, success?: string) {
    if (mutationLock.current) return;
    mutationLock.current = true; setBusy(true); setError(null); setNotice('');
    try { await action(); if (mounted.current) { if (success) setNotice(success); refresh(); } }
    catch (reason) { if (mounted.current) setError(reason); }
    finally { mutationLock.current = false; if (mounted.current) setBusy(false); }
  }
  function saveSettings(changes: { enabled?: boolean; default_connection_id?: string | null }) {
    void act(async () => { const result = await dataCenterApi.saveSettings({ ...changes, expected_revision: ai?.revision }, session.data.csrf_token); settings.setData(result); }, '全局模型设置已保存，对后续任务生效。');
  }
  function binding(feature: FeatureBinding, changes: Partial<Pick<FeatureBinding, 'enabled' | 'connection_id' | 'fallback_connection_id'>>) {
    void act(async () => { const result = await dataCenterApi.saveBinding(feature.id, { enabled: feature.enabled, connection_id: feature.connection_id, fallback_connection_id: feature.fallback_connection_id, ...changes }, session.data.csrf_token); settings.setData(result); }, '功能模型设置已保存，对后续任务生效。');
  }
  function probe(item: ConnectionView) {
    void act(async () => {
      const response = await dataCenterApi.probe({ ...connectionInput(item), id: item.id, ...(item.kind === 'model' ? {} : { source_id: item.source_ids[0] }) }, session.data.csrf_token);
      const result = response.data;
      setNotice(result.status === 'queued' ? '指定单项能力检测已排队，实际结果见运行监控和调用记录。' : `${item.name}：${result.message}（${duration(result.elapsed_ms)}，实际请求 ${result.request_count} 次）`);
    });
  }
  async function logout() {
    if (loggingOut) return;
    setLoggingOut(true); setError(null);
    try {
      await api.logout(session.data.csrf_token);
      clearPrivateDrafts();
      window.location.reload();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        clearPrivateDrafts();
        window.location.reload();
      } else {
        setError(new Error('退出账号未完成：' + messageOf(reason)));
        setLoggingOut(false);
      }
    }
  }
  return <div className="dc-live">
    <header className="dc-page-heading"><div><h1>数据管理</h1><p>{toolsOpen ? '研究材料、快照与数据工具。' : view === 'monitor' ? '当前运行、按来源与资料日回查核验记录。' : view === 'ai' ? '选择服务商，测试并启用模型。' : '看清费用与权限，按能力选择数据来源。'}</p></div><div className="dc-page-actions"><div id="dc-notifications"/><span className="dc-instance"><i/>{session.meta.app_mode === 'demo' ? '演示环境' : '本机实际状态'}</span>{view === 'monitor' && <button type="button" disabled={overview.busy} onClick={() => { refresh(); recovery?.reload(); }}><Icon name="refresh"/>刷新状态</button>}<button type="button" className="dc-desktop-logout" onClick={() => { void logout(); }} disabled={loggingOut}><Icon name="logout"/>{loggingOut ? '正在退出…' : '退出账号'}</button></div></header>
    <div className="dc-tabs"><nav aria-label="数据管理页面">{[['connections', '数据来源'], ['ai', 'AI 设置'], ['monitor', '运行监控']].map(([value, label]) => <button key={value} type="button" className={view === value && !toolsOpen ? 'selected' : ''} aria-current={view === value && !toolsOpen ? 'page' : undefined} onClick={() => changeView(value)}>{label}</button>)}</nav><button type="button" className={'dc-tools-button' + (toolsOpen ? ' selected' : '')} onClick={() => changeView(toolsOpen ? 'connections' : 'sources')}>{toolsOpen ? '返回数据来源' : '更多数据工具'}<Icon name="chevron"/></button></div>
    {error !== null && <ErrorNotice error={error}/>}{notice && <div className="dc-feedback" role="status">{notice}</div>}
    {toolsOpen && <div className="dc-legacy-tools">{legacyTools}</div>}
    {!toolsOpen && view === 'monitor' && <Monitor overview={overview.data?.data} connections={items} loading={overview.busy} error={overview.error} refresh={refresh} configure={configure} refreshVersion={refreshVersion}/>}
    <div hidden={toolsOpen || view !== 'connections'}>
      <details className="dc-advanced"><summary>iFinD 官方金融研究 · 八个领域 · 手动查询</summary><IfindResearch connections={items} session={session} configure={id => configure(id, 'market')}/></details>
      <DataSources session={session} index={index ?? null} refreshVersion={refreshVersion} refresh={refresh} configure={id => configure(id, 'market')} openAi={() => changeView('ai')} active={!toolsOpen && view === 'connections'}/>
      <details className="dc-registered-data dc-advanced" open={advancedDataOpen} onToggle={event => setAdvancedDataOpen(event.currentTarget.open)}><summary>其他来源与高级采集配置<Icon name="chevron"/></summary><p className="dc-muted">保留已登记来源、采集范围和历史记录；新增供应商需使用后台已有适配器。</p>
        <div className="dc-services-grid">{items.filter(item => item.kind !== 'model').map(item => <ServiceCard key={item.id} item={item} settings={ai} admin={admin} busy={busy} configure={() => configure(item.id)} test={() => probe(item)} toggle={() => { void act(async () => { await dataCenterApi.save({ ...connectionInput(item), enabled: !item.enabled }, session.data.csrf_token, item.id); }, item.enabled ? '服务已停用。' : '服务已启用。'); }} remove={() => { void act(async () => { await dataCenterApi.remove(item.id, session.data.csrf_token); }, '服务已移除，历史记录保留。'); }} setDefault={() => undefined}/>)}</div>
        <div ref={dataEditorTop}>{index && advancedDataOpen && <ConnectionEditor key={'data-' + dataEditorVersion} initial={dataEditing} initialKind="market" index={index} session={session} refresh={refresh} onSaved={refresh} cancel={() => configure(null, 'market')}/>}</div>
      </details>
    </div>
    <div hidden={toolsOpen || view !== 'ai'} className="dc-ai-page">
      <Panel title="AI 服务" className="dc-default-panel"><div className="dc-default-content"><div className="dc-default-choice"><label className="sr-only" htmlFor="dc-default-model">全局默认模型</label><select id="dc-default-model" value={ai?.default_connection_id ?? ''} disabled={!admin || busy || !ai} onChange={event => saveSettings({ default_connection_id: event.target.value || null })}><option value="">选择已验证并启用的模型</option>{models.map(item => <option key={item.id} value={item.id} disabled={!item.enabled || !tested(item)}>{item.name} · {item.model}{!item.enabled || !tested(item) ? '（未启用或未验证）' : ''}</option>)}</select><p>默认模型用于已启用的 AI 功能。</p></div><div className="dc-global-switch"><label className="dc-switch-label"><Toggle label="全局 AI 开关" checked={ai?.enabled ?? false} disabled={!admin || busy || !ai || (!ai.enabled && !ai.personal_model_ready && (!defaultModel?.enabled || !tested(defaultModel)))} onChange={() => saveSettings({ enabled: !ai?.enabled })}/><span>全局 AI {ai?.enabled ? '已开启' : '已关闭'}</span></label><Status value={defaultModel ? connectionState(defaultModel) : 'not_checked'} label={defaultCredentialIssue ? statusLabel(defaultCredentialIssue) : defaultModel ? tested(defaultModel) ? '最近调用通过' : '默认模型待检测' : '尚未配置默认模型'}/></div></div>{settings.error !== null && <ErrorNotice error={settings.error} retry={settings.reload}/>}<p className="dc-default-note">{!ai && settings.busy ? '正在读取模型设置…' : ai?.personal_connection_id ? '当前优先使用本人选择的 ChatGPT 计划；失效时不会自动切换到 API 计费路径。' : !ai?.enabled ? '填好配置并测试成功后，点击“设为默认并启用”。' : '业务生成由手动触发，刷新页面不调用模型。'}</p></Panel>
      <div ref={editorTop}>{index ? <ConnectionEditor key={'model-' + editorVersion} initial={editing} initialKind="model" simpleModel index={index} session={session} refresh={refresh} onSaved={refresh} cancel={() => configure(null)}/> : <Panel title="添加 AI 服务">{connections.error !== null ? <ErrorNotice error={connections.error} retry={connections.reload}/> : <div className="dc-loading" role="status">正在读取接入模板…</div>}</Panel>}</div>
      <Panel title="已接入模型" className="dc-services-panel" action={<button type="button" onClick={() => configure(null)} disabled={busy}><Icon name="plus"/>添加模型</button>}>
        {connections.error !== null && <ErrorNotice error={connections.error} retry={connections.reload}/>}
        <div className="dc-services-grid">{visible.map(item => <ServiceCard key={item.id} item={item} settings={ai} admin={admin} busy={busy} configure={() => configure(item.id)} test={() => probe(item)} toggle={() => { void act(async () => { await dataCenterApi.save({ ...connectionInput(item), enabled: !item.enabled }, session.data.csrf_token, item.id); }, item.enabled ? '服务已停用，后续任务不再使用该服务。' : '服务已启用。'); }} remove={() => { void act(async () => { await dataCenterApi.remove(item.id, session.data.csrf_token); if (editing?.id === item.id) { setEditing(null); setEditorVersion(version => version + 1); } }, '服务已移除，历史记录保留。'); }} setDefault={() => saveSettings({ default_connection_id: item.id })}/>)}</div>
        {!index && connections.busy ? <div className="dc-loading" role="status">正在读取已接入模型…</div> : !visible.length && !connections.error && <Empty title="还没有接入 API 模型">在上方选择服务商并填写配置。</Empty>}
        {filtered.length > 4 && <div className="dc-pagination"><span>共 {filtered.length} 项 · {visiblePage + 1} / {maxPage + 1}</span><button type="button" disabled={visiblePage === 0} onClick={() => setPage(visiblePage - 1)}>上一页</button><button type="button" disabled={visiblePage === maxPage} onClick={() => setPage(visiblePage + 1)}>下一页</button></div>}
      </Panel>
      {!toolsOpen && view === 'ai' && <ChatGPTAccounts key={session.data.user.id} session={session} refreshVersion={refreshVersion} onChanged={refresh}/>}
      <details className="dc-feature-bindings dc-advanced"><summary>按功能单独选择模型<Icon name="chevron"/></summary>{ai ? <FeatureBindings settings={ai} models={models} personalModels={personalModels} busy={busy} admin={admin} update={binding}/> : <Panel title="AI 功能使用模型">{settings.error !== null ? <ErrorNotice error={settings.error} retry={settings.reload}/> : <div className="dc-loading" role="status">正在读取功能绑定…</div>}</Panel>}</details>
    </div>
  </div>;
}
