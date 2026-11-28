import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from '../../shared/api';
import type { Envelope, RepairBatch } from './api';
import { dateTime, messageOf } from './shared';

type LocalService = { id: string; name: string; online: boolean; checked_at: string | null };
type Run = { id: string | null; phase: 'queued' | 'scanning' | 'applying' | 'submitted' | 'failed'; started_at: string;
  checked: number; total: number | null; current?: string | null; message?: string | null; batch?: RepairBatch | null };
type Progress = { checked_at: string; services: LocalService[]; run: Run | null };
type Health = 'checking' | 'up' | 'starting' | 'offline';
const inspectionPath = '/api/v1/data-center/inspection';
const active = (run: Run | null) => !!run && ['queued','scanning','applying'].includes(run.phase);
function stored(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function keep(key: string, attempt: string | null) {
  try { if (attempt) localStorage.setItem(key, attempt); else localStorage.removeItem(key); } catch { /* keep the in-memory attempt */ }
}
async function probe(path: string) {
  try {
    const response = await fetch('/health/' + path, { cache: 'no-store', signal: AbortSignal.timeout(5000) });
    return response.ok && (await response.json()).status === 'ok';
  } catch { return false; }
}

export function useInspectionRun(account: string, csrf: string, onSubmitted: () => void) {
  const key = 'system-inspection-attempt:' + account;
  const attempt = useRef<string | null>(stored(key));
  const epoch = useRef(0);
  const sendingRef = useRef(false);
  const pollNow = useRef<() => void>(() => {});
  const [status, setStatus] = useState<Progress | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [health, setHealth] = useState<Health>('checking');
  const [sending, setSending] = useState(false);
  const [uncertain, setUncertain] = useState(!!attempt.current);
  const [error, setError] = useState('');
  const [statusError, setStatusError] = useState('');
  const notified = useRef('');
  const notify = useRef(onSubmitted);
  notify.current = onSubmitted;

  useEffect(() => {
    let live = true, reading = false, requestAnother = false;
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      clearTimeout(timer);
      if (!live) return;
      if (reading) { requestAnother = true; return; }
      if (document.hidden) { timer = setTimeout(read, 10000); return; }
      reading = true;
      const token = epoch.current, pending = attempt.current;
      let delay = 60000;
      try {
        const { data } = await request<Envelope<Progress>>(inspectionPath + '/progress' + (pending ? '?operation_key=' + encodeURIComponent(pending) : ''));
        if (!data?.checked_at || !Array.isArray(data.services)) throw new Error('服务未返回本机状态。');
        if (!live || token !== epoch.current) return;
        setStatus(data); setHealth(data.services.every(s => s.online) ? 'up' : 'starting'); setStatusError('');
        if (pending && !data.run) {
          setUncertain(true); delay = 3000;
        } else {
          if (pending) { attempt.current = null; keep(key, null); setUncertain(false); setError(''); }
          setRun(data.run);
          if (active(data.run)) delay = 1800;
          else if (data.run?.batch?.actions.some(a => a.state === 'working' || a.state === 'waiting')) delay = 12000;
          if (data.run?.phase === 'submitted' && notified.current && notified.current !== data.run.started_at) notify.current();
          if (data.run?.phase === 'submitted') notified.current = data.run.started_at;
          else if (active(data.run)) notified.current = 'in progress';
        }
      } catch (reason) {
        if (!live || token !== epoch.current) return;
        setStatus(null);
        if (reason instanceof ApiError && (reason.status === 401 || reason.status === 403)) {
          setRun(null); setUncertain(false); attempt.current = null; keep(key, null);
          setStatusError(reason.status === 401 ? '登录已过期，请重新登录后查看修复。' : '当前账号需要管理员权限才能查看修复。');
        } else setStatusError('暂时读不到检修回执，系统会自动重查。');
        const [up, ready] = await Promise.all([probe('live'), probe('ready')]);
        if (live && token === epoch.current) setHealth(ready ? 'up' : up ? 'starting' : 'offline');
        delay = 5000;
      } finally {
        reading = false;
        if (live) { timer = setTimeout(read, requestAnother ? 0 : delay); requestAnother = false; }
      }
    }
    pollNow.current = () => { void read(); };
    const visible = () => { if (!document.hidden) void read(); };
    document.addEventListener('visibilitychange', visible);
    void read();
    return () => { live = false; epoch.current++; clearTimeout(timer); pollNow.current = () => {}; document.removeEventListener('visibilitychange', visible); };
  }, [key]);

  async function start() {
    if (sendingRef.current || active(run)) return;
    sendingRef.current = true; setSending(true); setError(''); setUncertain(false);
    const token = ++epoch.current;
    const operation = attempt.current ?? crypto.randomUUID();
    attempt.current = operation; keep(key, operation);
    try {
      const { data } = await request<Envelope<Run>>(inspectionPath + '/runs', { method: 'POST',
        headers: { 'Content-Type':'application/json','X-CSRF-Token':csrf,'Idempotency-Key':operation }, body:'{}' });
      if (token !== epoch.current) return;
      if (!data?.id || !data.phase) throw new Error('未收到任务编号。');
      setRun(data); setUncertain(false); attempt.current = null; keep(key, null); notified.current = 'in progress';
    } catch (reason) {
      if (token !== epoch.current) return;
      if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500) {
        attempt.current = null; keep(key, null); setError(messageOf(reason));
      } else setUncertain(true);
    } finally {
      sendingRef.current = false;
      if (token === epoch.current) { setSending(false); pollNow.current(); }
    }
  }
  return { status, run, health, sending, uncertain, error, statusError, start, busy: sending || active(run), refresh: () => pollNow.current() };
}

type Activity = ReturnType<typeof useInspectionRun>;
export function InspectionActivity({ activity, batch }: { activity: Activity; batch: RepairBatch | null }) {
  const { health, status, run, sending, uncertain } = activity;
  const unavailable = status?.services.filter(s => !s.online) ?? [];
  const rows = batch?.actions ?? [];
  const count = (state: string) => rows.filter(row => row.state === state).length;
  const verified = count('verified'), working = count('working'), waiting = count('waiting'), manual = count('manual'), limited = count('limited');
  let title = '', detail = '', value: number | undefined, max = 1;
  if (sending || uncertain) {
    title = sending ? '正在登记本次修复' : '正在找回本次回执';
    detail = uncertain ? '系统正在核对同一次操作。也可以点「重试本次修复」，已登记的任务会继续使用。' : '收到任务编号后即可关闭页面。';
  } else if (run?.phase === 'queued' || run?.phase === 'scanning') {
    title = run.phase === 'queued' ? '已登记，等待后台开始检修' : '正在检查数据来源与历史缺口';
    detail = run.message ?? (run.phase === 'queued' ? '离开页面后任务会继续。' : '检查完成后会列出本次可处理的项目。');
  } else if (run?.phase === 'applying') {
    title = '正在安排修复'; value = run.checked; max = Math.max(1, run.total ?? run.checked);
    detail = '已检查 ' + run.checked + ' / ' + (run.total ?? '…') + ' 项' + (run.current ? ' · ' + run.current : ' · 正在保存回执');
  } else if (run?.phase === 'failed') {
    title = '本次检修已停止'; detail = run.message || '后台未完成本次检查。请查看服务状态，再点「一键检修并修复」。';
  } else if (batch) {
    title = !rows.length ? '本次没有可立即修复的项目' : verified === rows.length ? '本次修复已全部核验' :
      working ? '正在核验修复结果' : waiting ? '等待采集条件' : '本次自动处理已结束';
    if (rows.length) { value = verified; max = rows.length; }
    detail = rows.length ? '已核验恢复 ' + verified + ' / ' + rows.length + ' 项' +
      (working ? ' · ' + working + ' 项处理中' : '') + (waiting ? ' · ' + waiting + ' 项等待条件' : '') +
      (manual ? ' · ' + manual + ' 项需你处理' : '') + (limited ? ' · ' + limited + ' 项受覆盖范围限制' : '') : '可在下方查看需授权或等待条件的项目。';
  }
  return <div className="inspection-activity">
    <div className="inspection-local"><div className={'inspection-service-state service-' + health}>
      <span aria-hidden="true"/><strong>{health === 'up' ? '本机服务已启动' : health === 'starting' ? '部分本机服务尚未就绪' : health === 'offline' ? '暂时连不上本机服务' : '正在检测本机服务'}</strong>
      {status && <small>最近检测 {dateTime(status.checked_at, true)}</small>}</div>
      <button type="button" className="dc-link" onClick={activity.refresh}>重新检测服务</button></div>
    {status && <details className="inspection-service-details"><summary>查看服务状态</summary><ul>{status.services.map(s => <li key={s.id}>
      <strong>{s.name}</strong><span>{s.online ? '已就绪' : s.id === 'database' ? '版本或模式待核对' : '近期未收到心跳'}</span>{s.checked_at && <small>{dateTime(s.checked_at, true)}</small>}</li>)}</ul></details>}
    {(health === 'offline' || health === 'starting') && <p className="inspection-help">{unavailable.length > 0 && unavailable.map(s => s.name).join('、') + '尚未就绪。'}在本机 Windows 桌面双击「天衍服务启动与检查」。它会检查并启动已安装的服务，就绪后打开运行监控。</p>}
    {activity.statusError && <p className="inspection-help">{activity.statusError}</p>}
    {activity.error && <p role="alert">{activity.error}</p>}
    {title && <div className="inspection-run" aria-label="本次检修进度"><div role="status"><strong>{title}</strong><small>{detail}</small></div>
      {run?.phase !== 'failed' && (value !== undefined || sending || uncertain || active(run)) &&
        <progress aria-label={run?.phase === 'applying' ? '安排修复进度' : batch && !active(run) && !uncertain && !sending ? '修复核验进度' : '本次检修进度'} value={value} max={max}/>}</div>}
  </div>;
}
