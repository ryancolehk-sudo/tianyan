import { useCallback, useEffect, useId, useState } from 'react';
import type { FormEvent } from 'react';
import type { CalendarInput, CalendarLookup, CalendarVersion, EvidenceInput, EvidenceVersion, Provider, SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { Card, Empty, ErrorState, Segmented } from '../../shared/ui';
import { useRead } from '../../shared/useRead';
import { useSubmit } from '../../shared/useSubmit';
import { discardPrivateDraft, usePrivateDraft } from '../../shared/privateDrafts';

type Workspace = { session: SessionResponse };
const states = { complete: '材料完整', partial: '材料受限', empty: '有效空快照', unavailable: '材料不足', stale: '材料过期', error: '读取失败' };
const exchangeNames = { SSE: '上交所', SZSE: '深交所', BSE: '北交所' };
function writable(session: SessionResponse) { return session.data.user.role !== 'viewer'; }
function time(value?: string | null) {
  return value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)) : '未知';
}
function localTime(value: string) { return new Date(new Date(value).getTime() + 8 * 3600_000).toISOString().slice(0, 23); }
function utc(value: string) { return new Date(value + '+08:00').toISOString(); }
function textOf(row: EvidenceVersion) { return 'text' in row.payload ? row.payload.text : 'summary' in row.payload ? row.payload.summary ?? '' : JSON.stringify(row.payload, null, 2); }
function Fingerprint({ value }: { value?: string | null }) { return <code className="fingerprint" title={value ?? undefined}>{value ? value.slice(0, 14) + '…' : '尚未生成'}</code>; }

function Pager({ next, previous, busy, onNext, onPrevious }: { next?: string | null; previous: boolean; busy: boolean; onNext: () => void; onPrevious: () => void }) {
  return <div className="resource-pager"><button disabled={!previous || busy} onClick={onPrevious}>上一页</button><span>按记录时间分页 · 非总数</span><button disabled={!next || busy} onClick={onNext}>下一页</button></div>;
}

type MaterialDraft = { key: string; revision: string; origin: '本人原创' | '外部材料'; text: string; url: string; published: string; rights: string };
function MaterialEditor({ session, source, editing, onSaved, onNew }: Workspace & { source?: Provider; editing: EvidenceVersion | null; onSaved: (row: EvidenceVersion) => void; onNew: () => void }) {
  const textLabelId = useId();
  const draftKey = editing ? 'evidence:correction:' + editing.id : 'evidence:new';
  const owner = session.data.user.id;
  const [draft, set] = usePrivateDraft<MaterialDraft>(owner, draftKey, () => ({
    key: editing?.source_key ?? 'note-' + crypto.randomUUID(), revision: String((editing?.version_number ?? 0) + 1),
    origin: editing?.authorship === 'external' ? '外部材料' : '本人原创', text: editing ? textOf(editing) : '',
    url: editing && 'source_url' in editing.payload ? editing.payload.source_url ?? '' : '', published: '', rights: editing?.rights_basis ?? '',
  }));
  const write = useSubmit(owner, draftKey);
  const field = (key: keyof MaterialDraft, value: string) => set({ ...draft, [key]: value });
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!source || !writable(session)) return;
    const body: EvidenceInput = { source_id: source.id, source_key: draft.key.trim(), source_revision: draft.revision.trim(),
      kind: 'manual', authorship: draft.origin === '本人原创' ? 'original' : 'external',
      payload: { text: draft.text, source_url: draft.origin === '外部材料' ? draft.url || null : null },
      published_at: draft.origin === '外部材料' && draft.published ? utc(draft.published) : null,
      rights_basis: draft.rights, supersedes_id: editing?.id ?? null };
    const saved = await write.submit(body, key => api.saveEvidence(body, session.data.csrf_token, key));
    if (saved) { discardPrivateDraft(owner, draftKey); onSaved(saved.data); }
  }
  return <Card title={editing ? '追加更正版本' : '保存研究材料'} className="material-editor">
    <p className="form-intro">{editing ? '保留旧版，新增一份有明确引用的更正。' : '保存本人研究记录，或留存有权使用的外部材料。'}</p>
    {!writable(session) && <p className="permission-note">当前为查看者，只能查看材料，不能保存或冻结。</p>}
    {!source && <p className="permission-note">人工材料录入端口尚未就绪，请检查数据源状态。</p>}
    <form aria-label="研究材料表单" onSubmit={save}>
      <fieldset disabled={!writable(session) || !source || write.busy}>
        <div className="form-pair"><label>材料编号<input value={draft.key} onChange={e => field('key', e.target.value)} maxLength={200} required readOnly={!!editing}/></label><label>版本标记<input value={draft.revision} onChange={e => field('revision', e.target.value)} maxLength={200} required/></label></div>
        <Segmented label="材料来源类型" options={['本人原创', '外部材料']} value={draft.origin} onChange={value => field('origin', value)}/>
        <p className="field-help">{draft.origin === '本人原创' ? '明确声明为本人原创记录。发布时间与接收时间均由服务器记录，不把观点当行情。' : '外部材料保持未核验；即使填写原文时间，也不会自动进入严格历史快照。'}</p>
        <label><span id={textLabelId}>材料内容</span><textarea aria-labelledby={textLabelId} rows={8} value={draft.text} onChange={e => field('text', e.target.value)} maxLength={20000} required/></label>
        {draft.origin === '外部材料' && <><label>来源链接（可留空）<input type="url" value={draft.url} onChange={e => field('url', e.target.value)} maxLength={2000}/></label><label>原文标注时间（北京时间，可留空）<input type="datetime-local" value={draft.published} onChange={e => field('published', e.target.value)} step="0.001"/></label></>}
        <label>保存权利依据<input value={draft.rights} onChange={e => field('rights', e.target.value)} maxLength={20000} placeholder="说明材料归属或保存授权，请勿填写账号或密钥" required/></label>
        <div className="form-actions"><button className="primary" disabled={!draft.text.trim() || !draft.rights.trim()}>{write.busy ? '正在保存…' : editing ? '保存更正版本' : '保存材料'}</button><button type="button" onClick={() => { discardPrivateDraft(owner, draftKey); onNew(); }}>{editing ? '取消更正' : '清空重填'}</button></div>
      </fieldset>
      {write.error && <ErrorState error={write.error}/>}
    </form>
    <p className="feature-note">未保存输入只留在本页面内存；同账号会话过期重登可恢复，主动退出或换账号清除。刷新/关闭前请先保存。</p>
  </Card>;
}

function MaterialDetail({ id }: { id: string }) {
  const read = useRead(useCallback(() => api.evidenceVersion(id), [id]));
  const row = read.data?.data;
  return <Card title="材料版本详情" className="material-detail">{read.busy ? <div role="status">正在读取材料版本…</div> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : row && <>
    <div className="resource-title"><h3>{row.source_key} · 第 {row.version_number} 版</h3><span className="badge muted">{row.is_current ? '当前版本' : '历史版本 · 只读'}</span></div>
    <dl className="resource-details"><dt>材料类型</dt><dd>{row.visibility === 'shared' && row.provenance === 'source_reported' ? '来源采集 · 共享只读' : row.authorship === 'original' ? '本人原创记录' : '外部材料 · 未核验'}</dd><dt>服务器接收</dt><dd>{time(row.received_at)}</dd><dt>原文/记录发布</dt><dd>{time(row.published_at)}</dd><dt>严格快照</dt><dd>{row.historical_eligible ? '可按接收与发生时点筛选' : '排除：外部时间尚未核验'}</dd><dt>保存权利依据</dt><dd>{row.rights_basis}</dd></dl>
    {row.visibility === 'shared' && <p className="feature-note">机器采集记录只读，发生时间与服务器首次接收分别保存；只能用于接收之后的研究，不补造过去已知的信息。</p>}<h3 className="body-label">材料正文</h3><pre className="material-body">{textOf(row)}</pre>
    <details className="resource-audit"><summary>查看版本与校验信息</summary><p>版本标记：{row.source_revision}</p><p>版本 ID：<code>{row.id}</code></p><p>更正来源：<code>{row.supersedes_id ?? '初始版本'}</code></p><p>SHA-256 内容指纹：<code>{row.content_hash}</code></p></details>
  </>}</Card>;
}

export function EvidenceDesk({ session, source, initialId, freeze }: Workspace & { source?: Provider; initialId?: string | null; freeze: (keys: string[]) => void }) {
  const [filter, setFilter] = useState(''); const [applied, apply] = useState('');
  const [cursor, setCursor] = useState<string | null>(null); const [history, setHistory] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.evidence(cursor, applied), [cursor, applied]));
  const health = useRead(api.sourceHealth);
  const [editing, setEditing] = useState<EvidenceVersion | null>(null); const [formVersion, setFormVersion] = useState(0);
  const [selected, select] = useState<string | null>(initialId ?? null); const [notice, setNotice] = useState('');
  const fresh = () => { setEditing(null); setFormVersion(value => value + 1); };
  const saved = (row: EvidenceVersion) => { setNotice(`已保存材料 ${row.source_key} 第 ${row.version_number} 版。`); select(row.id); fresh(); setCursor(null); setHistory([]); read.reload(); health.reload(); };
  const rows = !read.busy && !read.error ? read.data?.data.items ?? [] : [];
  return <><div className="resource-guidance">材料仅保存在本实例，默认仅本人可见。先保存材料、按截止时点冻结，再到复盘研究选择快照保存并发布判断。</div>
    {notice && <p className="success-note" role="status">{notice}</p>}
    <div className="evidence-layout"><MaterialEditor key={editing?.id ?? formVersion} session={session} source={source} editing={editing} onSaved={saved} onNew={fresh}/>
      <div className="evidence-read-column"><Card title="材料版本列表" className="evidence-list" action={<button onClick={() => { setCursor(null); setHistory([]); read.reload(); }}>刷新材料</button>}>
        <form className="resource-filter" onSubmit={e => { e.preventDefault(); apply(filter.trim()); setCursor(null); setHistory([]); read.reload(); }}><label><span className="sr-only">按材料编号筛选</span><input aria-label="按材料编号筛选" value={filter} maxLength={200} onChange={e => setFilter(e.target.value)} placeholder="按完整材料编号筛选"/></label><button>筛选</button><button type="button" onClick={() => { setFilter(''); apply(''); setCursor(null); setHistory([]); read.reload(); }}>全部材料</button></form>
        {read.busy ? <div className="resource-loading" role="status">正在读取材料列表…</div> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !rows.length ? <Empty title="还没有符合条件的材料">保存自己的第一份记录；外部材料可以留存，但不会被自动当作已核验事实。</Empty> : <div className="table-wrap" tabIndex={0} role="region" aria-label="材料版本列表"><table><thead><tr>{['材料编号 / 版本', '来源与状态', '接收时间', '内容指纹', '操作'].map(name => <th key={name} scope="col">{name}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><strong>{row.source_key}</strong><small>第 {row.version_number} 版 · {row.source_revision} · {row.is_current ? '当前版本' : '历史版本'}</small></td><td><span className="badge muted">{row.visibility === 'shared' && row.provenance === 'source_reported' ? '来源采集 · 共享只读' : row.authorship === 'original' ? '本人原创' : '外部未核验'}</span></td><td>{time(row.received_at)}</td><td><Fingerprint value={row.content_hash}/></td><td><div className="table-actions"><button aria-label={`查看材料 ${row.source_key} 版本 ${row.version_number}`} onClick={() => select(row.id)}>查看</button><button disabled={!writable(session) || !row.is_current || row.kind !== 'manual'} aria-label={`更正材料 ${row.source_key} 版本 ${row.version_number}`} onClick={() => { setEditing(row); select(row.id); }}>更正</button><button disabled={!writable(session)} aria-label={`冻结材料 ${row.source_key} 版本 ${row.version_number}`} onClick={() => freeze([row.source_key])}>冻结</button></div></td></tr>)}</tbody></table></div>}
        <Pager next={read.data?.data.next_cursor} previous={!!history.length} busy={read.busy} onNext={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); }} onPrevious={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); }}/>
      </Card>
      {selected && <MaterialDetail key={selected} id={selected}/>}
      <Card title="本机录入记录" action={<button onClick={health.reload}>刷新记录</button>}>{health.busy ? <p role="status">正在读取录入记录…</p> : health.error ? <ErrorState error={health.error} retry={health.reload}/> : !health.data?.data.items.length ? <Empty title="尚无本人的录入记录"/> : <div className="ingestion-events">{health.data.data.items.slice(0, 5).map(row => <div key={row.id}><span>{time(row.checked_at)}</span><strong>接受 {row.coverage.accepted} 项</strong><small>{row.reason}</small></div>)}</div>}<p className="feature-note">这里只记录本人的人工保存结果，不代表行情连接、费用或外部服务健康。</p></Card></div>
    </div></>;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => JSON.stringify(key) + ':' + canonicalJson(item)).join(',') + '}';
  return JSON.stringify(value);
}

function SnapshotDetail({ id, showEvidence }: { id: string; showEvidence: (id: string) => void }) {
  const read = useRead(useCallback(() => api.snapshot(id), [id]));
  const [verification, verify] = useState(''); const [verifying, setVerifying] = useState(false);
  const [page, setPage] = useState(0); const row = read.data?.data;
  useEffect(() => { if (row?.state !== 'pending' || read.busy || read.refreshing || read.error) return; const timer = setTimeout(read.reload, 1200); return () => clearTimeout(timer); }, [row?.state, read.busy, read.refreshing, read.error, read.reload]);
  async function check() {
    if (!row?.manifest || !row.manifest_hash) return;
    setVerifying(true);
    try {
      const result = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonicalJson(row.manifest)));
      const computed = [...new Uint8Array(result)].map(value => value.toString(16).padStart(2, '0')).join('');
      verify(computed === row.manifest_hash ? '快照指纹一致' : '快照指纹不一致，请保留版本编号并检查。');
    } catch { verify('暂时无法计算指纹，请稍后重试。'); }
    finally { setVerifying(false); }
  }
  return <div role="region" aria-label="快照详情"><Card title="快照详情" action={<button onClick={read.reload}>刷新此快照</button>}>{read.busy ? <p role="status">正在读取快照…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : row && <>
    <div className="resource-title"><h3>{row.state === 'ready' ? '已冻结' : row.state === 'failed' ? '冻结失败' : '后台冻结中'}</h3><span className="badge muted">{states[row.quality.status]}</span></div>
    <dl className="resource-details"><dt>截止时间</dt><dd>{time(row.as_of_time)}（北京时间）</dd><dt>记录时间</dt><dd>{time(row.recorded_at)}</dd><dt>精确材料版本</dt><dd>{row.state === 'ready' ? row.items.length : '尚未确定'}</dd><dt>快照编号</dt><dd><code>{row.id}</code></dd></dl>
    {(row.quality.reasons ?? []).map(reason => <p className="resource-guidance" key={reason}>{reason}</p>)}
    {row.state === 'pending' && <p role="status">后台尚未完成。你可以离开后再查看，不会扩大本次选中的材料范围。</p>}
    {row.error_code && <p className="permission-note">错误代码：{row.error_code}。本次没有生成可用冻结结果。</p>}
    {row.state === 'ready' && <><div className="manifest-check"><Fingerprint value={row.manifest_hash}/><button disabled={verifying} onClick={check}>{verifying ? '正在核对…' : '核对快照指纹'}</button>{verification && <span role="status">{verification}</span>}</div>
      {!row.items.length ? <Empty title="没有符合截止条件的材料">有效空快照与材料不足分别由上方状态说明。</Empty> : <div className="table-wrap" tabIndex={0} role="region" aria-label="快照材料版本"><table><thead><tr><th scope="col">精确版本 ID</th><th scope="col">内容指纹</th><th scope="col">回查</th></tr></thead><tbody>{row.items.slice(page * 20, page * 20 + 20).map(item => <tr key={item.evidence_version_id}><td><code>{item.evidence_version_id}</code></td><td><Fingerprint value={item.content_hash}/></td><td><button aria-label={`回查材料版本 ${item.evidence_version_id}`} onClick={() => showEvidence(item.evidence_version_id)}>查看材料版本</button></td></tr>)}</tbody></table></div>}
      {row.items.length > 20 && <Pager next={(page + 1) * 20 < row.items.length ? 'more' : null} previous={page > 0} busy={false} onNext={() => setPage(page + 1)} onPrevious={() => setPage(page - 1)}/>}
      <details className="resource-audit"><summary>查看完整冻结清单与来源版本</summary><pre>{JSON.stringify(row.manifest, null, 2)}</pre></details>
    </>}
  </>}</Card></div>;
}

export function SnapshotDesk({ session, selectedKeys, showEvidence }: Workspace & { selectedKeys: string[]; showEvidence: (id: string) => void }) {
  const owner = session.data.user.id;
  const [draft, setDraft] = usePrivateDraft(owner, 'snapshot:create', () => ({ cutoff: '', utcCutoff: null as string | null, keys: selectedKeys }), false);
  const { cutoff, keys } = draft;
  const [timeBusy, setTimeBusy] = useState(!cutoff); const [timeError, setTimeError] = useState<Error | null>(null);
  const [submitted, setSubmitted] = useState('');
  const [selected, select] = useState<string | null>(() => new URLSearchParams(location.search).get('snapshot')); const [cursor, setCursor] = useState<string | null>(null); const [history, setHistory] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.snapshots(cursor), [cursor])); const write = useSubmit(owner, 'snapshot:create');
  useEffect(() => {
    if (read.busy || read.error || !read.data?.data.items.some(row => row.state === 'pending')) return;
    const timer = setTimeout(read.reload, 1200);
    return () => clearTimeout(timer);
  }, [read.busy, read.error, read.data, read.reload]);
  useEffect(() => {
    if (cutoff) return;
    let active = true;
    api.session().then(value => { if (active) setDraft({ ...draft, cutoff: localTime(value.meta.generated_at), utcCutoff: value.meta.generated_at }); })
      .catch(reason => { if (active) setTimeError(reason instanceof Error ? reason : new Error('暂时无法读取服务器时间。')); })
      .finally(() => { if (active) setTimeBusy(false); });
    return () => { active = false; };
  }, []);
  async function refreshTime() {
    setTimeBusy(true); setTimeError(null);
    try { const stamp = (await api.session()).meta.generated_at; setDraft({ ...draft, cutoff: localTime(stamp), utcCutoff: stamp }); }
    catch (reason) { setTimeError(reason instanceof Error ? reason : new Error('暂时无法读取服务器时间。')); }
    finally { setTimeBusy(false); }
  }
  async function create(event: FormEvent) {
    event.preventDefault(); if (!writable(session)) return;
    const body = { as_of_time: draft.utcCutoff ?? utc(cutoff), required_capabilities: ['evidence.manual'], ...(keys.length ? { source_keys: keys } : {}) };
    const result = await write.submit(body, key => api.createSnapshot(body, session.data.csrf_token, key));
    if (result) { select(result.data.result_ref.id); setSubmitted(JSON.stringify(draft)); discardPrivateDraft(owner, 'snapshot:create'); setCursor(null); setHistory([]); read.reload(); }
  }
  return <><div className="resource-guidance">按截止时点选择当时可见的版本，再冻结材料和来源能力清单。外部未核验材料会被排除；这不是行情快照或自动规则结果。</div>
    <Card title="创建材料快照"><form className="snapshot-form" onSubmit={create}><label>截止时点（北京时间）<input type="datetime-local" step="0.001" value={cutoff} onChange={e => setDraft({ ...draft, cutoff: e.target.value, utcCutoff: null })} required disabled={timeBusy || write.busy || !writable(session)}/></label><button type="button" disabled={timeBusy || write.busy} onClick={() => void refreshTime()}>{timeBusy ? '正在读取时间…' : '使用当前时间'}</button><button className="primary" disabled={timeBusy || !cutoff || write.busy || !writable(session) || submitted === JSON.stringify(draft)}>{write.busy ? '正在提交…' : '创建冻结快照'}</button></form>
      {timeError && <ErrorState error={timeError} retry={() => void refreshTime()}/>}
      {submitted === JSON.stringify(draft) && <p className="field-help">该时点任务已提交，可在下方查看。需要新快照时请更新截止时点。</p>}
      <div className="snapshot-scope"><span>材料范围：{keys.length ? keys.join('、') : '本人的全部材料编号'}</span>{!!keys.length && <button disabled={timeBusy || write.busy} onClick={() => setDraft({ ...draft, keys: [] })}>改为全部材料</button>}</div>
      {!writable(session) && <p className="permission-note">查看者不能创建快照。</p>}{write.error && <ErrorState error={write.error}/>}</Card>
    <div className="snapshot-layout"><Card title="已有快照" action={<button onClick={read.reload}>刷新快照列表</button>}>{read.busy ? <p role="status">正在读取快照列表…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !read.data?.data.items.length ? <Empty title="尚无本人的材料快照">先保存材料，再按一个明确截止时点冻结。</Empty> : <div className="snapshot-list">{read.data.data.items.map(row => <button key={row.id} className={selected === row.id ? 'selected' : ''} onClick={() => select(row.id)}><span>{time(row.as_of_time)}</span><strong>{row.state === 'ready' ? '已冻结 · ' + states[row.quality.status] : row.state === 'pending' ? '冻结中' : '冻结失败'}</strong><Fingerprint value={row.manifest_hash}/></button>)}</div>}<Pager next={read.data?.data.next_cursor} previous={!!history.length} busy={read.busy} onNext={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); }} onPrevious={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); }}/></Card>
      {selected ? <SnapshotDetail key={selected} id={selected} showEvidence={showEvidence}/> : <Card title="选择快照"><Empty title="选择已有快照查看精确版本">冻结后可核对清单指纹，并回查当时引用的材料。</Empty></Card>}</div>
  </>;
}

export function CalendarDesk({ session }: Workspace) {
  const jsonLabelId = useId();
  const owner = session.data.user.id;
  const [raw, setRaw] = usePrivateDraft(owner, 'calendar:import', () => '');
  const [localError, setLocalError] = useState<Error | null>(null); const [notice, setNotice] = useState('');
  const [selected, select] = useState<CalendarVersion | null>(null); const [day, setDay] = useState('');
  const [answer, setAnswer] = useState<CalendarLookup | null>(null); const [lookupError, setLookupError] = useState<Error | null>(null); const [looking, setLooking] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null); const [history, setHistory] = useState<Array<string | null>>([]);
  const read = useRead(useCallback(() => api.calendars(cursor), [cursor])); const write = useSubmit(owner, 'calendar:import'); const admin = session.data.user.role === 'admin';
  async function choose(file?: File) {
    if (!file) return;
    if (file.size > 1_000_000) { setLocalError(new Error('日历文件超过 1 MB，请缩小覆盖范围。')); return; }
    try { setRaw(await file.text()); setLocalError(null); }
    catch { setLocalError(new Error('文件暂时无法读取，请重新选择。')); }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setLocalError(null); if (!admin) return;
    let body: CalendarInput;
    try { body = JSON.parse(raw) as CalendarInput; if (!body || typeof body !== 'object' || !Array.isArray(body.sessions)) throw new Error(); }
    catch { setLocalError(new Error('日历 JSON 格式不正确，尚未上传。请检查覆盖区间和逐日声明。')); return; }
    const result = await write.submit(body, key => api.saveCalendar(body, session.data.csrf_token, key));
    if (result) { setRaw(''); discardPrivateDraft(owner, 'calendar:import'); setNotice('日历版本已保存，来源仍为人工声明。'); select(result.data); setCursor(null); setHistory([]); read.reload(); }
  }
  async function lookup(next: boolean) {
    if (!selected || !day) return;
    setLooking(true); setLookupError(null); setAnswer(null);
    try { setAnswer((await (next ? api.nextSession(selected.id, day) : api.calendarSession(selected.exchange, selected.version, day))).data); }
    catch (reason) { setLookupError(reason instanceof Error ? reason : new Error('暂时无法查询，请重试。')); }
    finally { setLooking(false); }
  }
  return <><div className="resource-guidance">系统不预装或猜测真实交易日历。只有管理员可导入有明确来源、权利和完整逐日覆盖的版本；结果标记为人工声明，覆盖外保持未知。</div>
    {notice && <p role="status" className="success-note">{notice}</p>}
    <div className="calendar-layout"><Card title="导入日历版本"><form onSubmit={save} aria-label="日历导入表单"><fieldset disabled={!admin || write.busy}><label>选择日历 JSON 文件<input type="file" accept="application/json,.json" onChange={e => void choose(e.target.files?.[0])}/></label><label><span id={jsonLabelId}>日历 JSON</span><textarea aria-labelledby={jsonLabelId} rows={12} maxLength={1000000} value={raw} onChange={e => setRaw(e.target.value)} placeholder="粘贴自己的日历声明；不要包含账号、密钥或未获授权的资料。" required/></label><p className="field-help">需包含交易所、版本、来源、权利依据、覆盖起止日期及每一天的开闭市和时段。最大 3660 天、文件不超过 1 MB。</p><details className="resource-audit"><summary>查看字段结构（不含真实交易日）</summary><pre>{'{\n  "exchange": "SSE | SZSE | BSE",\n  "version": "自定版本",\n  "source_ref": "来源说明",\n  "rights_basis": "保存权利依据",\n  "coverage_start": "YYYY-MM-DD",\n  "coverage_end": "YYYY-MM-DD",\n  "sessions": [{"trade_day": "YYYY-MM-DD", "is_open": false, "segments": []}]\n}\n开市日 segments 逐段声明 start/end（HH:MM）与 kind（auction 或 continuous）。'}</pre></details><button className="primary" disabled={!raw.trim()}>{write.busy ? '正在导入…' : '导入日历版本'}</button></fieldset></form>{!admin && <p className="permission-note">当前账号只能查询已导入日历，不能新增版本。</p>}{(localError || write.error) && <ErrorState error={(localError || write.error)!}/>}</Card>
    <div className="calendar-read-column"><Card title="已导入日历" action={<button onClick={read.reload}>刷新日历</button>}>{read.busy ? <p role="status">正在读取日历版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !read.data?.data.items.length ? <Empty title="尚未导入交易日历">未接入时不会按周末或节假日猜测开市。</Empty> : <div className="calendar-version-list">{read.data.data.items.map(row => <button key={row.id} onClick={() => { select(row); setAnswer(null); }} className={selected?.id === row.id ? 'selected' : ''}><strong>{row.version}</strong><span>{exchangeNames[row.exchange]} · {row.coverage_start} 至 {row.coverage_end}</span><small>登记：{time(row.recorded_at)} · 人工声明</small></button>)}</div>}<Pager next={read.data?.data.next_cursor} previous={!!history.length} busy={read.busy} onNext={() => { setHistory([...history, cursor]); setCursor(read.data?.data.next_cursor ?? null); }} onPrevious={() => { setCursor(history.at(-1) ?? null); setHistory(history.slice(0, -1)); }}/></Card>
    <Card title="查询声明范围内的交易日">{!selected ? <Empty title="先选择一个日历版本"/> : <><h3>{selected.version} · {exchangeNames[selected.exchange]}</h3><p className="field-help">覆盖 {selected.coverage_start} 至 {selected.coverage_end}；来源：{selected.source_ref}</p><div className="calendar-lookup"><label>查询日期<input type="date" value={day} onChange={e => { setDay(e.target.value); setAnswer(null); }}/></label><button disabled={!day || looking} onClick={() => void lookup(false)}>查询当日</button><button disabled={!day || looking} onClick={() => void lookup(true)}>查询后续开市日</button></div>{looking && <p role="status">正在查询日历…</p>}{lookupError && <ErrorState error={lookupError}/>}<div className="calendar-answer" role="region" aria-label="日历查询结果">{answer && (answer.session ? <><h3>{answer.session.trade_day} · {answer.session.is_open ? '已声明开市' : '已声明休市'}</h3><span className="badge muted">人工日历 · 来源未自动核验</span>{!!answer.session.segments.length && <dl className="declared-segments">{answer.session.segments.map(segment => <div key={segment.start}><dt>{segment.kind === 'auction' ? '竞价时段' : '连续交易时段'}</dt><dd>{segment.start}—{segment.end}</dd></div>)}</dl>}</> : <Empty title="交易日未知">{answer.reason}</Empty>)}</div></>}</Card></div></div>
  </>;
}
