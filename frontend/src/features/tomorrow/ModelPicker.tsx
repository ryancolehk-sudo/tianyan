import { useState } from 'react';
import { CodexReview } from './CodexReview';
import { ApiReview } from './ApiReview';
import type { PreferencePage } from './PlanPreferences';
import type { DirectReviewInput, CodexSetup, ModelConnection, ReportView, SettingsView } from './model';
import './reviewChoice.css';

type ModelSource = ModelConnection['source'];
type Method = ModelSource | 'rule';
type Choice = { method?: Method } & Partial<Record<ModelSource, string>>;
const methods = [
  { id: 'chatgpt_plan', name: 'Codex 账号', detail: '使用 ChatGPT 账号额度' },
  { id: 'api', name: '我的 API', detail: '使用自己绑定的 API Key' },
  { id: 'rule', name: '规则复盘', detail: '只用已归档的收盘资料' },
] as const;
const codexSteps: Record<CodexSetup, [string, string]> = {
  connect: ['登录 ChatGPT 账号并授权使用 Codex 模型。', '登录 Codex 账号'],
  authorize: ['此账号需要重新授权，完成后即可选择模型。', '重新授权 Codex'],
  choose_model: ['账号已授权。请选择或手填要用的模型 ID。', '选择 Codex 模型'],
  test_model: ['账号已有模型，先测试当前模型能否生成。', '测试 Codex 模型'],
  enable_model: ['模型已通过测试。打开「用于我的 AI 功能」即可使用。', '启用 Codex 模型'],
  check_model: ['请检查当前账号的模型与连接状态。', '配置 Codex 账号'],
  unavailable: ['请在账号设置中查看本机登录条件。', '查看 Codex 接入状态'],
  ready: ['请选择这个账号要使用的模型。', '管理 Codex 模型'],
};

export function useReviewChoice(owner: string, returnedToCodex = false) {
  const key = 'tianyan:tomorrow:method:' + owner;
  const [choice, setChoice] = useState<Choice>(() => {
    if (returnedToCodex) return { method: 'chatgpt_plan' };
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || '{}');
      if (methods.some(item => item.id === saved?.method)) return saved;
    } catch { /* The form also works without browser storage. */ }
    return {};
  });
  function choose(method: Method, modelId?: string) {
    const next = { ...choice, method };
    if (method !== 'rule' && modelId) next[method] = modelId;
    setChoice(next);
    try { sessionStorage.setItem(key, JSON.stringify(next)); } catch { /* Keep the in-memory choice. */ }
  }
  return { choice, choose };
}

export function reviewSetup(settings: SettingsView | null, choice: Choice) {
  const options = settings?.model_options ?? [];
  const preferredId = settings?.model_connection_id || settings?.default_connection_id;
  const preferred = options.find(item => item.id === preferredId);
  const method = choice.method ?? (settings?.use_ai === false ? 'rule' : preferred?.source);
  const models = options.filter(item => item.source === method);
  const rememberedId = method && method !== 'rule' ? choice[method] : undefined;
  const model = rememberedId ? models.find(item => item.id === rememberedId)
    : models.find(item => item.id === preferredId) ?? (models.length === 1 ? models[0] : undefined);
  const sources = settings?.sources;
  const count = settings?.ai_source_ids.filter(id => !sources || sources.some(s => s.id === id && s.ai_allowed)).length ?? 0;
  const ready = !!settings && (method === 'rule' || (!!model && !settings.model_gate && count > 0));
  return { method, model, models, count, ready, selectedApiId: choice.api ?? (preferred?.source === 'api' ? preferred.id : undefined) };
}

type Props = {
  setup: ReturnType<typeof reviewSetup>; settings: SettingsView | null; report?: ReportView | null;
  generateLabel: string; writable: boolean; busy: boolean; admin: boolean; error: string;
  csrf: string; returnedToCodex: boolean;
  choose: (method: Method, modelId?: string) => void; generate: (direct?: DirectReviewInput) => void;
  configure: (page: PreferencePage) => void; reload: () => void;
};

export function ModelPicker({ setup, settings, report, generateLabel, writable, busy, admin, error, choose, generate, configure, reload, csrf, returnedToCodex }: Props) {
  const { method, model, models, count, ready } = setup;
  const ai = method === 'chatgpt_plan' || method === 'api';
  const account = method === 'chatgpt_plan';
  const [catalogRequest, requestCatalog] = useState(Number(returnedToCodex));
  const directCodex = writable && account && !!settings?.codex_accounts?.length;
  const directApi = writable && method === 'api' && !!settings?.api_connections?.length;
  const direct = directCodex || directApi;
  const connectionStep = account ? codexSteps[settings?.codex_setup ?? 'check_model']
    : settings?.api_bound === false ? ['填写接口地址、API Key 和模型 ID 后即可测试。', '绑定 API']
    : ['可测试并启用已绑定的服务，也可添加新 API。', '绑定或启用 API'];
  const edit = () => configure(account ? 'account' : 'models');
  const disabled = !writable || busy || !settings;
  const origin = report?.generation_models?.join('、') || (report?.author_type === 'rule' ? '规则复盘' : report?.author_type === 'manual' ? '人工修订' : '');
  let nextAction;
  if (!settings) nextAction = !error && <span role="status">正在读取复盘设置…</span>;
  else if (!writable) nextAction = <span>当前账号仅可阅读预案。</span>;
  else if (!method) nextAction = <span>先选择本次复盘方式。</span>;
  else if (ai && !models.length) nextAction = <>
    <span>{connectionStep[0]}</span>
    <button className="pr-generate" disabled={disabled} onClick={edit}>{connectionStep[1]}</button>
  </>;
  else if (ai && !model) nextAction = <span>请选择这次要用的模型。</span>;
  else if (ai && settings?.model_gate) nextAction = <>
    <span>{settings.model_gate}。{!admin && '请联系管理员开启后再生成。'}</span>
    {admin && <button className="pr-generate" disabled={disabled} onClick={() => configure('enable')}>开启预案 AI</button>}
  </>;
  else if (ai && !count) nextAction = <>
    <span>请选择允许 AI 复盘使用的资料。</span>
    <button className="pr-generate" disabled={disabled} onClick={() => configure('sources')}>选择复盘资料</button>
  </>;
  else if (ready) nextAction = <>
    {ai ? <button className="pr-source-count" disabled={disabled} onClick={() => configure('sources')}>已授权 {count} 项资料 · 修改</button>
      : <span>按固定规则汇总行情，不消耗模型额度。</span>}
    <button className="pr-generate" disabled={disabled} onClick={() => generate()}
      title={origin ? '本篇使用：' + origin : undefined}>{busy ? '处理中…' : generateLabel}</button>
  </>;

  return <div className="pr-review-choice" role="group" aria-label="本次复盘方式">
    <div className="pr-choice-heading"><strong>本次复盘方式</strong>{origin && <small>当前稿件 · {origin}</small>}</div>
    <div className="pr-method-options">{methods.map(item => <button key={item.id} type="button"
      aria-label={item.name} aria-pressed={item.id === method} disabled={disabled} onClick={() => {
        if (item.id === 'chatgpt_plan') requestCatalog(n => n + 1);
        if (item.id === 'api') reload();
        choose(item.id);
      }}>
      <span className="pr-method-dot" aria-hidden="true"/><strong>{item.name}</strong><small>{item.detail}</small>
    </button>)}</div>
    {directCodex && settings && <CodexReview key={(report?.id ?? 'new') + ':' + settings.revision} accounts={settings.codex_accounts!} settings={settings}
      report={report} request={catalogRequest} csrf={csrf} admin={admin} busy={busy}
      generate={input => generate({codex_review: input})} editSources={() => configure('sources')}/>}
    {directApi && settings && <ApiReview key={(report?.id ?? 'new') + ':' + settings.revision} connections={settings.api_connections!}
      settings={settings} report={report} admin={admin} busy={busy} selectedId={setup.selectedApiId} select={id => choose('api', id)}
      manage={id => { choose('api', id); configure('models'); }} editSources={() => configure('sources')}
      generate={input => generate({api_review: input})}/>}
    {!direct && ai && !!models.length && <div className="pr-model-line">
      <label>{account ? 'Codex 模型' : 'API 模型'}<select aria-label={account ? 'Codex 模型' : 'API 模型'} value={model?.id ?? ''} disabled={disabled} onChange={event => choose(method, event.target.value)}>
        <option value="" disabled>请选择模型</option>
        {models.map(item => <option key={item.id} value={item.id}>{item.model}{item.name && item.name !== item.model ? ' · ' + item.name : ''}</option>)}
      </select></label>
      {writable && <button disabled={busy} onClick={edit}>{account ? '管理 Codex 模型' : '管理 API'}</button>}
    </div>}
    {!direct && <div className="pr-choice-next">{nextAction}</div>}
    {error && <div role="alert" className="pr-choice-error">{error} <button onClick={reload}>刷新状态</button></div>}
  </div>;
}
