import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import type { Document, Revision, Scenario, SessionResponse, Validation, ValidationInput } from '../../generated/schema';
import { api, ApiError } from '../../shared/api';
import { Card, Empty, ErrorState } from '../../shared/ui';
import { useRead } from '../../shared/useRead';
import { useSubmit } from '../../shared/useSubmit';
import { discardPrivateDraft, usePrivateDraft } from '../../shared/privateDrafts';
import { ResearchToolbar } from './ResearchToolbar';
import { PlanChoice } from '../market/AuctionNotebook';
import { RecordDialog } from './RecordDialog';
import { EvidencePicker, SavedEvidence, SnapshotPicker } from './ReferencePickers';
import { recordedTime } from './recordTypes';

const outcomeNames = { supported: '材料支持原条件', refuted: '材料反证原条件', inconclusive: '不可判断 · 材料缺失' };
import { updateLocation } from '../../shared/navigation';

function changeUrl(values: Record<string, string | null>) {
  const url = new URL(location.href);
  Object.entries(values).forEach(([key, value]) => { if (value) url.searchParams.set(key, value); else url.searchParams.delete(key); });
  updateLocation(url, { replace: true });
}
function Pager({ cursor, previous, setCursor, setPrevious, next, busy }: { cursor: string | null; previous: Array<string | null>; setCursor: (v: string | null) => void; setPrevious: (v: Array<string | null>) => void; next?: string | null; busy: boolean }) {
  return <div className="resource-pager"><button type="button" disabled={!previous.length || busy} onClick={() => { setCursor(previous.at(-1) ?? null); setPrevious(previous.slice(0, -1)); }}>上一页</button><button type="button" disabled={!next || busy} onClick={() => { setPrevious([...previous, cursor]); setCursor(next ?? null); }}>下一页</button></div>;
}

export function Validations({ session }: { session: SessionResponse }) {
  const params = new URLSearchParams(location.search);
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  const [day, setDay] = useState(params.get('date') ?? today);
  const [planId, setPlan] = useState(params.get('plan') ?? ''); const [scenarioId, setScenario] = useState(params.get('scenario') ?? '');
  const [detail, setDetail] = useState(params.get('validation')); const [entry, setEntry] = useState(params.get('entry') === 'new' || !!params.get('correct'));
  const [correctionId, setCorrection] = useState<string | null>(params.get('correct')); const [refresh, setRefresh] = useState(0);
  const original = useRead(useCallback(() => correctionId ? api.validation(correctionId) : Promise.resolve(null), [correctionId]));
  const correction = original.data?.data ?? null;
  const plan = useRead(useCallback(() => planId ? api.researchRevision(planId) : Promise.resolve(null), [planId]));
  const documentId = plan.data?.data.document_id;
  const document = useRead(useCallback(() => documentId ? api.researchDocument(documentId) : Promise.resolve(null), [documentId]));
  const row = plan.data?.data; const doc = document.data?.data;
  const calendarId = row?.calendar_version_id;
  const sourceDay = doc?.source_trade_day;
  const calendar = useRead(useCallback(() => calendarId && sourceDay ? api.nextSession(calendarId, sourceDay) : Promise.resolve(null), [calendarId, sourceDay]));
  const trading = calendar.data?.data.session;
  const closeTime = trading?.is_open && trading.trade_day === doc?.target_trade_day && trading.segments.some(item => item.kind === 'continuous') ? trading.segments.map(item => item.end).sort().at(-1) : null;
  const closedAt = closeTime && doc?.target_trade_day ? `${doc.target_trade_day}T${closeTime}+08:00` : null;
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(timer); }, []);
  const scenario = row?.scenarios.find(item => item.id === scenarioId);
  const writable = session.data.user.role !== 'viewer';
  const allowed = writable && row?.publication_status === 'published' && !!scenario && doc?.target_trade_day === day && !!closedAt && new Date(closedAt).getTime() <= now;
  function close() { setEntry(false); setDetail(null); setCorrection(null); changeUrl({ validation: null, entry: null, correct: null }); }
  function choose(id: string) { close(); setPlan(id); setScenario(''); changeUrl({ plan: id, scenario: null }); }
  function view(id: string) { setEntry(false); setCorrection(null); setDetail(id || null); setRefresh(value => value + 1); changeUrl({ validation: id, entry: null, correct: null }); }
  return <><ResearchToolbar path="/research/validations" day={day} dateLabel="目标日期" dateInputLabel="验证目标日期" onDayChange={value => { setDay(value); choose(''); changeUrl({ date: value }); }}/>
    <div className="research-status-note"><span>逐情景核对原条件与证据，保留每次更正。行情、规则与 AI 未接入。</span><span className="badge muted">人工验证</span></div>
    <div className="validation-layout"><Card title="原预案与验证条件" className="auction-notebook validation-context"><PlanChoice key={day} day={day} value={planId} change={choose}/>
      {planId && (plan.busy || document.busy ? <p role="status">正在读取原预案与目标日…</p> : plan.error || document.error ? <ErrorState error={plan.error ?? document.error!} retry={() => { plan.reload(); document.reload(); }}/> : !row || !doc || doc.kind !== 'plan' ? <p role="alert">所选记录不是可读取的预案版本。</p> : <div className="fixed-plan"><h3>{row.title} · 第 {row.revision_number} 版</h3><p>来源 {doc.source_trade_day} → 目标 {doc.target_trade_day}</p><a href={`/research/plans?date=${doc.source_trade_day}&document=${doc.id}&revision=${row.id}`}>回查精确预案与上游研究</a>
        <label>原预案情景<select aria-label="原预案情景" value={scenarioId} onChange={event => { close(); setScenario(event.target.value); changeUrl({ scenario: event.target.value }); }}><option value="">请明确选择原情景</option>{row.scenarios.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        {scenario && <div className="scenario-summary"><h4>{scenario.label}</h4><p><strong>原触发条件：</strong>{scenario.trigger_text}</p><p><strong>原失效条件：</strong>{scenario.invalidation_text}</p></div>}
        {calendar.busy ? <p role="status">正在核对原日历收盘时点…</p> : calendar.error ? <ErrorState error={calendar.error} retry={calendar.reload}/> : closedAt ? <p className="reference-note">原日历声明收盘：{recordedTime(closedAt)}{new Date(closedAt).getTime() > now ? ' · 尚未收盘，暂不可验证' : ' · 服务器保存时再次核对'}</p> : <p className="permission-note">原日历未明确目标日收盘时点，暂不可验证。请先补充日历并发布新的预案。</p>}
        {row.publication_status !== 'published' && <p className="permission-note">该版本尚未发布或已撤回，不能新增验证。</p>}{doc.target_trade_day !== day && <p className="permission-note">目标日期与原预案不一致，请重新选择。</p>}
      </div>)}
    </Card><Card title="次日验证记录" className="validation-records">{!writable && <p className="permission-note">当前账号只有读取权限。</p>}{planId ? <ValidationList key={planId + ':' + scenarioId + ':' + refresh} plan={planId} scenario={scenarioId} allowed={allowed} view={view} create={() => { setCorrection(null); setEntry(true); changeUrl({ entry: 'new', correct: null, validation: null }); }}/>
      : <Empty title="先选择原预案与情景">按目标日查找已发布的历史版本，不使用新草稿替换原条件。</Empty>}<p className="feature-note">支持与反证是人工材料核对结果，不计算成交、胜率或收益。没有记录不等于验证通过。</p></Card></div>
    {entry && correctionId && (original.busy ? <p role="status">正在恢复原验证引用…</p> : original.error ? <ErrorState error={original.error} retry={original.reload}/> : null)}
    {entry && (!correctionId || correction) && row && doc && scenario && closedAt && <ValidationEditor key={row.id + ':' + scenario.id + ':' + (correction?.id ?? 'new')} plan={row} document={doc} scenario={scenario} closedAt={closedAt} previous={correction} session={session} close={close} review={() => view(correction?.id ?? '')} saved={value => { setEntry(false); view(value.id); }}/>}
    {detail && <ValidationDetails key={detail} id={detail} close={close} view={view} canWrite={writable} correct={value => { setPlan(value.plan_revision_id); setScenario(value.scenario_id); setCorrection(value.id); setDetail(null); setEntry(true); changeUrl({ plan: value.plan_revision_id, scenario: value.scenario_id, validation: null, entry: null, correct: value.id }); }}/>}
  </>;
}

function ValidationList({ plan, scenario, allowed, view, create }: { plan: string; scenario: string; allowed: boolean; view: (id: string) => void; create: () => void }) {
  const [cursor, setCursor] = useState<string | null>(null); const [previous, setPrevious] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.validations(plan, scenario, cursor), [plan, scenario, cursor]));
  const rows = read.data?.data.items;
  return <section className="validation-list"><div className="subsection-heading"><button onClick={read.reload}>刷新验证记录</button><button data-new-research className="primary" disabled={!allowed || read.busy || !!read.error || !!rows?.length} onClick={create}>记录次日验证</button></div>
    {read.busy ? <p role="status">正在读取验证记录…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !rows?.length ? <Empty title="尚无这个情景的验证">选择原情景后，在目标日收盘后记录结论或缺失原因。</Empty> : <><p className="field-help">每个原情景保留一条更正链。查看当前原件后追加更正，历史记录始终可读。</p><div className="table-wrap" role="region" aria-label="验证记录表格" tabIndex={0}><table><thead><tr>{['人工结论','更正状态','实际保存时间','操作'].map(name => <th key={name}>{name}</th>)}</tr></thead><tbody>{rows.map(item => <tr key={item.id}><td><span className={'badge ' + (item.outcome === 'inconclusive' ? 'muted' : 'pending')}>{outcomeNames[item.outcome]}</span></td><td>{item.superseded_by_id ? '已更正 · 原文保留' : '当前原件'}</td><td>{recordedTime(item.recorded_at)}</td><td><button aria-label={`查看验证 ${item.id}`} onClick={() => view(item.id)}>查看</button></td></tr>)}</tbody></table></div></>}
    <Pager cursor={cursor} previous={previous} setCursor={setCursor} setPrevious={setPrevious} next={read.data?.data.next_cursor} busy={read.busy}/>
  </section>;
}

function ObservationChoice({ plan, scenario, value, change }: { plan: string; scenario: string; value: string; change: (id: string) => void }) {
  const [cursor, setCursor] = useState<string | null>(null); const [previous, setPrevious] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.observations(plan, scenario, cursor), [plan, scenario, cursor]));
  return <section className="reference-picker"><h3>关联竞价观察（可选）</h3>{read.busy ? <p role="status">正在读取同情景观察…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : <label>关联观察<select aria-label="关联观察" value={value} onChange={event => change(event.target.value)}><option value="">不关联观察</option>{value && !read.data?.data.items.some(item => item.id === value) && <option value={value}>已选的精确观察</option>}{read.data?.data.items.map(item => <option key={item.id} value={item.id}>{item.phase} · {item.status === 'unavailable' ? '资料缺失' : '人工事实'} · {recordedTime(item.recorded_at)}</option>)}</select></label>}<Pager cursor={cursor} previous={previous} setCursor={setCursor} setPrevious={setPrevious} next={read.data?.data.next_cursor} busy={read.busy}/></section>;
}

type Entry = { snapshotId: string; observationId: string; outcome: ValidationInput['outcome']; reason: string; refs: string[] };
function ValidationEditor({ plan, document, scenario, closedAt, previous, session, close, review, saved }: { plan: Revision; document: Document; scenario: Scenario; closedAt: string; previous: Validation | null; session: SessionResponse; close: () => void; review: () => void; saved: (v: Validation) => void }) {
  const owner = session.data.user.id; const slot = 'validation:' + plan.id + ':' + scenario.id + ':' + (previous?.id ?? 'new');
  const [draft, setDraft] = usePrivateDraft<Entry>(owner, slot, () => ({ snapshotId: '', observationId: previous?.observation_id ?? '', outcome: 'inconclusive', reason: '', refs: [] }));
  const read = useRead(useCallback(() => draft.snapshotId ? api.snapshot(draft.snapshotId) : Promise.resolve(null), [draft.snapshotId])); const snapshot = read.data?.data;
  const write = useSubmit(owner, slot);
  const valid = snapshot?.state === 'ready' && snapshot.id !== plan.snapshot_id && new Date(snapshot.as_of_time) >= new Date(closedAt) && new Date(snapshot.as_of_time).getTime() <= Date.now();
  const canSave = session.data.user.role !== 'viewer' && plan.publication_status === 'published' && valid && !!draft.reason.trim() && !write.busy && !read.busy && !read.error && (draft.outcome === 'inconclusive' || !!draft.refs.length && ['complete', 'partial'].includes(snapshot?.quality.status ?? ''));
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!canSave) return;
    const input: ValidationInput = { plan_revision_id: plan.id, scenario_id: scenario.id, observation_id: draft.observationId || null, snapshot_id: draft.snapshotId, outcome: draft.outcome, reason: draft.reason, evidence_refs: [...draft.refs].sort(), supersedes_id: previous?.id ?? null };
    const result = await write.submit(input, key => api.saveValidation(input, session.data.csrf_token, key));
    if (result) { discardPrivateDraft(owner, slot); saved(result.data); }
  }
  return <RecordDialog title={previous ? '追加验证更正' : '记录次日验证'} busy={write.busy} onClose={close}><form className="research-editor" onSubmit={submit}>
    <p className="reference-note">{plan.title} · 第 {plan.revision_number} 版 / {scenario.label} · 目标 {document.target_trade_day}</p><p className="permission-note">人工核对原条件，支持或反证均须引用材料。输入暂留此页内存，刷新前请保存；实际保存时间由服务器生成。</p>
    {previous && <section className="validation-previous"><h3>保留原验证</h3><p>{outcomeNames[previous.outcome]} · {recordedTime(previous.recorded_at)}</p><p className="preserved-text">{previous.reason}</p><p className="field-help">以下保存新原件，原结论和证据不会覆盖。若另一窗口已更正，服务器拒绝分叉。</p></section>}
    <SnapshotPicker value={draft.snapshotId} change={id => setDraft({ ...draft, snapshotId: id, refs: [] })}/>{read.busy && <p role="status">正在核对验证快照…</p>}{read.error && <ErrorState error={read.error} retry={read.reload}/>} {snapshot && !valid && <p className="permission-note">需要目标日收盘后或之后的新快照，不能沿用预案快照。原日历收盘：{recordedTime(closedAt)}。</p>}
    <ObservationChoice plan={plan.id} scenario={scenario.id} value={draft.observationId} change={id => setDraft({ ...draft, observationId: id })}/>
    <label>人工验证结论<select aria-label="人工验证结论" value={draft.outcome} onChange={event => setDraft({ ...draft, outcome: event.target.value as Entry['outcome'] })}>{Object.entries(outcomeNames).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
    <EvidencePicker snapshot={snapshot ?? null} value={draft.refs} change={refs => setDraft({ ...draft, refs })} label="验证材料引用"/>
    <label>核对理由或缺失原因<textarea aria-label="核对理由或缺失原因" value={draft.reason} rows={5} maxLength={20000} required onChange={event => setDraft({ ...draft, reason: event.target.value })}/></label>
    {draft.outcome !== 'inconclusive' && !draft.refs.length && <p className="permission-note">支持或反证必须至少引用一项快照材料；材料不足时请保留不可判断和原因。</p>}
    {write.error && <ErrorState error={write.error}/>} {write.error instanceof ApiError && write.error.status === 409 && <button type="button" onClick={review}>核对原记录与更正链（保留输入）</button>}<div className="editor-actions"><button className="primary" disabled={!canSave}>{write.busy ? '正在保存…' : previous ? '保存验证更正' : '保存验证记录'}</button></div>
  </form></RecordDialog>;
}

function ValidationDetails({ id, close, view, correct, canWrite }: { id: string; close: () => void; view: (id: string) => void; correct: (row: Validation) => void; canWrite: boolean }) {
  const read = useRead(useCallback(() => api.validation(id), [id])); const row = read.data?.data;
  const plan = useRead(useCallback(() => row ? api.researchRevision(row.plan_revision_id) : Promise.resolve(null), [row]));
  const doc = useRead(useCallback(() => plan.data?.data ? api.researchDocument(plan.data.data.document_id) : Promise.resolve(null), [plan.data]));
  const scenario = plan.data?.data.scenarios.find(item => item.id === row?.scenario_id);
  return <RecordDialog title="次日验证详情" onClose={close}>{read.busy ? <p role="status">正在读取验证原件…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : row && <article className="saved-research-content"><span className="badge muted">只读 · {row.superseded_by_id ? '已更正，原文保留' : '当前原件'}</span><h3>{outcomeNames[row.outcome]}</h3><p>{scenario?.label ?? '固定原情景'}</p>{scenario && <div className="scenario-summary"><p>原触发条件：{scenario.trigger_text}</p><p>原失效条件：{scenario.invalidation_text}</p></div>}<h3>人工核对理由 / 缺失原因</h3><p className="preserved-text">{row.reason}</p>
    <dl className="resource-details"><dt>实际保存</dt><dd>{recordedTime(row.recorded_at)}</dd><dt>原情景编号</dt><dd><code>{row.scenario_id}</code></dd><dt>内容指纹</dt><dd><code>{row.content_hash}</code></dd></dl>
    {plan.error && <ErrorState error={plan.error} retry={plan.reload}/>} {doc.error && <ErrorState error={doc.error} retry={doc.reload}/>}
    {plan.data?.data && doc.data?.data && <p><a href={`/research/plans?date=${doc.data.data.source_trade_day}&document=${doc.data.data.id}&revision=${row.plan_revision_id}`}>回查原预案 {plan.data.data.title} 第 {plan.data.data.revision_number} 版与上游研究</a></p>}
    {row.observation_id && <p><a href={`/auction?legacy=1&plan=${row.plan_revision_id}&scenario=${row.scenario_id}&observation=${row.observation_id}${doc.data?.data.target_trade_day ? '&date=' + doc.data.data.target_trade_day : ''}`}>回查关联竞价观察原件</a></p>}
    <p><a href={`/data?view=snapshots&snapshot=${row.snapshot_id}`}>回查本次盘后冻结快照</a></p><SavedEvidence ids={row.evidence_refs}/>
    <section className="validation-chain"><h3>更正回溯链</h3>{row.supersedes_id ? <button onClick={() => view(row.supersedes_id!)}>查看前一份验证原文</button> : <p>这是本情景的首份验证。</p>}{row.superseded_by_id ? <button onClick={() => view(row.superseded_by_id!)}>查看后一份更正</button> : <button className="primary" disabled={!canWrite || plan.data?.data.publication_status !== 'published'} onClick={() => correct(row)}>追加更正</button>}</section><p className="permission-note">这里只核对研究条件与材料，不计算成交、胜率或收益。更正追加新原件，旧理由和证据始终保留。</p>
  </article>}</RecordDialog>;
}
