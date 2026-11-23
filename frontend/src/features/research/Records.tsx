import { useCallback, useEffect, useRef, useState } from 'react';
import type { Document as ResearchDocument, Revision, SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { Card, Empty, ErrorState } from '../../shared/ui';
import { useRead } from '../../shared/useRead';
import { discardPrivateDraft, hasUnsavedDraft, usePrivateDraft } from '../../shared/privateDrafts';
import { RecordDialog } from './RecordDialog';
import { ResearchEditor } from './ResearchEditor';
import { recordedTime, researchSlot, researchNames, publicationNames } from './recordTypes';
import type { ResearchKind } from './recordTypes';
import { SavedEvidence } from './ReferencePickers';
import { updateLocation } from '../../shared/navigation';
type View = { mode: 'edit' | 'view' | 'history'; documentId: string | null; revisionId: string | null } | null;

function RevisionBody({ value }: { value: Revision }) {
  return <div className="saved-research-content"><span className="badge muted">人工研究 · 观点与事实分开</span>{value.content.type === 'review' ? <pre className="material-body">{value.content.summary}</pre> : value.content.type === 'direction' ? value.content.items.map((item, index) => <section key={index}><h3>{item.label}</h3><pre className="material-body">{item.judgment}</pre>{item.core_instrument_ids.length > 0 && <p>关联了 {item.core_instrument_ids.length} 个基础资料中的核心标的。</p>}<small>精确材料引用：{item.evidence_refs.length} 项</small></section>) : value.scenarios.map((item, index) => <section key={index}><h3>{item.label}</h3><p className="field-help">人工条件 · 未执行监控</p><h4>触发条件</h4><pre className="material-body">{item.trigger_text}</pre><h4>失效条件</h4><pre className="material-body">{item.invalidation_text}</pre><details className="resource-audit"><summary>情景版本信息</summary><p>固定情景编号：<code>{item.id}</code></p></details></section>)}
    {!!value.content.limitations.length && <section className="research-limitations"><h3>本版限制</h3><ul>{value.content.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul></section>}
  </div>;
}

function ParentLinks({ ids }: { ids: string[] }) {
  const read = useRead(useCallback(async () => Promise.all(ids.map(async id => {
    const revision = (await api.researchRevision(id)).data;
    const document = (await api.researchDocument(revision.document_id)).data;
    return { revision, document };
  })), [ids.join(',')]), { retainOnTransientFailure: true });
  return <section><h3>固定上游研究版本</h3>{read.busy ? <p role="status">正在读取上游版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : <div className="fixed-research-links">{read.data?.map(({ revision, document }) => <a key={revision.id} href={`/research/${document.kind === 'review' ? 'reviews' : document.kind === 'direction' ? 'directions' : 'plans'}?${new URLSearchParams({ date: document.source_trade_day, document: document.id, revision: revision.id })}`} target="_blank" rel="noreferrer"><strong>{revision.title}</strong><span>第 {revision.revision_number} 版 · {publicationNames[revision.publication_status]}</span></a>)}</div>}</section>;
}

function RevisionDetails({ documentId, revisionId, edit, writable }: { documentId: string; revisionId: string | null; edit: (id: string) => void; writable: boolean }) {
  const read = useRead(useCallback(async () => {
    const document = (await api.researchDocument(documentId)).data;
    const revision = (await api.researchRevision(revisionId ?? document.head_revision_id)).data;
    if (revision.document_id !== document.id) throw new Error('所选版本不属于此文档。');
    return { document, revision };
  }, [documentId, revisionId]), { retainOnTransientFailure: true });
  const row = read.data;
  return read.busy ? <p role="status">正在读取研究版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : row && <>
    <div className="record-version-title"><div><h3>{row.revision.title}</h3><p>{row.document.source_trade_day} · 第 {row.revision.revision_number} 版 · {row.revision.revision_number === row.document.head_revision_number ? '文档当前版本' : '历史版本'}</p></div><span className="badge muted">{publicationNames[row.revision.publication_status]} · 只读</span></div>
    <dl className="resource-details">{row.document.target_trade_day && <><dt>目标交易日</dt><dd>{row.document.target_trade_day} · 文档固定目标</dd></>}<dt>保存时间</dt><dd>{recordedTime(row.revision.recorded_at)}</dd><dt>发布时间</dt><dd>{recordedTime(row.revision.published_at)}</dd><dt>材料截止</dt><dd><a href={`/data?view=snapshots&snapshot=${encodeURIComponent(row.revision.snapshot_id)}`} target="_blank" rel="noreferrer">回查冻结快照</a></dd><dt>日历引用</dt><dd>{row.revision.calendar_version_id ? '绑定了明确的人工日历版本' : '尚未绑定，只能保留草稿'}</dd></dl>
    <RevisionBody value={row.revision}/>
    {row.document.kind === 'plan' && row.revision.publication_status === 'published' && <p><a className="text-link" href={`/auction?legacy=1&date=${row.document.target_trade_day}&plan=${row.revision.id}`}>用这个固定预案查看或记录竞价观察</a></p>}
    <SavedEvidence ids={[...new Set(row.revision.content.type === 'review' ? row.revision.content.evidence_refs : row.revision.content.type === 'direction' ? row.revision.content.items.flatMap(item => item.evidence_refs) : row.revision.content.scenarios.flatMap(item => item.evidence_refs))]}/>
    {!!row.revision.upstream_revision_ids.length && <ParentLinks ids={row.revision.upstream_revision_ids}/>}
    <details className="resource-audit"><summary>查看精确版本与内容指纹</summary><p>版本编号：<code>{row.revision.id}</code></p><p>内容指纹：<code>{row.revision.content_hash}</code></p><p>前一修订：<code>{row.revision.based_on_revision_id ?? '初次记录'}</code></p></details>
    <div className="form-actions"><button className="primary" disabled={!writable} onClick={() => edit(row.revision.id)}>以此版内容新增修订</button><p className="field-help">保存会生成新版本，当前原文和引用继续保留。</p></div>
  </>;
}

function History({ documentId, view }: { documentId: string; view: (id: string) => void }) {
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.researchHistory(documentId, cursor), [documentId, cursor]), { retainOnTransientFailure: true });
  return <>{read.busy ? <p role="status">正在读取历史修订…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !read.data?.data.items.length ? <Empty title="没有可读取的历史版本"/> : <div className="research-history-list">{read.data.data.items.map(row => <button key={row.id} onClick={() => view(row.id)}><div><strong>{row.title}</strong><span>第 {row.revision_number} 版 · {publicationNames[row.publication_status]}</span></div><small>{recordedTime(row.recorded_at)}</small></button>)}</div>}
    <div className="resource-pager"><button disabled={!history.length || read.busy} onClick={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); }}>上一页</button><span>历史正文按精确版本读取</span><button disabled={!read.data?.data.next_cursor || read.busy} onClick={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); }}>下一页</button></div></>;
}

export function ResearchRecords({ session, kind, day, requestNew = 0 }: { session: SessionResponse; kind: ResearchKind; day: string; requestNew?: number }) {
  const owner = session.data.user.id;
  const writable = session.data.user.role !== 'viewer';
  const [context, setContext] = usePrivateDraft<View>(owner, `research:workspace:${kind}:${day}`, () => {
    const params = new URLSearchParams(location.search);
    return params.get('document') ? { mode: 'view', documentId: params.get('document'), revisionId: params.get('revision') } : null;
  }, false);
  const [cursor, setCursor] = useState<string | null>(null); const [history, setHistory] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.researchDocuments(kind, day, cursor), [kind, day, cursor]), { retainOnTransientFailure: true });
  const label = researchNames[kind];
  function open(value: NonNullable<View>) {
    if (value.mode === 'edit') {
      const slot = researchSlot(kind, day, value.documentId);
      if (!hasUnsavedDraft(owner, slot)) discardPrivateDraft(owner, slot);
    }
    setContext(value);
    const url = new URL(location.href);
    if (value.documentId) url.searchParams.set('document', value.documentId); else url.searchParams.delete('document');
    if (value.revisionId) url.searchParams.set('revision', value.revisionId); else url.searchParams.delete('revision');
    updateLocation(url, { replace: true });
  }
  function close() { setContext(null); const url = new URL(location.href); url.searchParams.delete('document'); url.searchParams.delete('revision'); updateLocation(url, { replace: true }); }
  // The explicit header action changes this counter; ordinary reads do not reopen it.
  const handledNew = useRef(requestNew);
  useEffect(() => {
    if (requestNew === handledNew.current) return;
    handledNew.current = requestNew;
    if (writable) open({ mode: 'edit', documentId: null, revisionId: null });
  }, [requestNew, writable]);
  return <><div id="research-records" tabIndex={-1} className="research-records-target"><Card title={`我的${label}记录`} className="research-records" action={<div className="table-actions"><button onClick={read.reload}>刷新记录</button><button data-new-research className="primary" disabled={!writable} onClick={() => open({ mode: 'edit', documentId: null, revisionId: null })}>新建{label}</button></div>}>
    <p className="field-help">{day} · 保存新增版本，发布固定本系统内的研究引用。行情与 AI 继续保持未接入。</p>
    {read.busy ? <p role="status">正在读取研究记录…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !read.data?.data.items.length ? <Empty title={`这一天还没有${label}记录`}>选择本人已冻结的材料快照，写下自己的判断；缺少日历时可以先保存草稿。</Empty> : <div className="table-wrap" tabIndex={0} role="region" aria-label="研究记录列表"><table><thead><tr><th scope="col">标题</th>{kind === 'plan' && <th scope="col">目标交易日</th>}<th scope="col">当前版本</th><th scope="col">状态</th><th scope="col">创建时间</th><th scope="col">操作</th></tr></thead><tbody>{read.data.data.items.map((row: ResearchDocument) => <tr key={row.id}><td><strong>{row.head_revision_title}</strong></td>{kind === 'plan' && <td>{row.target_trade_day}</td>}<td>第 {row.head_revision_number} 版</td><td><span className="badge muted">{publicationNames[row.head_publication_status]}</span></td><td>{recordedTime(row.created_at)}</td><td><div className="table-actions"><button aria-label={`查看研究 ${row.head_revision_title}`} onClick={() => open({ mode: 'view', documentId: row.id, revisionId: null })}>查看</button><button disabled={!writable} aria-label={`编辑研究 ${row.head_revision_title}`} onClick={() => open({ mode: 'edit', documentId: row.id, revisionId: null })}>编辑</button><button aria-label={`历史版本 ${row.head_revision_title}`} onClick={() => open({ mode: 'history', documentId: row.id, revisionId: null })}>历史版本</button></div></td></tr>)}</tbody></table></div>}
    <div className="resource-pager"><button disabled={!history.length || read.busy} onClick={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); }}>上一页</button><span>仅显示当前账号记录</span><button disabled={!read.data?.data.next_cursor || read.busy} onClick={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); }}>下一页</button></div>
  </Card></div>
  {context?.mode === 'edit' ? <ResearchEditor key={researchSlot(kind, day, context.documentId)} session={session} kind={kind} day={day} documentId={context.documentId} fromRevisionId={context.revisionId} close={close} saved={read.reload}/> : context?.documentId && <RecordDialog title={context.mode === 'history' ? '研究历史版本' : '研究版本详情'} onClose={close}>{context.mode === 'history' ? <History documentId={context.documentId} view={id => open({ ...context, mode: 'view', revisionId: id })}/> : <RevisionDetails documentId={context.documentId} revisionId={context.revisionId} writable={writable} edit={id => open({ ...context, mode: 'edit', revisionId: id })}/>}</RecordDialog>}
  </>;
}
