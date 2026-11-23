import { useCallback, useId, useState } from 'react';
import type { FormEvent } from 'react';
import type { Document as ResearchDocument, DirectionContent, PlanContent, Revision, RevisionInput, SessionResponse } from '../../generated/schema';
import { api, ApiError } from '../../shared/api';
import { ErrorState } from '../../shared/ui';
import { useRead } from '../../shared/useRead';
import { useSubmit } from '../../shared/useSubmit';
import { hasUnsavedDraft, usePrivateDraft } from '../../shared/privateDrafts';
import { RecordDialog } from './RecordDialog';
import { CalendarPicker, EvidencePicker, InstrumentPicker, ParentPicker, SnapshotPicker, qualityNames } from './ReferencePickers';
import { publicationNames, recordedTime, researchSlot, researchNames } from './recordTypes';
import type { ResearchKind } from './recordTypes';
import { ScenarioEditor, TargetDay } from './PlanFields';

type Content = RevisionInput['content'];
type Draft = {
  target_trade_day: string | null; title: string; snapshot_id: string; calendar_version_id: string; upstream_revision_ids: string[]; content: Content;
  document_id: string | null; expected_head: number; saved_revision_id: string | null;
  saved_status: Revision['publication_status']; saved_fingerprint: string; saved_time: string | null;
};
type Props = { session: SessionResponse; kind: ResearchKind; day: string; documentId: string | null; fromRevisionId: string | null; close: () => void; saved: () => void };
function bodyOf(value: Draft): RevisionInput {
  const content: Content = value.content.type === 'review' ? { ...value.content, evidence_refs: [...value.content.evidence_refs].sort() } : value.content.type === 'direction' ? { ...value.content, items: value.content.items.map(item => ({ ...item, label: item.label.trim(), core_instrument_ids: [...item.core_instrument_ids].sort(), evidence_refs: [...item.evidence_refs].sort() })) } : { ...value.content, scenarios: value.content.scenarios.map(item => ({ ...item, label: item.label.trim(), instrument_ids: [...item.instrument_ids].sort(), evidence_refs: [...item.evidence_refs].sort() })) };
  return { title: value.title.trim(), snapshot_id: value.snapshot_id, calendar_version_id: value.calendar_version_id || null, upstream_revision_ids: [...value.upstream_revision_ids].sort(), content: { ...content, limitations: content.limitations.map(text => text.trim()).filter(Boolean) } };
}
function fingerprint(value: Draft) { return JSON.stringify({ target_trade_day: value.target_trade_day, body: bodyOf(value) }); }
function fromRevision(document: ResearchDocument, row: Revision, copy: boolean): Draft {
  const result: Draft = { target_trade_day: document.target_trade_day, title: row.title, snapshot_id: row.snapshot_id, calendar_version_id: row.calendar_version_id ?? '', upstream_revision_ids: row.upstream_revision_ids, content: row.content,
    document_id: document.id, expected_head: document.head_revision_number, saved_revision_id: copy ? null : row.id, saved_status: copy ? 'draft' : row.publication_status, saved_fingerprint: '', saved_time: copy ? null : row.recorded_at };
  if (!copy) result.saved_fingerprint = fingerprint(result);
  return result;
}
function blank(kind: ResearchKind): Draft {
  return { target_trade_day: null, title: '', snapshot_id: '', calendar_version_id: '', upstream_revision_ids: [], content: kind === 'review' ? { type: 'review', summary: '', evidence_refs: [], limitations: [] } : kind === 'direction' ? { type: 'direction', items: [{ label: '', judgment: '', core_instrument_ids: [], evidence_refs: [] }], limitations: [] } : { type: 'plan', scenarios: [{ label: '', trigger_text: '', invalidation_text: '', instrument_ids: [], evidence_refs: [] }], limitations: [] },
    document_id: null, expected_head: 0, saved_revision_id: null, saved_status: 'draft', saved_fingerprint: '', saved_time: null };
}

function Conflict({ id, rebase }: { id: string; rebase: (document: ResearchDocument, revision: Revision, adopt: boolean) => void }) {
  const read = useRead(useCallback(async () => {
    const document = (await api.researchDocument(id)).data;
    return { document, revision: (await api.researchRevision(document.head_revision_id)).data };
  }, [id]));
  return <section className="research-conflict" aria-label="版本冲突比较"><h3>文档已有新版本，你的输入仍保留</h3>{read.busy ? <p role="status">正在读取最新版本供比较…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : read.data && <>
    <p>服务器当前第 {read.data.document.head_revision_number} 版：{read.data.revision.title}</p><pre className="material-body">{read.data.revision.content.type === 'review' ? read.data.revision.content.summary : read.data.revision.content.type === 'direction' ? read.data.revision.content.items.map(item => item.label + '：' + item.judgment).join('\n') : read.data.revision.content.scenarios.map(item => item.label + '：' + item.trigger_text + '\n失效条件：' + item.invalidation_text).join('\n')}</pre>
    <div className="form-actions"><button type="button" onClick={() => rebase(read.data!.document, read.data!.revision, false)}>保留我的输入，以最新版本继续</button><button type="button" onClick={() => rebase(read.data!.document, read.data!.revision, true)}>采用最新版本内容</button></div><p className="field-help">不会自动覆盖。选择后仍需手动保存为下一版本。</p>
  </>}</section>;
}

function EditorBody({ initial, loadError, retryLoad, ...props }: Props & { initial: Draft; loadError?: Error | null; retryLoad?: () => void }) {
  const { session, kind, day, documentId, close, saved } = props;
  const owner = session.data.user.id; const slot = researchSlot(kind, day, documentId);
  const [draft, set] = usePrivateDraft<Draft>(owner, slot, () => initial);
  const [restored, setRestored] = useState(() => hasUnsavedDraft(owner, slot));
  const [notice, setNotice] = useState('');
  const write = useSubmit(owner, slot + ':write');
  const summaryId = useId(); const limitsId = useId();
  const current = fingerprint(draft); const dirty = current !== draft.saved_fingerprint;
  const snapshotRead = useRead(useCallback(async () => draft.snapshot_id ? (await api.snapshot(draft.snapshot_id)).data : null, [draft.snapshot_id]));
  const snapshot = snapshotRead.busy ? null : snapshotRead.data;
  const writable = session.data.user.role !== 'viewer';
  function update(next: Draft) { set(next, fingerprint(next) !== next.saved_fingerprint); setNotice(''); }
  function confirmed(next: Draft) { set(next, false); saved(); }
  function changeSnapshot(id: string) {
    const content = draft.content.type === 'review' ? { ...draft.content, evidence_refs: [] } : draft.content.type === 'direction' ? { ...draft.content, items: draft.content.items.map(item => ({ ...item, evidence_refs: [] })) } : { ...draft.content, scenarios: draft.content.scenarios.map(item => ({ ...item, evidence_refs: [] })) };
    update({ ...draft, snapshot_id: id, content }); setNotice('已切换快照并清空材料勾选。人工文字保留，请重新核对引用。');
  }
  const nextRead = useRead(useCallback(async () => kind === 'plan' && draft.calendar_version_id ? (await api.nextSession(draft.calendar_version_id, day)).data : null, [kind, draft.calendar_version_id, day]));
  const next = !nextRead.busy && nextRead.data?.calendar_id === draft.calendar_version_id ? nextRead.data : null;
  const targetValid = kind !== 'plan' || !!draft.target_trade_day && draft.target_trade_day > day && (!draft.calendar_version_id || !nextRead.busy && next?.session?.trade_day === draft.target_trade_day);
  const validText = draft.content.type === 'review' ? !!draft.content.summary.trim() : draft.content.type === 'direction' ? draft.content.items.length > 0 && draft.content.items.every(item => item.label.trim() && item.judgment.trim()) : draft.content.scenarios.length > 0 && draft.content.scenarios.every(item => item.label.trim() && item.trigger_text.trim() && item.invalidation_text.trim());
  const valid = writable && !!draft.title.trim() && !!snapshot && snapshot.state === 'ready' && targetValid && validText && (kind === 'review' || draft.upstream_revision_ids.length > 0);
  const limited = snapshot?.quality.status === 'partial' || snapshot?.quality.status === 'empty';
  const publishable = valid && !dirty && !!draft.saved_revision_id && draft.saved_status === 'draft' && !!draft.calendar_version_id && snapshot?.quality.status !== 'unavailable' && snapshot?.quality.status !== 'error' && snapshot?.quality.status !== 'stale' && (!limited || draft.content.limitations.some(text => text.trim())) && day <= new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());

  async function save(event: FormEvent) {
    event.preventDefault(); if (!valid || !dirty || write.busy) return;
    const body = bodyOf(draft);
    const result = await write.submit({ operation: 'save', document: draft.document_id, head: draft.expected_head, target: draft.target_trade_day, body }, async key => {
      if (draft.document_id) return (await api.saveResearchRevision(draft.document_id, body, session.data.csrf_token, key, draft.expected_head)).data;
      const document = (await api.saveResearchDocument({ kind, source_trade_day: day, ...(kind === 'plan' ? { target_trade_day: draft.target_trade_day } : {}), revision: body }, session.data.csrf_token, key)).data;
      // The POST is confirmed before its read-back: a failed GET must never create a second document.
      confirmed({ ...draft, document_id: document.id, expected_head: document.head_revision_number, saved_revision_id: document.head_revision_id, saved_fingerprint: current, saved_status: document.head_publication_status, saved_time: null });
      return (await api.researchRevision(document.head_revision_id)).data;
    });
    if (result) {
      confirmed({ ...draft, document_id: result.document_id, expected_head: result.revision_number, saved_revision_id: result.id, saved_fingerprint: current, saved_status: result.publication_status, saved_time: result.recorded_at });
      setNotice(`已保存第 ${result.revision_number} 版。历史原文和引用继续保留。`);
    }
  }
  async function publish() {
    if (!publishable || write.busy || !draft.saved_revision_id) return;
    const result = await write.submit({ operation: 'publish', revision: draft.saved_revision_id, head: draft.expected_head }, key => api.publishResearchRevision(draft.saved_revision_id!, session.data.csrf_token, key, draft.expected_head));
    if (result) {
      confirmed({ ...draft, saved_status: result.data.publication_status, expected_head: result.data.document_head_revision_number });
      setNotice(`已发布第 ${result.data.revision_number} 版，供本系统内固定引用。`);
    }
  }
  async function readConfirmed() {
    if (!draft.saved_revision_id) return;
    const result = await write.submit({ operation: 'read-confirmed', id: draft.saved_revision_id }, () => api.researchRevision(draft.saved_revision_id!));
    if (result) { confirmed({ ...draft, saved_status: result.data.publication_status, saved_time: result.data.recorded_at }); setNotice('已重新核对保存的版本。'); }
  }
  function rebase(document: ResearchDocument, revision: Revision, adopt: boolean) {
    const next = adopt ? fromRevision(document, revision, false) : { ...draft, expected_head: document.head_revision_number, saved_revision_id: null, saved_fingerprint: '', saved_status: 'draft' as const, saved_time: null };
    set(next, !adopt); write.clearError(); setNotice(adopt ? '已采用服务器最新内容。' : '你的输入保留，下一次保存将基于最新版本新增修订。');
  }
  return <RecordDialog title={'编辑' + researchNames[kind]} busy={write.busy} onClose={close}>
    {loadError && <ErrorState error={loadError} retry={retryLoad}/>}
    <div className="research-editor-context"><span>来源日 <strong>{day}</strong></span><span>{draft.document_id ? `当前依据第 ${draft.expected_head} 版` : '新建研究'}</span><span className="badge muted">{dirty ? '尚未保存' : publicationNames[draft.saved_status]}</span></div>
    <p className="form-intro">记录你自己的判断。每次保存生成新修订，发布后可作为后续研究的固定引用。</p>
    {restored && <div className="permission-note">已恢复当前账号尚未保存的输入，未用服务器内容覆盖。<button type="button" disabled={write.busy} onClick={() => { set(initial, !!props.fromRevisionId); setRestored(false); write.clearError(); setNotice('已放弃暂存输入并载入所选版本。'); }}>放弃暂存输入，载入所选版本</button></div>}
    <form aria-label="研究版本表单" onSubmit={save}><fieldset disabled={!writable || write.busy}>
      <label>研究标题<input value={draft.title} onChange={event => update({ ...draft, title: event.target.value })} maxLength={300} required/></label>
      <div className="research-source-grid"><SnapshotPicker value={draft.snapshot_id} change={changeSnapshot}/><CalendarPicker value={draft.calendar_version_id} change={id => update({ ...draft, calendar_version_id: id })}/></div>
      {draft.snapshot_id && (snapshotRead.busy ? <p role="status">正在核对当前快照…</p> : snapshotRead.error ? <ErrorState error={snapshotRead.error} retry={snapshotRead.reload}/> : snapshot && <div className="snapshot-binding"><span className="badge muted">{qualityNames[snapshot.quality.status]}</span><span>截止 {recordedTime(snapshot.as_of_time)}</span><a target="_blank" rel="noreferrer" href={`/data?view=snapshots&snapshot=${snapshot.id}`}>核对快照原件</a></div>)}
      {kind !== 'review' && <ParentPicker kind={kind === 'plan' ? 'direction' : 'review'} day={day} value={draft.upstream_revision_ids} change={ids => update({ ...draft, upstream_revision_ids: ids })}/>}
      {kind === 'plan' && <TargetDay day={day} calendarId={draft.calendar_version_id} value={draft.target_trade_day ?? ''} fixed={!!draft.document_id} change={value => update({ ...draft, target_trade_day: value })} lookup={{ busy: nextRead.busy, error: nextRead.error, data: next, retry: nextRead.reload }}/>}
      {draft.content.type === 'review' ? <section className="research-writing"><h3>我的复盘</h3><label><span id={summaryId}>复盘正文</span><textarea aria-labelledby={summaryId} rows={9} value={draft.content.summary} required maxLength={20000} placeholder="写下你的观察、判断及依据" onChange={event => { if (draft.content.type === 'review') update({ ...draft, content: { ...draft.content, summary: event.target.value } }); }}/></label><EvidencePicker key={draft.snapshot_id} snapshot={snapshot} value={draft.content.evidence_refs} label="复盘材料引用" change={ids => { if (draft.content.type === 'review') update({ ...draft, content: { ...draft.content, evidence_refs: ids } }); }}/></section> : draft.content.type === 'direction' ? <section className="research-writing"><h3>我的方向判断</h3>{draft.content.items.map((item, index) => <DirectionEditor key={index} index={index} item={item} snapshot={snapshot} change={next => { if (draft.content.type === 'direction') update({ ...draft, content: { ...draft.content, items: draft.content.items.map((old, i) => i === index ? next : old) } }); }} remove={() => { if (draft.content.type === 'direction') update({ ...draft, content: { ...draft.content, items: draft.content.items.filter((_, i) => i !== index) } }); }} removable={draft.content.type === 'direction' && draft.content.items.length > 1}/>)}<button type="button" disabled={draft.content.items.length >= 40} onClick={() => { if (draft.content.type === 'direction') update({ ...draft, content: { ...draft.content, items: [...draft.content.items, { label: '', judgment: '', core_instrument_ids: [], evidence_refs: [] }] } }); }}>添加方向条目</button></section> : <PlanWriting content={draft.content} snapshot={snapshot} change={content => update({ ...draft, content })}/>}
      <label><span id={limitsId}>材料与判断限制（每行一项）</span><textarea aria-labelledby={limitsId} rows={3} maxLength={20000} value={draft.content.limitations.join('\n')} onChange={event => update({ ...draft, content: { ...draft.content, limitations: event.target.value.split('\n') } })} placeholder="写明材料缺失、尚未核验的内容或判断适用范围"/></label>
      {limited && <p className="permission-note">当前材料受限或为空，发布前必须填写限制说明。</p>}
      {!draft.calendar_version_id && <p className="permission-note">日历未知，可保存草稿；发布前需选择明确版本并新增修订。</p>}
      <div className="form-actions research-save-actions"><button className="primary" disabled={!valid || !dirty}>{write.busy ? '正在处理…' : '保存草稿'}</button><button type="button" disabled={!publishable || write.busy} onClick={() => void publish()}>发布此版本</button><span>{!dirty && draft.saved_revision_id ? `第 ${draft.expected_head} 版 · ${publicationNames[draft.saved_status]}` : '输入保留在当前页面内存'}</span></div>
    </fieldset></form>
    {notice && <p className="form-success" role="status">{notice}</p>}
    {write.error && <ErrorState error={write.error}/>}
    {draft.saved_revision_id && !draft.saved_time && !dirty && <div className="permission-note">创建已确认，正在等待原版本回读。<button type="button" disabled={write.busy} onClick={() => void readConfirmed()}>重新读取已保存版本</button></div>}
    {write.error instanceof ApiError && write.error.status === 412 && draft.document_id && <Conflict id={draft.document_id} rebase={rebase}/>}
    <p className="feature-note">发布只在本系统内固定版本，不对外发送。收起可暂存未保存输入；同账号会话过期后重登可恢复，主动退出或换账号清除，刷新/关闭前请先保存。</p>
  </RecordDialog>;
}


function PlanWriting({ content, snapshot, change }: { content: PlanContent; snapshot: import('../../generated/schema').Snapshot | null; change: (value: PlanContent) => void }) {
  return <section className="research-writing"><h3>观察条件与研究失效边界</h3><p className="field-help">以下文字供人工观察与后续核验，不执行条件代码、不启动实时监控或交易。</p>
    {content.scenarios.map((item, index) => <ScenarioEditor key={index} index={index} item={item} snapshot={snapshot} removable={content.scenarios.length > 1} change={next => change({ ...content, scenarios: content.scenarios.map((old, i) => i === index ? next : old) })} remove={() => change({ ...content, scenarios: content.scenarios.filter((_, i) => i !== index) })}/>)}
    <button type="button" disabled={content.scenarios.length >= 40} onClick={() => change({ ...content, scenarios: [...content.scenarios, { label: '', trigger_text: '', invalidation_text: '', instrument_ids: [], evidence_refs: [] }] })}>添加条件情景</button>
  </section>;
}

function DirectionEditor({ index, item, snapshot, change, remove, removable }: { index: number; item: DirectionContent['items'][number]; snapshot: import('../../generated/schema').Snapshot | null; change: (item: DirectionContent['items'][number]) => void; remove: () => void; removable: boolean }) {
  const id = useId();
  return <section className="direction-editor"><div className="subsection-heading"><h4>方向 {index + 1}</h4><button type="button" disabled={!removable} onClick={remove}>移除此方向</button></div><label>方向名称<input aria-label={`方向 ${index + 1} 名称`} value={item.label} maxLength={300} required onChange={event => change({ ...item, label: event.target.value })}/></label><label><span id={id}>方向 {index + 1} 人工判断</span><textarea aria-labelledby={id} aria-label={`方向 ${index + 1} 人工判断`} rows={5} maxLength={20000} required value={item.judgment} onChange={event => change({ ...item, judgment: event.target.value })}/></label><InstrumentPicker value={item.core_instrument_ids} change={ids => change({ ...item, core_instrument_ids: ids })}/><EvidencePicker key={snapshot?.id ?? ''} snapshot={snapshot} value={item.evidence_refs} change={ids => change({ ...item, evidence_refs: ids })} label={`方向 ${index + 1} 材料引用`}/></section>;
}

export function ResearchEditor(props: Props) {
  const read = useRead(useCallback(async () => {
    if (!props.documentId) return blank(props.kind);
    const document = (await api.researchDocument(props.documentId)).data;
    if (document.kind !== props.kind || document.source_trade_day !== props.day) throw new Error('文档类型或来源日与当前页面不一致。请从原研究日期打开。');
    const revision = (await api.researchRevision(props.fromRevisionId ?? document.head_revision_id)).data;
    if (revision.document_id !== document.id) throw new Error('所选版本不属于此文档。');
    return fromRevision(document, revision, !!props.fromRevisionId);
  }, [props.documentId, props.kind, props.day, props.fromRevisionId]));
  return read.busy && !read.data ? <RecordDialog title="读取研究编辑器" onClose={props.close}><p role="status">正在读取研究版本…</p></RecordDialog> : read.error && !read.data ? <RecordDialog title="无法读取研究版本" onClose={props.close}><ErrorState error={read.error} retry={read.reload}/></RecordDialog> : read.data && <EditorBody {...props} initial={read.data} loadError={read.error} retryLoad={read.reload}/>;
}
