import { useCallback, useEffect, useRef, useState } from 'react';
import type { SetStateAction } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { tomorrowApi } from './api';
import { displayTime, isRunning, label, records, slotLabel } from './model';
import type { EvidenceRef, ReportView, RunView, SettingsView, Slot, Validation } from './model';
import { PlanSettings } from './Settings';
import { ShadowProgress } from './Evaluation';
import { DossierBrowser } from './DossierBrowser';
import { CorePlan, PlanValidation, References, ReportRevision, ReportSection } from './Report';
import './tomorrow.css';

const roleNames: Record<string, string> = { market: '市场与事件研究', market_events: '市场与事件研究', candidates: '板块与标的研究', sectors_stocks: '板块与标的研究', conditional_plan: '条件预案研究', review_write: '全文核验与写作', sector_stock: '板块与标的研究', scenarios: '条件预案研究', plan: '条件预案研究', review: '全文核验与写作', synthesis: '全文核验与写作', preparation: '资料准备', snapshot: '冻结资料', calculations: '规则计算' };
function message(error: unknown) { return error instanceof Error ? error.message : '读取失败，请重试。'; }

// Preserve a displayed value during revalidation, but never paint a different
// day's/run's/report's value while its replacement is still being requested.
function useScopedValue<T>(scope: string, empty: T) {
  const [stored, setStored] = useState({ scope, value: empty });
  const setValue = useCallback((next: SetStateAction<T>) => {
    setStored(previous => ({ scope, value: typeof next === 'function'
      ? (next as (value: T) => T)(previous.scope === scope ? previous.value : empty) : next }));
  }, [scope]);
  return [stored.scope === scope ? stored.value : empty, setValue] as const;
}

export function Tomorrow({ session }: { session: SessionResponse }) {
  const nav = useNavigation(); const query = new URLSearchParams(nav.search);
  const day = query.get('source_trade_day') ?? query.get('trade_day') ?? '';
  const selectedRun = query.get('run') ?? ''; const selectedReport = query.get('report') ?? '';
  const writable = session.data.user.role !== 'viewer'; const csrf = session.data.csrf_token;
  const [runs, setRuns] = useScopedValue<RunView[] | null>(day, null); const [settings, setSettings] = useState<SettingsView | null>(null);
  const runId = selectedRun || runs?.[0]?.id || '';
  const runScope = `${day}:${runId}`;
  const [run, setRun] = useScopedValue<RunView | null>(runScope, null);
  const reportId = selectedReport || run?.report_id || '';
  const reportScope = `${runScope}:${reportId}`;
  const [report, setReport] = useScopedValue<ReportView | null>(reportScope, null);
  const [validations, setValidations] = useScopedValue<Validation[]>(reportScope, []); const [validationEpoch, setValidationEpoch] = useState(0);
  const [validationEvidence, setValidationEvidence] = useScopedValue<EvidenceRef[]>(reportScope, []);
  const [epoch, setEpoch] = useState(0); const [loading, setLoading] = useState(true); const [reportBusy, setReportBusy] = useState(false);
  const [readError, setReadError] = useState(''); const [settingsError, setSettingsError] = useState(''); const [runError, setRunError] = useState(''); const [reportError, setReportError] = useState(''); const [validationError, setValidationError] = useState('');
  const [actionError, setActionError] = useState(''); const [actionBusy, setActionBusy] = useState(false); const [notice, setNotice] = useState('');
  const [useAI, setUseAI] = useState(true); const [slot, setSlot] = useState<Slot>('manual'); const [targetDay, setTargetDay] = useState(''); const [cutoff, setCutoff] = useState('');
  const actionLock = useRef(false); const listSequence = useRef(0);
  function navigate(values: Record<string, string | null>) { const url = new URL(location.href); for (const [key, value] of Object.entries(values)) { if (value) url.searchParams.set(key, value); else url.searchParams.delete(key); } updateLocation(url); }
  function chooseRun(id: string) { navigate({ run: id, report: null }); }
  useEffect(() => {
    let active = true; const sequence = ++listSequence.current;
    setLoading(true); setReadError('');
    tomorrowApi.runs(day).then(value => { if (active && sequence === listSequence.current) setRuns(value.data.items); }).catch(error => { if (active) setReadError(message(error)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [day, epoch]);
  useEffect(() => {
    let active = true; setSettingsError('');
    tomorrowApi.settings().then(value => { if (active) { setSettings(value.data); setUseAI(value.data.use_ai); } }).catch(error => { if (active) setSettingsError(message(error)); });
    return () => { active = false; };
  }, [epoch]);
  useEffect(() => {
    let active = true; setRunError('');
    if (runId) tomorrowApi.run(runId).then(value => { if (active) setRun(value.data); }).catch(error => { if (active) setRunError(message(error)); });
    return () => { active = false; };
  }, [runScope, epoch]);
  useEffect(() => {
    if (!run || !isRunning(run)) return;
    let active = true; let timer: number;
    const poll = async () => {
      try {
        const value = (await tomorrowApi.run(run.id)).data; if (!active) return;
        setRun(value); setRunError('');
        setRuns(items => items?.map(item => item.id === value.id ? value : item) ?? [value]);
        if (!isRunning(value)) return;
      } catch (error) { if (active) setRunError(message(error)); }
      if (active) timer = window.setTimeout(poll, 4000);
    };
    timer = window.setTimeout(poll, 2500); return () => { active = false; window.clearTimeout(timer); };
  }, [runScope, run?.id, run?.state]);
  useEffect(() => {
    let active = true; setReportError(''); setValidationError(''); setReportBusy(!!reportId);
    if (reportId) tomorrowApi.report(reportId).then(value => { if (active) setReport(value.data); }).catch(error => { if (active) setReportError(message(error)); }).finally(() => { if (active) setReportBusy(false); });
    return () => { active = false; };
  }, [reportScope, epoch]);
  useEffect(() => {
    let active = true; setValidationError('');
    if (reportId) Promise.allSettled([tomorrowApi.validations(reportId), tomorrowApi.validationEvidence(reportId)]).then(results => {
      if (!active) return;
      const [history, evidence] = results;
      if (history.status === 'fulfilled') setValidations(history.value.data.items);
      else setValidationError(message(history.reason));
      if (evidence.status === 'fulfilled') setValidationEvidence(evidence.value.data.items);
      else setValidationError(value => [value, '验证依据读取失败：' + message(evidence.reason)].filter(Boolean).join('；'));
    });
    return () => { active = false; };
  }, [reportScope, validationEpoch, epoch]);
  useEffect(() => { window.dispatchEvent(new CustomEvent('ty-terminal-context', { detail: { trade_day: report?.source_trade_day ?? run?.source_trade_day ?? day, mode: 'review' } })); }, [day, report?.id, run?.id]);
  async function generate() {
    if (!writable || actionLock.current) return; actionLock.current = true; setActionBusy(true); setActionError(''); setNotice('');
    try {
      const value = (await tomorrowApi.generate({ ...(day ? { source_trade_day: day } : {}), ...(targetDay ? { target_trade_day: targetDay } : {}), ...(cutoff ? { as_of_time: cutoff + '+08:00' } : {}), slot, use_ai: useAI }, csrf)).data;
      setRun(value); setRuns(items => [value, ...(items ?? []).filter(item => item.id !== value.id)]); chooseRun(value.id); setNotice('研究任务已安排，可在下方查看阶段进度。');
    } catch (error) { setActionError(message(error)); } finally { actionLock.current = false; setActionBusy(false); }
  }
  async function prepare() {
    if (!writable || actionLock.current) return; actionLock.current = true; setActionBusy(true); setActionError(''); setNotice('');
    const codes = records(report?.dossier.watchlist).map(row => row.code).filter((code): code is string => typeof code === 'string');
    try { await tomorrowApi.prepare({ ...(day || report?.source_trade_day ? { source_trade_day: day || report?.source_trade_day } : {}), candidates: codes }, csrf); setNotice('资料准备已安排。采集状态请在数据管理中查看，准备完成后重新生成研究。'); }
    catch (error) { setActionError(message(error)); } finally { actionLock.current = false; setActionBusy(false); }
  }
  function savedReport(value: ReportView) { setReport(value); navigate({ report: value.id }); setNotice(value.status === 'confirmed' ? '当前预案已人工确认。' : '修订已保存，共享判断及受影响章节已更新。'); }
  return <div className="tp-root" aria-busy={loading || reportBusy}>
    <div className="tp-toolbar"><div className="tp-date"><label>来源交易日<input type="date" value={day} onChange={e => navigate({ source_trade_day: e.target.value, trade_day: null, run: null, report: null })}/></label><button onClick={() => navigate({ source_trade_day: null, trade_day: null, run: null, report: null })}>最近已保存</button><span className="tp-badge">{session.meta.app_mode === 'demo' ? '演示环境 · 非真实行情' : '本人研究 · 缓存证据'}</span></div><button aria-label="重读已存结果" aria-busy={loading || reportBusy} onClick={() => setEpoch(value => value + 1)}>{(loading || reportBusy) && report ? '正在更新…' : '重读已存结果'}</button></div>
    <section className="tp-panel tp-generator"><div className="tp-actions"><h2>明日预案</h2><label className="tp-check"><input type="checkbox" checked={useAI} disabled={!writable || actionBusy} onChange={e => setUseAI(e.target.checked)}/>使用 AI 分阶段研究</label><button className="primary" disabled={!writable || actionBusy || isRunning(run)} onClick={generate}>{actionBusy ? '正在安排…' : useAI ? '生成研究草稿' : '生成仅规则稿'}</button><button disabled={!writable || actionBusy} onClick={prepare}>准备研究资料</button></div><p className="tp-muted">打开页面只读取已保存结果。生成时冻结资料截止点，再进行计算、研究与核验；仅规则稿保留事实和数据缺口。</p><details className="tp-generation-options"><summary>生成日期与资料截止</summary><div className="tp-form-grid"><label>稿件类型<select value={slot} onChange={e => setSlot(e.target.value as Slot)}><option value="manual">手动研究</option><option value="evening">晚间完整稿</option><option value="overnight">早间补充</option></select></label><label>目标交易日<input type="date" value={targetDay} onChange={e => setTargetDay(e.target.value)}/></label><label>接收截止（北京时间）<input type="datetime-local" step="1" value={cutoff} onChange={e => setCutoff(e.target.value)}/></label></div><small>日期留空时由交易日历确定。历史补采资料会标记为重建，晚间稿和早间补充保留各自记录。</small></details></section>
    {actionError && <div role="alert" className="tp-error">{actionError}<a href="/data?view=connections">检查 AI 与资料接入</a></div>}{notice && <p role="status" className="tp-success">{notice}</p>}
    {readError && <div role="alert" className="tp-error">{readError}<button onClick={() => setEpoch(value => value + 1)}>重试读取</button></div>}
    {settingsError && <div role="alert" className="tp-error">设置读取失败：{settingsError}<button onClick={() => setEpoch(value => value + 1)}>重试设置</button></div>}
    {loading && !runs ? <p role="status" className="tp-empty">正在读取已存研究任务…</p> : runs && <div className="tp-history"><label>已存研究<select value={runId} onChange={e => chooseRun(e.target.value)} disabled={!runs.length}><option value="" disabled>{runs.length ? '选择已存稿件' : '尚无研究任务'}</option>{runs.map(item => <option key={item.id} value={item.id}>{item.source_trade_day} → {item.target_trade_day} · {slotLabel[item.slot]} · {label(item.state)} · {displayTime(item.created_at)}</option>)}</select></label><small>{runs.length ? `共 ${runs.length} 份任务` : '可使用已有缓存生成第一份草稿'}</small></div>}
    {runError && <div role="alert" className="tp-error">任务进度读取失败：{runError}<button onClick={() => setEpoch(value => value + 1)}>重试进度</button></div>}
    {run && <section className="tp-panel tp-run" aria-label="研究任务进度"><div className="tp-actions"><b>{slotLabel[run.slot]} · {label(run.state)}</b><span>{run.stage}</span><small>{displayTime(run.created_at)}</small></div>{!!run.stages?.length && <ol className="tp-stages">{run.stages.map((stage, i) => <li key={i} className={stage.state === 'running' ? 'tp-current' : ''}><span>{i + 1}</span>{roleNames[stage.role] ?? `研究阶段 ${i + 1}`}<small>{label(stage.state)}</small></li>)}</ol>}{isRunning(run) && <p role="status" className="tp-muted">后台持续研究中，当前页面会自动更新进度。</p>}{run.state === 'outcome_unknown' && <p className="tp-warning">模型请求结果尚未核实。请先在运行监控确认，避免重复产生费用。</p>}{!!run.limitations?.length && <ul className="tp-limitations">{run.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>}</section>}
    {reportBusy && !report && <p role="status" className="tp-empty">正在读取已保存的八章研究…</p>}
    {reportError && <div role="alert" className="tp-error">报告读取失败：{reportError}<button onClick={() => setEpoch(value => value + 1)}>重试报告</button></div>}
    {report && <div className="tp-report" key={report.id + ':' + report.revision_number}>
      {report.based_on_id && <p className="tp-relationship">本稿包含对前稿的补充或修订。<button onClick={() => navigate({ report: report.based_on_id! })}>查看所依据的原稿</button></p>}
      <CorePlan report={report}/><ReportRevision report={report} csrf={csrf} writable={writable} saved={savedReport}/>
      <div className="tp-chapters-heading"><h2>完整研究</h2><span>八个章节 · 按依据与数据就绪程度展开</span></div>{report.sections.map(section => <ReportSection key={section.key} section={section} refs={report.evidence_refs}/>)}
      <DossierBrowser runId={report.run_id} refs={report.evidence_refs}/>
      <section className="tp-panel"><h3>本次证据目录</h3><References ids={report.evidence_refs.map(ref => ref.id)} refs={report.evidence_refs}/>{!report.evidence_refs.length && <p className="tp-warning">本稿没有可读取的证据，不能据此形成确定判断。</p>}</section>
      {validationError && <div role="alert" className="tp-error">验证记录读取失败：{validationError}<button onClick={() => setValidationEpoch(value => value + 1)}>重试验证记录</button></div>}<PlanValidation report={report} evidence={validationEvidence} items={validations} csrf={csrf} writable={writable} reload={() => setValidationEpoch(value => value + 1)}/>
    </div>}
    {!loading && !readError && !runId && !selectedReport && <div className="tp-panel tp-empty"><h3>研究从已缓存的事实开始</h3><p>目前还没有保存的预案。可先准备资料，再生成草稿；缺少的数据会保留缺口说明。</p></div>}
    <ShadowProgress refreshKey={`${epoch}:${report?.id ?? ''}:${run?.completed_at ?? ''}`}/>{settings && <PlanSettings settings={settings} csrf={csrf} writable={writable} saved={value => { setSettings(value); setUseAI(value.use_ai); }}/>}<p className="tp-footer">事实、规则计算、AI 判断与人工修订分别留存。需要盘口观察的条件由本人确认。</p>
  </div>;
}
