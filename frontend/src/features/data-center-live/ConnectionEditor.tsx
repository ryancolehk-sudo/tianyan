import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { Icon } from '../../shared/icons';
import { usePrivateDraft } from '../../shared/privateDrafts';
import { SourceRuntime } from '../data/SourceRuntime';
import { connectionInput, credentialError, dataCenterApi } from './api';
import type { ConnectionIndex, ConnectionInput, ConnectionKind, ConnectionView, ProbeResult, Protocol } from './api';
import { duration, Empty, ErrorNotice, Panel, reasonLabel, Status, useResource } from './shared';

const modelPresets: { id: string; name: string; endpoint: string; protocol: Protocol; hint: string }[] = [
  { id: 'custom', name: '自定义', endpoint: '', protocol: 'openai_chat', hint: '自定义公网 HTTPS 接口' },
  { id: 'openai', name: 'OpenAI', endpoint: 'https://api.openai.com/v1', protocol: 'openai_chat', hint: 'api.openai.com' },
  { id: 'deepseek', name: 'DeepSeek', endpoint: 'https://api.deepseek.com', protocol: 'openai_chat', hint: 'api.deepseek.com' },
  { id: 'qwen', name: '通义千问', endpoint: 'https://dashscope.aliyuncs.com/compatible-mode/v1', protocol: 'openai_chat', hint: 'dashscope.aliyuncs.com' },
  { id: 'glm', name: '智谱 GLM', endpoint: 'https://open.bigmodel.cn/api/paas/v4', protocol: 'openai_chat', hint: 'open.bigmodel.cn' },
  { id: 'kimi', name: 'Kimi', endpoint: 'https://api.moonshot.cn/v1', protocol: 'openai_chat', hint: 'api.moonshot.cn' },
  { id: 'anthropic', name: 'Anthropic', endpoint: 'https://api.anthropic.com/v1', protocol: 'anthropic_messages', hint: 'api.anthropic.com' },
];

function DataCapabilities({ selected, locked, occupied, template, kind, onChange, onSources, version }: {
  selected: string[]; template: string[]; kind: ConnectionKind;
  onChange: (ids: string[]) => void; onSources: (names: Record<string, string>) => void;
  version: number; locked: string[]; occupied: Record<string, string>;
}) {
  const read = useResource(api.dataSources);
  const data = read.data?.data;
  const sources = data?.items.filter(source => source.provider.kind === kind && template.includes(source.family)) ?? [];
  useEffect(() => { if (version) read.reload(); }, [version, read.reload]);
  useEffect(() => { if (data) onSources(Object.fromEntries(data.items.map(source => [source.provider.id, source.provider.name]))); }, [data, onSources]);
  return <>
    <fieldset className="dc-source-picks"><legend>关联已登记的数据能力</legend>
      {read.error ? <ErrorNotice error={read.error} retry={read.reload}/> : !data && read.busy ? <p role="status">正在读取数据能力…</p>
        : sources.length ? <div className="dc-source-list">{sources.map(source => <label key={source.provider.id}><input type="checkbox" checked={selected.includes(source.provider.id)} disabled={locked.includes(source.provider.id) || !!occupied[source.provider.id]} onChange={event => onChange(event.target.checked ? [...selected, source.provider.id] : selected.filter(id => id !== source.provider.id))}/><span>{source.provider.name}<small>{occupied[source.provider.id] ? `已由 ${occupied[source.provider.id]} 管理` : source.family}</small></span><Status value={source.collection_state}/></label>)}</div>
          : <p className="dc-muted">还没有该类型的已登记能力，请先展开下方高级配置登记。</p>}
      {!!locked.length && <p className="dc-muted">已有能力保留关联；关闭单项请在下方能力配置中调整。</p>}
      {sources.some(source => occupied[source.provider.id]) && <p className="dc-muted">已有关联服务的能力可在左侧配置；更换归属前需移除原服务。</p>}
    </fieldset>
  </>;
}

function AdvancedDataSettings({ session, refresh }: { session: SessionResponse; refresh: () => void }) {
  const read = useResource(api.dataSources);
  return <details className="dc-advanced dc-legacy-source"><summary>高级数据配置 · 登记与采集范围<Icon name="chevron"/></summary>{read.data ? <SourceRuntime data={read.data.data} session={session} reload={() => { read.reload(); refresh(); }}/>
    : read.error ? <ErrorNotice error={read.error} retry={read.reload}/> : <Empty title="正在读取原有采集配置"/>}</details>;
}

export function ConnectionEditor({ initial, initialKind = 'model', simpleModel = false, index, session, onSaved, cancel, refresh, useForPlan }: {
  initial: ConnectionView | null; index: ConnectionIndex; session: SessionResponse;
  initialKind?: ConnectionKind; simpleModel?: boolean;
  onSaved: (item: ConnectionView) => void; cancel: () => void; refresh: () => void; useForPlan?: (id: string) => void;
}) {
  const [saved, setSaved] = useState(initial);
  const [kind, setKind] = useState<ConnectionKind>(initial?.kind ?? initialKind);
  const firstTemplate = index.templates.find(template => template.kind === (initial?.kind ?? initialKind));
  const [template, setTemplate] = useState(initial?.template ?? firstTemplate?.id ?? 'custom');
  const draftId = 'data-center:' + (initial?.id ?? initialKind + '-new');
  const [name, setName] = usePrivateDraft(session.data.user.id, draftId + ':name', () => initial?.name ?? '', false);
  const [endpoint, setEndpoint] = usePrivateDraft(session.data.user.id, draftId + ':endpoint', () => initial?.endpoint ?? firstTemplate?.endpoint ?? '', false);
  const [protocol, setProtocol] = usePrivateDraft<Protocol>(session.data.user.id, draftId + ':protocol', () => initial?.protocol ?? 'openai_chat', false);
  const [model, setModel] = usePrivateDraft(session.data.user.id, draftId + ':model', () => initial?.model ?? '', false);
  const [preset, setPreset] = usePrivateDraft(session.data.user.id, draftId + ':preset', () => modelPresets.find(item => item.endpoint && initial?.endpoint.startsWith(item.endpoint))?.id ?? 'custom', false);
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [sourceIds, setSourceIds] = useState(initial?.source_ids ?? []);
  const [sourceLabels, setSourceLabels] = useState<Record<string, string>>({});
  const [testSource, setTestSource] = useState(initial?.source_ids[0] ?? '');
  const [timeout, setTimeout] = usePrivateDraft(session.data.user.id, draftId + ':timeout', () => initial?.options.timeout_seconds ?? 60, false);
  const [maxTokens, setMaxTokens] = usePrivateDraft(session.data.user.id, draftId + ':max-tokens', () => initial?.options.max_output_tokens ?? 1200, false);
  const [inputPrice, setInputPrice] = usePrivateDraft(session.data.user.id, draftId + ':input-price', () => String(initial?.options.input_price_per_million_cny ?? ''), false);
  const [outputPrice, setOutputPrice] = usePrivateDraft(session.data.user.id, draftId + ':output-price', () => String(initial?.options.output_price_per_million_cny ?? ''), false);
  const [models, setModels] = useState<{ id: string; name?: string }[]>([]);
  const [busy, setBusy] = useState<'save' | 'test' | 'discover' | 'enable' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<ProbeResult | null>(null);
  const [notice, setNotice] = useState('');
  const [dirty, setDirty] = useState(!initial);
  const [dataVersion, setDataVersion] = useState(0);
  const admin = session.data.user.role === 'admin';
  const credentialBlocked = !!apiKey && !index.credential_store_ready;
  const savedHealth = index.items.find(item => item.id === saved?.id) ?? saved;
  const credentialIssue = savedHealth ? credentialError(savedHealth) : null;
  const currentTested = savedHealth && !credentialIssue && savedHealth.last_test?.status === 'success'
    && (savedHealth.tested_revision ?? savedHealth.last_test.revision) === savedHealth.revision && !dirty;
  const templates = index.templates.filter(item => item.kind === kind);
  const selectedTemplate = index.templates.find(item => item.id === template);
  const occupied = Object.fromEntries(index.items.filter(item => item.id !== saved?.id).flatMap(item => item.source_ids.map(id => [id, item.name])));
  function changed() { setDirty(true); setResult(null); setNotice(''); setError(null); }
  function changeKind(value: ConnectionKind) {
    const selected = index.templates.find(item => item.kind === value);
    setKind(value); setTemplate(selected?.id ?? 'custom'); setEndpoint(selected?.endpoint ?? '');
    setSourceIds([]); setTestSource(''); setModels([]); setApiKey(''); changed();
  }
  function body(forceDisabled = false): ConnectionInput {
    return { name: name.trim() || (simpleModel ? (modelPresets.find(item => item.id === preset)?.name ?? '自定义') + '模型' : ''), kind, template, endpoint: endpoint.trim(), protocol: kind === 'model' ? protocol : null,
      model: kind === 'model' ? model.trim() || null : null, ...(apiKey ? { api_key: apiKey } : {}),
      enabled: forceDisabled || !saved ? false : saved.enabled, source_ids: sourceIds,
      options: { timeout_seconds: timeout, max_output_tokens: maxTokens,
        ...(inputPrice === '' ? {} : { input_price_per_million_cny: Number(inputPrice) }),
        ...(outputPrice === '' ? {} : { output_price_per_million_cny: Number(outputPrice) }) },
      ...(saved ? { expected_revision: saved.revision } : {}) };
  }
  function configurationChanged() {
    if (!saved || apiKey) return true;
    return endpoint.trim() !== saved.endpoint || template !== saved.template || protocol !== (saved.protocol ?? 'openai_chat')
      || (kind === 'model' && model.trim() !== (saved.model ?? '')) || sourceIds.join(',') !== saved.source_ids.join(',')
      || timeout !== saved.options.timeout_seconds || maxTokens !== saved.options.max_output_tokens
      || (inputPrice === '' ? null : Number(inputPrice)) !== (saved.options.input_price_per_million_cny ?? null)
      || (outputPrice === '' ? null : Number(outputPrice)) !== (saved.options.output_price_per_million_cny ?? null);
  }
  async function persist(forceDisabled = false) {
    const response = await dataCenterApi.save(body(forceDisabled), session.data.csrf_token, saved?.id);
    setSaved(response.data); setApiKey(''); setShowKey(false); setDirty(false); onSaved(response.data);
    return response.data;
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return; setBusy('save'); setError(null); setResult(null);
    try {
      const value = await persist(configurationChanged());
      setNotice(!value.enabled ? `配置已保存。请${value.kind === 'model' ? '测试调用' : '检测当前配置'}，通过后在服务卡片启用。` : '配置已保存，实际运行结果见运行监控。');
    } catch (reason) { setError(reason); } finally { setBusy(null); }
  }
  async function test() {
    if (busy) return;
    const form = document.getElementById('dc-connection-form') as HTMLFormElement | null;
    if (!form?.reportValidity()) return;
    setBusy('test'); setError(null); setNotice(''); setResult(null);
    try {
      const connection = dirty || !saved ? await persist(configurationChanged()) : saved;
      const value = await dataCenterApi.probe({ ...connectionInput(connection), id: connection.id,
        ...(kind === 'model' ? {} : { source_id: testSource || connection.source_ids[0] }) }, session.data.csrf_token);
      setResult(value.data); refresh();
    } catch (reason) { setError(reason); } finally { setBusy(null); }
  }
  async function discover() {
    if (busy) return;
    if (!endpoint.trim()) { setError(new Error('请先填写接口地址。')); return; }
    if (credentialIssue && !apiKey) { setError(new Error(`${reasonLabel(credentialIssue)}。请先重新填写有效 API Key。`)); return; }
    setBusy('discover'); setError(null); setNotice(''); setResult(null);
    try {
      const value = await dataCenterApi.discover({ ...body(true), name: name.trim() || '模型列表检测', ...(saved ? { id: saved.id } : {}) }, session.data.csrf_token);
      setModels(value.data.models ?? []);
      if (value.data.status === 'success') setNotice(`已获取 ${value.data.models?.length ?? 0} 个模型。模型列表可读不代表生成调用已通过。`);
      else setResult(value.data);
    } catch (reason) { setError(reason); } finally { setBusy(null); }
  }
  async function activate() {
    if (busy || !currentTested || !savedHealth) return;
    setBusy('enable'); setError(null); setNotice('');
    try {
      const enabled = savedHealth.enabled ? savedHealth : (await dataCenterApi.save({ ...connectionInput(savedHealth), enabled: true }, session.data.csrf_token, savedHealth.id)).data;
      setSaved(enabled);
      if (useForPlan) { useForPlan(enabled.id); return; }
      const settings = await dataCenterApi.settings();
      await dataCenterApi.saveSettings({ enabled: true, default_connection_id: enabled.id, expected_revision: settings.data.revision }, session.data.csrf_token);
      setNotice(settings.data.personal_connection_id ? '模型已设为全局默认并启用。已选择的本人 ChatGPT 计划仍优先生效。' : '模型已设为全局默认并启用，后续手动 AI 任务使用此配置。'); refresh();
    } catch (reason) { setError(reason); refresh(); } finally { setBusy(null); }
  }
  function choosePreset(value: string) {
    const selected = modelPresets.find(item => item.id === value)!;
    const previous = modelPresets.find(item => item.id === preset);
    setPreset(value); setEndpoint(selected.endpoint); setProtocol(selected.protocol); setModels([]); setApiKey(''); setShowKey(false);
    if (!name || name === previous?.name + '模型') setName(selected.name + '模型');
    changed();
  }
  return <Panel title={simpleModel ? saved ? '配置 AI 服务' : '连接 AI 服务' : saved ? '配置接入' : '添加接入'} className={'dc-editor' + (simpleModel ? ' dc-simple-model' : '')} action={saved && <button type="button" className="dc-text-button" onClick={cancel} disabled={!!busy}><Icon name="plus"/>新增</button>}>
    <p className="dc-editor-hint">{simpleModel ? '选择服务商 → 填写配置 → 测试并保存' : '选择类型后填写对应配置。'}</p>
    {simpleModel && <div className="dc-model-presets" role="group" aria-label="AI 服务商">{modelPresets.map(item => <button type="button" key={item.id} className={preset === item.id ? 'selected' : ''} aria-pressed={preset === item.id} disabled={!admin || !!busy} onClick={() => choosePreset(item.id)}><strong>{item.name}</strong><small>{item.hint}</small></button>)}</div>}
    {!admin && <div className="dc-alert">当前账号可查看配置，修改需要管理员角色。</div>}
    {!index.credential_store_ready && <div className="dc-alert dc-alert-warn"><Icon name="shield"/><span>凭据存储尚未就绪；请完成本机密钥挂载后保存新的 API Key。</span></div>}
    <form id="dc-connection-form" onSubmit={submit} autoComplete="off">
      <fieldset disabled={!admin || !!busy} className="dc-form-fields">
        {!simpleModel && <label>接入类型<select aria-label="接入类型" value={kind} disabled={!!saved} onChange={event => changeKind(event.target.value as ConnectionKind)}><option value="model">AI 模型</option><option value="market">行情数据</option><option value="news">新闻资讯</option></select><small>支持 AI 模型、行情数据、新闻资讯。</small></label>}
        {!simpleModel && (kind !== 'model' || templates.length > 1) && <label>供应商模板<select value={template} disabled={!!saved} onChange={event => { const item = index.templates.find(row => row.id === event.target.value); setTemplate(event.target.value); setEndpoint(item?.endpoint ?? ''); setSourceIds([]); setTestSource(''); changed(); }}>{templates.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}{!templates.length && <option value={template}>暂无可用模板</option>}</select></label>}
        {!simpleModel && <label>服务名称<input value={name} onChange={event => { setName(event.target.value); changed(); }} placeholder={kind === 'model' ? '例如：我的研究模型' : '例如：我的行情服务'} required maxLength={120}/></label>}
        <label>接口地址{kind !== 'model' && (selectedTemplate?.endpoints?.length ?? 0) > 1 ? <select aria-label="接口地址" value={endpoint} onChange={event => { setEndpoint(event.target.value); changed(); }}>{selectedTemplate?.endpoints?.map(url => <option value={url} key={url}>{url}</option>)}</select> : <input aria-label="接口地址" value={endpoint} type="url" onChange={event => { setEndpoint(event.target.value); changed(); }} placeholder="https://api.example.com/v1" required readOnly={kind !== 'model'}/>}{kind !== 'model' && <small>由供应商模板定义；具体能力与使用范围单独登记。</small>}</label>
        <label>API Key<span className="dc-key-input"><input aria-label="API Key" type={showKey ? 'text' : 'password'} value={apiKey} onChange={event => { setApiKey(event.target.value); changed(); }} required={!!credentialIssue} placeholder={credentialIssue ? '凭据缺失或失效，请重新填写 API Key' : saved?.credential_configured ? '已安全保存；留空保留原 Key' : '填写供应商提供的 API Key'} autoComplete="new-password" spellCheck={false} maxLength={4096}/><button type="button" aria-label={showKey ? '隐藏新输入的 API Key' : '显示新输入的 API Key'} onClick={() => setShowKey(!showKey)}><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button></span>{credentialIssue ? <small className="dc-bad">{reasonLabel(credentialIssue)} · 需重新填写并保存有效 API Key。</small> : saved?.credential_configured && <small>{saved.credential_origin === 'environment' ? '当前使用本机环境凭据' : '凭据已加密保存'} · 已保存的 Key 不回显</small>}</label>
        {kind === 'model' ? <>
          {!simpleModel && <label>接口协议<select value={protocol} onChange={event => { setProtocol(event.target.value as Protocol); setModels([]); changed(); }}><option value="openai_chat">OpenAI Chat Completions</option><option value="openai_responses">OpenAI Responses</option><option value="anthropic_messages">Anthropic Messages</option></select></label>}
          <label>模型名称<span className="dc-model-input"><input aria-label="模型名称" list="dc-model-options" value={model} onChange={event => { setModel(event.target.value); changed(); }} placeholder="选择或输入模型 ID" required maxLength={200}/><button type="button" className="dc-button-gold" onClick={discover} disabled={credentialBlocked}>{busy === 'discover' ? '获取中…' : '获取模型'}</button></span><datalist id="dc-model-options">{models.map(item => <option key={item.id} value={item.id}>{item.name ?? item.id}</option>)}</datalist></label>
          <details className="dc-advanced"><summary>高级设置<Icon name="chevron"/></summary><div className="dc-advanced-fields">{simpleModel && <><label>服务名称<input value={name} onChange={event => { setName(event.target.value); changed(); }} placeholder={(modelPresets.find(item => item.id === preset)?.name ?? '自定义') + '模型'} maxLength={120}/></label><label>接口协议<select value={protocol} onChange={event => { setProtocol(event.target.value as Protocol); setModels([]); changed(); }}><option value="openai_chat">OpenAI Chat Completions</option><option value="openai_responses">OpenAI Responses</option><option value="anthropic_messages">Anthropic Messages</option></select></label></>}<label>请求超时（秒）<input type="number" min={5} max={180} value={timeout} onChange={event => { setTimeout(Number(event.target.value)); changed(); }} required/></label><label>生成上限（tokens）<input type="number" min={32} max={8192} value={maxTokens} onChange={event => { setMaxTokens(Number(event.target.value)); changed(); }} required/></label><label>输入单价（元 / 百万 tokens）<input type="number" min={0} max={100000} step="any" value={inputPrice} onChange={event => { setInputPrice(event.target.value); changed(); }} placeholder="未设置则费用未知"/></label><label>输出单价（元 / 百万 tokens）<input type="number" min={0} max={100000} step="any" value={outputPrice} onChange={event => { setOutputPrice(event.target.value); changed(); }} placeholder="未设置则费用未知"/></label></div></details>
        </> : <>
          {template === 'ifind_mcp' ? <p>八个金融领域使用独立手动研究入口，不开启后台轮询。连接检测只读取指数板块工具目录；取数权限需逐项验证。</p> : <DataCapabilities selected={sourceIds} locked={saved?.source_ids ?? []} occupied={occupied} onSources={setSourceLabels} template={selectedTemplate?.source_families ?? (template === 'tushare' ? index.items.filter(item => item.template === template).flatMap(item => item.capabilities?.map(capability => capability.family) ?? []) : [template])} kind={kind} version={dataVersion} onChange={ids => { setSourceIds(ids); if (!ids.includes(testSource)) setTestSource(ids[0] ?? ''); changed(); }}/>}
          {sourceIds.length > 1 && <label>本次检测能力<select value={testSource || sourceIds[0]} onChange={event => setTestSource(event.target.value)}>{sourceIds.map(id => <option value={id} key={id}>{sourceLabels[id] ?? index.items.flatMap(item => item.capabilities ?? []).find(item => item.source_id === id)?.name ?? id}</option>)}</select><small>每次只检测选定的这一项，不代表其他能力已验证。</small></label>}
        </>}
      </fieldset>
      {error !== null && <ErrorNotice error={error}/>}
      {notice && <div className="dc-feedback" role="status">{notice}</div>}
      {result && <div className={'dc-probe dc-probe-' + result.status} role="status"><div><Status value={result.status}/><span>{duration(result.elapsed_ms)} · 实际请求 {result.request_count} 次</span></div><p>{result.status === 'queued' ? '指定单项能力检测已排队，实际结果见调用记录。' : result.message}</p>{result.preview && <pre>{result.preview}</pre>}{result.error_code && <small>原因：{result.error_code}</small>}{result.job_id && <small>任务：{result.job_id}</small>}</div>}
      <div className="dc-form-actions"><button type="button" onClick={test} disabled={!admin || !!busy || credentialBlocked || (kind !== 'model' && template !== 'ifind_mcp' && !sourceIds.length)}><span className="dc-play"/>{busy === 'test' ? '正在检测…' : kind === 'model' ? simpleModel ? '测试并保存' : '测试调用' : template === 'ifind_mcp' ? '检测iFinD连接' : '检测单项能力'}</button><button type="submit" className="dc-primary" disabled={!admin || !!busy || credentialBlocked || (kind !== 'model' && template !== 'ifind_mcp' && !sourceIds.length)}>{busy === 'save' ? '正在保存…' : '保存接入'}</button>{simpleModel && <button type="button" className="dc-button-gold" onClick={() => { void activate(); }} disabled={!admin || !!busy || !currentTested}>{busy === 'enable' ? '正在启用…' : useForPlan ? '选中并返回预案' : '设为默认并启用'}</button>}</div>
      <p className="dc-form-footnote">{kind === 'model' ? '首次保存默认为未启用；测试会进行一次有上限的短文本生成。' : template === 'ifind_mcp' ? '保存不查询数据。检测仅验证指数工具目录；启用后在iFinD研究面板手动取数。' : '保存不会直接发起采集。检测由行情 worker 按现有范围与限流执行。'}</p>
    </form>
    {kind !== 'model' && <AdvancedDataSettings session={session} refresh={() => { setDataVersion(version => version + 1); refresh(); }}/>}
  </Panel>;
}
