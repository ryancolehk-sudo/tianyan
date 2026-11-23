import { api } from '../../shared/api';
import { usePaneScroll, useWorkspaceState } from '../../shared/workspaceState';
import { Card, Empty, Failure, Loading, Table, useBoundRead } from './components';
import type { ReviewProps } from './context';

function sealTime(value:unknown){if(value==null||value==='')return '未知';const s=String(value).padStart(6,'0');return /^\d{6}$/.test(s)?s.slice(0,2)+':'+s.slice(2,4)+':'+s.slice(4):String(value);}
export function ReviewComparison(p:ReviewProps) {
  const query=new URLSearchParams({trade_day:p.day,kind:'comparison',...(p.asOf?{as_of_time:p.asOf}:{})});
  const read=useBoundRead('comparison:'+query,()=>api.reviewAnalysis(query));
  const value=read.data?.data.comparison;
  const key=`${p.session.data.user.id}:review-comparison:${p.day}:${p.asOf}`;
  const root=usePaneScroll(key+':scroll');
  const [filter,setFilter]=useWorkspaceState(key+':filter','all');
  return <div ref={root} className="dr-comparison"><Card title="今日与上一交易日 · 梯队变化"><Failure error={read.error} retry={read.reload}/>{read.busy?<Loading/>:value?<><p className="dr-note">{value.previous_trade_day||'前日待核'} → {value.trade_day||p.day} · {value.comparable?'同源同范围可比':'资料范围不足，保留未知'} · 晋级 {value.promoted_n} · 明确掉队 {value.failed_n} · 未观测／待核 {value.unknown_n}</p><select aria-label="梯队变化范围" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部原日候选</option><option value="promoted">晋级</option><option value="failed">明确掉队</option><option value="unknown">未观测／资料不足</option></select><div className="dr-comparison-scroll" data-scroll-key="transitions"><Table label="相邻交易日梯队变化"><thead><tr><th>个股</th><th>原日→今日板数</th><th>核对结果</th><th>依据</th></tr></thead><tbody>{value.records.filter(row=>filter==='all'||row.state===filter).map(row=><tr key={row.code}><td><button onClick={()=>p.select({stock:row.code,view:'direction'})}>{row.name}</button></td><td>{row.previous_height??'—'} → {row.current_height??'—'}</td><td title={row.reason}>{{promoted:'晋级',failed:'明确掉队',unknown:'未观测／资料不足'}[row.state]}</td><td>{row.previous_evidence&&<button onClick={()=>p.openRef(row.previous_evidence!)}>原日</button>}{row.current_evidence&&<button onClick={()=>p.openRef(row.current_evidence!)}>今日</button>}</td></tr>)}</tbody></Table></div>{!value.records.length&&<Empty>原日候选名单不足，不能生成晋级率或掉队名单。</Empty>}<p className="dr-note">{read.data?.data.scope_note}</p></>:<Empty>比较资料待核。</Empty>}</Card>
    <Card title="炸板与回封 · 已存记录"><div className="dr-comparison-scroll" data-scroll-key="seal-events"><Table label="炸板与回封原始记录"><thead><tr><th>个股</th><th>来源记录</th><th>首次／末次封板</th><th>证据</th></tr></thead><tbody>{value?.events.map(event=><tr key={event.code}><td>{event.name}</td><td>{event.state==='Z'?'炸板日榜':'封板日榜'} · 打开 {event.open_times??'未知'} 次</td><td>{sealTime(event.first_seal)} ／ {sealTime(event.last_seal)}</td><td>{event.evidence&&<button onClick={()=>p.openRef(event.evidence!)}>原始记录</button>}</td></tr>)}</tbody></Table></div>{!value?.events.length&&<Empty>没有可核的炸板／打开记录，不能从收盘价格反推过程。</Empty>}<p className="dr-note">首末时刻与打开次数分别展示。未存逐次炸板回封事件，不补造时间线。</p></Card>
  </div>;
}
