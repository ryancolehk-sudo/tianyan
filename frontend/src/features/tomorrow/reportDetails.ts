import { decimal, research } from './reportResearch';
import type { Row } from './reportResearch';

type Study = ReturnType<typeof research>;
type Stock = Study['stocks'][number];
type Group = Study['groups'][number];

export function boardTime(value: unknown) {
  const raw=String(value ?? '').trim();
  const digits=/^\d{5,6}$/.test(raw) ? raw.padStart(6,'0') : /^\d{2}:\d{2}(:\d{2})?$/.test(raw) ? raw.replace(/:/g,'').padEnd(6,'0') : '';
  return /^(?:[01]\d|2[0-3])[0-5]\d[0-5]\d$/.test(digits) ? digits.slice(0,2)+':'+digits.slice(2,4) : '无有效时间';
}
export function money(s: Stock) { return decimal(s.flow===null ? null : s.flow/1e4, '亿'); }
export function amount(s: Stock) { return decimal(s.amount===null ? null : s.amount/1e5, '亿'); }
function volume(s:Stock) {
  if (s.ratio===null) return '五日基准缺失';
  const status=s.ratio>=1.5?'放量':s.ratio<0.8?'缩量':'接近常量';
  return status+' · '+decimal(s.ratio,'倍');
}
function funds(s:Stock) {
  if(s.flow===null) return '缺当日资金净额';
  if(s.flow===0) return '净额持平';
  return (s.flow>0?'净流入 ':'净流出 ')+decimal(Math.abs(s.flow)/1e4,'亿');
}
function seal(s:Stock) {
  if(s.open===null) return '未披露开板次数';
  return (s.open===0?'未开板':'开板 '+s.open+' 次')+' · 首封 '+boardTime(s.firstTime)+' / 末封 '+boardTime(s.lastTime);
}
function phase(s:Stock) {
  if(s.close===null||s.lo===null||s.hi===null||s.ratio===null) return '量价窗口不足';
  if(s.close<s.lo) return '跌破前低';
  if(s.close>s.hi) return s.ratio>=1.5?'放量突破前高':s.ratio<0.8?'缩量越过前高':'常量越过前高';
  if(s.five!==null&&s.five<0&&s.pct!==null&&s.pct>0) return '区间内反弹';
  if(s.kind==='Z') return '上冲回落';
  return s.five===null?'区间内观察':s.five>0?'区间内上行':s.five<0?'区间内回落':'区间震荡';
}
function plan(s:Stock) {
  if(s.kind==='D') return '先看跌停打开后的成交，持续封住则暂停修复预案。';
  if(s.kind==='U'&&s.open===0) return '首度开板时核对'+(s.avg===null?'成交承接':' '+decimal(s.avg,'元')+' 附近承接')+'及同业溢价。';
  if(s.kind==='U') return '先看分歧后能否回封，再核对同业是否跟随。';
  if(s.kind==='Z') return '再次触及涨停并封住后，再复核突破情景。';
  return s.avg===null?'先补齐成交均价，核对同业强弱。':'回踩 '+decimal(s.avg,'元')+' 后观察量能与同业表现。';
}
function alert(s:Stock) {
  if(s.kind==='D') return '跌停卖盘延续，流动性风险仍在。';
  if(s.avg!==null && (s.kind==='U'||s.kind==='Z')) return '失守 '+decimal(s.avg,'元')+' 且反抽不能收复，撤下短线强势假设。';
  return s.lo===null?'支撑资料不足': '失守前20日低点 '+decimal(s.lo,'元')+'，复核区间假设。';
}
function groupFlow(g:Group) {
  const covered=g.members.filter(s=>s.kind==='U'&&s.flow!==null).sort((a,b)=>Math.abs(b.flow!)-Math.abs(a.flow!));
  return covered.length ? covered.slice(0,2).map(s=>s.name+' '+funds(s)).join('；')+'（封板样本 '+covered.length+'/'+g.up+'）':'封板样本缺当日资金净额';
}
function tier(s:Stock) {return s.height===null?'未披露板数':s.height===1?'首板':s.height+'连板';}
export function stockDetail(s:Stock) {return {phase:phase(s),volume:volume(s),funds:funds(s),plan:plan(s),seal:seal(s)};}
export function newsPoints(source:string,title:string) {
  const sentences=source.split(/[。！？\n]|(?=[①②③④⑤⑥⑦])/u).map(s=>s.trim()).filter(s=>s.length>7&&s!==title);
  return Array.from(new Set(sentences)).slice(0,3);
}

export function detailTables(study:Study,pool:Row,breadth:Row) {
  const sealed=study.stocks.filter(s=>s.kind==='U');
  const volumeRows=study.stocks.map(s=>({股票:s.name+'（'+s.code+'）',价格:decimal(s.close,'元'),主力资金:money(s),量价阶段:phase(s),关键支撑:decimal(s.lo,'元'),关键压力:decimal(s.hi,'元'),量能健康度:volume(s),成交均价:decimal(s.avg,'元'),当日换手:decimal(s.turn,'%'),资金判断:funds(s)+(s.flow!==null&&s.flow<0&&s.pct!==null&&s.pct>0?' · 价升资出':'')}));
  const forecasts=study.stocks.map(s=>({股票:s.name,趋势预测:phase(s)+'。'+s.trend,操作建议:plan(s),预警信号:alert(s)}));
  const counts=study.groups.map(g=>({板块:g.name,涨停数:g.up,占比:decimal(g.up*100/sealed.length,'%'),代表标的:g.members.filter(s=>s.kind==='U').map(s=>s.name+'（'+tier(s)+'）').join('、'),资金方向:groupFlow(g)}));
  const unmatched=sealed.filter(s=>!s.industry);
  if(unmatched.length) counts.push({板块:'未匹配二级行业',涨停数:unmatched.length,占比:decimal(unmatched.length*100/sealed.length,'%'),代表标的:unmatched.map(s=>s.name).join('、'),资金方向:'未归入行业'});
  const byBoard=[...sealed].sort((a,b)=>(b.height??0)-(a.height??0)||(b.amount??0)-(a.amount??0));
  const ladder=byBoard.map(s=>({连板高度:tier(s),标的:s.name,代码:s.code,板块:s.industry??(s.rawIndustry||'未披露行业'),涨幅:decimal(s.pct,'%'),成交额:amount(s),封板质量:seal(s),性质判断:s.open===null?'未披露开板次数':s.open>0?'回封有分歧；'+funds(s):'封住后未再打开；'+funds(s)}));
  const stockRow=(s:Stock)=>({标的:s.name,代码:s.code,涨幅:decimal(s.pct,'%'),成交额:amount(s),核心逻辑:tier(s)+'，'+funds(s)+'；'+phase(s),质量评级:(s.open===null?'无开板记录':s.open===0?'封板稳定':s.open===1?'一次分歧':'反复分歧')+' · '+seal(s)});
  const leaders=study.groups.flatMap(g=>{
    const candidates=g.members.filter(s=>s.kind==='U'&&s.open!==null&&s.amount!==null);
    // One candidate per industry; fewer reopens, then more turnover, then higher board.
    candidates.sort((a,b)=>a.open!-b.open!||b.amount!-a.amount!||(b.height??0)-(a.height??0));
    return candidates.length?[{stock:candidates[0],group:g}]:[];
  }).slice(0,3);
  const themeOf=(s:Stock)=>s.industry??study.sectors.find(t=>t.members.some(m=>m.code===s.code))?.name??(s.rawIndustry||'未披露行业');
  const strong=study.stocks.filter(s=>s.verifiedUnsealed&&s.kind!=='U'&&s.pct!==null&&s.pct>0).sort((a,b)=>b.pct!-a.pct!).map(s=>({标的:s.name,代码:s.code,板块:themeOf(s),涨幅:decimal(s.pct,'%'),成交额:amount(s),状态:phase(s)+'，'+funds(s),后续策略:plan(s)}));
  const following=study.groups.slice(0,5).map(g=>({板块:g.name,观察逻辑:g.reason+' '+g.continuity,观察标的:g.members.filter(s=>s.kind==='U').slice(0,2).map(s=>s.name+'（'+tier(s)+'）').join('、'),量价分析:g.members.filter(s=>s.kind==='U').slice(0,2).map(s=>s.name+' '+volume(s)+'，'+funds(s)).join('；')}));
  const trends=study.sectors.filter(s=>s.week!==null).map(s=>({板块:s.name,近5日趋势:decimal(s.week,'%'),当前阶段:s.change>0?(s.week!>0?'五日上行、当日延续':'五日回落、当日反弹'):(s.week!>0?'五日上行、当日回吐':'五日与当日均偏弱'),风险提示:'当日 '+decimal(s.change,'%')+'，'+s.breadthText+'。'+s.row.后续逻辑}));
  const chase=byBoard.filter(s=>(s.height??0)>=3||(s.open??0)>=2||(s.flow!==null&&s.flow<0)).map(s=>({预警标的:s.name+'（'+tier(s)+'）',当前状态:seal(s),预警原因:[(s.height??0)>=3?'连板高度达到 '+s.height+' 板':'',(s.open??0)>=2?'反复开板 '+s.open+' 次':'',s.flow!==null&&s.flow<0?funds(s):''].filter(Boolean).join('；'),建议:plan(s)}));
  const levels=study.stocks.filter(s=>s.lo!==null&&s.close!==null&&s.close<=s.lo*1.03).sort((a,b)=>a.close!/a.lo!-b.close!/b.lo!).map(s=>({关键位:s.name+' · '+decimal(s.lo,'元'),当前状态:'收 '+decimal(s.close,'元')+'，较前低 '+decimal((s.close!/s.lo!-1)*100,'%'),破位影响:s.close!<s.lo!?'支撑已失守，区间假设失效':'距前低不足或等于 3%，留意卖压',应对措施:s.close!<s.lo!?'反抽重新站回该位前，维持破位观察。':'关注收盘能否守住前低，跌破后核对板块是否同步走弱。'}));
  const number=(v:unknown)=>v===null||v===undefined||String(v).trim()===''?NaN:Number(v);
  const up=number(pool.up),down=number(pool.down),broken=number(pool.broken_rate_pct),share=number(breadth.up_share_pct);
  const riskScore=Number.isFinite(share)&&Number.isFinite(broken)&&Number.isFinite(up+down)&&up+down>0?Math.round(10*((1-share/100)+down/(up+down)+broken/100)/3):null;
  return {volumeRows,forecasts,counts,ladder,leaders,strong,following,trends,chase,levels,stockRow,groupFlow,riskScore,
    riskLevel:riskScore===null?'资料不足':riskScore>=7?'较高风险':riskScore>=4?'中等风险':'较低风险'};
}
