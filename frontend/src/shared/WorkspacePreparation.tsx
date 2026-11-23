import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { KeyboardEvent, PropsWithChildren } from 'react';
import { WorkspacePreparation as Preparation } from './workspacePreload';
import './workspace-preparation.css';
import { startPageHref, usePreferences } from './preferences';
import { useReadNotification } from './BackgroundRefresh';

export function WorkspacePreparation({ admin, children }: PropsWithChildren<{ admin: boolean }>) {
  const { preferences } = usePreferences();
  const currentPreferences = useRef({ ...preferences }); Object.assign(currentPreferences.current, preferences);
  const [preparation] = useState(() => { const target = new URL(location.href); if (target.pathname === '/') target.pathname = startPageHref(preferences); return new Preparation(target, admin, currentPreferences.current); });
  const steps = useSyncExternalStore(preparation.subscribe, () => preparation.snapshot);
  const [details, setDetails] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const finished = steps.every(step => step.state === 'ready' || step.state === 'failed');
  const ready = steps.filter(step => step.state === 'ready').length;
  const failed = steps.filter(step => step.state === 'failed').length;
  const loading = steps.filter(step => step.state === 'loading').length;
  useReadNotification({ key: preparation, waiting: !finished, showWaiting: true, title: '工作空间准备',
    error: failed ? `${failed}项资料未完成，其余内容可正常使用。` : null,
    message: `已完成 ${ready}/${steps.length}${loading ? ` · ${loading}项正在准备` : ''}，收起窗口后后台继续。`,
    action: () => setDetails(true), actionLabel: '查看准备进度', opensDialog: true });
  useEffect(() => { preparation.start(); return preparation.dispose; }, [preparation]);
  useEffect(() => {
    const node = dialog.current;
    if (details) { node?.showModal(); node?.querySelector<HTMLButtonElement>('button')?.focus(); }
    return () => node?.close();
  }, [details]);
  function containFocus(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key !== 'Tab') return;
    const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
    const first = buttons[0], last = buttons.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
  function enter() { setDetails(false); requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('.notification-button')?.focus()); }
  return <>
    {children}
    {details && <dialog ref={dialog} className="workspace-preparation" aria-labelledby="workspace-preparation-title" aria-describedby="workspace-preparation-description" onKeyDown={containFocus} onCancel={event => { event.preventDefault(); enter(); }}>
      <p className="eyebrow">TIANYAN WORKSPACE</p>
      <h2 id="workspace-preparation-title">工作空间后台准备</h2>
      <p id="workspace-preparation-description">页面已可使用。常用资料最多两项同时准备，当前页面优先；收起这个窗口后，后台仍会继续。</p>
      <div className="workspace-preparation-progress"><span>资料准备</span><b>{ready} / {steps.length}</b></div>
      <progress aria-label="工作空间准备进度" max={steps.length} value={ready}/>
      <ul aria-live="polite">{steps.map(step => <li key={step.label} data-state={step.state}><span className="workspace-step-dot" aria-hidden="true">{step.state === 'ready' ? '✓' : step.state === 'failed' ? '!' : ''}</span><div><span>{step.label}</span>{step.error && <small>{step.error}</small>}</div><b>{({ waiting: '等待中', loading: '读取中', ready: '已就绪', failed: '待重试' })[step.state]}</b></li>)}</ul>
      <p className="workspace-preparation-note">{failed ? '未完成的资料可重试，其余页面已可使用。' : '读取本机已保存资料；空数据会保留为空，不会自动补采。'}</p>
      <div className="workspace-preparation-actions"><button onClick={enter}>收起进度 · 后台继续</button>{finished && failed > 0 && <button className="primary" onClick={preparation.retry}>重试未完成项</button>}</div>
    </dialog>}
  </>;
}
