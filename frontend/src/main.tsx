import { WorkspacePreparation } from './shared/WorkspacePreparation';
import { StrictMode, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import type { SessionResponse } from './generated/schema';
import { api, ApiError } from './shared/api';
import { Brand, ErrorState, Notice } from './shared/ui';
import { Shell } from './shared/Shell';
import { Page } from './features/pages';
import { researchRoutes, routes } from './features/navigation';
import { authorizeDraftOwner, clearPrivateDrafts } from './shared/privateDrafts';
import { installNavigation, navigateTo, restoreNavigation, useNavigation } from './shared/navigation';
import './shared/productStyles';
import './style.css';
import { terminalTabs, terminalView } from './shared/terminalNavigation';
import { isDailyReview } from './shared/reviewNavigation';
import { NewsSessionProvider } from './features/news-center/NewsSession';
import { ChatGPTLogin } from './features/chatgpt/ChatGPTLogin';
import { authorizeReadScope } from './shared/readCache';
import { PreferenceProvider, preferencesFromSession, startPageHref } from './shared/preferences';
import { MarketSessionProvider } from './shared/marketSession';
import { RecoveryHealthProvider } from './shared/RecoveryHealth';
import './features/terminal/OverviewDensity.css';
import './shared/terminal-theme.css';
import './shared/workspace-density.css';
import { PageOutline } from './shared/PageOutline';

function Login({ onLogin, expired, signingOut }: { onLogin: (s: SessionResponse) => void; expired: boolean; signingOut: boolean }) {
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const form = e.currentTarget; const values = new FormData(form); setBusy(true); setError(null);
    try { onLogin(await api.login(String(values.get('username')), String(values.get('password')))); form.reset(); }
    catch (e) { setError(e instanceof Error ? e : new Error('登录失败，请重试。')); }
    finally { setBusy(false); }
  }
  return <main className="login"><div className="login-intro"><Brand artwork/><div className="login-intro-caption"><h1>让每一次判断，有据可循。</h1><p>观察市场 · 整理方向 · 记录预案</p></div></div><div className="login-side"><form className="login-form" onSubmit={submit}><span className="badge">天衍系统 · 独立工作空间</span><h2>登录工作空间</h2><p className="secondary">从事实出发，保留每一步研究。</p>{expired && <Notice>会话已失效，请重新登录。</Notice>}<label>账号<input name="username" autoComplete="username" required maxLength={80} pattern={'[a-z0-9_.\\-]+'}/></label><label>密码<input name="password" type="password" autoComplete="current-password" required maxLength={1024}/></label>{error && <ErrorState error={error}/>}<button className="primary" disabled={busy}>{busy ? '正在登录…' : '进入工作台 →'}</button><small>使用本实例管理员创建的账号。尚未初始化时，请按照新电脑搭建指南创建管理员。</small>{signingOut ? <p role="status" className="secondary">正在结束上次登录…</p> : <ChatGPTLogin passwordBusy={busy}/>}</form><span className="login-footer">天衍系统 · 事实 / 判断 / 验证</span></div></main>;
}

const desktopLaunch = (() => {
  const ticket = new URLSearchParams(location.hash.slice(1)).get('desktop_ticket');
  if (ticket === null) return null;
  history.replaceState(history.state, '', location.pathname + location.search);
  return api.desktopSession(ticket).then(value => ({ value }), error => ({ error }));
})();

function App() {
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [expired, setExpired] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const hadSession = useRef(false);
  const checks = useRef(0);
  const identity = useRef('');
  const navigation = useNavigation();
  const path = navigation.path;
  const tomorrow = path === '/research/tomorrow';
  const review = tomorrow || isDailyReview(path, navigation.search, navigation.hash);
  const isTerminal = review || (path === '/workbench' || path === '/auction') && new URLSearchParams(navigation.search).get('legacy') !== '1';
  const isOverview = isTerminal && path === '/workbench' && (new URLSearchParams(navigation.search).get('view') || 'overview') === 'overview';
  const isAuction = isTerminal && !review && terminalView(path,navigation.search) === 'auction';
  const isNewsCenter = path === '/news' && new URLSearchParams(navigation.search).get('legacy') !== '1';
  const isSystem = path === '/system';
  // Keep a module's shared reads/forms alive when only its tab or URL context changes.
  // Older document routes still need a fresh instance for each navigation entry.
  const dataView = new URLSearchParams(navigation.search).get('view') ?? 'connections';
  const pageInstance = tomorrow ? 'tomorrow' : review ? 'daily-review' : isTerminal ? 'terminal' : isNewsCenter ? 'news-center'
    : isSystem ? 'system-settings' : path === '/data' && ['connections', 'data-sources', 'ai', 'monitor'].includes(dataView) ? 'data-center' : navigation.view;
  const title = isNewsCenter ? '资讯中心' : tomorrow ? '明日预案' : review ? '每日复盘' : isTerminal ? terminalTabs.find(([key]) => key === terminalView(path, navigation.search))![1] : researchRoutes.find(([p]) => p === path)?.[1] ?? routes.find(([p]) => p === path)?.[1] ?? '页面不存在';
  function acceptSession(value: SessionResponse) { const scope = (value.data as typeof value.data & { cache_scope?: string | null }).cache_scope ?? null; const next = `${value.meta.app_mode}:${value.data.user.id}:${value.data.user.role}:${scope ?? ''}`; if (identity.current && identity.current !== next) window.dispatchEvent(new Event('session-changed')); identity.current = next; authorizeReadScope(scope); authorizeDraftOwner(value.data.user.id); hadSession.current = true; setSession(value); setExpired(false); }
  function acceptLogin(value: SessionResponse) { checks.current++; acceptSession(value); setLoading(false); setError(null); }
  function check(initial = false, revalidate = false) {
    const version = ++checks.current;
    if (initial) setLoading(true);
    setError(null);
    const sessionRequest = initial && desktopLaunch ? desktopLaunch.then(result => {
      if ('error' in result) throw new Error('桌面入口已过期，请重新双击桌面图标。');
      return result.value;
    }) : api.session();
    sessionRequest.then(value => { if (version === checks.current) { acceptSession(value); if (revalidate) window.dispatchEvent(new Event('read-cache-revalidate')); } }).catch(e => {
      if (version === checks.current && !(e instanceof ApiError && e.status === 401)) setError(e instanceof Error ? e : new Error('连接失败。'));
    }).finally(() => { if (version === checks.current) setLoading(false); });
  }
  useEffect(() => {
    const expire = () => { checks.current++; if (hadSession.current) setExpired(true); hadSession.current = false; setSession(null); setLoading(false); setError(null); };
    window.addEventListener('session-expired', expire); check(true);
    return () => { checks.current++; window.removeEventListener('session-expired', expire); };
  }, []);
  useEffect(installNavigation, []);
  useLayoutEffect(() => {
    if (session && !loading && location.pathname === '/') {
      const target = new URL(startPageHref(preferencesFromSession(session)), location.origin);
      target.search = location.search; target.hash = location.hash;
      navigateTo(target.pathname + target.search + target.hash, true);
    }
  }, [session?.data.user.id, loading]);
  useEffect(() => { if (session && !loading) check(); }, [navigation.view]);
  useEffect(() => {
    let last = 0;
    const validate = () => { if (hadSession.current && document.visibilityState === 'visible' && Date.now() - last > 500) { last = Date.now(); check(false, true); } };
    window.addEventListener('focus', validate); window.addEventListener('read-permission-denied', validate); document.addEventListener('visibilitychange', validate);
    return () => { window.removeEventListener('focus', validate); window.removeEventListener('read-permission-denied', validate); document.removeEventListener('visibilitychange', validate); };
  }, []);
  useLayoutEffect(() => { if (session && !loading) return restoreNavigation(navigation); }, [navigation.sequence, session?.data.user.id, loading]);
  useEffect(() => { document.title = `${session ? title : '登录'} · 天衍系统`; }, [session, title]);
  async function logout() {
    if (!session) return;
    const csrf = session.data.csrf_token;
    checks.current++;
    hadSession.current = false; setSession(null); setLoading(false); setExpired(false); setError(null); setSigningOut(true);
    clearPrivateDrafts();
    try { await api.logout(csrf); checks.current++; clearPrivateDrafts(); }
    catch (e) { if (e instanceof ApiError && e.status === 401) { hadSession.current = false; setSession(null); setExpired(false); setError(null); } else setError(e instanceof Error ? e : new Error('退出失败，请重试。')); }
    finally { setSigningOut(false); }
  }
  if (loading) return <main className="loading" role="status">正在连接天衍工作空间…</main>;
  if (!session) return error ? <main className="loading"><ErrorState error={error} retry={() => check(true)}/></main> : <Login onLogin={acceptLogin} expired={expired} signingOut={signingOut}/>;
  return <PreferenceProvider key={identity.current} session={session}><MarketSessionProvider><RecoveryHealthProvider><WorkspacePreparation admin={session.data.user.role === 'admin'}><Shell path={path} navigationSequence={navigation.sequence} session={session} onLogout={logout}><main id="main-content" className={`content ${isTerminal || path === '/workbench' ? 'workbench-content' : ''} ${isNewsCenter ? 'news-center-content' : ''} ${isSystem ? 'system-settings-content' : ''}`} tabIndex={-1}>{!isTerminal && !isNewsCenter && !isSystem && path !== '/workbench' && <div className="page-heading"><div><p className="eyebrow">TIANYAN RESEARCH WORKSPACE</p><h1>{title}</h1></div><span className="page-context">市场事实 · 我的判断 · 后续验证</span></div>}{error && <ErrorState error={error} retry={() => check()}/>}{!isOverview && !isAuction && (isTerminal || isNewsCenter || isSystem || path === '/data') && <PageOutline context={navigation.path + navigation.search}/>}<NewsSessionProvider session={session}><Page key={pageInstance} path={path} session={session}/></NewsSessionProvider>{!isTerminal && !isNewsCenter && !isSystem && path !== '/workbench' && <footer><span>天衍系统</span> 状态以实际接入、配置和记录为准。</footer>}</main></Shell></WorkspacePreparation></RecoveryHealthProvider></MarketSessionProvider></PreferenceProvider>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);

const acceptanceRun = new URLSearchParams(location.search).get('acceptance_run') ?? '';
if (/^[a-z0-9][a-z0-9-]{0,63}$/i.test(acceptanceRun)) {
  document.documentElement.dataset.acceptance = 'loading';
  void import('./shared/workbenchAcceptance').then(module => module.startAcceptanceRecorder(acceptanceRun))
    .catch(() => { document.documentElement.dataset.acceptance = 'failed'; });
}
