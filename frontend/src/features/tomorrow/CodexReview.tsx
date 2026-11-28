import { useEffect, useState } from 'react';
import { api } from '../../shared/api';
import type { CodexReviewInput, PlanAccount } from './model';
import { ReviewMaterials, type ReviewControls } from './ReviewMaterials';

type Model = { id: string; name?: string };
const validId = (id: string) => /^[!-~]{1,200}$/.test(id);
const catalogKey = (account: PlanAccount) => 'tianyan:plan:codex-models:' + account.id + ':' + account.revision;
const pendingCatalogs = new Map<string, Promise<Model[]>>();

function cachedModels(account: PlanAccount): Model[] | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(catalogKey(account)) || 'null');
    if (saved && saved.until > Date.now() && Array.isArray(saved.models)) return saved.models;
  } catch { /* Optional tab cache. */ }
  return null;
}

function loadModels(account: PlanAccount, csrf: string) {
  const key = catalogKey(account);
  const pending = pendingCatalogs.get(key);
  if (pending) return pending;
  const task = api.chatGPTModels(account.id, csrf).then(({data}) => {
    if (data.status !== 'success') throw new Error(data.message || '暂时无法读取账号的模型列表。');
    const models = (data.models ?? []).filter(item => validId(item.id));
    try { sessionStorage.setItem(key, JSON.stringify({models, until: Date.now() + 10 * 60_000})); } catch { /* In-memory results still work. */ }
    return models;
  }).finally(() => pendingCatalogs.delete(key));
  pendingCatalogs.set(key, task);
  return task;
}

type ModelsProps = { account: PlanAccount; csrf: string; request: number; disabled: boolean; review: (model: string) => void };
function AccountModels({ account, csrf, request, disabled, review }: ModelsProps) {
  const [models, setModels] = useState<Model[] | null>(() => cachedModels(account));
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [custom, setCustom] = useState('');
  const [showCustom, setShowCustom] = useState(false);
  useEffect(() => {
    if (!request && !reload) return;
    const cached = !reload && cachedModels(account);
    if (cached) { setModels(cached); return; }
    let active = true;
    setLoading(true); setError('');
    loadModels(account, csrf).then(items => { if (active) setModels(items); })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : '读取模型失败，请重试。'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [account.id, account.revision, csrf, request, reload]);
  const options = models ?? [];
  const shown = account.model && !options.some(item => item.id === account.model)
    ? [{id: account.model}, ...options] : options;
  const showInput = showCustom || (!!error || models?.length === 0) && !shown.length;
  return <>
    <div className="pr-catalog-head"><strong>点模型开始复盘</strong>
      <button type="button" disabled={loading} onClick={() => setReload(n => n + 1)}>
        {loading ? '正在获取模型…' : models || error ? '刷新模型' : '获取可用模型'}</button>
    </div>
    {error && <p role="alert" className="pr-choice-error">{error}</p>}
    {models?.length === 0 && !error && <p>账号没有返回可选模型，可填写有权使用的模型 ID。</p>}
    {shown.length > 0 && <div className="pr-catalog-list" aria-label="可用的 Codex 模型">{shown.map(item =>
      <button key={item.id} type="button" aria-label={'用 ' + item.id + ' 复盘'} disabled={disabled} onClick={() => review(item.id)}>
        <span><strong>{item.name || item.id}</strong>{item.name && item.name !== item.id && <small>{item.id}</small>}</span>
        <span className="pr-catalog-verb" aria-hidden="true">复盘 ›</span>
      </button>)}</div>}
    {showInput ? <form className="pr-custom-model" onSubmit={e => { e.preventDefault(); if (!disabled && validId(custom.trim())) review(custom.trim()); }}>
      <label>自定义模型 ID<input autoComplete="off" spellCheck={false} maxLength={200} value={custom}
        placeholder="填写此账号可用的模型 ID" onChange={e => setCustom(e.target.value)}/></label>
      <button className="pr-generate" disabled={disabled || !validId(custom.trim())}>用此模型复盘</button>
    </form> : <button type="button" className="pr-custom-link" onClick={() => setShowCustom(true)}>使用自定义模型 ID</button>}
  </>;
}

type Props = ReviewControls & { accounts: PlanAccount[]; csrf: string; request: number; generate: (input: CodexReviewInput) => void };

export function CodexReview({ accounts, csrf, request, generate, ...controls }: Props) {
  const [accountId, setAccountId] = useState(accounts[0].id);
  const account = accounts.find(item => item.id === accountId) ?? accounts[0];
  const [accountChanged, setAccountChanged] = useState(false);

  return <section className="pr-direct-codex" aria-label="用 Codex 复盘">
    {accounts.length > 1 && <label className="pr-model-line">Codex 账号<select value={account.id} disabled={controls.busy}
      onChange={e => { setAccountId(e.target.value); setAccountChanged(true); }}>
      {accounts.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
    <ReviewMaterials key={account.id} {...controls}>{(scope, disabled, start) =>
      <AccountModels key={catalogKey(account)} account={account} csrf={csrf} request={request || Number(accountChanged)} disabled={disabled}
        review={model => start(() => generate({connection_id: account.connection_id, model, ...scope}))}/>}
    </ReviewMaterials>
  </section>;
}
