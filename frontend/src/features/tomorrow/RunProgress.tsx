import { useEffect, useState } from 'react';
import { isRunning, type RunView } from './model';
import { tomorrowApi } from './api';
import './runProgress.css';

const phases = [
  ['preparation', '准备资料'], ['market_events', '市场与事件'], ['sectors_stocks', '板块与个股'],
  ['conditional_plan', '条件预案'], ['review_write', '全文核验'],
] as const;

function duration(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return seconds < 60 ? seconds + ' 秒' : Math.floor(seconds / 60) + ' 分 ' + seconds % 60 + ' 秒';
}

export function RunProgress({run, history, readError}: {run: RunView; history: RunView[] | null; readError: string}) {
  const active = isRunning(run);
  const [now, setNow] = useState(Date.now);
  const [past, setPast] = useState<RunView[]>([]);
  useEffect(() => {
    let current = true;
    if (active && run.requested_model) tomorrowApi.runs().then(result => {
      if (current) setPast(result.data.items);
    }).catch(() => {});
    return () => { current = false; };
  }, [active, run.id, run.requested_model]);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  const stages = run.stages ?? [];
  const ai = !!run.requested_model || stages.length > 0;
  const steps = ai ? phases : phases.slice(0, 1);
  const last = stages[stages.length - 1];
  const index = Math.max(run.report_id && ai ? 1 : 0, ...stages.map(s => phases.findIndex(([id]) => id === s.role)));
  const done = run.state === 'succeeded' ? steps.length : Math.min(index, steps.length - 1);
  const elapsed = (active ? now : Date.parse(run.completed_at || '')) - Date.parse(run.created_at);
  const unique = new Map([...past, ...(history ?? [])].map(record => [record.id, record]));
  const samples = [...unique.values()].filter(r => r.state === 'succeeded' && run.requested_model && !r.error_code
    && Date.parse(r.completed_at || '') <= Date.parse(run.created_at)
    && r.requested_model === run.requested_model && r.slot === run.slot && r.completed_at)
    .map(r => Date.parse(r.completed_at!) - Date.parse(r.created_at)).filter(ms => ms > 0).sort((a,b) => a-b);
  const typical = (samples[Math.floor(samples.length / 2)] + samples[Math.floor((samples.length - 1) / 2)]) / 2;
  const estimate = samples.length < 3 || !Number.isFinite(elapsed) ? '同模型的完成记录不足，暂无法估时'
    : typical > elapsed ? '预计还需约 ' + duration(typical - elapsed) + '（参考同模型历史耗时）' : '已超过同模型历史中位耗时，剩余时间待定';
  const batch = last && stages.filter(s => s.role === last.role).length;
  const stage = run.error_code === 'retry_exhausted' && !stages.length ? '资料准备多次重试后停止，尚未进入模型复盘'
    : !active && run.model_error ? run.model_error : active && !stages.length && run.stage === '等待冻结研究资料' ? '等待资料准备完成' : run.stage;
  return <section className="pr-run-progress" data-active={active && !readError || undefined} aria-label="预案生成进度">
    <div className="pr-progress-heading"><strong>{active ? '预案生成中' : run.state === 'succeeded' ? '生成完成' : '生成已停止'}</strong>
      {Number.isFinite(elapsed) && <span>{active ? '已等待 ' : '共用时 '}{duration(elapsed)}</span>}</div>
    <p role="status">{[run.requested_model, stage, batch && batch > 1 ? '第 ' + batch + ' 批' : ''].filter(Boolean).join(' · ')}</p>
    <div className="pr-progress-track" role="progressbar" aria-label="已完成阶段" aria-valuemin={0}
      aria-valuenow={done} aria-valuemax={steps.length} aria-valuetext={`已完成 ${done}/${steps.length} 个阶段`}>
      <span className="pr-progress-fill" style={{width: `${done / steps.length * 100}%`}}/>
    </div>
    <div className="pr-progress-meta"><span>已完成 {done}/{steps.length} 个阶段</span>{active && <span>{estimate}</span>}</div>
    <ol>{steps.map(([id,name],i) => <li key={id} data-state={i < done ? 'done' : i === done && active ? 'current' : undefined}
      aria-current={i === done && active ? 'step' : undefined}>{i < done ? '✓ ' : ''}{name}</li>)}</ol>
    {readError && <p role="alert">进度读取失败，显示上次状态。{active && '正在重试。'}</p>}
  </section>;
}
