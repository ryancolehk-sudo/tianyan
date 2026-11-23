import { useEffect, useRef, useState } from 'react';
import type { CollectionJobReceipt, FetchRun, RuntimeSource, SessionResponse, Job } from '../../generated/schema';
import { api, ApiError } from '../../shared/api';
import { useSubmit } from '../../shared/useSubmit';
import { ErrorState } from '../../shared/ui';

const attemptLabels: Record<string, string> = { complete: '完整返回', partial: '部分返回', empty: '无数据', unavailable: '暂不可用', error: '失败' };
const timestamp = (value: string | null | undefined) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未知';
function today() {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type)?.value).join('-');
}

export function MinuteBackfillControls({ session, sourceId, instrument, interval, day, lookback, runtime, collectionEnabled, reload }: {
  session: SessionResponse; sourceId: string; instrument: string; interval: string; day: string;
  lookback?: number | null; runtime?: RuntimeSource; collectionEnabled: boolean; reload: () => void;
}) {
  const owner = session.data.user.id;
  const intent = JSON.stringify([instrument, interval, day, sourceId]);
  const submit = useSubmit(owner, 'minute-backfill:' + intent);
  const locked = useRef(false);
  const alive = useRef(true);
  const [receipt, setReceipt] = useState<CollectionJobReceipt | null>(null);
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState('');
  const [monitorError, setMonitorError] = useState<Error | null>(null);
  const [run, setRun] = useState<FetchRun | null>(null);
  const [epoch, setEpoch] = useState(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const limit = Math.min(7, lookback ?? 7);
  const earliest = new Date(today() + 'T00:00:00Z'); earliest.setUTCDate(earliest.getUTCDate() - limit);
  const validDay = /^\d{4}-\d{2}-\d{2}$/.test(day) && day >= earliest.toISOString().slice(0, 10) && day <= today();
  const permitted = Boolean(collectionEnabled && runtime?.provider.enabled && runtime.provider.configured && runtime.provider.entitlement_status === 'verified'
    && runtime.provider.rights.display === 'allowed' && runtime.provider.rights.cache === 'allowed' && !runtime.paused_reason);
  const cooling = Boolean(runtime?.cooldown_until && Date.parse(runtime.cooldown_until) > Date.now());
  async function requestBackfill() {
    if (locked.current || !permitted || !validDay || cooling) return;
    locked.current = true; setPending(true); setNote('正在提交补采请求…'); setMonitorError(null); setRun(null);
    const response = await submit.submit({ sourceId, instrument, interval, day }, key => api.refreshSource(sourceId, session.data.csrf_token, key, { day }));
    if (!alive.current) return;
    const result = response?.data;
    if (!result) { locked.current = false; setPending(false); setNote('补采请求未成功提交；缓存查询仍可使用。'); return; }
    if (result.source_id !== sourceId || result.requested_day !== day || result.acquisition_mode !== 'backfill') {
      locked.current = false; setPending(false); setNote('返回的补采意图不一致，请到数据中心核对；没有将其视为成功。'); return;
    }
    setReceipt(result); setNote('补采任务已排队，排队不等于成功。');
  }
  useEffect(() => {
    if (!receipt || !pending) return;
    let active = true, reading = false, checks = 0;
    const inspect = async () => {
      if (!active || reading || document.visibilityState !== 'visible') return;
      if (++checks > 40) { setNote('任务仍待核对。可手动检查任务，或到数据中心查看调用记录；尚未确认补采成功。'); return; }
      reading = true;
      try {
        let job: Job | undefined;
        try { job = (await api.job(receipt.job_id)).data; }
        catch (error) { if (!(error instanceof ApiError && (error.status === 403 || error.status === 404))) throw error; }
        if (!active) return;
        if (job && (job.id !== receipt.job_id || job.kind !== 'source.collect')) throw new Error('任务身份不一致，补采结果未确认。');
        if (job?.state === 'queued' || job?.state === 'running') { setNote(job.state === 'queued' ? '补采任务已排队，排队不等于成功。' : '补采任务执行中，尚未确认来源覆盖与结果。'); return; }
        const runs = (await api.fetchRuns(sourceId, undefined, receipt.job_id)).data.items;
        if (!active) return;
        const result = runs.find(item => item.job_id === receipt.job_id && item.source_id === sourceId && item.requested_day === day && item.acquisition_mode === 'backfill' && item.capability.split('+').includes('bar.minute'));
        if (!result) {
          if (job?.state === 'failed' || job?.state === 'cancelled') { setNote('补采任务未完成，尚无可绑定的来源调用结果；请到数据中心核对。'); setPending(false); locked.current = false; }
          else setNote(job ? '任务已结束，正在核对对应日期的来源调用记录；暂不判定补采成功。' : '当前账号不能读取任务状态，仍在核对该任务的来源调用记录；暂不判定补采失败或成功。');
          return;
        }
        setRun(result); setMonitorError(null); setPending(false); locked.current = false;
        setNote(result.status === 'empty' ? '来源未覆盖所选日期或没有返回记录；不代表市场无成交。'
          : result.status === 'partial' ? '来源部分返回，缺口或已存冲突仍可能存在；未覆盖原有记录，尚未完整补齐。'
          : result.status === 'complete' ? '本次补采请求已处理，已重读实际保存记录；不保证整日完整或实时。'
          : result.reason === 'rate_limited' ? '来源限流，等待冷却后再尝试；本次没有确认补齐。' : '补采调用失败或不可用，原缓存保留；详情见数据中心。');
        reload();
      } catch (error) { if (active) setMonitorError(error instanceof Error ? error : new Error('暂时无法核对补采任务。')); }
      finally { reading = false; }
    };
    void inspect(); const timer = window.setInterval(() => { void inspect(); }, 3000);
    const visible = () => { void inspect(); }; document.addEventListener('visibilitychange', visible);
    return () => { active = false; window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [receipt, pending, epoch, day, sourceId, reload]);
  return <aside className="minute-backfill" aria-label="分钟历史补采">
    <div><strong>补采所选日期 · {day}</strong><button type="button" disabled={submit.busy || pending || !validDay || !permitted || cooling} onClick={() => { void requestBackfill(); }}>补采所选日期</button></div>
    <p>仅补来源实际返回的缺失分钟，已存冲突不覆写。最多回溯{limit}个自然日，不保证来源覆盖或完整补齐；本操作会请求外部来源。</p>
    {!validDay && <p>日期超出本机安全补采范围，请选择最近{limit}个自然日内的日期。</p>}
    {!permitted && <p>采集未开启、来源停用或使用范围未核实，当前不能补采。</p>}{cooling && <p>来源正在冷却，最早可尝试时间：{timestamp(runtime?.cooldown_until)}。</p>}
    {note && <p role="status">{note}</p>}{submit.error && <ErrorState error={submit.error}/>}
    {receipt && <p>请求日期：{receipt.requested_day} · 任务：<code>{receipt.job_id}</code></p>}
    {run && <p>实际结果：{attemptLabels[run.status]} · 新增 {run.accepted ?? '未知'} / 重复 {run.duplicates ?? '未知'} / 未纳入 {run.rejected ?? '未知'}。原单位与原时标保持。</p>}
    {pending && <button type="button" onClick={() => { setMonitorError(null); setEpoch(value => value + 1); }}>检查补采任务</button>}
    {monitorError && <ErrorState error={monitorError} retry={() => setEpoch(value => value + 1)}/>}
    <a href="/data?view=calls">查看数据中心调用记录</a>
  </aside>;
}
