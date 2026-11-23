import { useEffect, useRef, useState } from 'react';
import { ApiError, write } from '../../shared/api';
import { percent } from '../terminal/data';
import { amount as money, number } from '../daily-review/model';
import type { PageProps } from '../terminal/Components';
import type { Envelope, RunResult } from './types';
import '../../shared/historyPlot.css';

type AuctionMetric = 'auction_pct' | 'auction_amount_cny' | 'auction_turnover_rate' | 'auction_volume_ratio' | 'previous_height' | 'yesterday_up' | 'yesterday_broken' | 'yesterday_lhb';
type Result = RunResult & { coverage?: { source_rows: number; usable_rows: number; source_state?: string; received_at?: string | null; previous_pool_state: string; previous_lhb_state: string } };
const filters: [AuctionMetric, string][] = [['auction_pct','竞价涨幅 ≥（%）'],['auction_amount_cny','竞价成交额 ≥（元）'],['auction_turnover_rate','竞价换手率 ≥（%）'],['auction_volume_ratio','来源量比 ≥（倍）'],['previous_height','昨日板数 ≥']];

export function AuctionScreening({ data, session, asOfTime, selectStock, watch }: PageProps) {
  const [open, setOpen] = useState(false), [inputs, setInputs] = useState<Record<string,string>>({}), [group, setGroup] = useState('all');
  const [sort, setSort] = useState<AuctionMetric>('auction_amount_cny'), [scope, setScope] = useState<'market'|'watchlist'>('market');
  const [result, setResult] = useState<Result|null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const ticket = useRef(0), context = `${data.trade_day}:${asOfTime}:${session.data.user.id}:${session.data.csrf_token}`;
  const [resultContext, setResultContext] = useState('');
  const active = resultContext===context?result:null;
  useEffect(()=>{ticket.current++;setResult(null);setError('');setBusy(false);return()=>{ticket.current++;};},[context]);
  useEffect(()=>{
    const clear=()=>{ticket.current++;setResult(null);setBusy(false);};
    const events=['session-changed','session-expired','read-permission-denied'];
    events.forEach(event=>window.addEventListener(event,clear));
    return()=>events.forEach(event=>window.removeEventListener(event,clear));
  },[]);
  async function run(offset=0, resultState='matched', pinned=false) {
    const id=++ticket.current;setBusy(true);setError('');
    try {
      const conditions = filters.filter(([key])=>inputs[key]?.trim()).map(([field])=>({field,operator:'gte',value:inputs[field].trim()}));
      if(conditions.some(c=>!/^\d{1,18}(?:\.\d{1,12})?$/.test(c.value) && !(c.field==='auction_pct' && /^-?\d{1,18}(?:\.\d{1,12})?$/.test(c.value))))throw new Error('请输入有效数字，金额、量比、换手率和板数应大于或等于零。');
      if(group!=='all')conditions.push({field:group as AuctionMetric,operator:'eq',value:'1'});
      if(!conditions.length)conditions.push({field:'auction_amount_cny',operator:'gte',value:'0'});
      const cutoff=pinned?active?.as_of_time:asOfTime;
      const response=await write<Envelope<Result>>('/api/v1/screening/preview',{
        screen_context:'auction',day:data.trade_day,...(cutoff?{as_of_time:cutoff}:{}),scope,state:resultState,offset,limit:100,
        definition:{conditions,sort_field:sort,sort_direction:'desc'},
      },session.data.csrf_token,crypto.randomUUID());
      if(id===ticket.current){setResult(response.data);setResultContext(context);}
    }catch(e){if(id===ticket.current){setError(e instanceof Error?e.message:'竞价筛选失败。');if(e instanceof ApiError && [401,403].includes(e.status))setResult(null);}}
    finally{if(id===ticket.current)setBusy(false);}
  }
  return <details className="review-extension" onToggle={e=>setOpen(e.currentTarget.open)}><summary>竞价成交筛选 · 完整已保存样本</summary>{open&&<div className="review-extension-body"><form onSubmit={e=>{e.preventDefault();run();}}><div className="review-extension-tools">{filters.map(([key,label])=><label key={key}>{label}<input aria-label={label} inputMode="decimal" value={inputs[key]??''} onChange={e=>{setInputs({...inputs,[key]:e.target.value});ticket.current++;setResult(null);setBusy(false);}} style={{width:110}}/></label>)}</div><div className="review-extension-tools"><label>昨日分组 <select aria-label="竞价昨日分组" value={group} onChange={e=>{setGroup(e.target.value);ticket.current++;setResult(null);setBusy(false);}}><option value="all">全部</option><option value="yesterday_up">昨日涨停</option><option value="yesterday_broken">昨日炸板</option><option value="yesterday_lhb">昨日上榜</option></select></label><label>范围 <select aria-label="竞价筛选范围" value={scope} onChange={e=>{setScope(e.target.value as typeof scope);ticket.current++;setResult(null);setBusy(false);}}><option value="market">已保存竞价表</option><option value="watchlist">我的自选</option></select></label><label>降序排列 <select aria-label="竞价筛选排序" value={sort} onChange={e=>{setSort(e.target.value as AuctionMetric);ticket.current++;setResult(null);setBusy(false);}}>{filters.map(([key,label])=><option key={key} value={key}>{label.split(' ≥')[0]}</option>)}</select></label><button type="submit" disabled={busy||session.data.user.role==='viewer'}>{busy?'正在筛选…':'筛选已存竞价'}</button></div></form>
    {error&&<p role="alert">{error}</p>}{active&&<>{active.coverage?.source_state && active.coverage.source_state!=='complete' && <p role="status">{active.coverage.source_state==='permission_denied'?'竞价来源未启用或当前无读取权限。':'所选日竞价成交表尚未完整归档。'}</p>}<p className="history-plot-note">原竞价表 {active.coverage?.source_rows ?? active.counts.total} 条，当前范围 {active.counts.total} 只，命中 {active.counts.matched}，数据不足 {active.counts.insufficient}。资料日 {active.trade_day}。{active.coverage?.received_at && ` 实际接收 ${new Date(active.coverage.received_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}。`}</p><div className="review-extension-tools"><button disabled={busy} onClick={()=>run(0,'matched',true)}>查看命中</button><button disabled={busy} onClick={()=>run(0,'insufficient',true)}>查看数据不足</button><button disabled={busy||active.offset===0} onClick={()=>run(Math.max(0,active.offset-100),active.items[0]?.state ?? 'matched',true)}>上一页</button><button disabled={busy||!active.has_more} onClick={()=>run(active.offset+100,active.items[0]?.state ?? 'matched',true)}>下一页</button></div><div className="review-extension-scroll"><table aria-label="竞价成交筛选结果"><thead><tr><th>股票</th><th>竞价涨幅</th><th>竞价成交额</th><th>竞价换手率</th><th>来源量比</th><th>昨日板数</th><th>自选</th><th>缺项</th></tr></thead><tbody>{active.items.map(r=><tr key={r.code}><td><button onClick={()=>selectStock(r.code)}>{r.name} · {r.code}</button></td><td>{percent(number(r.metrics.auction_pct))}</td><td>{money(number(r.metrics.auction_amount_cny))}</td><td>{percent(number(r.metrics.auction_turnover_rate))}</td><td>{r.metrics.auction_volume_ratio ?? '—'}倍</td><td>{r.metrics.previous_height ?? '—'}</td><td><button disabled={!r.instrument_id||watch.busy||watch.saving||session.data.user.role==='viewer'} onClick={()=>watch.toggle(r)}>{watch.has(r)?'移出自选':'加入自选'}</button></td><td>{r.checks.filter(c=>c.state==='insufficient').map(c=>c.reason).join('；')||'—'}</td></tr>)}</tbody></table></div>{!active.items.length&&<p>当前条件下没有该类结果。</p>}</>}
    <p className="history-plot-note">竞价成交额与封单金额分开展示；量比沿用来源的“倍”口径。仅手动筛选本机缓存，来源未提供精确成交时刻，不套用盘中180秒规则。</p>
  </div>}</details>;
}
