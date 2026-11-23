import { useEffect, useRef, useState } from 'react';
import { api } from '../../shared/api';
import type { ChatGPTStatus } from './model';
import { chatGPTCallbackMessage, continueToChatGPT } from './model';
import './chatgpt.css';

export function ChatGPTLogin({ passwordBusy }: { passwordBusy: boolean }) {
  const [status, setStatus] = useState<ChatGPTStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  const lock = useRef(false);
  const mounted = useRef(true);
  const callback = chatGPTCallbackMessage(new URLSearchParams(location.search).get('chatgpt'));
  useEffect(() => {
    mounted.current = true; let current = true;
    api.chatGPTStatus().then(value => { if (current) { setStatus(value.data); setError(''); } })
      .catch(() => { if (current) { setStatus(null); setError('暂时无法确认 ChatGPT 登录是否可用，可以重试或使用原账号登录。'); } });
    return () => { current = false; mounted.current = false; };
  }, [version]);
  async function login(newAccount = false) {
    if (lock.current || !status?.available || passwordBusy) return;
    lock.current = true; setBusy(true); setError('');
    try { const response = await api.chatGPTLogin({ mode: 'login', ...(newAccount ? { new_account: true } : {}) }); continueToChatGPT(response.data.authorization_url); }
    catch (reason) {
      if (mounted.current) { setError(reason instanceof Error ? reason.message : '官方登录暂未完成，请重新发起。'); setBusy(false); }
      lock.current = false;
    }
  }
  return <section className="cg-login" aria-label="ChatGPT 登录">
    <div className="cg-login-divider"><span>或使用 ChatGPT</span></div>
    {callback && <p className="cg-callback" role="status">{callback}</p>}
    <button type="button" className="cg-continue" disabled={!status?.available || busy || passwordBusy} onClick={() => { void login(); }}>{busy ? '正在前往官方登录…' : 'Continue with ChatGPT'}</button>
    <button type="button" className="text-link" style={{ width: '100%', justifyContent: 'center', marginTop: 10 }} disabled={!status?.available || busy || passwordBusy} onClick={() => { void login(true); }}>使用其他 ChatGPT 账号</button>
    <p className="cg-login-note">在 OpenAI 官方页面完成登录。本机支持符合资格的 Plus / Pro 账号授权计划使用，具体权限及额度以官方授权结果为准。</p>
    <p className="cg-login-note">已有天衍账号请先使用原账号登录，再在数据中心连接 ChatGPT，以保留原工作空间。此处首次登录会创建独立空间。</p>
    {!status && !error && <p className="cg-login-status" role="status">正在检查本机接入状态…</p>}
    {status && !status.available && <p className="cg-login-status" role="status">{status.reason || '当前环境未开放 ChatGPT 登录，请使用本机入口或原账号。'}</p>}
    {error && <div className="cg-login-error" role="alert"><span>{error}</span><button type="button" onClick={() => setVersion(value => value + 1)}>重试接入检查</button></div>}
  </section>;
}
