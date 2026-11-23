import { useState } from 'react';
import { request } from '../../shared/api';
import { useNavigation, updateLocation } from '../../shared/navigation';
import { useBoundRead } from '../daily-review/components';
import { dateTime } from '../terminal/data';
import type { TopicResearchView } from '../../generated/schema';
import './topicResearch.css';

type Props = { embedded?: boolean; keyword?: string; topicKey?: string; source?: string; day?: string; asOf?: string };
export function TopicResearch(props: Props) {
  const nav = useNavigation(), query = new URLSearchParams(nav.search);
  const keyword = props.keyword ?? query.get('topic') ?? '';
  const topicKey = props.topicKey ?? query.get('sector') ?? keyword;
  const source = props.source ?? query.get('topic_source') ?? 'keyword';
  const day = props.day || query.get('trade_day') || new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());
  const asOf = props.asOf ?? query.get('as_of_time') ?? '';
  const identity = JSON.stringify([keyword, topicKey, source, day, asOf]);
  const [page, setPage] = useState({identity, cursors:['']});
  const cursors = page.identity === identity ? page.cursors : [''];
  const params = new URLSearchParams({keyword, topic_key:topicKey, topic_source:source, trade_day:day,
    ...(asOf ? {as_of_time:asOf} : {}), ...(cursors.at(-1) ? {cursor:cursors.at(-1)!} : {})});
  const read = useBoundRead('topic:' + params, async () => {
    if (!keyword) return null;
    const response = await request<{data:TopicResearchView}>('/api/v1/news/topics?' + params);
    const value = response.data;
    if (!value || !Array.isArray(value.gaps) || !Array.isArray(value.events) || !Array.isArray(value.members)) {
      throw new Error('题材资料格式暂未核实，请重试本区。');
    }
    return response;
  });
  const data = read.data?.data;
  const sourceLabel = ({ keyword:'关键词检索', dc:'东方财富板块', stock:'指定证券' } as Record<string,string>)[source] || '指定来源';
  const back = new URLSearchParams({trade_day:day, sector:query.get('sector') || '', stock:query.get('stock') || '',
    ...(asOf ? {as_of_time:asOf} : {}), ...(query.get('research_scope') ? {research_scope:query.get('research_scope')!} : {})});
  return <section className={'topic-research' + (props.embedded ? ' is-embedded' : '')} aria-label="题材研究">
    {!props.embedded && <><header><h1>题材专题</h1><a href={'/workbench?' + back + (query.get('return_section')==='board-direction'?'#board-direction':'')}>返回原工作台 →</a></header><form className="topic-search" onSubmit={event=>{event.preventDefault();const form=new FormData(event.currentTarget);const value=String(form.get('topic')||'').trim();if(!value)return;const url=new URL(location.href);url.searchParams.set('topic',value);url.searchParams.delete('sector');url.searchParams.set('topic_source','keyword');updateLocation(url);}}><label>题材关键词<input key={keyword} name="topic" defaultValue={keyword} placeholder="输入题材名称或关键词" maxLength={80} required/></label><button>查找已存线索</button></form></>}
    {keyword && <h2>{keyword}</h2>}<p className="topic-meta">{day} · {sourceLabel} · {data ? `材料截至 ${dateTime(data.as_of_time)}` : asOf ? `材料截至 ${dateTime(asOf)}` : '读取当日已知材料'}</p>
    {keyword && <details className="topic-identity"><summary>检索范围</summary><dl><dt>来源</dt><dd>{sourceLabel}</dd><dt>来源身份</dt><dd>{source}:{topicKey}</dd><dt>资料日期</dt><dd>{day}</dd><dt>接收截止</dt><dd>{data ? dateTime(data.as_of_time) : asOf ? dateTime(asOf) : '所选日期结束前，且不晚于当前时点'}</dd></dl></details>}
    {read.error && <p role="alert">{read.error.message}<button onClick={read.reload}>重试本区</button></p>}
    {read.busy && <div className="topic-loading" role="status">正在读取原日成员与事件证据…</div>}
    {data && <><p>已匹配 {data.total} 条来源记录。关联范围仅限所示题材身份；不同来源同名题材分别研究。</p>
      <details className="topic-gaps"><summary>资料覆盖与缺口</summary>{data.gaps.map(gap=><p key={gap}>{gap}</p>)}</details>
      <h3>事件时间线</h3><ol className="topic-timeline">{data.events.map(event=><li key={event.evidence_id}><span className={'topic-basis '+event.basis}>{{source_explicit:'来源明确关联',manual_confirmed:'人工已确认',keyword_clue:'关键词线索'}[event.basis]}</span><h4>{event.title}</h4>{event.summary&&<details><summary>阅读摘要</summary><p>{event.summary}</p></details>}<p className="topic-meta">{event.source_name} · 发布 {dateTime(event.published_at)} · 接收 {dateTime(event.received_at)}</p><p>{event.basis_note}</p>{event.source_url&&/^https?:\/\//.test(event.source_url)&&<a href={event.source_url} target="_blank" rel="noopener noreferrer">查看原文 ↗</a>}<details><summary>证据版本</summary><p>{event.evidence_id}</p><p>{event.content_hash}</p></details></li>)}</ol>{!data.events.length&&<p>该时点暂无匹配资讯；不据此推断没有事件。</p>}
      <div className="topic-pagination"><button disabled={cursors.length<2} onClick={()=>setPage({identity,cursors:cursors.slice(0,-1)})}>上一页</button><span>第 {cursors.length} 页</span><button disabled={!data.next_cursor} onClick={()=>data.next_cursor&&setPage({identity,cursors:[...cursors,data.next_cursor]})}>下一页</button></div>
      {!props.embedded && <><h3>原日成员与当日表现</h3><div className="topic-members"><table><thead><tr><th>个股</th><th>代码</th><th>当日涨幅</th><th>成员依据</th></tr></thead><tbody>{data.members.map((row,i)=><tr key={String(row.code)+i}><td><a href={'/workbench?'+new URLSearchParams({...Object.fromEntries(back),stock:String(row.code),research_scope:'all'})}>{String(row.name)}</a></td><td>{String(row.code)}</td><td>{row.pct_change==null?'未观测':Number(row.pct_change).toFixed(2)+'%'}</td><td><details><summary>原日成员证据</summary><pre>{JSON.stringify(row.evidence,null,2)}</pre></details></td></tr>)}</tbody></table></div>{!data.members.length&&<p>没有可核的原日成员记录，其他事件材料仍可阅读。</p>}</>}
    </>}
  </section>;
}
