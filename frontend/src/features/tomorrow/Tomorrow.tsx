import paperStyles from './paperReport.css?inline';
import { ProgressCircle } from '../../shared/ProgressCircle';
import { PlanPreferences, type PreferencePage } from './PlanPreferences';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { SetStateAction } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { ApiError } from '../../shared/api';
import { runsPath, tomorrowApi } from './api';
import { freshRead, peekRead } from '../../shared/readCache';
import { displayTime, isRunning, label, records, slotLabel } from './model';
import type { DirectReviewInput, EvidenceRef, ReportView, RunView, SettingsView, Slot, Validation } from './model';
import { PaperReport } from './PaperReport';
import { ModelPicker, reviewSetup, useReviewChoice } from './ModelPicker';
import { ReportHistory } from './ReportHistory';
import { RunProgress } from './RunProgress';
import { SessionBrief } from './SessionBrief';
import { DailyOverview } from './DailyOverview';
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
  const oauthQuery = new URLSearchParams(location.search);
  const returnedToCodex = oauthQuery.get('setup') === 'codex' && oauthQuery.get('chatgpt') === 'connected';
  const [preferences, setPreferences] = useState<PreferencePage | null>(() =>
    oauthQuery.get('setup') === 'codex' && !returnedToCodex ? 'account' : null);
  const { choice, choose } = useReviewChoice(session.data.user.id, returnedToCodex);
  const nav = useNavigation(); const query = new URLSearchParams(nav.search);
  const day = query.get('source_trade_day') ?? query.get('trade_day') ?? '';
  const selectedRun = query.get('run') ?? ''; const selectedReport = query.get('report') ?? '';
  const view = query.get('view') || 'report';
  const writable = session.data.user.role !== 'viewer'; const csrf = session.data.csrf_token;
  const cachedRuns = peekRead<{data:{items:RunView[]}}>(runsPath(day))?.data.items ?? null;
  const [runs, setRuns] = useScopedValue<RunView[] | null>(day, cachedRuns); const [settings, setSettings] = useState<SettingsView | null>(null);
  const newest = runs?.[0];
  const readingDay = day || runs?.reduce((latest, item) => item.source_trade_day > latest ? item.source_trade_day : latest, '');
  const dayRuns = runs?.filter(item => item.source_trade_day === readingDay);
  const dayReport = dayRuns?.find(item => item.report_id);
  const latest = dayRuns?.[0];
  const followLatest = latest && (isRunning(latest) || (latest.requested_model && latest.state === 'failed'));
  const runId = selectedRun || (view === 'workspace' ? newest?.id : !selectedReport && followLatest ? latest.id : dayReport?.id || latest?.id) || '';
  const listedRun = runs?.find(item => item.id === runId);
  const listedReportId = listedRun?.report_id;
  const runScope = `${day}:${runId}`;
  const [details, setRun] = useScopedValue<RunView | null>(runScope, null);
  const run = details ?? listedRun ?? null;
  const reportId = selectedReport || run?.report_id || '';
  const reportScope = `${runScope}:${reportId}`;
  const [report, setReport] = useScopedValue<ReportView | null>(reportScope, null);
  const [validations, setValidations] = useScopedValue<Validation[]>(reportScope, []); const [validationEpoch, setValidationEpoch] = useState(0);
  const [validationEvidence, setValidationEvidence] = useScopedValue<EvidenceRef[]>(reportScope, []);
  const [epoch, setEpoch] = useState(0); const [loading, setLoading] = useState(true); const [reportBusy, setReportBusy] = useState(false);
  const [readError, setReadError] = useState(''); const [settingsError, setSettingsError] = useState(''); const [runError, setRunError] = useState(''); const [reportError, setReportError] = useState(''); const [validationError, setValidationError] = useState('');
  const [actionError, setActionError] = useState(''); const [actionBusy, setActionBusy] = useState(false); const [actionNotice, setNotice] = useState('');
  const [submittedRun, setSubmittedRun] = useState('');
  const [settingsVersion, setSettingsVersion] = useState(0);
  const settingsSequence = useRef(0);
  const setup = reviewSetup(settings, choice);
  const [slot, setSlot] = useState<Slot>('manual'); const [targetDay, setTargetDay] = useState(''); const [cutoff, setCutoff] = useState('');
  const actionLock = useRef(false); const listSequence = useRef(0);
  const pendingSubmission = useRef<{intent: string; body: Parameters<typeof tomorrowApi.generate>[0]; key: string} | null>(null);
  function navigate(values: Record<string, string | null>) { const url = new URL(location.href); for (const [key, value] of Object.entries(values)) { if (value) url.searchParams.set(key, value); else url.searchParams.delete(key); } updateLocation(url); }
  function chooseRun(id: string) { navigate({ run: id, report: null }); }
  useEffect(() => {
    let active = true;
    let timer: number;
    const sequence = ++listSequence.current;
    const refresh = async () => {
      try {
        const value = await freshRead(() => tomorrowApi.runs(day));
        if (active && sequence === listSequence.current) { setRuns(value.data.items); setReadError(''); }
      } catch (error) {
        if (active) setReadError(message(error));
      } finally {
        if (active) {
          setLoading(false);
          if (!selectedRun && !selectedReport) timer = window.setTimeout(refresh, 60000);
        }
      }
    };
    setLoading(true); setReadError(''); void refresh();
    return () => { active = false; window.clearTimeout(timer); };
  }, [day, epoch, selectedRun, selectedReport]);
  useEffect(() => {
    const sequence = ++settingsSequence.current;
    freshRead(tomorrowApi.settings).then(value => {
      if (sequence === settingsSequence.current) { setSettings(value.data); setSettingsError(''); }
    }).catch(error => {
      if (sequence === settingsSequence.current) { setSettings(null); setSettingsError(message(error)); }
    });
    return () => { settingsSequence.current++; };
  }, [settingsVersion, session.data.user.id]);
  useEffect(() => {
    let active = true; setRunError('');
    if (runId) freshRead(() => tomorrowApi.run(runId)).then(value => { if (active) setRun(value.data); }).catch(error => { if (active) setRunError(message(error)); });
    return () => { active = false; };
  }, [runScope, listedReportId, epoch]);
  useEffect(() => {
    if (!run || !isRunning(run)) return;
    let active = true; let timer: number;
    const poll = async () => {
      try {
        const value = (await freshRead(() => tomorrowApi.run(run.id))).data; if (!active) return;
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
    if (reportId && view === 'workspace') Promise.allSettled([tomorrowApi.validations(reportId), tomorrowApi.validationEvidence(reportId)]).then(results => {
      if (!active) return;
      const [history, evidence] = results;
      if (history.status === 'fulfilled') setValidations(history.value.data.items);
      else setValidationError(message(history.reason));
      if (evidence.status === 'fulfilled') setValidationEvidence(evidence.value.data.items);
      else setValidationError(value => [value, '验证依据读取失败：' + message(evidence.reason)].filter(Boolean).join('；'));
    });
    return () => { active = false; };
  }, [reportScope, validationEpoch, epoch, view]);
  useEffect(() => { window.dispatchEvent(new CustomEvent('ty-terminal-context', { detail: { trade_day: report?.source_trade_day ?? run?.source_trade_day ?? day, mode: 'review' } })); }, [day, report?.id, run?.id]);
  function savedSettings(value: SettingsView) {
    settingsSequence.current++;
    setSettings(value); setSettingsError(''); setActionError('');
  }
  function refreshSettings() { setSettingsVersion(value => value + 1); }
  function closePreferences() {
    setPreferences(null);
    if (preferences === 'models') refreshSettings();
    if (query.get('setup') === 'codex') navigate({ setup: null, chatgpt: null });
  }
  async function generate(paper?: ReportView, direct?: DirectReviewInput) {
    if (!writable || !settings || (!direct && !setup.ready) || actionLock.current || isRunning(run)
      || run?.state === 'outcome_unknown' || run?.error_code === 'outcome_unknown') return;
    actionLock.current = true; setActionBusy(true); setActionError(''); setNotice('');
    try {
      const input = paper ? {
        source_trade_day: paper.source_trade_day, target_trade_day: paper.target_trade_day ?? undefined,
        as_of_time: paper.as_of_time, slot: paper.slot, regenerate: true,
      } : view !== 'workspace' ? {
        source_trade_day: day || run?.source_trade_day, slot: 'manual' as const, regenerate: !!run,
      } : { ...(day ? { source_trade_day: day } : {}), ...(targetDay ? { target_trade_day: targetDay } : {}),
        ...(cutoff ? { as_of_time: cutoff + '+08:00' } : {}), slot };
      const body = { ...input, expected_settings_revision: settings.revision, use_ai: !!direct || setup.method !== 'rule',
        ...(direct ?? (setup.model && { model_connection_id: setup.model.id })) };
      // A lost response can arrive after the server has already enabled the connection.
      // Retry the original payload and key when the selected model, materials and report are the same.
      const intent = JSON.stringify(body, (field, value) =>
        ['connection_revision', 'expected_settings_revision', 'enable_connection', 'enable_ai'].includes(field) ? undefined : value);
      if (pendingSubmission.current?.intent !== intent) pendingSubmission.current = {intent, body, key: crypto.randomUUID()};
      const pending = pendingSubmission.current;
      const value = (await tomorrowApi.generate(pending.body, csrf, pending.key)).data;
      pendingSubmission.current = null;
      setRun(value); setRuns(items => [value, ...(items ?? []).filter(item => item.id !== value.id)]);
      chooseRun(value.id);
      setSubmittedRun(value.id);
      if (direct) refreshSettings();
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408) {
        pendingSubmission.current = null;
        setActionError(message(error));
      } else {
        setActionError(message(error) + '。提交结果待确认，再次点同一模型可核对这次任务。');
      }
      refreshSettings();
    } finally { actionLock.current = false; setActionBusy(false); }
  }
  async function prepare() {
    if (!writable || actionLock.current) return; actionLock.current = true; setActionBusy(true); setActionError(''); setNotice('');
    const codes = records(report?.dossier.watchlist).map(row => row.code).filter((code): code is string => typeof code === 'string');
    try { await tomorrowApi.prepare({ ...(day || report?.source_trade_day ? { source_trade_day: day || report?.source_trade_day } : {}), candidates: codes }, csrf); setNotice('资料准备已安排。采集状态请在数据管理中查看，准备完成后重新生成研究。'); }
    catch (error) { setActionError(message(error)); } finally { actionLock.current = false; setActionBusy(false); }
  }
  function savedReport(value: ReportView) { setReport(value); navigate({ report: value.id }); setNotice(value.status === 'confirmed' ? '当前预案已人工确认。' : '修订已保存，共享判断及受影响章节已更新。'); }
  function show(view: string | null) { navigate({ view, settings: null }); }
  const working = isRunning(run);
  const notice = actionNotice || (working && run?.id === submittedRun
    ? run.requested_model ? '已提交 ' + run.requested_model + ' 复盘。' : '预案任务已提交。' : '');
  const uncertain = run?.state === 'outcome_unknown' || run?.error_code === 'outcome_unknown';
  const failed = run?.state === 'failed' || run?.state === 'cancelled';
  const progress = run && (working || (failed && run.requested_model)) && !selectedReport
    ? <RunProgress key={run.id} run={run} history={runs} readError={runError}/> : null;
  const lastStage = run?.stages?.[run.stages.length - 1];
  const stageName = lastStage && roleNames[lastStage.role];
  const reason = run?.error_code === 'invalid_response'
    ? '已收到模型回复，但未能读取正文。此次记录未保留具体原因。'
    : run?.model_error || run?.stage;
  const modelIssue = run?.requested_model && !working && (run.error_code || run.state === 'blocked')
    ? run.requested_model + ' · ' + (stageName ? stageName + '未完成。' : '') + reason : '';
  function picker(label: string, paper?: ReportView) {
    if (working || uncertain) return null;
    return <ModelPicker settings={settings} setup={setup} report={paper} generateLabel={label} writable={writable}
      admin={session.data.user.role === 'admin'} busy={actionBusy} error={actionError || settingsError}
      choose={(method, id) => { choose(method, id ?? reviewSetup(settings, { ...choice, method }).model?.id); setActionError(''); setNotice(''); }}
      csrf={csrf} returnedToCodex={returnedToCodex} generate={direct => generate(paper, direct)} configure={setPreferences} reload={refreshSettings}/>;
  }
  const preferencesDialog = preferences && <PlanPreferences page={preferences} open={setPreferences} close={closePreferences}
    session={session} model={setup.model} apiModelId={setup.selectedApiId} refresh={refreshSettings} enabled={savedSettings}
    useModel={(source, id) => { choose(source, id); if (preferences !== 'models') refreshSettings(); closePreferences(); }}>
    {preferences === 'evidence' ? <section>{report ? <><p>{report.source_trade_day} · 本篇实际引用 {report.evidence_refs.length} 条依据</p><References ids={report.evidence_refs.map(ref => ref.id)} refs={report.evidence_refs}/>{report.limitations.map((item, index) => <p key={index}>{item}</p>)}</> : <p>生成报告后显示本篇实际依据。</p>}</section>
      : settings && (preferences === 'schedule' || preferences === 'sources' || preferences === 'portfolio') ?
        <PlanSettings key={preferences} section={preferences} expanded settings={settings} csrf={csrf} writable={writable}
          saved={value => { savedSettings(value); if (preferences === 'sources') closePreferences(); }}/>
      : <p>{settingsError || '正在读取设置…'} {settingsError && <button onClick={refreshSettings}>重新读取</button>}</p>}
  </PlanPreferences>;
  if (view === 'history') return <ReportHistory back={() => show(null)}
    openDay={date => navigate({view:null, source_trade_day:date, trade_day:null, run:null, report:null})}
    openReport={item => navigate({view:null, source_trade_day:item.source_trade_day, trade_day:null, run:item.id, report:item.report_id || null})}/>;
  if (view === 'settings' || view === 'stats') return <div className="tp-root tp-standalone">
    <div className="tp-actions"><button onClick={() => show(null)}>返回预案</button>{view === 'settings' && <h2>预案设置</h2>}</div>
    {view === 'stats' ? <ShadowProgress refreshKey={String(epoch)}/> : <>
      {settings && <PlanSettings settings={settings} expanded showSources={query.get('settings') === 'sources'}
        csrf={csrf} writable={writable} saved={savedSettings}/>}
      {settingsError && <p role="alert">{settingsError} <button onClick={refreshSettings}>重试</button></p>}
      <div className="tp-actions"><button onClick={() => show('workspace')}>生成记录与校对</button><button onClick={() => show('stats')}>查看生成统计</button></div>
    </>}
  </div>;
  if (view !== 'workspace') {
    const navigation = <div className="pr-reader-nav">
      {report && <button disabled={actionBusy || !report.can_export} title={report.can_export ? "保存这份八章预案" : "报告引用来源未允许导出"} onClick={async event => {
        const paper = event.currentTarget.closest('article');
        if (!paper || actionLock.current) return;
        const preview = window.open('', '_blank');
        if (!preview) { window.alert('请允许打开打印预览后再试。'); return; }
        preview.opener = null;
        actionLock.current = true; setActionBusy(true); setActionError('');
        try {
          const fresh = (await tomorrowApi.report(report.id)).data;
          if (!fresh.can_export || fresh.content_hash !== report.content_hash) {
            throw new Error('报告或引用权限已变化，请刷新后重试。');
          }
        } catch (error) {
          preview.close(); setNotice(message(error)); return;
        } finally {
          actionLock.current = false; setActionBusy(false);
        }
        const doc = preview.document;
        doc.documentElement.lang = 'zh-CN';
        doc.title = (report.target_trade_day || report.source_trade_day) + ' 明日预案';
        const copy = doc.importNode(paper, true);
        copy.querySelectorAll('.pr-review-choice,.pr-model-control,.pr-reader-nav,button,.pr-method').forEach(el => el.remove());
        const style = doc.createElement('style');
        style.textContent = paperStyles + '\n@page{size:A4 landscape;margin:12mm}body{margin:0;background:white}.pr-paper{width:100%;max-width:none}.pr-paper .pr-scroll{overflow:visible}.pr-paper table{min-width:0!important}.pr-paper thead{display:table-header-group}';
        doc.head.append(style);
        doc.body.append(copy);
        preview.requestAnimationFrame(() => { void doc.fonts.ready.then(() => preview.print()); });
      }}>导出 PDF</button>}
      {notice && <small role="status">{notice}</small>}
      <button onClick={() => show('history')}>历史预案</button><button onClick={() => setPreferences('menu')}>设置</button>
      {preferencesDialog}</div>;
    if (report) return <PaperReport key={report.id} report={report} navigation={navigation} modelPicker={
      <>{progress}
        {modelIssue && !progress && <div role="status" className="pr-model-issue"><p>{modelIssue}</p>
          {report.author_type === 'rule' ? <span>下方是规则草稿。</span> : report.ai_completed_roles?.length === 0 &&
            <span>本次没有生成 AI 正文，下方保留已有草稿。</span>} <button onClick={() => show('workspace')}>查看生成记录</button></div>}
        {uncertain ? !modelIssue && <button onClick={() => show('workspace')}>查看生成记录</button> : picker('重新复盘', report)}
      </>
    }/>;
    const error = reportError || runError || readError;
    const reading = !error && ((!runs && !selectedReport) || (runId && !run) || reportId);
    const title = error ? '暂时无法读取预案' : reading ? '正在读取预案…' : working ? '预案生成中'
      : uncertain ? '生成结果待核实' : failed ? '上次生成未完成' : '这天还没有预案';
    let detail = '';
    if (error) detail = error;
    else if (working) detail = [run?.requested_model, run?.stage].filter(Boolean).join(' · ') || '完成后会自动显示正文。';
    else if (modelIssue) detail = modelIssue;
    else if (uncertain) detail = '请先在生成记录中核对上次调用结果。';
    else if (failed) detail = run?.error_code === 'retry_exhausted' ? '上次任务多次重试后停止，未生成正文。' : '这次任务已停止，尚无可阅读的正文。';
    else if (!reading && settings?.enabled) detail = '交易日 ' + settings.evening_time + ' 定时生成，也可以现在手动生成。';
    return <div className="pr-ground pr-reader"><article className="pr-paper" aria-label="本期预案">
      <div className="pr-heading"><h1>{run?.target_trade_day ? run.target_trade_day + ' 交易预案' : '明日预案'}</h1>{navigation}</div>
      {(day || run?.source_trade_day) && <p className="pr-date-line">{day || run?.source_trade_day} 收盘资料</p>}
      {progress && !reportError ? progress : <div className="pr-status" aria-live="polite"><h2>{!error && (reading || working) && <ProgressCircle aria-label={title} isIndeterminate/>} {title}</h2>{detail && <p>{detail}</p>}
        {error ? <button onClick={() => setEpoch(v => v + 1)}>重新读取</button>
          : uncertain && <button onClick={() => show('workspace')}>查看生成记录</button>}
      </div>}
      {!error && !reading && picker(failed ? '重新生成预案' : '生成预案')}
    </article></div>;
  }
  return <div className="tp-root" aria-busy={loading || reportBusy}>
    <div className="tp-actions"><button onClick={() => report ? show(null) : navigate({view:null, run:null, report:null})}>{report ? '返回报告正文' : '返回预案'}</button></div>
    {progress}{report && <SessionBrief report={report}/>}
    <div className="tp-toolbar"><div className="tp-date"><label>来源交易日<input type="date" value={day} onChange={e => navigate({ source_trade_day: e.target.value, trade_day: null, run: null, report: null })}/></label><button onClick={() => navigate({ source_trade_day: null, trade_day: null, run: null, report: null })}>最近已保存</button><span className="tp-badge">{session.meta.app_mode === 'demo' ? '演示环境 · 非真实行情' : '本人研究 · 缓存证据'}</span></div><button aria-label="重读已存结果" aria-busy={loading || reportBusy} onClick={() => setEpoch(value => value + 1)}>{(loading || reportBusy) && report ? '正在更新…' : '重读已存结果'}</button></div>
    <section className="tp-panel tp-generator"><div className="tp-actions"><h2>明日预案</h2><button disabled={!writable || actionBusy} onClick={prepare}>准备研究资料</button></div>
      {picker('生成预案')}<details className="tp-generation-options"><summary>生成日期与资料截止</summary><div className="tp-form-grid"><label>稿件类型<select value={slot} onChange={e => setSlot(e.target.value as Slot)}><option value="manual">手动研究</option><option value="evening">晚间完整稿</option><option value="overnight">早间补充</option></select></label><label>目标交易日<input type="date" value={targetDay} onChange={e => setTargetDay(e.target.value)}/></label><label>接收截止（北京时间）<input type="datetime-local" step="1" value={cutoff} onChange={e => setCutoff(e.target.value)}/></label></div><small>日期留空时由交易日历确定。历史补采资料会标记为重建，晚间稿和早间补充保留各自记录。</small></details></section>
    {preferencesDialog}{notice && <p role="status" className="tp-success">{notice}</p>}
    {readError && <div role="alert" className="tp-error">{readError}<button onClick={() => setEpoch(value => value + 1)}>重试读取</button></div>}
    {loading && !runs ? <p role="status" className="tp-empty">正在读取生成记录…</p> : runs && <div className="tp-history"><label>生成记录<select value={runId} onChange={e => chooseRun(e.target.value)} disabled={!runs.length}><option value="" disabled>{runs.length ? '选择一次生成记录' : '尚无研究任务'}</option>{runs.map(item => <option key={item.id} value={item.id}>{item.source_trade_day} → {item.target_trade_day} · {slotLabel[item.slot]} · {label(item.state)} · {displayTime(item.created_at)}</option>)}</select></label><small>{runs.length ? `共 ${runs.length} 份任务` : '可使用已有缓存生成第一份草稿'}</small></div>}
    {runError && <div role="alert" className="tp-error">任务进度读取失败：{runError}<button onClick={() => setEpoch(value => value + 1)}>重试进度</button></div>}
    {run && <section className="tp-panel tp-run" aria-label="研究任务进度"><div className="tp-actions"><b>{slotLabel[run.slot]} · {label(run.state)}</b><span>{run.stage}</span><small>{displayTime(run.created_at)}</small></div>{!!run.stages?.length && <ol className="tp-stages">{run.stages.map((stage, i) => <li key={i} className={stage.state === 'running' ? 'tp-current' : ''}><span>{i + 1}</span>{roleNames[stage.role] ?? `研究阶段 ${i + 1}`}<small>{label(stage.state)}</small></li>)}</ol>}{isRunning(run) && <p role="status" className="tp-muted">后台持续研究中，当前页面会自动更新进度。</p>}{run.state === 'outcome_unknown' && <p className="tp-warning">模型请求结果尚未核实。请先在运行监控确认，避免重复产生费用。</p>}{!!run.limitations?.length && <ul className="tp-limitations">{run.limitations.filter(item => run.report_id || !item.includes('已保留规则资料草稿')).map((item, i) => <li key={i}>{item}</li>)}</ul>}</section>}
    {reportBusy && !report && <p role="status" className="tp-empty">正在读取已保存的八章研究…</p>}
    {reportError && <div role="alert" className="tp-error">报告读取失败：{reportError}<button onClick={() => setEpoch(value => value + 1)}>重试报告</button></div>}
    {report && <div className="tp-report" key={report.id + ':' + report.revision_number}>
      {report.based_on_id && <p className="tp-relationship">本稿包含对前稿的补充或修订。<button onClick={() => navigate({ report: report.based_on_id! })}>查看所依据的原稿</button></p>}
      <DailyOverview report={report}/><CorePlan report={report}/><ReportRevision report={report} csrf={csrf} writable={writable} saved={savedReport}/>
      <div className="tp-chapters-heading"><h2>完整研究</h2><span>八个章节 · 按依据与数据就绪程度展开</span></div>{report.sections.map(section => <ReportSection key={section.key} section={section} refs={report.evidence_refs}/>)}
      <DossierBrowser runId={report.run_id} refs={report.evidence_refs}/>
      <section className="tp-panel"><h3>本次证据目录</h3><References ids={report.evidence_refs.map(ref => ref.id)} refs={report.evidence_refs}/>{!report.evidence_refs.length && <p className="tp-warning">本稿没有可读取的证据，不能据此形成确定判断。</p>}</section>
      {validationError && <div role="alert" className="tp-error">验证记录读取失败：{validationError}<button onClick={() => setValidationEpoch(value => value + 1)}>重试验证记录</button></div>}<PlanValidation report={report} evidence={validationEvidence} items={validations} csrf={csrf} writable={writable} reload={() => setValidationEpoch(value => value + 1)}/>
    </div>}
    {!loading && !readError && !runId && !selectedReport && <div className="tp-panel tp-empty"><h3>研究从已缓存的事实开始</h3><p>目前还没有保存的预案。可先准备资料，再生成草稿；缺少的数据会保留缺口说明。</p></div>}
    {settings && <PlanSettings showSources={query.get('settings') === 'sources'} settings={settings} csrf={csrf} writable={writable}
      saved={savedSettings}/>}
  </div>;
}
