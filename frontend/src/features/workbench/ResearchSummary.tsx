import { useCallback, useEffect, useRef, useState } from 'react';
import type { Document as ResearchDocument } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Empty, ErrorState, Segmented } from '../../shared/ui';
import { useTradingContext } from './TradingContext';
import './tradingWidgets.css';

const labels = { review:'复盘', direction:'方向', plan:'预案' } as const;
const status = { draft:'草稿', published:'已发布', withdrawn:'已撤回' } as const;
const route = { review:'reviews',direction:'directions',plan:'plans' } as const;

export function ResearchSummary() {
  const { day } = useTradingContext();
  const selectedDay = day || new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());
  const [kind,setKind] = useState('全部');
  const [selected,setSelected] = useState<ResearchDocument | null>(null);
  const read = useRead(useCallback(async () => {
    const results = await Promise.all((['review','direction','plan'] as const).map(value => api.researchDocuments(value,selectedDay)));
    return { day:selectedDay, rows:results.flatMap(result => result.data.items), truncated:results.some(result => !!result.data.next_cursor) };
  },[selectedDay]));
  useEffect(() => setSelected(null),[selectedDay]);
  const rows = !read.busy && read.data?.day === selectedDay ? read.data.rows.filter(row => kind === '全部' || labels[row.kind] === kind) : [];
  return <section className="trading-widget" aria-label="我的研究记录"><div className="trade-toolbar"><Segmented label="研究类型" options={['全部','复盘','方向','预案']} value={kind} onChange={setKind}/><button disabled={read.busy} onClick={read.reload}>刷新</button></div><p className="trade-meta">{selectedDay} · 当前账号 · 人工研究记录 · 未按个股筛选</p>{read.busy ? <p role="status">正在读取研究记录…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !rows.length ? <Empty title="该日还没有研究记录">在研究工作区保存复盘、方向或预案后，这里展示真实记录。</Empty> : <div className="trade-news-list">{rows.map(row => <button key={row.id} onClick={() => setSelected(row)}><span><strong>{row.head_revision_title}</strong><small>{labels[row.kind]} · {status[row.head_publication_status]} · 第 {row.head_revision_number} 版{row.target_trade_day ? ` · 目标 ${row.target_trade_day}` : ''}</small></span><span aria-hidden="true">›</span></button>)}</div>}{read.data?.truncated && <p className="trade-meta">当前为有界记录，请到研究工作区查看其余条目。</p>}<a className="more-link" href={`/research/reviews?date=${selectedDay}`}>进入研究工作区 ›</a>{selected && <ResearchDrawer key={selected.head_revision_id} document={selected} close={() => setSelected(null)}/>}</section>;
}

function ResearchDrawer({document,close}:{document:ResearchDocument;close:()=>void}) {
  const read = useRead(useCallback(() => api.researchRevision(document.head_revision_id),[document.head_revision_id]));
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const opener = window.document.activeElement; dialog.current?.showModal(); return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus({preventScroll:true}); }; },[]);
  const revision = read.data?.data;
  return <dialog ref={dialog} className="trade-drawer" aria-label="研究记录详情" onCancel={close} onClose={close}><header><strong>{document.head_revision_title}</strong><button onClick={close}>关闭</button></header><div className="trade-drawer-body"><p className="trade-meta">{document.source_trade_day} · {labels[document.kind]} · {status[document.head_publication_status]} · 第 {document.head_revision_number} 版</p>{read.busy ? <p role="status">正在读取版本…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : revision && <>{revision.content.type === 'review' ? <p className="trade-news-summary">{revision.content.summary}</p> : revision.content.type === 'direction' ? revision.content.items.map((item,index) => <section key={index}><h3>{item.label}</h3><p className="trade-news-summary">{item.judgment}</p><small>明确关联 {item.core_instrument_ids.length} 个标的</small></section>) : revision.content.scenarios.map((item,index) => <section key={index}><h3>{item.label}</h3><p className="trade-news-summary">触发：{item.trigger_text}</p><p className="trade-news-summary">失效：{item.invalidation_text}</p></section>)}{revision.content.limitations.length > 0 && <details className="trade-details"><summary>研究限制</summary>{revision.content.limitations.map((item,index) => <p key={index}>{item}</p>)}</details>}</>}<p><a href={`/research/${route[document.kind]}?date=${document.source_trade_day}&document=${document.id}&revision=${document.head_revision_id}`}>查看或编辑完整研究 ›</a></p></div></dialog>;
}
