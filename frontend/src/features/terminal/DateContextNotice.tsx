import {useNavigation} from '../../shared/navigation';
import {useMarketSession} from '../../shared/marketSession';
import {dateContext} from './dateContext';
import type {WorkbenchData} from './types';
import {monitorHref} from '../../shared/dataMonitorLink';
export function DateContextNotice({data,requestedDay,asOfTime,reload}:{data:WorkbenchData|null;requestedDay:string;asOfTime:string;reload:()=>void}) {
  const nav=useNavigation();const {market}=useMarketSession(data?.market_session);
  const state=dateContext(requestedDay,data?.trade_day||'',market?.trade_day||'',asOfTime);
  if (state.state==='current'||state.state==='loading')return null;
  const latest=new URLSearchParams(nav.search);latest.delete('trade_day');latest.delete('as_of_time');
  return <aside className="ty-date-context" role="status"><strong>{state.message}</strong><button onClick={reload}>重读本页资料</button>{(requestedDay||asOfTime)&&<a href={nav.path+'?'+latest}>返回当前交易日</a>}<a href={monitorHref({context:'current',returnTo:nav.path+nav.search+nav.hash})}>查看当前运行 · 全部来源</a><details><summary>日期核对信息</summary><p>网址所选日：{requestedDay||'自动'} · 接收截止：{asOfTime||'未设置'} · 实际返回日：{data?.trade_day||'待核'} · 更新时间：{data?.latest_at||'未提供'}</p></details></aside>;
}
