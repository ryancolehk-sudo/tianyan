import { ProgressCircle } from '../../shared/ProgressCircle';
import { useEffect, useRef, useState } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { usePrivateDraft } from '../../shared/privateDrafts';
import { capabilityApi } from './capabilityApi';
import type { MinuteJob } from './capabilityApi';
import { ErrorNotice, reasonLabel, Status } from './shared';

export function FullMinuteJob({ session, ready }: { session: SessionResponse; ready: boolean }) {
  const owner = session.data.user.id;
  const [day, setDay] = usePrivateDraft(owner, 'full-minute:day', () => '', false);
  const [fullMarket, setFullMarket] = usePrivateDraft(owner, 'full-minute:scope', () => false, false);
  const [jobContext, setJobContext] = usePrivateDraft<{ jobId: string; day: string; fullMarket: boolean } | null>(owner, 'full-minute:job', () => null, false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [job, setJob] = useState<MinuteJob | null>(null);
  const [version, setVersion] = useState(0);
  const submitting = useRef(false);
  const admin = session.data.user.role === 'admin';
  const active = !!jobContext && (!job || ['queued', 'running', 'retry_wait', 'started'].includes(job.state));
  useEffect(() => {
    if (!jobContext) return;
    let stopped = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const response = await capabilityApi.minuteJob(jobContext.jobId);
        if (stopped) return;
        setJob(response.data); setError(null);
        if (['queued', 'running', 'retry_wait', 'started'].includes(response.data.state)) timer = window.setTimeout(() => { void load(); }, 3000);
      } catch (reason) { if (!stopped) setError(reason); }
    };
    void load();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [jobContext, version]);
  async function start(cursor?: string) {
    if (!admin || submitting.current || busy || active || !ready || !consent) return;
    submitting.current = true; setBusy(true); setError(null);
    const context = cursor && jobContext ? jobContext : { day, fullMarket };
    try {
      const response = await capabilityApi.fullMinute({ ...(context.day ? { day: context.day } : {}), ...(cursor ? { cursor } : {}), full_market: context.fullMarket, allow_points: true }, session.data.csrf_token);
      setJob({ job_id: response.data.job_id, state: response.data.state, error_code: null, progress: null });
      setJobContext({ jobId: response.data.job_id, day: context.day, fullMarket: context.fullMarket }); setConsent(false);
    } catch (reason) { setError(reason); } finally { submitting.current = false; setBusy(false); }
  }
  const progress = job?.progress;
  return <section className="dc-full-minute"><h3>按需补齐分钟数据</h3><p>每次最多处理 20 只标的，查看结果后可继续下一段。补齐指定日数据，不自动循环扫描全市场。</p>{!ready && <p className="dc-permission-note">先完成数据服务检测、启用连接，并在“全量分钟”能力中明确选择通达信积分路径。</p>}
    <fieldset disabled={!admin || !ready || busy || active} className="dc-full-minute-fields"><label>补齐范围<select aria-label="分钟补齐范围" value={fullMarket ? 'market' : 'configured'} onChange={event => { setFullMarket(event.target.value === 'market'); setConsent(false); }}><option value="configured">当前配置标的</option><option value="market">全市场 · 分段执行</option></select></label><label>交易日期<input aria-label="分钟补齐交易日期" type="date" value={day} onChange={event => { setDay(event.target.value); setConsent(false); }}/><small>留空使用最近可核验交易日</small></label></fieldset>
    {job && <div className="dc-minute-progress" role="status"><Status value={job.state}/>{progress ? <><p>{progress.day} · 共 {progress.scope_count} 只 · 已处理至 {progress.completed_through} 只</p><ProgressCircle value={progress.completed_through} maxValue={Math.max(1, progress.scope_count)} size="lg" aria-label="分钟补齐进度"/><p>本段请求 {progress.chunk_requested} 只，返回 {progress.chunk_completed} 只，失败 {progress.failed_count} 只；剩余 {progress.remaining} 只。</p>{!!progress.failed_instruments.length && <p>本段失败：{progress.failed_instruments.join('、')}</p>}{progress.is_complete && <p>本次范围已全部成功返回。</p>}{progress.scope_traversed && !progress.is_complete && <p>本次范围已处理完毕，失败标的尚未补齐。</p>}</> : <p>任务已提交，等待读取本段范围与进度。</p>}{job.error_code && <p className="dc-bad">{reasonLabel(job.error_code)}</p>}</div>}
    <label className="dc-points-consent"><input type="checkbox" checked={consent} disabled={!admin || !ready || busy || active} onChange={event => setConsent(event.target.checked)}/>允许本次分钟补齐消耗积分</label><div className="dc-provider-actions"><button type="button" className="dc-button-gold" disabled={!admin || !ready || busy || active || !consent} onClick={() => { void start(); }}>{busy ? '正在提交…' : '启动本段补齐'}</button>{progress?.next_cursor && <button type="button" disabled={!admin || !ready || busy || active || !consent} onClick={() => { void start(progress.next_cursor!); }}>继续下一段（最多 20 只）</button>}{jobContext && <button type="button" disabled={busy} onClick={() => setVersion(value => value + 1)}>刷新任务进度</button>}</div>
    {error !== null && <ErrorNotice error={error} retry={jobContext ? () => setVersion(value => value + 1) : undefined}/>}
  </section>;
}
