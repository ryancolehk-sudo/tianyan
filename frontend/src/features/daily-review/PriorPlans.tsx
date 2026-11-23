import { useState } from 'react';
import type { Document } from '../../generated/schema';
import { api } from '../../shared/api';
import { Empty, Failure, Loading, useBoundRead } from './components';
import type { ReviewProps } from './context';

export function PriorPlans(p:ReviewProps) {
  const read=useBoundRead(`target-plans:${p.day}:${p.asOf}`,()=>p.asOf?Promise.resolve(null):api.targetPlans(p.day));
  return <><Failure error={read.error} retry={read.reload}/>{p.asOf?<Empty>历史时点模式不读取后来保存的人工判断。请通过原件固定版本核对。</Empty>:read.busy?<Loading/>:read.data?.data.items.length?read.data.data.items.map(doc=><Plan key={doc.id} doc={doc} day={p.day}/>):<Empty>没有目标为今日的人工预案。</Empty>}{read.data?.data.next_cursor&&<p>当前显示首批预案，完整记录请进入验证页查看。</p>}<a href={'/research/validations?date='+p.day}>进入原预案验证 →</a><p className="dr-note">AI生成报告与人工发布预案分别保留；普通文本条件不自动判断。支持、反证、无法判断由本人确认保存，更正追加版本。</p></>;
}
function Plan({doc,day}:{doc:Document;day:string}) {
  const [open,setOpen]=useState(false),[chosen,setChosen]=useState('');
  const history=useBoundRead(`plan-history:${doc.id}:${open}`,()=>open?api.researchHistory(doc.id):Promise.resolve(null));
  const versions=history.data?.data.items.filter(row=>row.publication_status==='published')??[];
  const revisionId=chosen||versions[0]?.id||'';
  const revision=useBoundRead(`plan-exact:${revisionId}:${open}`,()=>open&&revisionId?api.researchRevision(revisionId):Promise.resolve(null));
  const validations=useBoundRead(`plan-validations:${revisionId}:${open}`,()=>open&&revisionId?api.validations(revisionId):Promise.resolve(null));
  const row=revision.data?.data;
  return <details onToggle={e=>setOpen(e.currentTarget.open)}><summary>{doc.head_revision_title} · 人工研究 · {doc.source_trade_day}</summary><Failure error={history.error||revision.error||validations.error} retry={()=>{history.reload();revision.reload();validations.reload();}}/>{history.busy&&<Loading/>}<label>已发布精确版本 <select value={revisionId} onChange={e=>setChosen(e.target.value)}>{versions.map(v=><option key={v.id} value={v.id}>v{v.revision_number} · {v.title}</option>)}</select></label>{!versions.length&&!history.busy&&<p>当前返回的版本没有已发布预案，草稿不参加验证。</p>}{history.data?.data.next_cursor&&<p>尚有更多历史版本，可到原件页查看。</p>}{row&&<><p>原件 v{row.revision_number} · {row.id}</p><p>发布时间：{row.published_at?new Date(row.published_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未知'}</p>{row.scenarios.map(s=><section className="dr-plan-scenario" key={s.id}><h4>{s.label}</h4><p>触发条件：{s.trigger_text}</p><p>失效条件：{s.invalidation_text}</p><p>原预案证据 {s.evidence_refs.length} 条</p><a href={'/research/validations?'+new URLSearchParams({date:day,plan:row.id,scenario:s.id})}>核对实际证据并确认结果 →</a>{validations.data?.data.items.filter(v=>v.scenario_id===s.id).map(v=><p key={v.id}><a href={'/research/validations?'+new URLSearchParams({date:day,plan:row.id,scenario:s.id,validation:v.id})}>已存核对：{v.outcome==='supported'?'支持':v.outcome==='refuted'?'反证':'无法判断'}{v.superseded_by_id?'（已更正，原件保留）':''}</a></p>)}</section>)}</>}{validations.data?.data.next_cursor&&<p>更多核对记录请进入验证页。</p>}</details>;
}
