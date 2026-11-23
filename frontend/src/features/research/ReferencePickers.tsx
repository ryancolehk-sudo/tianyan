import { useCallback, useState } from 'react';
import type { EvidenceVersion, Snapshot, Revision } from '../../generated/schema';
import { api } from '../../shared/api';
import { Empty, ErrorState } from '../../shared/ui';
import { useRead } from '../../shared/useRead';
import { recordedTime } from './recordTypes';

export const qualityNames = { complete: '材料完整', partial: '材料受限', empty: '有效空快照', unavailable: '材料不足', stale: '材料过期', error: '读取失败' };

function PageButtons({ previous, next, busy, back, forward }: { previous: boolean; next?: string | null; busy: boolean; back: () => void; forward: () => void }) {
  return <div className="resource-pager"><button type="button" disabled={!previous || busy} onClick={back}>上一页</button><button type="button" disabled={!next || busy} onClick={forward}>下一页</button></div>;
}

export function SnapshotPicker({ value, change }: { value: string; change: (id: string) => void }) {
  const [cursor, setCursor] = useState<string | null>(null); const [history, setHistory] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.snapshots(cursor), [cursor]));
  const rows = read.data?.data.items.filter(row => row.state === 'ready') ?? [];
  return <section className="reference-picker"><div className="subsection-heading"><h3>材料快照</h3><button type="button" onClick={read.reload}>刷新快照</button></div>
    {read.busy ? <p role="status">正在读取本人快照…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : <>
      <label>选择冻结快照<select aria-label="选择冻结快照" value={value} onChange={event => change(event.target.value)} required><option value="">请明确选择一个已冻结快照</option>{value && !rows.some(row => row.id === value) && <option value={value}>当前修订绑定的精确快照</option>}{rows.map(row => <option value={row.id} key={row.id}>{recordedTime(row.as_of_time)} · {qualityNames[row.quality.status]} · {row.id.slice(0, 8)}</option>)}</select></label>
      {!rows.length && <p className="field-help">本页没有已冻结快照。可先到<a href="/data?view=snapshots" target="_blank" rel="noreferrer">数据中心冻结材料</a>，再刷新列表。</p>}
      <PageButtons previous={!!history.length} next={read.data?.data.next_cursor} busy={read.busy} back={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); }} forward={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); }}/>
    </>}
  </section>;
}

export function CalendarPicker({ value, change }: { value: string; change: (id: string) => void }) {
  const [cursor, setCursor] = useState<string | null>(null); const [history, setHistory] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.calendars(cursor), [cursor]));
  const rows = read.data?.data.items ?? [];
  return <section className="reference-picker"><div className="subsection-heading"><h3>交易日历</h3><button type="button" onClick={read.reload}>刷新日历</button></div>{read.busy ? <p role="status">正在读取日历版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : <>
    <label>日历版本<select aria-label="日历版本" value={value} onChange={event => change(event.target.value)}><option value="">未知 · 仅保存草稿</option>{value && !rows.some(row => row.id === value) && <option value={value}>当前修订绑定的精确日历版本</option>}{rows.map(row => <option key={row.id} value={row.id}>{row.version} · {row.exchange} · {row.coverage_start} 至 {row.coverage_end}</option>)}</select></label>
    <PageButtons previous={!!history.length} next={read.data?.data.next_cursor} busy={read.busy} back={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); }} forward={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); }}/>
    <p className="field-help">日历为人工声明。发布时服务器再次核对来源日是否明确开市；覆盖未知时不会按星期猜测。</p>
  </>}</section>;
}

export function EvidencePicker({ snapshot, value, change, label }: { snapshot: Snapshot | null; value: string[]; change: (ids: string[]) => void; label: string }) {
  const [offset, setOffset] = useState(0);
  const ids = snapshot?.items.map(row => row.evidence_version_id) ?? [];
  const selectedPage = ids.slice(offset, offset + 20).join(',');
  const read = useRead(useCallback(async () => {
    const rows: EvidenceVersion[] = []; const page = selectedPage ? selectedPage.split(',') : [];
    for (let i = 0; i < page.length; i += 5) rows.push(...await Promise.all(page.slice(i, i + 5).map(async id => (await api.evidenceVersion(id)).data)));
    return rows;
  }, [selectedPage]));
  return <section className="evidence-picker" aria-label={label}><h4>{label} <span className="secondary">已选 {value.length} 项</span></h4>{!snapshot ? <p className="field-help">先选择快照，再引用其中的精确材料版本。</p> : read.busy ? <p role="status">正在读取材料版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !ids.length ? <p className="field-help">此快照的有效材料清单为空。人工判断须说明材料限制。</p> : <>
    <div className="evidence-options">{read.data?.map(row => <div key={row.id}><label className="check-label"><input type="checkbox" aria-label={`引用材料 ${row.source_key} 第 ${row.version_number} 版`} checked={value.includes(row.id)} onChange={event => change(event.target.checked ? [...value, row.id] : value.filter(id => id !== row.id))}/><span><strong>{row.source_key} · 第 {row.version_number} 版</strong><small>{'text' in row.payload ? row.payload.text.slice(0, 110) : '保留精确原始材料引用'}</small></span></label><a href={`/data?view=materials&evidence=${row.id}`} target="_blank" rel="noreferrer">查看原文</a></div>)}</div>
    <div className="resource-pager"><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>前一组材料</button><span>每组最多 20 项</span><button type="button" disabled={offset + 20 >= ids.length} onClick={() => setOffset(offset + 20)}>后一组材料</button></div>
  </>}</section>;
}

function PublishedHistory({ documentId, value, change, label }: { documentId: string; label: string; value: string[]; change: (ids: string[]) => void }) {
  const [cursor, setCursor] = useState<string | null>(null); const [history, setHistory] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.researchHistory(documentId, cursor), [documentId, cursor]));
  const rows = read.data?.data.items.filter(row => row.publication_status === 'published') ?? [];
  return <>{read.busy ? <p role="status">正在读取已发布历史版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : <div className="published-revisions">{rows.length ? rows.map(row => <label className="check-label" key={row.id}><input type="checkbox" aria-label={`引用${label} ${row.title} 第 ${row.revision_number} 版`} checked={value.includes(row.id)} onChange={event => change(event.target.checked ? [...value, row.id] : value.filter(id => id !== row.id))}/><span><strong>{row.title} · 第 {row.revision_number} 版</strong><small>已发布 · {recordedTime(row.published_at)}</small></span></label>) : <p className="field-help">本页没有已发布版本。草稿不能作为固定上游。</p>}</div>}
    <PageButtons previous={!!history.length} next={read.data?.data.next_cursor} busy={read.busy} back={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); }} forward={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); }}/></>;
}

function SelectedParent({ id, remove }: { id: string; remove: () => void }) {
  const read = useRead(useCallback(() => api.researchRevision(id), [id])); const row: Revision | undefined = read.data?.data;
  return <div className="selected-parent">{read.busy ? <span role="status">正在核对固定引用…</span> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : row && <span><strong>{row.title} · 第 {row.revision_number} 版</strong><small>精确引用不会跟随新版本改变</small></span>}<button type="button" onClick={remove}>移除此引用</button></div>;
}

export function ParentPicker({ day, value, change, kind = 'review' }: { day: string; kind?: 'review' | 'direction'; value: string[]; change: (ids: string[]) => void }) {
  const [documentId, select] = useState(''); const [cursor, setCursor] = useState<string | null>(null); const [history, setHistory] = useState<Array<string | null>>([]);
  const label = kind === 'review' ? '复盘' : '方向';
  const read = useRead(useCallback(() => api.researchDocuments(kind, day, cursor), [kind, day, cursor]));
  return <section className="reference-picker"><div className="subsection-heading"><h3>固定上游{label}</h3><button type="button" onClick={read.reload}>刷新{label}</button></div>
    <p className="field-help">选择同一天已发布的精确版本。当前草稿之前的历史已发布版也可引用。</p>
    {value.map(id => <SelectedParent key={id} id={id} remove={() => change(value.filter(item => item !== id))}/>)}
    {read.busy ? <p role="status">正在读取同日{label}…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !read.data?.data.items.length ? <Empty title={`同日尚无${label}记录`}>先保存并发布上游研究，再建立精确版本引用。</Empty> : <label>选择{label}文档<select aria-label={`选择${label}文档`} value={documentId} onChange={event => select(event.target.value)}><option value="">请选择文档，再选已发布版本</option>{read.data?.data.items.map(row => <option value={row.id} key={row.id}>{row.head_revision_title} · 当前第 {row.head_revision_number} 版</option>)}</select></label>}
    <PageButtons previous={!!history.length} next={read.data?.data.next_cursor} busy={read.busy} back={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); select(''); }} forward={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); select(''); }}/>
    {documentId && <PublishedHistory key={documentId} documentId={documentId} value={value} change={change} label={label}/>}
  </section>;
}

export function InstrumentPicker({ value, change }: { value: string[]; change: (ids: string[]) => void }) {
  const [query, setQuery] = useState('');
  const read = useRead(useCallback(() => api.instruments(query), [query]));
  return <section className="instrument-picker"><h4>核心标的</h4><label>查找基础资料<input type="search" value={query} maxLength={80} onChange={event => setQuery(event.target.value)}/></label>
    {read.busy ? <p role="status">正在读取基础资料…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !read.data?.data.items.length ? <p className="field-help">没有可选的标的基础资料，不填入原型股票或代码。可以保留文字方向。</p> : read.data?.data.items.map(row => <label className="check-label" key={row.id}><input type="checkbox" checked={value.includes(row.id)} onChange={event => change(event.target.checked ? [...value, row.id] : value.filter(id => id !== row.id))}/>{row.name} · {row.symbol}</label>)}
    {!!value.length && <p className="field-help">已固定 {value.length} 个标的。<button type="button" onClick={() => change([])}>清空标的引用</button></p>}
  </section>;
}

export function SavedEvidence({ ids }: { ids: string[] }) {
  const [offset, setOffset] = useState(0); const page = ids.slice(offset, offset + 20).join(',');
  const read = useRead(useCallback(async () => {
    const rows: EvidenceVersion[] = []; const values = page ? page.split(',') : [];
    for (let i = 0; i < values.length; i += 5) rows.push(...await Promise.all(values.slice(i, i + 5).map(async id => (await api.evidenceVersion(id)).data)));
    return rows;
  }, [page]));
  return <section><h3>精确材料引用</h3>{read.busy ? <p role="status">正在回查材料版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !ids.length ? <p className="field-help">本版没有选取材料引用，快照与限制说明仍按原版保留。</p> : <div className="fixed-research-links">{read.data?.map(row => <a key={row.id} href={`/data?view=materials&evidence=${row.id}`} target="_blank" rel="noreferrer"><strong>{row.source_key} · 第 {row.version_number} 版</strong><span>查看当时引用的材料原文</span></a>)}</div>}
    {ids.length > 20 && <div className="resource-pager"><button type="button" disabled={!offset} onClick={() => setOffset(offset - 20)}>前一组引用</button><button type="button" disabled={offset + 20 >= ids.length} onClick={() => setOffset(offset + 20)}>后一组引用</button></div>}
  </section>;
}
