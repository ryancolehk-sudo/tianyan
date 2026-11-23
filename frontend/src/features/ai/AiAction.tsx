import { useEffect, useRef, useState } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api, request, write } from '../../shared/api';
import { monitorHref } from '../../shared/dataMonitorLink';
import './aiAction.css';

type EvidenceRef = { id: string; title: string; url?: string | null; source_name?: string | null; source_time?: string | null };
type Run = { id: string; job_id: string; feature: string; state: string; stage: string; model: string;
  billing_source?: 'api' | 'chatgpt_plan';
  created_at: string; completed_at: string | null; error_code: string | null; limitations: string[]; evidence_refs: EvidenceRef[];
  result: { summary: string; items: { title: string; text: string; evidence_refs: string[] }[] } | null };
type Settings = { enabled: boolean; personal_connection_id?: string | null; personal_model_ready?: boolean;
  features: { id: string; enabled: boolean; implemented: boolean; effective_connection_id: string | null }[] };
type Envelope<T> = { data: T };
type Props = { feature: 'theme_summary' | 'news_summary'; session?: SessionResponse; tradeDay?: string | null;
  asOfTime?: string; newsId?: string; inputAvailable?: boolean };
const active = (row: Run | null) => !!row && ['queued', 'running', 'retry_wait', 'preparing', 'requesting', 'validating'].includes(row.state);
const timestamp = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });

export function AiAction({ feature, session: supplied, tradeDay, asOfTime, newsId, inputAvailable = true }: Props) {
  const [session, setSession] = useState(supplied);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const sending = useRef(false);
  const generation = useRef(0);
  const attemptKey = useRef<{ key: string; regenerate: boolean } | null>(null);
  const context = [feature, tradeDay ?? '', asOfTime ?? '', newsId ?? '', supplied?.data.user.id ?? ''].join('|');
  useEffect(() => {
    const epoch = ++generation.current;
    let live = true;
    setRun(null); setError(''); setLoading(true); attemptKey.current = null;
    const query = new URLSearchParams({ feature, ...(tradeDay ? { trade_day: tradeDay } : {}),
      ...(asOfTime ? { as_of_time: asOfTime } : {}), ...(newsId ? { news_id: newsId } : {}) });
    Promise.all([supplied ? Promise.resolve(supplied) : api.session(), request<Envelope<Settings>>('/api/v1/ai/settings'),
      request<Envelope<{ items: Run[] }>>('/api/v1/ai/runs?' + query)])
      .then(([identity, config, history]) => { if (live && generation.current === epoch) {
        setSession(identity); setSettings(config.data); setRun(history.data.items[0] ?? null);
      } })
      .catch(reason => { if (live) setError(reason instanceof Error ? reason.message : 'AI 状态读取失败。'); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [context]);
  useEffect(() => {
    if (!active(run)) return;
    let timer: number | undefined;
    let live = true;
    const epoch = generation.current;
    const poll = async () => {
      if (document.visibilityState !== 'visible') return;
      try {
        const response = await request<Envelope<Run>>('/api/v1/ai/runs/' + encodeURIComponent(run!.id));
        if (live && generation.current === epoch) { setRun(response.data); setError(''); }
      } catch (reason) { if (live) setError(reason instanceof Error ? reason.message : '任务状态读取失败。'); }
      if (live) timer = window.setTimeout(poll, 5000);
    };
    const onVisible = () => { if (document.visibilityState === 'visible') { window.clearTimeout(timer); void poll(); } };
    timer = window.setTimeout(poll, 1000);
    document.addEventListener('visibilitychange', onVisible);
    return () => { live = false; window.clearTimeout(timer); document.removeEventListener('visibilitychange', onVisible); };
  }, [run?.id, run?.state, context]);
  const binding = settings?.features.find(row => row.id === feature);
  const ready = !!settings?.enabled && !!binding?.enabled && !!binding.effective_connection_id
    && (!settings.personal_connection_id || !!settings.personal_model_ready);
  const usingPlan = !!settings?.personal_connection_id;
  const planLimit = run?.error_code === 'subscription_sharing_usage_limit_exceeded';
  const allowed = !!session && session.data.user.role !== 'viewer';
  async function generate(regenerate: boolean) {
    if (!session || sending.current || active(run) || !ready || !inputAvailable) return;
    sending.current = true; setBusy(true); setError('');
    const epoch = generation.current;
    if (!attemptKey.current || attemptKey.current.regenerate !== regenerate) attemptKey.current = { key: crypto.randomUUID(), regenerate };
    try {
      const response = await write<Envelope<Run>>('/api/v1/ai/runs', { feature, regenerate,
        ...(tradeDay ? { trade_day: tradeDay } : {}), ...(asOfTime ? { as_of_time: asOfTime } : {}),
        ...(newsId ? { news_id: newsId } : {}) }, session.data.csrf_token, attemptKey.current.key);
      if (generation.current === epoch) { setRun(response.data); attemptKey.current = null; }
    } catch (reason) { if (generation.current === epoch) setError(reason instanceof Error ? reason.message : '生成请求未确认，请重试。'); }
    finally { sending.current = false; setBusy(false); }
  }
  const title = feature === 'theme_summary' ? '题材归纳' : '新闻摘要';
  return <section className="ai-action" aria-label={title}>
    <div className="ai-action-toolbar"><span className="ai-reference-badge">AI 参考</span>
      <button type="button" disabled={loading || busy || active(run) || !ready || !allowed || !inputAvailable}
        onClick={() => void generate(!!run)}>{busy ? '提交中…' : active(run) ? '正在生成…' : run ? '重新生成' : '生成' + title}</button></div>
    {loading ? <p role="status">正在读取模型配置与已存结果…</p> : !ready ? <p className="ai-action-note">启用 AI 并分配模型后可手动生成。<a href="/data">前往接入管理 ›</a></p> : <p className="ai-action-note">点击后调用模型，页面刷新只读取已存结果。</p>}
    {usingPlan && <p className="ai-action-note">Using ChatGPT plan · 使用本人已授权套餐。<a href="https://chatgpt.com/settings/usage" target="_blank" rel="noopener noreferrer">管理使用额度</a></p>}
    {!inputAvailable && <p className="ai-action-note">当前条目缺少正文或原生摘要，资料不足。</p>}
    {error && <p className="ai-action-error" role="alert">{error}</p>}
    {active(run) && <div className="ai-action-progress" role="status"><span/>{run!.stage}</div>}
    {run && !active(run) && !run.result && <p className="ai-action-error">{run.state === 'unknown' || run.error_code === 'result_unknown' ? '请求结果未知，未自动重发。可核对调用记录后手动重新生成。' : '本次未产生可用结果。'}{run.error_code && <small> {run.error_code}</small>}</p>}
    {planLimit && <p className="ai-action-error">已达到套餐或此应用的使用限制。<a href="https://chatgpt.com/settings/usage" target="_blank" rel="noopener noreferrer">管理使用额度</a></p>}
    {run?.result && <div className="ai-action-result"><p>{run.result.summary}</p>{run.result.items.map((item, index) => <article key={index}><h4>{item.title}</h4><p>{item.text}</p><small>依据 {item.evidence_refs.map(ref => run.evidence_refs.find(row => row.id === ref)?.title ?? ref).join('、')}</small></article>)}</div>}
    {run && <details className="ai-action-evidence"><summary>依据、范围与生成记录</summary><p>模型 {run.model} · {timestamp(run.created_at)}（北京时间）</p>
      {run.billing_source === 'chatgpt_plan' && <p>本次使用本人 ChatGPT 套餐；未按 API 单价估算费用。</p>}
      {run.limitations.map((item, index) => <p key={index}>{item}</p>)}<ul>{run.evidence_refs.map(ref => <li key={ref.id}>{ref.url && /^https?:\/\//.test(ref.url) ? <a href={ref.url} target="_blank" rel="noopener noreferrer">{ref.title}</a> : ref.title}{ref.source_name && <small> · {ref.source_name}</small>}</li>)}</ul>
      <a href={monitorHref({ tradeDay, asOfTime, context: asOfTime ? 'history' : 'current', returnTo: location.pathname + location.search + location.hash })}>查看当前运行与调用记录 ›</a></details>}
  </section>;
}
