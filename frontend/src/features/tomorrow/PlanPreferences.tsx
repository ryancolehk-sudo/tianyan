import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { SessionResponse } from '../../generated/schema';
import { dataCenterApi, type ConnectionIndex } from '../data-center-live/api';
import { ConnectionEditor } from '../data-center-live/ConnectionEditor';
import { useResource } from '../data-center-live/shared';
import { ChatGPTAccounts } from '../chatgpt/ChatGPTAccounts';
import { tomorrowApi } from './api';
import type { ModelConnection, SettingsView } from './model';
import '../data-center-live/dataCenterLive.css';

const names = {
  delivery: '飞书附件预览', menu: '预案设置', connections: '管理模型', evidence: '本篇数据依据',
  models: '我的 API', account: 'Codex 账号与模型', enable: '开启预案 AI', schedule: '自动生成',
  sources: '选择复盘资料', portfolio: '持仓与风险偏好',
};
export type PreferencePage = keyof typeof names;

type ModelsProps = { session: SessionResponse; modelId?: string; useForPlan: (id: string) => void };

function ApiEditor({ index, reload, session, modelId, useForPlan }: ModelsProps & { index: ConnectionIndex; reload: () => void }) {
  const services = index.items.filter(item => item.kind === 'model' && item.template !== 'chatgpt_plan');
  const [selected, setSelected] = useState(() => services.find(item => item.id === modelId)?.id ?? services[0]?.id ?? '');
  const [editor, setEditor] = useState({ id: selected, version: 0 });
  function open(id: string) {
    setSelected(id);
    setEditor(previous => ({ id, version: previous.version + 1 }));
  }
  return <>
    {!!services.length && <label className="pr-bound-models">已绑定的 API
      <select value={selected} onChange={event => open(event.target.value)}>
        {services.map(item => <option key={item.id} value={item.id}>{item.name} · {item.model || '待选模型'}</option>)}
        <option value="">添加新 API</option>
      </select>
    </label>}
    <ConnectionEditor key={editor.version} initial={services.find(item => item.id === editor.id) ?? null}
      index={index} simpleModel session={session} onSaved={item => { setSelected(item.id); reload(); }}
      refresh={reload} cancel={() => open('')} useForPlan={useForPlan}/>
  </>;
}

function Models({ onChanged, ...props }: ModelsProps & { onChanged: () => void }) {
  const index = useResource(dataCenterApi.integrations);
  if (!index.data) return <p>{index.error ? '模型列表读取失败。' : '正在读取模型…'}{index.error && <button onClick={index.reload}>重试</button>}</p>;
  return <ApiEditor {...props} index={index.data.data} reload={() => { index.reload(); onChanged(); }}/>;
}

type Props = {
  page: PreferencePage; open: (page: PreferencePage) => void; close: () => void; session: SessionResponse;
  model?: ModelConnection; apiModelId?: string; refresh: () => void; enabled: (settings: SettingsView) => void;
  useModel: (source: ModelConnection['source'], id: string) => void; children: ReactNode;
};

export function PlanPreferences({ page, open, close, session, model, apiModelId, refresh, enabled, useModel, children }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const pending = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { const el = dialog.current; el?.showModal(); return () => el?.close(); }, []);
  useEffect(() => setError(''), [page]);
  async function enable() {
    if (!model || pending.current) return;
    pending.current = true;
    setBusy(true); setError('');
    try {
      enabled((await tomorrowApi.enableAi(model.id, session.data.csrf_token)).data);
      close();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '开启失败，请重试。');
      refresh();
    } finally { pending.current = false; setBusy(false); }
  }
  return createPortal(<dialog className="pr-preferences" ref={dialog} aria-labelledby="pr-settings-title"
    onCancel={event => { event.preventDefault(); if (!pending.current) close(); }}>
    <header>{page !== 'menu' && <button disabled={busy} onClick={() => open('menu')}>返回设置</button>}
      <h2 id="pr-settings-title">{names[page]}</h2><button disabled={busy} onClick={close}>关闭</button>
    </header>
    {page === 'menu' ? <nav>{(['connections', 'evidence', 'schedule', 'sources', 'portfolio'] as const).map(item =>
      <button key={item} onClick={() => open(item)}>{names[item]} <span aria-hidden="true">›</span></button>)}</nav>
      : page === 'connections' ? <nav><button onClick={() => open('account')}>Codex 账号与模型</button><button onClick={() => open('models')}>我的 API</button></nav>
      : page === 'models' ? <Models session={session} modelId={apiModelId ?? (model?.source === 'api' ? model.id : undefined)}
        onChanged={refresh} useForPlan={id => useModel('api', id)}/>
      : page === 'account' ? <ChatGPTAccounts session={session} refreshVersion={0} onChanged={refresh} useForPlan={id => useModel('chatgpt_plan', id)}/>
      : page === 'enable' ? <div>
        <p>将开启系统 AI 总开关和明日预案的调用权限。点击生成预案时，系统才会把你勾选的资料发送给模型。</p>
        <p>本次模型　<strong>{model?.model ?? '请先返回选择可用模型'}</strong></p>
        <button disabled={!model || busy || session.data.user.role !== 'admin'} onClick={enable}>{busy ? '正在开启…' : '确认开启'}</button>
        {error && <p role="alert">{error}</p>}
      </div> : children}
  </dialog>, document.body);
}
