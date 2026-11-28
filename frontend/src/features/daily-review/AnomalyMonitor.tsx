import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { pinyin } from 'pinyin-pro';
import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { updateLocation } from '../../shared/navigation';
import './anomalyMonitor.css';

type Row = {code:string; name:string; change?:number; deviation?:number; target?:number; days?:number;
  state?:number; rule?:string; date?:string; three?:number|null; times?:number; start?:string;
  end?:string; kind?:string; remaining?:number; market?:string; link?:string};
type Snapshot = {day:string; received_at:string; predictions:Row[]; history:Row[]; repeated:Row[]; pool:Row[]};
type Group = 'predictions'|'history'|'repeated'|'pool';
const status = (r:Row) => r.state===1?'已触发':r.state===0?'未触发':'待验证';
const pct = (n?:number|null) => n == null?'—':(n>0?'+':'')+n.toFixed(2)+'%';
function Change({n}:{n?:number|null}) {
  return <span className={n==null?'':n>0?'am-up':n<0?'am-down':''}>{pct(n)}</span>;
}
function badge(r:Row,history:boolean) {
  return <b className={'am-badge '+(r.state===2?'next':'')}>{history?status(r):r.state===2?'次日':'当日'}</b>;
}
function Records({rows,group,cards}:{rows:Row[]; group:Group; cards:boolean}) {
  const area=useRef<HTMLDivElement>(null);
  const [size,setSize]=useState(1),[index,setIndex]=useState(0);
  useLayoutEffect(()=>{
    const el=area.current!;
    const resize=()=>setSize(Math.max(1,cards?Math.floor((el.clientHeight+6)/104)*(el.clientWidth>=550?2:1)
      :Math.floor((el.clientHeight-27)/(group==='pool'||group==='repeated'?30:34))));
    resize(); const observer=new ResizeObserver(resize);observer.observe(el);return ()=>observer.disconnect();
  },[cards,group]);
  const pages=Math.max(1,Math.ceil(rows.length/size)), page=Math.min(index,pages-1);
  const slice=rows.slice(page*size,(page+1)*size), history=group==='history';
  const headings=group==='pool'?['名称 / 代码','监控类型','监控期','剩余','市场','公告']:
    group==='repeated'?['名称 / 代码','3日偏离','10日异动','今日涨幅','天数']:
    [history?'异动状态':'时间','股票名称',history?'当日涨幅':'今日涨幅','触发异动涨幅','累计偏离值'];
  return <><div className="am-records" ref={area}>{!rows.length?<div className="am-empty">暂无符合条件的记录</div>:
    cards?<div className="am-grid">{slice.map((r,i)=><article key={i}>
      <header><strong>{r.name}</strong><Change n={r.change}/><small>{r.days}日偏离 <Change n={r.deviation}/></small></header>
      <div>{badge(r,history)} 收盘涨跌幅门槛 <em>{pct(r.target)}</em></div>
      <p>{history?r.date+' · ':''}{r.state===2?'次一交易日收盘验证':status(r)+'严重异动'}</p>
      <footer title={r.rule}>{r.rule}</footer></article>)}</div>:
    <table className={'am-table-'+group}><thead><tr>{headings.map(h=><th key={h}>{h}</th>)}{history&&<th className="am-rule-cell">触发规则</th>}</tr></thead><tbody>{slice.map((r,i)=><tr key={i}
      className={group==='repeated' && Math.abs(r.three??0)>=20?'am-highlight':''}>
      {group==='pool'?<><td><strong>{r.name}</strong><small>{r.code}</small></td><td><span className="am-kind">{r.kind}</span></td>
        <td title={r.start+' 至 '+r.end}>{r.start}<small>至 {r.end}</small></td><td><b className="am-days">{r.remaining}天</b></td><td>{r.market}</td>
        <td>{r.link?.startsWith('https://')?<a href={r.link} target="_blank" rel="noreferrer">↗ 公告</a>:'—'}</td></>:
      group==='repeated'?<><td><strong>{r.name}</strong><small>{r.code}</small></td><td title="腾讯前复权日线，近三个交易日区间涨幅减对应指数区间涨幅"><Change n={r.three}/></td>
        <td>{r.times}次</td><td><Change n={r.change}/></td><td>{r.days}天</td></>:
      <><td>{badge(r,history)}{history?<small>{r.date}</small>:r.state!==2&&<small>{status(r)}</small>}</td><td><strong>{r.name}</strong><small>{r.code}</small></td>
        <td><Change n={r.change}/></td><td><em>{pct(r.target)}</em></td><td title={r.rule}><Change n={r.deviation}/><small>{r.days}日 · {r.rule?.match(/[+-]\d+%/)?.[0]??'同向异动'}</small></td>{history&&<td className="am-rule-cell">{r.rule}</td>}</>}</tr>)}</tbody></table>}</div>
    <footer className="am-pager"><span>共 {rows.length} 条</span>{pages>1&&<><button disabled={!page} onClick={()=>setIndex(page-1)}>上一页</button>
    <span>{page+1} / {pages}</span><button disabled={page+1>=pages} onClick={()=>setIndex(page+1)}>下一页</button></>}</footer></>;
}

export function AnomalyMonitor({search}:{search:string}) {
  const root=useRef<HTMLElement>(null);
  const [cards,setCards]=useState(false),[filter,setFilter]=useState('全部');
  const [kind,setKind]=useState('全部'),[draft,setDraft]=useState(''),[query,setQuery]=useState(''),[historyDay,setHistoryDay]=useState('');
  const [smallHistory,setSmallHistory]=useState(false);
  const params=new URLSearchParams(search),day=params.get('trade_day'),asOf=params.get('as_of_time');
  function latest(){const url=new URL(location.href);url.searchParams.delete('trade_day');url.searchParams.delete('as_of_time');updateLocation(url);}
  const q=new URLSearchParams();if(day)q.set('trade_day',day);if(asOf)q.set('as_of_time',asOf);
  const path='/api/v1/review-data/anomaly?'+q;
  const read=useCachedLoader(()=>request<{data:Snapshot|null}>(path),path,'异动监控');
  const data=read.data?.data,busy=read.busy,error=read.error?.message;
  useLayoutEffect(()=>{
    const el=root.current!; const fit=()=>el.style.height=Math.max(235,window.innerHeight-el.getBoundingClientRect().top-14)+'px';
    fit();const observer=new ResizeObserver(fit);if(el.parentElement)observer.observe(el.parentElement);window.addEventListener('resize',fit);
    return ()=>{observer.disconnect();window.removeEventListener('resize',fit);};
  },[]);
  const matches=useMemo(()=>{
    const key=query.trim().toLowerCase().replace(/\s/g,''); const memo=new Map<string,boolean>();
    return (r:Row)=>{if(!key||r.code.includes(key)||r.name.includes(key))return true;
      if(!memo.has(r.name)){const syllables=pinyin(r.name,{toneType:'none',type:'array'});memo.set(r.name,[syllables.join(''),syllables.map(s=>s[0]).join('')].some(s=>s.toLowerCase().includes(key)));}
      return memo.get(r.name)!;};
  },[query]);
  const histories=(data?.history??[]).filter(matches),pool=(data?.pool??[]).filter(matches);
  const dates=[...new Set(histories.map(r=>r.date!))];
  const current=(data?.predictions??[]).filter(matches);
  return <section className="am-monitor" ref={root} aria-label="异动监控">
    <header className="am-head"><h2>异动计算器</h2>
    <form onSubmit={e=>{e.preventDefault();setQuery(draft);setHistoryDay('');}}><input type="search" aria-label="搜索代码、名称或拼音" placeholder="输入代码 / 名称 / 拼音" value={draft} onChange={e=>{setDraft(e.target.value);if(!e.target.value)setQuery('');}}/><button>查询</button></form>
    <span>最终异动认定以官方披露为准</span></header>
    {!data?<div className="am-empty" role="status">{busy?'正在读取盘后归档…':error||'当前时点暂无盘后归档'} {!busy&&<button onClick={()=>day||asOf?latest():read.reload()}>{day||asOf?'最近归档':'重新读取'}</button>}</div>:
    <div className={'am-board'+(smallHistory?' am-mobile-history':'')}>
      <section className="am-pane am-pred" aria-label="严重异动预测">
        <header><h2>严重异动预测</h2><time>{data.day}</time><div className="am-view">
          {[false,true].map(v=><button key={String(v)} aria-pressed={cards===v} onClick={()=>setCards(v)}>{v?'卡片':'表格'}视图</button>)}</div>
          <button className="am-mobile-date" onClick={()=>setSmallHistory(true)}>历史</button></header>
        <Records key={query+cards} rows={current} group="predictions" cards={cards}/>
      </section>
      <section className="am-pane am-repeat" aria-label="多次异动"><header><h2>多次异动</h2><small>十日内至少两次</small></header>
        <Records key={query} group="repeated" cards={false} rows={data.repeated.filter(matches)}/>
      </section>
      <section className="am-pane am-pool" aria-label="监控池"><header><h2>监控池</h2></header>
        <div className="am-filters">{['全部','交易所风险提示','严重异常波动'].map(k=><button key={k} aria-pressed={k===kind} onClick={()=>setKind(k)}>{k} <b>{pool.filter(r=>k==='全部'||k===r.kind).length}</b></button>)}</div>
        <Records key={kind+query} group="pool" cards={false} rows={pool.filter(r=>kind==='全部'||r.kind===kind)}/>
      </section>
      <section className="am-pane am-history" aria-label="历史预测"><header><h2>历史预测</h2>
        <select aria-label="历史日期" value={historyDay} onChange={e=>setHistoryDay(e.target.value)}><option value="">近90天</option>{dates.map(d=><option key={d}>{d}</option>)}</select>
        <div className="am-filters">{['全部','已触发','未触发'].map(f=><button key={f} aria-pressed={filter===f} onClick={()=>setFilter(f)}>{f}</button>)}</div>
        <button className="am-mobile-date" onClick={()=>setSmallHistory(false)}>当期</button></header>
        <Records key={query+filter+historyDay+cards} rows={histories.filter(r=>(!historyDay||r.date===historyDay)&&(filter==='全部'||status(r)===filter))} group="history" cards={cards}/>
      </section>
    </div>}
    <footer className="am-source"><span>东方财富、腾讯 · 交易日 16:30 后采集</span><span>{data?'交易日 '+data.day+' · 采集 '+new Date(data.received_at).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'}):''}</span>{(day||asOf)&&<button onClick={latest}>最近归档</button>}</footer>
  </section>;
}
