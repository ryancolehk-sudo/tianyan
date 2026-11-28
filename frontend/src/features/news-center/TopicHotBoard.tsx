import { useState } from 'react';
import type { HotEvent } from '../../generated/schema';
import { chinaTime } from './model';
import './topicHotBoard.css';

const outletShort: Record<string, string> = {
  '财联社':'财', '同花顺':'同', '东方财富':'东', '中国新闻网':'中', '巨潮资讯':'巨', '香港电台':'港', '香港政府新聞網':'政',
};

function HeatChart({ item, at, large }: { item: HotEvent; at: string; large: boolean }) {
  const [hover, setHover] = useState<number | null>(null);
  const values = item.history;
  const width = large ? 320 : 90, height = large ? 102 : 34, pad = 4;
  const observed = values.flatMap((value,index) => value === null ? [] : [{value,index}]);
  const max = Math.max(1, ...observed.map(p => p.value)) * 1.06;
  const x = (index: number) => pad + index / Math.max(1,values.length-1) * (width - 2*pad);
  const y = (value: number) => height - pad - value / max * (height - 2*pad);
  const segments: {value:number; index:number}[][] = [];
  for (const point of observed) {
    if (!segments.length || segments.at(-1)!.at(-1)!.index !== point.index-1) segments.push([]);
    segments.at(-1)!.push(point);
  }
  const stamp = (index: number) => chinaTime(new Date(Date.parse(at) - (values.length-1-index)*3_600_000).toISOString());
  const focus = hover !== null && hover < values.length ? hover : null;
  const last = observed.at(-1);
  const desc = '近 24 小时报道热度，' + observed.length + ' 个有记录的时点';
  return <div className={'nc-heat-chart' + (large ? ' nc-heat-large' : '')}>
    {large && <div className="nc-heat-chart-label"><span>24 小时报道热度</span>
      <strong>{focus === null ? '逐小时' : stamp(focus) + ' · ' + (values[focus]?.toFixed(1) ?? '暂无观测')}</strong></div>}
    {last ? <svg viewBox={'0 0 ' + width + ' ' + height} preserveAspectRatio="none" role="img"
      aria-label={desc} tabIndex={large ? 0 : undefined}
      onPointerMove={large ? e => {const box=e.currentTarget.getBoundingClientRect(); setHover(Math.max(0, Math.min(values.length-1,
        Math.round((e.clientX-box.left)/box.width*(values.length-1)))));} : undefined}
      onPointerLeave={large ? () => setHover(null) : undefined} onBlur={large ? () => setHover(null) : undefined}
      onKeyDown={large ? e => {if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {e.preventDefault();
        setHover(Math.max(0, Math.min(values.length-1, (focus ?? values.length-1) + (e.key === 'ArrowLeft' ? -1 : 1))));}} : undefined}>
      <title>{desc}</title>
      <defs><linearGradient id={'heat-fill-'+item.id} x1="0" y1="0" x2="0" y2="1" gradientUnits="objectBoundingBox">
        <stop offset="0%" stopColor="currentColor" stopOpacity=".19"/><stop offset="100%" stopColor="currentColor" stopOpacity=".01"/>
      </linearGradient></defs>
      {large && [0,.5,1].map(n => <line className="nc-heat-gridline" key={n} x1={pad} x2={width-pad} y1={y(n*max)} y2={y(n*max)}/>)}
      {segments.map(run => {
        const start = run[0], end = run[run.length-1];
        const pts = run.map(p => x(p.index)+','+y(p.value)).join(' ');
        return <g key={start.index}>{run.length > 1 ? <>
          <polygon points={x(start.index)+','+(height-pad)+' '+pts+' '+x(end.index)+','+(height-pad)} fill={'url(#heat-fill-'+item.id+')'}/>
          <polyline points={pts} fill="none" stroke="currentColor" strokeWidth={2} vectorEffect="non-scaling-stroke" strokeLinejoin="round"/>
        </> : <circle cx={x(start.index)} cy={y(start.value)} r={2.2} fill="currentColor"/>}</g>;
      })}
      <circle cx={x(last.index)} cy={y(last.value)} r={3.2} fill="currentColor" stroke="var(--nc-hot-paper)" strokeWidth={1.4}/>
      {focus !== null && <line x1={x(focus)} x2={x(focus)} y1={pad} y2={height-pad} stroke="currentColor" strokeDasharray="2 3"/>}
    </svg> : <span className="nc-heat-no-points">暂无逐时记录</span>}
    {large && last && <div className="nc-heat-axis"><span>24 小时前</span><span>{stamp(values.length-1)}</span></div>}
  </div>;
}

function trendLabel(item: HotEvent) {
  if (item.trend === 'up' || item.trend === 'down') return (item.trend === 'up' ? '↑ ' : '↓ ') + Math.abs(item.change_pct ?? 0).toFixed(0) + '%';
  return {flat:'持平',new:'新增关注',unknown:'暂不比较'}[item.trend];
}

function RankedEvent({ item, index, at, onSelect }: { item: HotEvent; index: number; at: string; onSelect: (id: string) => void }) {
  const lead = index === 0, compact = index >= 3;
  const reports = item.reports ?? [];
  const latest = reports[0] ?? item;
  const rank = String(index+1).padStart(2,'0');
  return <li className={'nc-hot-event ' + (lead ? 'nc-hot-lead' : compact ? 'nc-hot-row' : 'nc-hot-side')}>
    <span className="nc-hot-rank" aria-label={'排名 '+rank}>{compact ? rank : 'NO.'+rank}</span>
    <span className="nc-hot-delta" data-trend={item.trend} title="与 6 小时前相比">{trendLabel(item)}</span>
    <div className="nc-hot-text">
      <h3><button type="button" onClick={() => onSelect(item.id)}>{item.title}</button></h3>
      {item.excerpt && <p>{item.excerpt}</p>}
      {item.importance === 'important' && <span className="nc-hot-important" title={item.reason ?? undefined}>重要{item.event_type ? ' · ' + heatCategories[item.event_type] : ''}</span>}
    </div>
    <HeatChart item={item} at={at} large={lead}/>
    <div className="nc-hot-index" aria-label={'报道热度 '+item.heat}><strong>{item.heat.toFixed(1)}</strong>{lead && <small>报道热度</small>}</div>
    <div className="nc-hot-origins">
      <span className="nc-hot-outlets" aria-hidden="true">{item.sources.slice(0,4).map(name =>
        <span key={name} style={{'--outlet-tone':outletTone[name] ?? '#586278'} as React.CSSProperties} title={name}>{outletShort[name] ?? name.charAt(0)}</span>)}</span>
      <details><summary title={item.sources.join('、')}>{item.sources.length} 家来源 · {reports.length} 篇报道</summary>
        <div>{reports.map(r => <button key={r.id} type="button" onClick={() => onSelect(r.id)}>
          <time>{chinaTime(r.published_at,true)}</time> {r.source_name} · {r.title}</button>)}</div></details>
    </div>
    <span className="nc-hot-latest" title={chinaTime(latest.published_at,true)}>最新来源 {latest.source_name} · {chinaTime(latest.published_at)}</span>
  </li>;
}

const outletTone: Record<string,string> = {'财联社':'#9f4036','东方财富':'#91502a','同花顺':'#9c3548','中国新闻网':'#346176','香港电台':'#655674','巨潮资讯':'#436865'};
const heatCategories = {order:'订单与中标', supply:'供应变化', catalyst:'产业催化'};

export function TopicHotBoard({ events, rankedAt, onSelect }: { events: HotEvent[]; rankedAt:string; onSelect:(id:string)=>void }) {
  return <section className="nc-hot-board" aria-label="财经热点榜">
    <header><div><h2>财经热点榜</h2><p>过去 48 小时 · 按报道热度排序</p></div>
      <div className="nc-hot-asof"><time dateTime={rankedAt}>{chinaTime(rankedAt,true)} 更新</time>
        <details><summary>热度怎么算</summary><p>同一事件至少有两家来源报道才上榜。每家来源只计最近一篇，刚发布时计 10 分，24 小时后权重减半。折线按小时回看本机当时已收到的报道，采集记录不足处留空。涨跌与 6 小时前比较，变化在 10% 以内显示持平。</p></details></div></header>
    {events.length ? <ol className="nc-hot-list" data-count={Math.min(3,events.length)}>
      {events.map((item,index) => <RankedEvent key={item.id} item={item} index={index} at={rankedAt} onSelect={onSelect}/>)}</ol>
      : <p className="nc-hot-empty">近 48 小时，当前筛选下还没有两家来源共同报道的事件。</p>}
  </section>;
}
