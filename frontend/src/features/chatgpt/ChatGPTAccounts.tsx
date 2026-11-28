import { useCallback, useEffect, useRef, useState } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api, ApiError } from '../../shared/api';
import type { ProbeResult } from '../data-center-live/api';
import { dateTime, duration, Panel, Toggle } from '../data-center-live/shared';
import type { ChatGPTAccount, ChatGPTStatus } from './model';
import { chatGPTCallbackMessage, chatGPTUsageURL, continueToChatGPT } from './model';
import './chatgpt.css';

const accountStates: Record<ChatGPTAccount['state'], string> = {
  connected: '已连接', identity_only: '仅登录身份', disconnected: '已断开', refreshing: '授权更新中',
  refresh_unknown: '授权更新结果待确认', reauthorization_required: '需要重新授权', client_error: '本机客户端异常',
};

function isUsageLimit(code: string | null | undefined) {
  return !!code && /usage_limit|quota|rate_limit|plan_limit|insufficient_credits/i.test(code);
}

function Welcome({ account, csrf, completed }: { account: ChatGPTAccount; csrf: string; completed: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true; dialog.current?.showModal();
    return () => { mounted.current = false; dialog.current?.close(); };
  }, []);
  async function acknowledge() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    try { await api.chatGPTWelcome(account.id, csrf); if (mounted.current) completed(); }
    catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : '确认未完成，请重试。'); }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  return <dialog className="cg-welcome" ref={dialog} aria-labelledby="cg-welcome-title" onCancel={event => event.preventDefault()}>
    <h2 id="cg-welcome-title">You're using your ChatGPT plan</h2>
    <p>你已授权本机天衍使用 ChatGPT 计划。完成当前模型测试并开启“用于我的 AI 功能”后，符合资格的请求会使用此账号的计划额度或可用余额。</p>
    <p>登录和刷新页面不会生成内容。额度及本应用的使用权限可以在 <a href={chatGPTUsageURL} target="_blank" rel="noopener noreferrer">ChatGPT 使用设置</a>管理；这与单独填写 API Key 的计费方式分别记录。</p>
    {error && <p role="alert">{error}</p>}
    <button type="button" autoFocus disabled={busy} onClick={() => { void acknowledge(); }}>{busy ? '正在确认…' : 'Got it'}</button>
  </dialog>;
}

export function ChatGPTAccounts({ session, refreshVersion, onChanged, useForPlan }: {
  session: SessionResponse; refreshVersion: number; onChanged: () => void; useForPlan?: (id: string) => void;
}) {
  const [status, setStatus] = useState<ChatGPTStatus | null>(null);
  const [accounts, setAccounts] = useState<ChatGPTAccount[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [model, setModel] = useState('');
  const [models, setModels] = useState<{ id: string; name?: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [usageLimit, setUsageLimit] = useState(false);
  const [notice, setNotice] = useState('');
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const reads = useRef(0);
  const mounted = useRef(true);
  const lock = useRef(false);
  const csrf = session.data.csrf_token;
  const current = accounts.find(item => item.id === selected) ?? null;
  const chosen = accounts.find(item => item.use_for_ai) ?? null;
  const welcome = accounts.find(item => item.sharing_granted && item.welcome_pending && item.state !== 'disconnected');
  const canRequest = !!status?.available && !!current?.sharing_granted && current.state === 'connected' && !current.reauthorization_required;
  const changedModel = !!current && model.trim() !== (current.model ?? '');
  const callback = chatGPTCallbackMessage(new URLSearchParams(location.search).get('chatgpt'));

  const reload = useCallback(async () => {
    const sequence = ++reads.current;
    if (mounted.current) setLoading(true);
    try {
      const [connection, list] = await Promise.all([api.chatGPTStatus(), api.chatGPTAccounts()]);
      if (!mounted.current || sequence !== reads.current) return;
      setStatus(connection.data); setAccounts(list.data.items);
      setSelected(previous => list.data.items.some(item => item.id === previous) ? previous
        : list.data.items.find(item => item.use_for_ai)?.id ?? list.data.items[0]?.id ?? null);
    } catch (reason) {
      if (mounted.current && sequence === reads.current) setError(reason instanceof Error ? reason.message : '读取 ChatGPT 账号失败，请重试。');
    } finally { if (mounted.current && sequence === reads.current) setLoading(false); }
  }, [session.data.user.id]);

  useEffect(() => {
    mounted.current = true; void reload();
    return () => { mounted.current = false; reads.current++; };
  }, [reload, refreshVersion]);
  useEffect(() => {
    setModel(current?.model ?? ''); setModels([]); setError(''); setNotice(''); setUsageLimit(false); setConfirmDisconnect(false);
  }, [selected]);
  useEffect(() => { setModel(current?.model ?? ''); }, [current?.revision, current?.model]);
  useEffect(() => {
    function clear() { reads.current++; setAccounts([]); setSelected(null); setModels([]); setModel(''); setStatus(null); setNotice(''); setError(''); setUsageLimit(false); }
    window.addEventListener('session-expired', clear); window.addEventListener('session-changed', clear);
    return () => { window.removeEventListener('session-expired', clear); window.removeEventListener('session-changed', clear); };
  }, []);

  async function act(action: () => Promise<string | void>) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(''); setUsageLimit(false); setNotice('');
    try {
      const result = await action();
      if (mounted.current) { if (result) setNotice(result); await reload(); onChanged(); }
    } catch (reason) {
      if (mounted.current) {
        setError(reason instanceof Error ? reason.message : '操作未完成，请稍后重试。');
        setUsageLimit(reason instanceof ApiError && reason.status === 429);
        // Read the authoritative revision after conflicts, expired permission or
        // failed tests. This never repeats a model request.
        await reload();
      }
    } finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  async function connect(account?: ChatGPTAccount) {
    if (lock.current || !status?.available) return;
    lock.current = true; setBusy(true); setError(''); setNotice('');
    try {
      const response = await api.chatGPTLogin({ mode: 'connect', ...(account ? { account_id: account.id } : {}) }, csrf);
      continueToChatGPT(response.data.authorization_url);
    } catch (reason) {
      if (mounted.current) { setError(reason instanceof Error ? reason.message : '官方登录暂未完成，请重试。'); setBusy(false); }
      lock.current = false;
    }
  }
  function resultMessage(result: ProbeResult, discovery = false) {
    if (result.status !== 'success') {
      setError(result.message); setUsageLimit(isUsageLimit(result.error_code));
      if (result.status === 'unknown') setError(result.message + ' 请求结果不明，系统不会自动重新发送；请先查看调用记录。');
      return undefined;
    }
    return discovery ? `已获取 ${(result.models ?? []).length} 个可用模型；获取列表不代表生成成功。`
      : `当前模型短文本生成测试通过 · ${duration(result.elapsed_ms)} · 实际请求 ${result.request_count} 次。`;
  }

  return <Panel title="我的 ChatGPT" className="cg-card" action={<span className="dc-tag">仅本人使用</span>}>
    <p className="cg-intro"><strong>使用 ChatGPT 身份登录，并选择使用自己的计划。</strong><br/>符合资格的 Plus / Pro 账号可授权；可用模型、范围与额度以官方结果为准。本实例不另设付费计划。</p>
    {callback && <p className="cg-status-note" role="status">{callback}</p>}
    {loading && !status && <p className="cg-status-note" role="status">正在读取本机接入与本人账号…</p>}
    {status && !status.available && <p className="cg-status-note" role="status">{status.reason || '当前环境暂不支持 ChatGPT 官方登录，请使用运行天衍的本机入口。'}</p>}
    {!!accounts.length && <div className="cg-account-picker"><label htmlFor="cg-account">我的 ChatGPT 账号</label><select id="cg-account" value={selected ?? ''} disabled={busy} onChange={event => setSelected(event.target.value)}>{accounts.map(account => <option key={account.id} value={account.id}>{account.label || account.email || 'ChatGPT 账号'}{account.use_for_ai ? ' · 我的 AI 使用中' : ''}{account.state === 'disconnected' ? '（已断开）' : ''}</option>)}</select></div>}
    {current && <div className="cg-account-details">
      <div className="cg-account-identity"><strong>{current.label || current.email || 'ChatGPT 账号'}</strong><span className="dc-tag">{accountStates[current.state] ?? '状态待确认'}</span></div>
      {current.email && current.label !== current.email && <p className="cg-identity-caption">{current.email}</p>}
      <dl className="cg-state-list">
        <div><dt>身份连接</dt><dd>{current.state === 'disconnected' ? '本机已断开' : current.reauthorization_required ? '需要重新登录' : '已记录本人身份'}</dd></div>
        <div><dt>计划授权</dt><dd>{current.sharing_granted ? '已授予使用权限' : '尚未授予'}</dd></div>
        <div><dt>当前模型测试</dt><dd>{current.tested ? '生成测试通过' : '未通过当前配置测试'}</dd></div>
        <div><dt>实际配置模型</dt><dd>{current.model || '尚未指定'}</dd></div>
      </dl>
      {!current.sharing_granted && <p className="cg-status-note">身份登录成功不等于计划授权成功。重新登录并授予计划权限后，才能获取模型和测试生成。</p>}
      {(current.reauthorization_required || ['refresh_unknown', 'client_error'].includes(current.state)) && <p className="cg-status-note">授权当前不可用于新请求。请重新登录确认；不会自动改用 API Key。</p>}
      <label className="cg-model-label" htmlFor="cg-model">我的 ChatGPT 模型</label>
      <div className="cg-model-fields"><input id="cg-model" value={model} maxLength={200} placeholder="选择或手填可用模型 ID" autoComplete="off" disabled={busy || !canRequest} onChange={event => setModel(event.target.value)}/><button type="button" disabled={busy || !canRequest} onClick={() => { const account = current; void act(async () => { const value = await api.chatGPTModels(account.id, csrf); if (mounted.current) setModels(value.data.models ?? []); return resultMessage(value.data, true); }); }}>获取可用模型</button></div>
      {!!models.length && <select className="cg-model-options" aria-label="已获取的 ChatGPT 模型" value="" disabled={busy || !canRequest} onChange={event => setModel(event.target.value)}><option value="" disabled>选择一个已获取模型</option>{models.map(item => <option key={item.id} value={item.id}>{item.name && item.name !== item.id ? `${item.name} · ${item.id}` : item.id}</option>)}</select>}
      <p className="cg-explanation">获取模型会联系官方。测试会生成一段受限短文本，并使用此账号的计划额度；本人业务开关不影响手动测试。</p>
      <div className="cg-model-actions"><button type="button" disabled={busy || !canRequest || !model.trim() || !changedModel} onClick={() => { const account = current; void act(async () => { await api.chatGPTUpdate(account.id, { expected_revision: account.revision, model: model.trim() }, csrf); return '模型已保存，旧测试结果不再验证新模型。请手动执行当前模型测试。'; }); }}>保存模型</button><button type="button" className="dc-button-gold" disabled={busy || !canRequest || !current.model || changedModel} onClick={() => { const account = current; void act(async () => { const value = await api.chatGPTProbe(account.id, csrf); return resultMessage(value.data); }); }}>{busy ? '正在处理…' : '测试当前模型'}</button></div>
      {changedModel && <p className="cg-explanation">请先保存新模型，再测试。模型列表成功不代表短文本生成成功。</p>}
      {current.last_test && <p className="cg-explanation">最近生成测试：{dateTime(current.last_test.at)} · {duration(current.last_test.elapsed_ms)}{current.last_test.error_code ? ` · ${current.last_test.error_code}` : ''}{!current.tested && ' · 不能验证当前模型'}</p>}
      <div className="cg-selection"><div><strong>用于我的 AI 功能</strong><small>{current.use_for_ai ? '已选择此账号的计划路径' : '完成当前模型测试后，可明确选择此路径'}</small></div><Toggle label="用于我的 AI 功能" checked={current.use_for_ai} disabled={busy || (!current.use_for_ai && (!canRequest || !current.tested || changedModel))} onChange={() => { const account = current; void act(async () => { await api.chatGPTUpdate(account.id, { expected_revision: account.revision, use_for_ai: !account.use_for_ai }, csrf); return account.use_for_ai ? '已取消个人 ChatGPT 计划路径，后续使用管理员为功能配置的模型。' : '后续本人业务使用所选 ChatGPT 计划；全局 AI 和功能开关仍然生效。'; }); }}/></div>
      {current.use_for_ai && !current.enabled && <div className="cg-confirm" role="status"><p>计划路径已保留，当前模型未启用。请通过当前模型测试，再明确恢复使用。只有明确关闭上方计划选择后，后续才会按系统模型配置处理。</p><div className="cg-confirm-actions"><button type="button" className="dc-button-gold" disabled={busy || !canRequest || !current.tested || changedModel} onClick={() => { const account = current; void act(async () => { await api.chatGPTUpdate(account.id, { expected_revision: account.revision, use_for_ai: true }, csrf); return '已明确恢复用于我的 AI；后续本人业务使用此账号的计划，全局 AI 和功能开关仍然生效。'; }); }}>恢复用于我的 AI</button></div></div>}
      <p className="cg-plan-line">{current.use_for_ai ? <strong>{current.enabled ? 'Using ChatGPT plan' : 'ChatGPT 计划路径待恢复'}</strong> : <span>计划尚未用于本人业务</span>}<a className="cg-usage" href={chatGPTUsageURL} target="_blank" rel="noopener noreferrer">Manage usage</a></p>
      <p className="cg-explanation">额度由 ChatGPT 管理，本机不推测剩余额度或无限使用。切换上方查看账号不会改变调用路径，请使用“用于我的 AI 功能”确认。计划失败不会自动转到 API Key。</p>
      {current.use_for_ai && !canRequest && <p className="cg-status-note">个人路径的选择已保留，后续生成暂被阻止。请重新登录，或明确关闭个人路径后使用系统配置。</p>}
      {useForPlan && current.enabled && current.tested && canRequest && !changedModel && <button type="button" className="dc-button-gold" disabled={busy} onClick={() => useForPlan(current.connection_id)}>使用此模型复盘</button>}
      <div className="cg-account-actions"><button type="button" disabled={busy || !status?.available} onClick={() => { void connect(current); }}>重新登录此账号</button><button type="button" disabled={busy || current.state === 'disconnected'} onClick={() => setConfirmDisconnect(true)}>断开此账号</button></div>
      {confirmDisconnect && <div className="cg-confirm"><p>将清除本机保存的此账号授权并停止后续使用。远端撤销是否成功会单独告知，已生成的研究记录保留。</p><div className="cg-confirm-actions"><button type="button" disabled={busy} onClick={() => { const account = current; void act(async () => { const result = await api.chatGPTDisconnect(account.id, csrf); setModels([]); setModel(''); setConfirmDisconnect(false); return result.data.remote_revocation_unconfirmed ? '本机授权已删除；远端撤销未确认。可在 ChatGPT 使用设置中检查并撤销本应用访问。' : '本机授权已删除，后续请求已停止。'; }); }}>确认断开</button><button type="button" disabled={busy} onClick={() => setConfirmDisconnect(false)}>取消</button></div></div>}
      {current.remote_revocation_unconfirmed && <p className="cg-status-note">远端撤销未确认。请在 <a className="cg-usage" href={chatGPTUsageURL} target="_blank" rel="noopener noreferrer">Manage usage</a> 检查本应用访问。</p>}
    </div>}
    {chosen && current?.id !== chosen.id && <p className="cg-status-note">本人当前 AI 路径：{chosen.label || chosen.email || '另一 ChatGPT 账号'} · {chosen.model || '尚未指定模型'}。查看其他账号不会切换此路径。</p>}
    <div className="cg-empty-action"><button type="button" className="cg-continue" disabled={!status?.available || busy} onClick={() => { void connect(); }}>Continue with ChatGPT</button><button type="button" className="dc-text-button" disabled={busy || loading} onClick={() => { setError(''); void reload(); }}>刷新本人状态</button></div>
    <p className="cg-explanation">从当前工作空间连接可保留原账号与资料。ChatGPT 密码只在 OpenAI 官方页面输入，本机页面不接收密码或要求粘贴 Token。</p>
    {!accounts.length && !loading && <p className="cg-status-note">尚未连接本人 ChatGPT 账号。原 API Key 接入仍可由管理员单独配置。</p>}
    {notice && <div className="cg-feedback" role="status">{notice}</div>}
    {error && <div className="cg-error" role="alert">{usageLimit && <div className="cg-error-head">ChatGPT · Usage limit reached</div>}<span>{error}</span>{usageLimit && <><br/><a className="cg-usage cg-usage-primary" href={chatGPTUsageURL} target="_blank" rel="noopener noreferrer">Manage usage</a></>}</div>}
    {welcome && <Welcome key={welcome.id} account={welcome} csrf={csrf} completed={() => { setAccounts(items => items.map(item => item.id === welcome.id ? { ...item, welcome_pending: false } : item)); void reload(); }}/>}</Panel>;
}
