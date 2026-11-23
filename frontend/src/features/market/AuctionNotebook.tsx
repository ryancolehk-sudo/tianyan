import { useCallback, useId, useState } from 'react';
import type { FormEvent } from 'react';
import type { Document, Observation, ObservationFact, ObservationInput, Revision, Scenario, SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { Card, Empty, ErrorState } from '../../shared/ui';
import { useRead } from '../../shared/useRead';
import { useSubmit } from '../../shared/useSubmit';
import { discardPrivateDraft, usePrivateDraft } from '../../shared/privateDrafts';
import { RecordDialog } from '../research/RecordDialog';
import { SavedEvidence, SnapshotPicker } from '../research/ReferencePickers';
import { recordedTime } from '../research/recordTypes';

import { updateLocation } from '../../shared/navigation';

function changeUrl(values: Record<string, string | null>) {
  const url = new URL(location.href);
  Object.entries(values).forEach(([key, value]) => { if (value) url.searchParams.set(key, value); else url.searchParams.delete(key); });
  updateLocation(url, { replace: true });
}

function Pager({ back, forward, busy }: { back?: () => void; forward?: () => void; busy: boolean }) {
  return <div className="resource-pager"><button disabled={!back || busy} onClick={back}>上一页</button><button disabled={!forward || busy} onClick={forward}>下一页</button></div>;
}

export function PlanChoice({ day, value, change }: { day: string; value: string; change: (id: string) => void }) {
  const [documentId, setDocument] = useState('');
  const [cursor, setCursor] = useState<string | null>(null); const [previous, setPrevious] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.targetPlans(day, cursor), [day, cursor]));
  return <section className="reference-picker"><div className="subsection-heading"><h3>选择原预案</h3><button onClick={read.reload}>刷新预案</button></div><p className="field-help">按目标日查找，跨休市也不按“昨天”猜测。请明确选择已发布版本；新草稿之前的旧版仍可引用。</p>
    {read.busy ? <p role="status">正在查找目标日预案…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !read.data?.data.items.length ? <Empty title="目标日尚无预案"><a href="/research/plans">先编写并发布条件预案</a></Empty> : <label>预案文档<select aria-label="选择预案文档" value={documentId} onChange={event => setDocument(event.target.value)}><option value="">请选择文档，再选择已发布版</option>{read.data?.data.items.map(row => <option value={row.id} key={row.id}>{row.head_revision_title} · 来源 {row.source_trade_day} · 当前第 {row.head_revision_number} 版</option>)}</select></label>}
    <Pager busy={read.busy} back={previous.length ? () => { setCursor(previous.at(-1) ?? null); setPrevious(previous.slice(0, -1)); setDocument(''); } : undefined} forward={read.data?.data.next_cursor ? () => { setPrevious([...previous, cursor]); setCursor(read.data?.data.next_cursor ?? null); setDocument(''); } : undefined}/>
    {documentId && <PublishedPlans key={documentId} documentId={documentId} value={value} change={change}/>}
  </section>;
}

function PublishedPlans({ documentId, value, change }: { documentId: string; value: string; change: (id: string) => void }) {
  const radioGroup = useId();
  const [cursor, setCursor] = useState<string | null>(null); const [previous, setPrevious] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.researchHistory(documentId, cursor), [documentId, cursor]));
  const rows = read.data?.data.items.filter(row => row.publication_status === 'published') ?? [];
  return <>{read.busy ? <p role="status">正在读取预案历史版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : rows.length ? <div className="published-revisions">{rows.map(row => <label className="check-label" key={row.id}><input type="radio" name={radioGroup} aria-label={`选择预案 ${row.title} 第 ${row.revision_number} 版`} checked={value === row.id} onChange={() => change(row.id)}/><span><strong>{row.title} · 第 {row.revision_number} 版</strong><small>已发布 · {recordedTime(row.published_at)}</small></span></label>)}</div> : <p className="field-help">本页没有已发布版本，草稿不能用于竞价观察。</p>}
    <Pager busy={read.busy} back={previous.length ? () => { setCursor(previous.at(-1) ?? null); setPrevious(previous.slice(0, -1)); } : undefined} forward={read.data?.data.next_cursor ? () => { setPrevious([...previous, cursor]); setCursor(read.data?.data.next_cursor ?? null); } : undefined}/></>;
}

export function AuctionNotebook({ session }: { session: SessionResponse }) {
  const params = new URLSearchParams(location.search);
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  const [day, setDay] = useState(params.get('date') ?? today);
  const [planId, setPlan] = useState(params.get('plan') ?? ''); const [scenarioId, setScenario] = useState(params.get('scenario') ?? '');
  const [entry, setEntry] = useState(params.get('entry') === 'new'); const [detail, setDetail] = useState(params.get('observation'));
  const [refresh, setRefresh] = useState(0);
  const plan = useRead(useCallback(() => planId ? api.researchRevision(planId) : Promise.resolve(null), [planId]));
  const documentId = plan.data?.data.document_id;
  const document = useRead(useCallback(() => documentId ? api.researchDocument(documentId) : Promise.resolve(null), [documentId]));
  const row = plan.data?.data; const doc = document.data?.data;
  const scenario = row?.scenarios.find(item => item.id === scenarioId);
  const canWrite = session.data.user.role !== 'viewer';
  function choose(id: string) { setPlan(id); setScenario(''); setEntry(false); setDetail(null); changeUrl({ plan: id, scenario: null, entry: null, observation: null }); }
  function chooseScenario(id: string) { setScenario(id); setEntry(false); setDetail(null); changeUrl({ scenario: id, entry: null, observation: null }); }
  function close() { setEntry(false); setDetail(null); changeUrl({ entry: null, observation: null }); }
  function view(id: string) { setDetail(id); changeUrl({ observation: id, entry: null }); }
  return <Card title="原预案与竞价观察记录" className="auction-notebook" action={<a className="more-link" href="/research/plans">查看研究预案 ›</a>}>
    <div className="subsection-heading"><label>目标日期<input aria-label="竞价目标日期" type="date" required value={day} onChange={event => { setDay(event.target.value); choose(''); changeUrl({ date: event.target.value }); }}/></label><span className="badge muted">人工观察 · 自动行情未接入</span></div>
    <PlanChoice key={day} day={day} value={planId} change={choose}/>
    {row && doc && (plan.error || document.error) && <ErrorState error={plan.error ?? document.error!} retry={() => { plan.reload(); document.reload(); }}/>}
    {planId && ((plan.busy && !row) || (document.busy && !doc) ? <p role="status">正在读取原预案与目标日…</p> : (plan.error || document.error) && (!row || !doc) ? <ErrorState error={plan.error ?? document.error!} retry={() => { plan.reload(); document.reload(); }}/> : !row || !doc || doc.kind !== 'plan' ? <p role="alert">所选记录不是可读取的预案版本。</p> : <>
      <div className="fixed-plan"><h3>{row.title} · 第 {row.revision_number} 版</h3><p>来源 {doc.source_trade_day} → 目标 {doc.target_trade_day} · {row.publication_status === 'published' ? '已发布的固定引用' : '该版本尚未发布或已撤回，不能新增观察'}</p><a href={`/research/plans?date=${doc.source_trade_day}&document=${doc.id}&revision=${row.id}`}>查看这个精确预案版本</a>
        <label>原预案情景<select aria-label="原预案情景" value={scenarioId} onChange={event => chooseScenario(event.target.value)}><option value="">选择情景</option>{row.scenarios.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        {scenario ? <div className="scenario-summary"><h4>{scenario.label}</h4><p><strong>原触发条件：</strong>{scenario.trigger_text}</p><p><strong>原失效条件：</strong>{scenario.invalidation_text}</p><details><summary>固定情景信息</summary><code>{scenario.id}</code></details></div> : <p className="field-help">选择原情景后记录观察；不会自动替换成新版情景。</p>}
      </div>
      <div className="subsection-heading"><h3>我的观察记录</h3><button data-new-research className="primary" disabled={!canWrite || !scenario || row.publication_status !== 'published' || doc.target_trade_day !== day} onClick={() => { setEntry(true); changeUrl({ entry: 'new', observation: null }); }}>记录竞价观察</button></div>
      {!canWrite && <p className="permission-note">当前账号只有读取权限。</p>}{doc.target_trade_day !== day && <p className="permission-note">所选目标日期与这个固定预案不一致，请切换日期或重新选择预案。</p>}
      <ObservationList key={planId + ':' + scenarioId + ':' + refresh} plan={planId} scenario={scenarioId} view={view}/>
      {entry && scenario && doc.target_trade_day && <ObservationEditor key={planId + ':' + scenarioId} plan={row} document={doc} scenario={scenario} session={session} close={close} saved={value => { setEntry(false); setRefresh(refresh + 1); view(value.id); }}/>}
    </>)}
    {detail && <ObservationDetails id={detail} close={close}/>}
    {planId && <p><a href={`/research/validations?date=${day}&plan=${planId}${scenarioId ? '&scenario=' + scenarioId : ''}`}>进入原情景次日验证 ›</a></p>}
    <p className="feature-note">人工记录不等于条件通过。观察时点、录入时间和晚补标识分别保存，事实与人工解释分开；自动行情与监控尚未接入。</p>
  </Card>;
}

function ObservationList({ plan, scenario, view }: { plan: string; scenario: string; view: (id: string) => void }) {
  const [cursor, setCursor] = useState<string | null>(null); const [previous, setPrevious] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.observations(plan, scenario, cursor), [plan, scenario, cursor]));
  return <section className="observation-list"><button onClick={read.reload}>刷新观察记录</button>{read.busy ? <p role="status">正在读取观察记录…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !read.data?.data.items.length ? <Empty title="当前版本尚无观察记录">此处只列所选精确版本{scenario ? '与情景' : ''}的记录。没有数据不代表条件通过或未触发。</Empty> : <><p className="field-help">本页实际读取 {read.data.data.items.length} 条记录；不是市场命中统计。</p><div className="table-wrap" tabIndex={0} role="region" aria-label="竞价观察记录表格"><table><thead><tr>{['检查点','资料状态','录入状态','实际保存时间','操作'].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{read.data.data.items.map(row => <tr key={row.id}><td>{row.phase}</td><td>{row.status === 'unavailable' ? '资料缺失 · 不可判断' : '人工事实抄录'}</td><td>{row.timeliness === 'late' ? '晚补记录' : '当时记录'}</td><td>{recordedTime(row.recorded_at)}</td><td><button aria-label={`查看观察 ${row.id}`} onClick={() => view(row.id)}>查看</button></td></tr>)}</tbody></table></div></>}
    <Pager busy={read.busy} back={previous.length ? () => { setCursor(previous.at(-1) ?? null); setPrevious(previous.slice(0, -1)); } : undefined} forward={read.data?.data.next_cursor ? () => { setPrevious([...previous, cursor]); setCursor(read.data?.data.next_cursor ?? null); } : undefined}/></section>;
}

type Entry = { phase: Observation['phase']; status: ObservationInput['status']; snapshotId: string; commentary: string; facts: ObservationFact[] };
const blankFact = (): ObservationFact => ({ field: '', value: '', unit: '', evidence_ref: '' });
function FactMaterialChoice({ ids, value, change, index }: { ids: string[]; value: string; change: (id: string) => void; index: number }) {
  const [offset, setOffset] = useState(0); const page = ids.slice(offset, offset + 20).join(',');
  const read = useRead(useCallback(async () => {
    const rows = []; const selected = page ? page.split(',') : [];
    for (let position = 0; position < selected.length; position += 5) rows.push(...await Promise.all(selected.slice(position, position + 5).map(async id => (await api.evidenceVersion(id)).data)));
    return rows;
  }, [page]));
  return <section>{read.error ? <ErrorState error={read.error} retry={read.reload}/> : <label>事实 {index + 1} 材料<select aria-label={`事实 ${index + 1} 材料`} value={value} required disabled={read.busy} onChange={event => change(event.target.value)}><option value="">{read.busy ? '正在读取材料名称…' : '请选择快照中的精确材料'}</option>{value && !read.data?.some(row => row.id === value) && <option value={value}>已选的精确材料版本</option>}{read.data?.map(row => <option value={row.id} key={row.id}>{row.source_key} · 第 {row.version_number} 版 · {row.available_at ? recordedTime(row.available_at) : '可得时间未知'}</option>)}</select></label>}
    {ids.length > 20 && <div className="resource-pager"><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>前一组材料</button><button type="button" disabled={offset + 20 >= ids.length} onClick={() => setOffset(offset + 20)}>后一组材料</button></div>}</section>;
}
function ObservationEditor({ plan, document, scenario, session, close, saved }: { plan: Revision; document: Document; scenario: Scenario; session: SessionResponse; close: () => void; saved: (value: Observation) => void }) {
  const owner = session.data.user.id; const slot = 'auction:new:' + plan.id + ':' + scenario.id;
  const [draft, setDraft] = usePrivateDraft<Entry>(owner, slot, () => ({ phase: '09:25', status: 'unavailable', snapshotId: '', commentary: '', facts: [blankFact()] }));
  const read = useRead(useCallback(() => draft.snapshotId ? api.snapshot(draft.snapshotId) : Promise.resolve(null), [draft.snapshotId]));
  const snapshot = read.data?.data;
  const write = useSubmit(owner, slot);
  const observed = `${document.target_trade_day}T${draft.phase}:00+08:00`;
  const ids = snapshot?.items.map(item => item.evidence_version_id) ?? [];
  const factRefs = [...new Set(draft.facts.map(item => item.evidence_ref).filter(Boolean))].sort().join(',');
  const material = useRead(useCallback(async () => {
    const values = await Promise.all((factRefs ? factRefs.split(',') : []).map(id => api.evidenceVersion(id)));
    return values.map(item => item.data);
  }, [factRefs]));
  const late = material.data?.some(item => !item.available_at || new Date(item.available_at) > new Date(observed) || item.event_at && new Date(item.event_at) > new Date(observed));
  const snapshotValid = snapshot?.state === 'ready' && snapshot.id !== plan.snapshot_id && new Date(snapshot.as_of_time) >= new Date(observed) && new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(snapshot.as_of_time)) === document.target_trade_day;
  const allowed = session.data.user.role !== 'viewer' && plan.publication_status === 'published';
  const canSave = allowed && snapshotValid && !write.busy && !read.busy && (draft.status === 'unavailable' ? !!draft.commentary.trim() : !material.busy && !material.error && !late && draft.facts.every(item => item.field.trim() && item.value.trim() && item.unit.trim() && ids.includes(item.evidence_ref)));
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!canSave) return;
    const facts = draft.status === 'observed' ? draft.facts : [];
    const input: ObservationInput = { plan_revision_id: plan.id, scenario_id: scenario.id, snapshot_id: draft.snapshotId, observed_at: observed,
      status: draft.status, facts, commentary: draft.commentary.trim() || null, evidence_refs: [...new Set(facts.map(item => item.evidence_ref))].sort() };
    const result = await write.submit(input, key => api.saveObservation(input, session.data.csrf_token, key));
    if (result) { discardPrivateDraft(owner, slot); saved(result.data); }
  }
  function factChange(index: number, value: ObservationFact) { setDraft({ ...draft, facts: draft.facts.map((item, offset) => offset === index ? value : item) }); }
  return <RecordDialog title="记录竞价观察" busy={write.busy} onClose={close}><form className="research-editor" onSubmit={submit}>
    <p className="reference-note">{plan.title} · 第 {plan.revision_number} 版 / {scenario.label}</p><p className="permission-note">只记录人工观察。超过检查点录入会标“晚补”，不能回填成当时已保存的判断。输入暂留此页内存，刷新前请保存。</p>
    <label>观察检查点<select aria-label="观察检查点" value={draft.phase} onChange={event => setDraft({ ...draft, phase: event.target.value as Entry['phase'] })}>{['09:15','09:20','09:25'].map(phase => <option key={phase}>{phase}</option>)}</select></label><p className="field-help">{document.target_trade_day} {draft.phase} · 北京时间；须有原日历明确的竞价时段，服务器核对发布时间和目标日。</p>
    <SnapshotPicker value={draft.snapshotId} change={id => setDraft({ ...draft, snapshotId: id, facts: draft.facts.map(item => ({ ...item, evidence_ref: '' })) })}/>
    {read.error && <ErrorState error={read.error} retry={read.reload}/>} {draft.snapshotId && !read.busy && snapshot && !snapshotValid && <p className="permission-note">需要目标日的新快照，材料截止不能早于所选检查点；不能沿用原预案的旧快照。</p>}
    <label>资料状态<select aria-label="观察资料状态" value={draft.status} onChange={event => setDraft({ ...draft, status: event.target.value as Entry['status'] })}><option value="unavailable">资料缺失 · 不可判断</option><option value="observed">有材料 · 人工抄录事实</option></select></label>
    {draft.status === 'observed' && <section className="observation-facts" aria-label="观察事实"><h3>事实条目</h3><p className="field-help">逐条填写字段、文字值、单位并选择精确材料。材料必须在检查点前已可得，不估算封单或撤单。</p>{draft.facts.map((item, index) => <section className="direction-editor" key={index}>
      <label>事实 {index + 1} 字段<input aria-label={`事实 ${index + 1} 字段`} value={item.field} maxLength={200} required onChange={event => factChange(index, { ...item, field: event.target.value })}/></label><label>事实 {index + 1} 值<textarea aria-label={`事实 ${index + 1} 值`} value={item.value} maxLength={20000} rows={3} required onChange={event => factChange(index, { ...item, value: event.target.value })}/></label><label>事实 {index + 1} 单位<input aria-label={`事实 ${index + 1} 单位`} value={item.unit} maxLength={200} required onChange={event => factChange(index, { ...item, unit: event.target.value })}/></label>
      <FactMaterialChoice key={draft.snapshotId} ids={ids} value={item.evidence_ref} index={index} change={id => factChange(index, { ...item, evidence_ref: id })}/>{item.evidence_ref && <a href={`/data?view=materials&evidence=${item.evidence_ref}`} target="_blank" rel="noreferrer">核对本条材料原文</a>}
      <button type="button" disabled={draft.facts.length <= 1} onClick={() => setDraft({ ...draft, facts: draft.facts.filter((_, offset) => offset !== index) })}>移除此事实</button></section>)}<button type="button" disabled={draft.facts.length >= 100} onClick={() => setDraft({ ...draft, facts: [...draft.facts, blankFact()] })}>添加事实条目</button>
      {material.error && <ErrorState error={material.error} retry={material.reload}/>} {late && <p className="permission-note">所选材料在检查点之后才可得或时间未知，不能填为当时事实。请选择当时已知材料，或记录资料缺失。</p>}
    </section>}
    <label>人工解释或缺失原因<textarea aria-label="人工解释或缺失原因" rows={4} required={draft.status === 'unavailable'} value={draft.commentary} maxLength={20000} onChange={event => setDraft({ ...draft, commentary: event.target.value })}/></label>
    {write.error && <ErrorState error={write.error}/>}<div className="editor-actions"><button className="primary" disabled={!canSave}>{write.busy ? '正在保存…' : '保存观察记录'}</button></div>
  </form></RecordDialog>;
}

function ObservationDetails({ id, close }: { id: string; close: () => void }) {
  const read = useRead(useCallback(() => api.observation(id), [id])); const row = read.data?.data;
  const plan = useRead(useCallback(() => row ? api.researchRevision(row.plan_revision_id) : Promise.resolve(null), [row]));
  const document = useRead(useCallback(() => plan.data?.data ? api.researchDocument(plan.data.data.document_id) : Promise.resolve(null), [plan.data]));
  const scenario = plan.data?.data.scenarios.find(item => item.id === row?.scenario_id);
  return <RecordDialog title="竞价观察详情" onClose={close}>{read.busy ? <p role="status">正在读取已保存观察…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : row && <article className="saved-research-content"><span className="badge muted">只读 · {row.status === 'unavailable' ? '资料缺失，不可判断' : '人工事实抄录'} · {row.timeliness === 'late' ? '晚补记录' : '当时记录'}</span>
    <h3>{scenario?.label ?? '固定原预案情景'} · {row.phase}</h3><dl className="resource-details"><dt>观察时点</dt><dd>{recordedTime(row.observed_at)}</dd><dt>实际保存</dt><dd>{recordedTime(row.recorded_at)}</dd><dt>原情景编号</dt><dd><code>{row.scenario_id}</code></dd><dt>内容指纹</dt><dd><code>{row.content_hash}</code></dd></dl>
    {plan.error && <ErrorState error={plan.error} retry={plan.reload}/>} {document.error && <ErrorState error={document.error} retry={document.reload}/>}
    {plan.data?.data && document.data?.data && <a href={`/research/plans?date=${document.data.data.source_trade_day}&document=${document.data.data.id}&revision=${row.plan_revision_id}`}>回查原预案 {plan.data.data.title} 第 {plan.data.data.revision_number} 版</a>}
    <p><a href={`/data?view=snapshots&snapshot=${row.snapshot_id}`}>回查本次冻结快照</a></p><h3>事实抄录</h3>{row.status === 'unavailable' ? <p>没有可核查事实，不判断条件是否满足。</p> : row.facts.map((fact, index) => <section key={index}><h4>{fact.field} · {fact.unit}</h4><p className="preserved-text">{fact.value}</p><a href={`/data?view=materials&evidence=${fact.evidence_ref}`}>查看本条原材料</a></section>)}
    <h3>人工解释 / 缺失原因</h3><p className="preserved-text">{row.commentary ?? '本条没有填写人工解释。'}</p><SavedEvidence ids={row.evidence_refs}/><p className="permission-note">这是不可覆盖的观察原件。晚补不代表当时已知决策；未进行自动条件验证或交易。</p>
  </article>}</RecordDialog>;
}
