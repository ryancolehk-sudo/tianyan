import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { api } from '../../shared/api';
import type { ReviewProps } from './context';
import { Failure, Loading, useBoundRead } from './components';
import { ReviewOverview } from './Overview';
import type { Coverage } from './model';
import { charts, Plot } from './postcloseVisuals';
import { PostcloseAnalytics } from './PostcloseAnalytics';
import { PostcloseRankings } from './PostcloseRankings';
import { HeightHistory } from './HeightHistory';
import './postclose.css';
export type { Postclose } from './postcloseVisuals';

export function PostcloseOverview(props: ReviewProps & { automaticDay: boolean; onResolvedDay: (day: string) => void; onCoverage: (entries: Coverage[]) => void; researchHeader?: ReactNode; researchFooter?: ReactNode }) {
  const [mode, setMode] = useState('charts'); const [expanded,setExpanded]=useState<number|null>(null); const [lookback, setLookback] = useState('20'); const [research, setResearch] = useState(false);
  const [hoverDay, setHoverDay] = useState<string | null>(null);
  useEffect(() => setHoverDay(null), [props.day, props.asOf, lookback, mode, expanded]);
  const params = new URLSearchParams({ ...(!props.automaticDay ? { trade_day: props.day } : {}), lookback, ...(props.asOf ? { as_of_time: props.asOf } : {}) });
  const read = useBoundRead(`postclose:${params}`, async () => {
    const response=await api.reviewOverview(params); const value=response?.data;
    if(!value||!Array.isArray(value.records)||!Array.isArray(value.trade_days)||!Array.isArray(value.metric_definitions)||!Array.isArray(value.coverage)||!value.rankings||typeof value.rankings!=='object'||value.records.some(point=>!point||typeof point.trade_day!=='string'||!point.values||!point.leaders)||(!props.automaticDay&&value.trade_day!==props.day))throw new Error('行情总览响应不完整，请重试。');
    return response;
  });
  useEffect(() => { if (props.asOf) return; const timer = window.setInterval(read.reload, 30000); return () => clearInterval(timer); }, [props.day, props.asOf, lookback, read.reload]);
  const data = read.data?.data;
  useEffect(() => { if (data?.trade_day && props.automaticDay) props.onResolvedDay(data.trade_day); }, [data?.trade_day, props.automaticDay, props.onResolvedDay]);
  useEffect(()=>{if(data?.coverage)props.onCoverage(data.coverage);},[data?.coverage,props.onCoverage]);
  const dayIndex = data?.trade_day ? data.trade_days.indexOf(data.trade_day) : -1;
  return <section className="pcv-workspace" aria-label="行情总览"><div className="pcv-toolbar"><nav aria-label="行情总览视图">{[['charts', '图表'], ['analytics', '统计分析'], ['height', '连板高度图']].map(([key, label]) => <button key={key} aria-pressed={mode === key} className={mode === key ? 'selected' : ''} onClick={() => { setMode(key); setExpanded(null); setResearch(false); }}>{label}</button>)}</nav><div><button aria-label="上一交易日" disabled={dayIndex<=0} onClick={()=>data&&props.select({trade_day:data.trade_days[dayIndex-1]})}>上一日</button><button aria-label="下一交易日" disabled={!data||dayIndex<0||dayIndex>=data.trade_days.length-1} onClick={()=>data&&props.select({trade_day:data.trade_days[dayIndex+1]})}>下一日</button>{mode!=='height'&&<label className="pcv-range"><select aria-label="统计区间" value={lookback} onChange={e => setLookback(e.target.value)}><option value="20">近20日</option><option value="60">近60日</option></select></label>}<span className="pcv-schedule" title="北京时间；未发布字段每15分钟重试，17:30核对晚到数据；服务启动后自动补采。">交易日16:30更新</span><button aria-expanded={research} onClick={() => setResearch(!research)}>{research ? '收起记录' : '研究记录'}</button></div></div>
    <Failure error={data ? read.error : null} retry={read.reload}/>{read.error&&!data&&<div className="dr-error" role="alert">{read.error.message}<button onClick={read.reload}>重试</button></div>}{!data ? read.error ? null : <Loading text="正在读取盘后统计与榜单…"/> : research ? <div className="pcv-research">{props.researchHeader}<ReviewOverview {...props}/>{props.researchFooter}</div> : mode==='height'?<HeightHistory day={props.day} automaticDay={props.automaticDay} asOf={props.asOf} selectDay={day=>props.select({trade_day:day})}/>: <div className="pcv-split"><div className="pcv-chart-pane" aria-label="图表区域">{mode==='analytics'?<PostcloseAnalytics data={data}/>:expanded!==null?<div className="pcv-expanded"><div className="pcv-focus-controls"><button onClick={()=>setExpanded(null)}>全部图表</button><span>{data.records[0]?.trade_day} — {data.trade_day}</span></div><Plot spec={charts[expanded]} points={data.records} detailed/></div>:<div className="pcv-charts">{charts.map((spec,index)=><Plot key={spec.title} spec={spec} points={data.records} linkedDay={hoverDay} onDayChange={setHoverDay} expand={()=>setExpanded(index)}/>)}</div>}</div><PostcloseRankings data={data} select={props.select}/></div>}
  </section>;
}
