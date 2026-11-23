import { useCallback, useEffect, useRef, useState } from 'react';
import type { BarSeries, CollectionJobReceipt, DailyHistoryRequest, SessionResponse } from '../../generated/schema';
import { api, ApiError } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { useSubmit } from '../../shared/useSubmit';

const historyReasons: Record<string, string> = {
  history_adjustment_unsupported: '当前按需历史仅支持不复权；前后复权只显示实际已存口径。',
  history_source_not_configured: '尚未配置该证券的 Tushare 历史来源。',
  permission_missing: '历史来源的缓存或展示条件尚未满足。', source_disabled: '历史来源已停用。',
  source_paused: '历史来源已暂停，请在数据中心核对。', credentials_missing: '请在本机安全配置已有套餐凭据。',
  collection_disabled: '本机采集已关闭；保留显示已有历史。', instrument_identity_unverified: '该证券目录身份尚未核实。',
};
function today() { return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date()); }
function previous(day: string) { const date = new Date(day + 'T00:00:00Z'); date.setUTCDate(date.getUTCDate() - 1); return date.toISOString().slice(0, 10); }
function fiveYearsBefore(day: string) { const date = new Date(day + 'T00:00:00Z'); const month = date.getUTCMonth(); date.setUTCFullYear(date.getUTCFullYear() - 5); if (date.getUTCMonth() !== month) date.setUTCDate(0); return date.toISOString().slice(0, 10); }
type Props = { instrument: string; view: BarSeries; before: string; busy: boolean; onComplete: (endDay?: string) => void };

export function DailyHistoryControls(props: Props) {
  const session = useRead(useCallback(() => api.session(), []));
  if (!session.data) return <p className="feature-note">{session.error ? '无法核对采集权限；已有缓存仍可查看。' : '正在核对历史采集权限…'}</p>;
  return <AuthorizedHistory key={session.data.data.user.id + ':' + props.instrument + ':' + props.view.adjustment} {...props} session={session.data}/>;
}

function AuthorizedHistory({ instrument, view, before, busy, onComplete, session }: Props & { session: SessionResponse }) {
  const admin = session.data.user.role === 'admin';
  const write = useSubmit(session.data.user.id, 'daily-history:' + instrument);
  const [job, setJob] = useState<CollectionJobReceipt | null>(null);
  const [notice, setNotice] = useState('');
  const [polling, setPolling] = useState(false);
  const [oldestAttempted, setOldestAttempted] = useState<string | null>(null);
  const alive = useRef(true), autoAttempted = useRef(false);
  const complete = useRef(onComplete);
  useEffect(() => { complete.current = onComplete; }, [onComplete]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const enabled = admin && view.history_supported === true && Boolean(view.history_source_id);
  const instrumentCode = view.instrument.symbol + '.' + ({ SSE: 'SH', SZSE: 'SZ', BSE: 'BJ' }[view.instrument.exchange]);
  async function collect(older: boolean) {
    if (!enabled || write.busy || polling) return;
    const end = older ? previous(oldestAttempted ?? view.coverage_start_day ?? today()) : today();
    const start = fiveYearsBefore(end) < '1990-01-01' ? '1990-01-01' : fiveYearsBefore(end);
    if (end < '1990-01-01') { setNotice('已到本机允许查询的最早范围。'); return; }
    const body: DailyHistoryRequest = { instrument_id: instrument, start_day: start, end_day: end, adjustment: 'unadjusted' };
    const result = await write.submit(body, key => api.collectMarketHistory(body, session.data.csrf_token, key));
    if (!alive.current) return;
    if (result) {
      if (result.data.instrument_id !== instrument || result.data.source_id !== view.history_source_id || result.data.requested_start_day !== start || result.data.requested_end_day !== end) {
        setNotice('任务回执与所选股票或范围不一致；请在数据中心核对，图表保留原值。'); return;
      }
      setNotice(`历史采集已排队：${start} 至 ${end}；保留已有图表。`);
      if (older) setOldestAttempted(start);
      setJob(result.data); setPolling(true);
    }
  }
  useEffect(() => {
    if (!job) return;
    let active = true, timer = 0, attempts = 0;
    const poll = async () => {
      if (!active) return;
      if (document.visibilityState !== 'visible') { timer = window.setTimeout(poll, 3000); return; }
      const [state, calls] = await Promise.allSettled([api.job(job.job_id), api.fetchRuns(job.source_id, null, job.job_id)]);
      if (!active) return;
      if (state.status === 'fulfilled' && (state.value.data.id !== job.job_id || state.value.data.kind !== 'source.collect')) {
        setNotice('任务身份不一致；尚未确认本次历史结果，请到数据中心核对。'); setPolling(false); return;
      }
      const run = calls.status === 'fulfilled' ? calls.value.data.items.find(item => item.job_id === job.job_id && item.source_id === job.source_id
        && item.acquisition_mode === 'daily_history' && item.requested_instrument_code === instrumentCode
        && item.requested_start_day === job.requested_start_day && item.requested_end_day === job.requested_end_day) : undefined;
      const jobState = state.status === 'fulfilled' ? state.value.data.state : undefined;
      if (jobState === 'failed' || jobState === 'cancelled') {
        setNotice('本次历史采集未完成；已有图表保留，请查看数据中心调用记录后重试。'); setPolling(false); return;
      }
      if (run && jobState !== 'queued' && jobState !== 'running' && ['complete', 'partial', 'empty'].includes(run.status)) {
        setNotice(run.status === 'empty' ? '该查询窗口返回空；不生成或填补K线，可继续向前查询。' : run.status === 'partial' ? '本次窗口仅部分返回；正在读取已保存数据，覆盖缺口见数据中心。' : '历史已返回，正在核对本机图表；返回窗口不代表完整上市历史。');
        setPolling(false);
        complete.current(run.status === 'empty' ? undefined : job.requested_end_day ?? undefined);
        return;
      }
      if (run && jobState !== 'queued' && jobState !== 'running' && ['error', 'unavailable'].includes(run.status)) {
        setNotice('本次来源调用失败或不可用；已有图表保留，尚未确认本次历史返回。'); setPolling(false); return;
      }
      if (++attempts >= 30) { setNotice('任务仍可能在后台等待或重试；已有图表保留，可在数据中心查看结果。'); setPolling(false); return; }
      if (state.status === 'rejected' && !(state.reason instanceof ApiError && state.reason.status === 404)) setNotice('任务状态暂时无法读取，正在重试；已有图表保留。');
      timer = window.setTimeout(poll, 3000);
    };
    void poll();
    return () => { active = false; window.clearTimeout(timer); };
  }, [job, instrumentCode]);
  useEffect(() => {
    if (autoAttempted.current || !enabled || busy || before || write.busy || polling) return;
    // One bounded automatic attempt per account, stock and local day in this
    // tab. Store no credential, price, research or supplier response here.
    const day = today();
    const insufficient = !view.items.length || Boolean(view.coverage_start_day && view.coverage_start_day > fiveYearsBefore(day));
    if (!insufficient) return;
    autoAttempted.current = true;
    const key = `tianyan:history-attempt:${session.data.user.id}:${instrument}:${day}`;
    try { if (sessionStorage.getItem(key)) return; sessionStorage.setItem(key, '1'); } catch { /* The in-memory guard still prevents repeats. */ }
    void collect(false);
  });
  return <div className="daily-history-controls" aria-label="历史日K采集">
    <div className="daily-history-actions"><button type="button" disabled={!enabled || write.busy || polling} onClick={() => void collect(false)}>{write.busy ? '正在提交…' : polling ? '历史采集中…' : '更新近五年日K'}</button>
      <button type="button" disabled={!enabled || write.busy || polling || !view.coverage_start_day || (oldestAttempted ?? view.coverage_start_day) <= '1990-01-01'} onClick={() => void collect(true)}>获取再早五年</button></div>
    {notice && <p role="status" className="feature-note">{notice}</p>}
    {write.error && <p role="alert" className="feature-note">{write.error.message} 已有图表保留；可稍后重试。</p>}
    {!admin && <p className="feature-note">当前账号可查看历史；获取新股票历史由管理员执行。</p>}
    {view.history_reason && <p className="feature-note">{historyReasons[view.history_reason] ?? '历史采集条件尚未满足，请在数据中心核对。'}</p>}
    <details className="minute-chart-evidence"><summary>历史采集范围</summary><p>已购 Tushare 优先；每次只请求当前标的一个五年以内窗口，最多6000条。自动获取每日在本标签页仅尝试一次；缓存刷新不会请求供应商。可逐窗向前获取，不承诺停牌或上市前有K线。</p></details>
  </div>;
}
