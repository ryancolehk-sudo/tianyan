import type { LadderObservation, LadderRecord } from '../../src/features/terminal/ladderModel';
import { terminalDay, terminalDays, stockA, stockB } from './terminalTestFixtures';

// Synthetic market records for browser tests. Never source observations.
export function ladderFixture(empty=false):LadderObservation {
  const row=(code:string,name:string,height:number,day:string,index:number):LadderRecord=>({code,name,height,day,previous_height:height>1?height-1:0,status:'sealed',themes:[index%2?'合成题材乙':'合成题材甲',...(index>1?['合成关联题材'+index]:[])],theme_source:'合成测试原因',limit_type:index===0?'一字板':null,pct_change:index===0?0:index===1?-1:10+index,price:10+index,auction_price:9+index,first_time:`09:${String(25+index).padStart(2,'0')}:01`,last_time:`14:${String(20+index).padStart(2,'0')}:00`,seal_amount:index===0?0:10e6*index,amount:index===0?0:10e7*index,actual_float:10e8,actual_turnover:index===0?0:10,source_time:null,received_at:day+'T16:01:00+08:00',price_basis:'daily_close',stale:false});
  const days=terminalDays.map((day,i)=>({day,previous_day:terminalDays[i+1]??null,state:'complete',rows:empty?[]:[row(stockA,'合成甲',5-i,day,0),row(stockB,'合成乙',4,day,1),row('999997.SZ','合成三板',3,day,4),row('999998.SZ','合成二板',2,day,5),...Array.from({length:12},(_,n)=>row(`${999900+n}.SZ`,'合成股'+n,1,day,n+2))],eliminated:empty?[]:[{...row('999993.SZ','合成淘汰',3,day,2),status:'eliminated',first_time:null,last_time:null,pct_change:-2},{...row('999994.SZ','合成炸板',2,day,3),status:'opened',pct_change:3}],coverage:[]}));
  const candles=Object.fromEntries(days[0].rows.map(r=>[r.code,[...terminalDays].reverse().map((day,i)=>({day,open:10+i,high:12+i,low:9+i,close:11+i}))]));
  const latest_quotes=Object.fromEntries(days.flatMap(d=>d.rows).map(r=>[r.code,{...r,day:terminalDay,pct_change:r.code===stockA?7:1}]));
  return {trade_day:terminalDay,quote_day:terminalDay,available_days:terminalDays,days,candles,latest_quotes,state:empty?'missing':'complete',as_of_time:null,built_at:terminalDay+'T18:00:00+08:00',can_export:false,scope:'合成浏览器测试数据，非真实市场'};
}
