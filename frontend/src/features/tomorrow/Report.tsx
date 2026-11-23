import { useState } from 'react';
import type { FormEvent } from 'react';
import { tomorrowApi } from './api';
import { displayTime, label, records, safeUrl, text } from './model';
import type { EvidenceRef, Judgment, ReportView, ScenarioStatus, Section, Validation } from './model';

export function References({ ids, refs }: { ids: string[]; refs: EvidenceRef[] }) {
  const allowed = new Set(ids);
  const entries = refs.filter(ref => allowed.has(ref.id));
  const sourceKey = JSON.stringify([ids, entries.map(ref => [ref.id, ref.content_hash, ref.source_id, ref.source_name, ref.received_at, ref.source_time, ref.published_at, ref.available_at, ref.url, ref.source_url])]);
  const [view, setView] = useState({ sourceKey, open: false, limit: 50 });
  const open = view.sourceKey === sourceKey && view.open;
  if (!ids.length) return null;
  return <details className="tp-references" open={open} onToggle={event => { const nextOpen = event.currentTarget.open; setView(current => ({ sourceKey, open: nextOpen, limit: nextOpen && current.sourceKey === sourceKey ? current.limit : 50 })); }}><summary>查看依据（{entries.length}）</summary>{open && <>{entries.slice(0, view.limit).map((ref, i) => <div className="tp-reference" key={ref.id}><b>{i + 1}. {ref.source_name}</b><small>资料时间：{displayTime(ref.source_time ?? ref.published_at ?? ref.available_at)} · 接收时间：{displayTime(ref.received_at)}</small>{safeUrl(ref.url ?? ref.source_url) && <a href={safeUrl(ref.url ?? ref.source_url)!} target="_blank" rel="noopener noreferrer">打开原文 ↗</a>}</div>)}{view.limit < entries.length && <button className="tp-more" onClick={() => setView(current => ({ sourceKey, open: true, limit: current.sourceKey === sourceKey ? current.limit + 50 : 50 }))}>加载更多依据（已显示 {Math.min(view.limit, entries.length)} / {entries.length}）</button>}{entries.length < ids.length && <p className="tp-warning">部分依据暂不可读取，请核对来源权限。</p>}</>}</details>;
}
function Limits({ items }: { items: string[] }) { return items.length ? <ul className="tp-limitations">{[...new Set(items)].map((item, i) => <li key={i}>{item}</li>)}</ul> : null; }
export function ReportAudit({ report }: { report: ReportView }) {
  const audit = report.ai_audit;
  const binding = report.audit_binding;
  const passed = audit?.audit_status === 'passed' && binding?.passed === true && binding.audit_scope_complete === true;
  const hasAudit = !!audit || !!binding;
  const rawIssues = audit?.audit_issues;
  const issues = (Array.isArray(rawIssues) ? rawIssues : rawIssues ? [rawIssues] : []).map(issue => {
    if (typeof issue === 'string') return issue;
    if (issue && typeof issue === 'object') {
      const row = issue as Record<string, unknown>;
      const detail = row.message ?? row.issue ?? row.text ?? row.description ?? row.reason ?? row.summary;
      return detail ? text(detail) : '审查记录中存在需人工核对的问题。';
    }
    return '审查记录中存在需人工核对的问题。';
  });
  return <section className={`tp-panel tp-audit ${passed ? 'tp-audit-passed' : ''}`} aria-label="全文审查结果"><div className="tp-actions"><h3>全文审查</h3><b className={passed ? 'tp-success' : 'tp-audit-pending'}>{passed ? 'AI 全文审查通过' : hasAudit ? 'AI 全文审查待复核' : '尚未完成 AI 全文审查'}</b></div><p className="tp-muted">{passed ? '正文与已研究条件由 AI 核对，数值表格由程序核对，记录与本稿对应。' : hasAudit ? '当前可见稿尚未取得完整且通过的审查记录，请核对下列问题与研究内容。' : '本稿暂无完整的 AI 全文审查记录，可先核对事实与资料缺口。'}人工确认仍由本人单独完成。</p>{issues.length > 0 && <ul className="tp-limitations">{issues.map((issue, i) => <li key={i}>{issue}</li>)}</ul>}{hasAudit && !passed && !issues.length && <p className="tp-warning">审查未通过、范围不完整或稿件已修订；请核对当前内容。</p>}<small>本人确认：{report.status === 'confirmed' ? '当前稿已由本人确认' : '当前稿尚待本人确认'}</small></section>;
}

function NextChecks({ scenario }: { scenario: Record<string, unknown> }) {
  const checks = records(scenario.next_checks);
  if (!checks.length) return null;
  return <section className="tp-next-checks" aria-label="具体观察检查"><h4>具体观察检查</h4>{checks.map((check, i) => <article key={i}><dl>{([['检查对象', 'subject'], ['观察时间', 'window'], ['支持条件', 'support'], ['失效条件', 'invalidation'], ['缺少资料', 'missing_data']] as const).map(([title, key]) => <div key={key}><dt>{title}</dt><dd>{text(check[key])}</dd></div>)}</dl></article>)}</section>;
}

export function ReportSection({ section, refs }: { section: Section; refs: EvidenceRef[] }) {
  const [expanded, setExpanded] = useState(false);
  const visibleRows = expanded ? section.rows : section.rows.slice(0, 30);
  return <details className="tp-panel tp-section"><summary><svg className="tp-disclosure-chevron" width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"/></svg><strong>{section.title}</strong><span className={section.state === 'complete' ? 'tp-success' : 'tp-muted'}>{label(section.state)}</span></summary>
    <div className="tp-section-content">{section.summary && <p className="tp-section-summary">{section.summary}</p>}
    {section.analysis?.length ? section.analysis.map((analysis, i) => <article className={`tp-analysis ${analysis.authorship === 'manual' ? 'tp-manual-analysis' : ''}`} key={i}><b>{analysis.authorship === 'manual' ? '人工判断' : 'AI 研究'} · {analysis.title}</b>{analysis.review_required && <p className="tp-warning">本段待复核：已有人工修订，原 AI 内容保留供对照。</p>}<p className="tp-prose">{analysis.text}</p><References ids={analysis.evidence_refs} refs={refs}/></article>) : (section.paragraphs ?? []).map((paragraph, i) => <p className="tp-prose" key={i}>{paragraph}</p>)}
    {!!section.columns.length && !!section.rows.length && <div className="tp-table-wrap"><table><caption className="tp-sr">{section.title}数据</caption><thead><tr>{section.columns.map(column => <th key={column.key}>{column.label}</th>)}</tr></thead><tbody>{visibleRows.map((row, i) => <tr key={i}>{section.columns.map(column => <td key={column.key}>{text(row[column.key])}</td>)}</tr>)}</tbody></table></div>}
    {!expanded && section.rows.length > 30 && <button onClick={() => setExpanded(true)}>展开全部 {section.rows.length} 条</button>}
    {!section.summary && !section.paragraphs?.length && !section.rows.length && <p className="tp-muted">本章暂无足够资料。</p>}
    <Limits items={section.limitations ?? []}/><References ids={section.evidence_refs ?? []} refs={refs}/></div>
  </details>;
}

export function CorePlan({ report }: { report: ReportView }) {
  const [allScenarios, setAllScenarios] = useState(false);
  const [allWatched, setAllWatched] = useState(false);
  const directions = records(report.dossier.core_directions);
  const watched = records(report.dossier.watchlist);
  const scenarios = records(report.dossier.scenarios);
  const risks = records(report.dossier.risks);
  function rowTitle(row: Record<string, unknown>) { return text(row.name ?? row.title ?? row.label ?? row.object_name ?? row.code ?? row.object_id ?? row.summary); }
  function rowBody(row: Record<string, unknown>) { return text(row.judgment ?? row.rationale ?? row.reason ?? row.candidate_reasons ?? row.description ?? row.summary); }
  function watchedBody(row: Record<string, unknown>) {
    const researched = row.judgment ?? row.rationale ?? row.reason ?? row.description ?? row.summary;
    return text(researched) === '—' && row.candidate_reasons ? '规则候选原因：' + text(row.candidate_reasons) : rowBody(row);
  }
  return <>
    <article className="tp-summary tp-panel"><div className="tp-summary-heading"><span>明日研究重点</span><b>{report.source_trade_day} → {report.target_trade_day || '目标交易日待核实'}</b></div><h2>先核对事实，再等待条件确认</h2><p className="tp-prose">{report.summary || '暂无可用判断，请先核对下方数据缺口。'}</p><small>资料截止：{displayTime(report.as_of_time)}（北京时间） · {report.status === 'confirmed' ? '已人工确认' : '研究草稿 · 待人工确认'}</small></article>
    <ReportAudit report={report}/>
    <div className="tp-core-grid"><article className="tp-panel"><h3>核心方向</h3>{directions.length ? <ul className="tp-core-list">{directions.map((row, i) => <li key={i}><b>{rowTitle(row)}</b><span>{rowBody(row)}</span>{row.judgment_authorship === 'manual' && <small className="tp-manual-mark">人工判断 · 已保留</small>}</li>)}</ul> : <p className="tp-muted">尚无已确认的核心方向。</p>}</article><article className="tp-panel" aria-label="重点观察候选"><h3>重点观察</h3><p className="tp-muted">候选共 {watched.length} 条 · 按研究覆盖顺序展示</p>{watched.length ? <ul className="tp-core-list">{(allWatched ? watched : watched.slice(0, 6)).map((row, i) => <li key={i}><b>{rowTitle(row)}</b><span>{watchedBody(row)}</span>{row.judgment_authorship === 'manual' && <small className="tp-manual-mark">人工判断 · 已保留</small>}</li>)}</ul> : <p className="tp-muted">候选条件不足，暂不列观察标的。</p>}{watched.length > 6 && <button className="tp-more" onClick={() => setAllWatched(value => !value)}>{allWatched ? '收起候选' : `展开其余候选（${watched.length - 6} 条）`}</button>}<p className="tp-muted">更多对象与依据见完整研究底稿。</p></article><article className="tp-panel tp-risk-card"><h3>失效条件与重大风险</h3>{risks.length ? <ul className="tp-core-list">{risks.map((row, i) => <li key={i}><b>{rowTitle(row)}</b><span>{rowBody(row)}</span>{row.judgment_authorship === 'manual' && <small className="tp-manual-mark">人工判断 · 已保留</small>}</li>)}</ul> : <p className="tp-muted">尚无充分资料形成风险判断，请核对数据缺口。</p>}<Limits items={report.limitations ?? []}/></article></div>
    <section className="tp-panel"><h3>明日确认条件</h3>{!scenarios.length ? <p className="tp-muted">尚无可验证的条件预案。缺数据不视为条件成立。</p> : <div className="tp-scenarios">{(allScenarios ? scenarios : scenarios.slice(0, 6)).map((scenario, i) => <article key={text(scenario.id ?? scenario.scenario_id ?? i)}><div className="tp-scenario-heading"><b>{text(scenario.name ?? scenario.title ?? scenario.label ?? scenario.object_name ?? scenario.object_id ?? `观察情景 ${i + 1}`)}</b><span>{label(String(scenario.status ?? 'pending'))}</span></div><p className="tp-scenario-object">观察对象：{text(scenario.object_name ?? scenario.object_id ?? scenario.subject)}</p><dl>{([['观察时段', 'time_window'], ['前置条件', 'preconditions'], ['触发条件', 'trigger'], ['确认条件', 'confirmation'], ['失效条件', 'invalidation'], ['风险否决', 'risk_veto'], ['所需资料', 'required_data'], ['验证方式', 'verification']] as const).map(([title, key]) => <div key={key}><dt>{title}</dt><dd>{text(scenario[key] ?? (key === 'time_window' ? scenario.observation_window : key === 'trigger' ? scenario.trigger_condition : key === 'confirmation' ? scenario.confirmation_condition : key === 'invalidation' ? scenario.invalidation_condition : key === 'verification' ? (scenario.verification_mode ?? (scenario.verification_method === 'manual' ? '人工确认' : null)) : null))}</dd></div>)}</dl><NextChecks scenario={scenario}/><References ids={Array.isArray(scenario.evidence_refs) ? scenario.evidence_refs.map(String) : []} refs={report.evidence_refs}/></article>)}</div>}{scenarios.length > 6 && <button className="tp-more" onClick={() => setAllScenarios(value => !value)}>{allScenarios ? '收起其余情景' : `展开全部 ${scenarios.length} 个情景`}</button>}</section>
  </>;
}

export function ReportRevision({ report, csrf, writable, saved }: { report: ReportView; csrf: string; writable: boolean; saved: (value: ReportView) => void }) {
  const [open, setOpen] = useState(false); const [summary, setSummary] = useState(report.summary);
  const [judgments, setJudgments] = useState<Judgment[]>(report.judgments ?? []);
  const [reason, setReason] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  function edit(index: number, patch: Partial<Judgment>) { setJudgments(rows => rows.map((row, i) => i === index ? { ...row, ...patch } : row)); }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!reason.trim() || busy || !writable) return;
    if (judgments.some(row => !row.object_id.trim() || !row.judgment.trim())) { setError('请补全每条判断的对象和内容。'); return; }
    setBusy(true); setError('');
    try { saved((await tomorrowApi.revise(report.id, { expected_revision: report.revision_number, summary, judgments, reason: reason.trim() }, csrf)).data); setOpen(false); }
    catch (e) { setError(e instanceof Error ? e.message : '修订保存失败。'); } finally { setBusy(false); }
  }
  async function confirm() { if (busy || !writable) return; setBusy(true); setError(''); try { saved((await tomorrowApi.confirm(report.id, report.revision_number, csrf)).data); } catch (e) { setError(e instanceof Error ? e.message : '确认失败。'); } finally { setBusy(false); } }
  const refOptions = report.evidence_refs.map(ref => ({ id: ref.id, name: ref.source_name }));
  return <section className="tp-panel tp-revision"><div className="tp-actions"><h3>人工核对</h3><button disabled={!writable || busy} onClick={() => setOpen(!open)} aria-expanded={open}>修订研究判断</button><button className="primary" disabled={!writable || busy || open || report.status === 'confirmed'} onClick={confirm}>{busy && !open ? '正在确认…' : report.status === 'confirmed' ? '本稿已人工确认' : '确认当前预案'}</button></div><p className="tp-muted">修订会保存原因并形成新记录，原始事实及昨晚预案保留。确认代表本人已核对研究内容。</p>
    {open && <form onSubmit={submit}><fieldset disabled={busy}><legend>修订内容</legend><label>研究摘要<textarea aria-label="研究摘要" rows={4} value={summary} onChange={e => setSummary(e.target.value)} required maxLength={4000}/></label>{judgments.map((judgment, i) => <div className="tp-judgment" key={i}><div className="tp-form-grid"><label>对象类型<select aria-label={`判断 ${i + 1} 对象类型`} value={judgment.object_type} onChange={e => edit(i, { object_type: e.target.value as Judgment['object_type'] })}><option value="market">市场</option><option value="sector">板块</option><option value="stock">股票</option><option value="event">事件</option></select></label><label>对象名称或代码<input aria-label={`判断 ${i + 1} 对象`} required value={judgment.object_id} maxLength={120} onChange={e => edit(i, { object_id: e.target.value })}/></label></div><label>人工判断<textarea aria-label={`判断 ${i + 1} 内容`} required rows={3} value={judgment.judgment} maxLength={3000} onChange={e => edit(i, { judgment: e.target.value })}/></label>{!!refOptions.length && <div className="tp-form-grid">{([['support_refs', '支持依据'], ['refuting_refs', '反证依据']] as const).map(([key, title]) => <label key={key}>{title}<select multiple aria-label={`判断 ${i + 1} ${title}`} value={judgment[key] ?? []} onChange={e => edit(i, { [key]: Array.from(e.target.selectedOptions).map(option => option.value) })}>{refOptions.map((ref, n) => <option key={ref.id} value={ref.id}>{n + 1}. {ref.name}</option>)}</select></label>)}</div>}<button type="button" onClick={() => setJudgments(rows => rows.filter((_, index) => index !== i))}>移除此条判断</button></div>)}<button type="button" onClick={() => setJudgments(rows => [...rows, { object_type: 'stock', object_id: '', judgment: '', support_refs: [], refuting_refs: [] }])}>添加人工判断</button><label>修订原因<textarea aria-label="修订原因" required rows={2} value={reason} maxLength={2000} onChange={e => setReason(e.target.value)} placeholder="说明哪些依据改变了你的判断"/></label></fieldset><div className="tp-actions"><button className="primary" disabled={busy || !reason.trim()}>{busy ? '正在保存…' : '保存修订'}</button><button type="button" disabled={busy} onClick={() => setOpen(false)}>收起编辑</button></div></form>}
    {error && <div className="tp-error" role="alert">{error}<small>本次编辑仍保留，核对服务器的新记录后再保存。</small></div>}
  </section>;
}

export function PlanValidation({ report, evidence, items, csrf, writable, reload }: { report: ReportView; evidence: EvidenceRef[]; items: Validation[]; csrf: string; writable: boolean; reload: () => void }) {
  const scenarios = records(report.dossier.scenarios).filter(row => row.id || row.scenario_id);
  const [scenario, setScenario] = useState(''); const [status, setStatus] = useState<ScenarioStatus>('pending');
  const [comment, setComment] = useState(''); const [refs, setRefs] = useState<string[]>([]); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!scenario || !comment.trim() || busy || !writable) return; if (['triggered', 'confirmed', 'invalidated', 'risk_veto'].includes(status) && !refs.length) { setError('此观察结果需要选择目标交易日的实际依据。'); return; }
    setBusy(true); setError(''); setNotice('');
    try { await tomorrowApi.validate(report.id, { scenario_id: scenario, status, comment: comment.trim(), evidence_refs: refs }, csrf); setComment(''); setNotice('验证已追加，原预案内容保留。'); reload(); }
    catch (e) { setError(e instanceof Error ? e.message : '验证保存失败。'); } finally { setBusy(false); }
  }
  function scenarioName(id: string) { const row = scenarios.find(row => row.id === id || row.scenario_id === id); return row ? text(row.name ?? row.title ?? row.label ?? row.object_name ?? row.object_id) : '原预案情景'; }
  return <details className="tp-panel tp-validation"><summary>次日验证 <span>{items.length ? `${items.length} 条记录` : '尚未验证'}</span></summary><p className="tp-muted">对照本稿原有条件追加观察结果。缺数据应记为“数据不足”；次日上涨不能代替条件验证。</p>{items.length ? <div className="tp-validation-list">{items.map((item, i) => <article key={item.id ?? i}><b>{scenarioName(item.scenario_id)} · {label(item.status)}</b><p className="tp-prose">{item.comment}</p><small>{displayTime(item.created_at)}</small><References ids={item.evidence_refs ?? []} refs={[...report.evidence_refs, ...evidence]}/></article>)}</div> : <p className="tp-muted">还没有针对本稿的验证记录。</p>}
    {!scenarios.length ? <p className="tp-warning">本稿尚未形成可验证情景。</p> : <form onSubmit={submit}><fieldset disabled={!writable || busy}><legend>追加验证</legend><div className="tp-form-grid"><label>预案情景<select required value={scenario} onChange={e => setScenario(e.target.value)}><option value="">请选择原预案情景</option>{scenarios.map((row, i) => <option key={i} value={String(row.id ?? row.scenario_id)}>{text(row.name ?? row.title ?? row.label ?? row.object_name ?? row.object_id ?? `情景 ${i + 1}`)}</option>)}</select></label><label>观察结果<select value={status} onChange={e => setStatus(e.target.value as ScenarioStatus)}>{(['pending', 'triggered', 'confirmed', 'invalidated', 'risk_veto', 'insufficient_data'] as const).map(value => <option key={value} value={value}>{label(value)}</option>)}</select></label></div><label>观察说明<textarea aria-label="观察说明" required rows={3} value={comment} onChange={e => setComment(e.target.value)} maxLength={4000} placeholder="说明观察时间、条件是否满足及缺少的资料"/></label>{!!evidence.length && <label>目标交易日的观察依据<select multiple value={refs} onChange={e => setRefs(Array.from(e.target.selectedOptions).map(option => option.value))}>{evidence.map((ref, i) => <option key={ref.id} value={ref.id}>{i + 1}. {ref.source_name} · {displayTime(ref.source_time ?? ref.published_at ?? ref.available_at ?? ref.received_at)}</option>)}</select></label>}{!evidence.length && <p className="tp-warning">目标交易日观察依据尚未就绪，可记录待观察或数据不足。</p>}<button className="primary" disabled={!scenario || !comment.trim() || busy}>{busy ? '正在保存…' : '追加验证结果'}</button></fieldset></form>}
    {error && <div className="tp-error" role="alert">{error}</div>}{notice && <p className="tp-success" role="status">{notice}</p>}
  </details>;
}
