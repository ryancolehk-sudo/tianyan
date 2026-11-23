import { useEffect, useState } from 'react';
import { tomorrowApi } from './api';
import type { ShadowEvaluation } from './model';

function nonnegative(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}
function count(value: unknown) { const parsed = nonnegative(value); return parsed !== null && Number.isInteger(parsed) ? String(parsed) : '—'; }
function money(value: unknown) { const parsed = nonnegative(value); return parsed === null ? null : '¥' + parsed.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 6 }); }
function duration(value: unknown) {
  const parsed = nonnegative(value);
  if (parsed === null) return '未知';
  return parsed < 60_000 ? `${(parsed / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} 秒` : `${(parsed / 60_000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 分钟`;
}

export function ShadowProgress({ refreshKey }: { refreshKey: string }) {
  const [data, setData] = useState<ShadowEvaluation | null>(null);
  const [busy, setBusy] = useState(true); const [failed, setFailed] = useState(false); const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true; setBusy(true); setFailed(false);
    tomorrowApi.evaluation().then(value => { if (active) setData(value.data); }).catch(() => { if (active) setFailed(true); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [refreshKey, retry]);
  const progress = data?.progress;
  const complete = progress?.observation_window_complete === true;
  const costs = data?.model_usage?.cost_cny;
  const unknownAttempts = nonnegative(costs?.unknown_attempts);
  const total = unknownAttempts !== null && unknownAttempts > 0 ? null : money(costs?.total);
  const knownTotal = money(costs?.known_total);
  return <section className="tp-panel tp-evaluation" aria-label="影子运行进度"><div className="tp-actions"><h3>影子运行进度</h3><span className="tp-muted">{complete ? '观察天数已达标 · 待本人质量验收' : '持续积累研究样本'}</span><button onClick={() => setRetry(value => value + 1)} disabled={busy}>{busy ? '正在读取…' : '重读运行记录'}</button></div>
    {busy && !data && <p className="tp-muted" role="status">正在读取已保存的运行记录…</p>}
    {failed && <p className="tp-muted" role="status">运行记录读取失败，报告与设置仍可使用。{data && '下方保留上次读取的记录。'}</p>}
    {!busy && !failed && !progress && <p className="tp-muted">暂无可用的影子运行记录。</p>}
    {progress && <><dl className="tp-evaluation-metrics"><div><dt>真实提前保存样本</dt><dd>{count(progress.eligible_shadow_target_day_count)} / {count(progress.goal)} 个交易日</dd></div><div><dt>已保存 / 本人确认</dt><dd>{count(data?.reports?.latest_count)} / {count(data?.reports?.confirmed_latest_count)} 份</dd></div><div><dt>实际 AI 费用</dt><dd>{total ?? '未知'}</dd>{!total && knownTotal && <small>已知部分 {knownTotal}</small>}{unknownAttempts !== null && unknownAttempts > 0 && <small>{count(unknownAttempts)} 次调用费用未知</small>}</div><div><dt>95% 任务完成耗时</dt><dd>{duration(data?.timing?.end_to_end_ms_p95)}</dd></div><div><dt>核心资料缺失</dt><dd>{count(data?.data_quality?.core_data_missing)} 份</dd></div></dl><p className="tp-muted">{progress.acceptance_status === 'requires_human_quality_review' || complete ? '观察窗口已完成，仍需本人核对事实、证据、条件与修订质量。' : '按目标交易日统计提前保存的研究，历史重建不计入真实提前样本。'}费用按实际调用记录展示，缺少费用记录时保留未知。</p></>}
  </section>;
}
