// Canonical values stay in memory only. This version supports the overview's
// default first page; other live layouts need their own exact response binding.
export const proofCheckerVersion='overview-default-first-page-v2-20261114';
// Historical workbench_snapshot.frozen.cjs, not this implementation's digest.
export const historicalProofBaselineSha256='5ac0b47b07e4d9d10473ada35dece251df5eb05f2aeaadec4292f97e44d8323b';
// The recorder invokes only supported bindings; legacy pure comparators below
// remain available to isolated checks. Raw responses/DOM snapshots stay private.
import type { IntradayFundSector, Stock, TerminalView, WorkbenchData } from '../features/terminal/types';
const canonical=(value: unknown)=>JSON.stringify(value);
const clean=(value: unknown)=>String(value??'').replace(/\s+/g,' ').trim();
const number=(value: number | null | undefined,digits=0)=>value==null||!Number.isFinite(value)?'—':new Intl.NumberFormat('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits}).format(value);
const percent=(value: number | null | undefined)=>value==null||!Number.isFinite(value)?'—':`${value>0?'+':''}${value.toFixed(2)}%`;
const money=(value: number | null | undefined,digits=2)=>value==null||!Number.isFinite(value)?'—':number(value/1e8,digits);
const signedMoney=(value: number | null | undefined,digits=1)=>value==null||!Number.isFinite(value)?'—':`${value>0?'+':''}${money(value,digits)}`;
const dateFormat=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
function dateTime(value: string | null | undefined){if(!value)return '—';return /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)&&Number.isFinite(Date.parse(value))?dateFormat.format(new Date(value)):value.replace('T',' ').slice(0,19);}
function quoteTiming(stock: Pick<Stock, 'price_basis' | 'source_snapshot_time' | 'source_received_at' | 'source_time'>){
 return stock.price_basis==='snapshot'?`服务器快照 ${dateTime(stock.source_snapshot_time)} · 接收 ${dateTime(stock.source_received_at)} · 逐股成交时刻待核`:
  stock.price_basis==='daily_close'?`所选交易日收盘日线 · 归档接收 ${dateTime(stock.source_received_at)}`:
  stock.source_time?`行情时点 ${dateTime(stock.source_time)}（北京时间） · 接收 ${dateTime(stock.source_received_at)}`:'行情时点待核对';
}
function stockTitle(stock: Stock){
 return clean(`${stock.name} ${stock.code}${stock.source_name?'\n实际供数：'+stock.source_name:''}\n${quoteTiming(stock)}`);
}
function readPrimarySnapshot(view: TerminalView,activeRoot?: Element){
 const root=activeRoot??document.querySelector('.ty-terminal-'+view);
 if(!root)return {found:false as const,view};
 const text=(node: Element | null | undefined)=>(node?.textContent??'').replace(/\s+/g,' ').trim();
 const stock=(node: Element)=>{const button=node.querySelector('.ty-stock-name');const description=(button?.getAttribute('aria-describedby')??'').split(/\s+/).filter(Boolean).map(id=>text(document.getElementById(id))).join(' ');const title=button?.getAttribute('title')??description;const code=/\b\d{6}\.(?:SH|SZ|BJ)\b/.exec(title)?.[0]??null;return {code,title:title.replace(/\s+/g,' ').trim(),label:text(button?.querySelector('.ty-stock-label')),codeLabel:text(button?.querySelector('small')),cells:[...node.querySelectorAll(':scope > td')].map(text)};};
 const overviewPanel=root.querySelector('.ty-rank-panel'),overviewTable=overviewPanel?.querySelector('.ty-rank-table');
 const overviewHeaders=[...(overviewTable?.querySelectorAll('thead th')??[])];
 return {found:true as const,view,visible:root.getClientRects().length>0&&!root.closest("[hidden]"),loading:!!root.querySelector('.ty-loading-strip'),
  overview:[...root.querySelectorAll('.ty-rank-panel .ty-rank-table')].map(table=>[...table.querySelectorAll('tbody tr')].map(stock)),
  overviewControls:{workspace:text(root.querySelector('[aria-label="综合盯盘工作区"] button[aria-pressed="true"]')),
   visible:!!overviewTable?.getClientRects().length&&!overviewTable?.closest('[hidden]'),
   search:overviewPanel?.querySelector<HTMLInputElement>('[aria-label="个股涨幅榜搜索"]')?.value??null,
   scope:overviewPanel?.querySelector<HTMLSelectElement>('[aria-label="个股涨幅榜范围"]')?.value??null,
   pagination:overviewPanel?.querySelector('.ty-rank-count')?.getAttribute('aria-label')??'',
   headers:overviewHeaders.map(node=>node.getAttribute('aria-label')??text(node)),
   sorts:overviewHeaders.flatMap((node,index)=>{const direction=node.getAttribute('aria-sort');return direction&&direction!=='none'?[{index,direction}]:[];})},
  ladder:[...root.querySelectorAll('.ty-ladder-table tbody tr')].map(stock),
  auction:[...root.querySelectorAll('.ty-auction-stock-list')].map(list=>({label:list.getAttribute('aria-label'),
   rolling:!!list.closest('.has-current-seal'),source:text(list.closest('.ty-auction-day')?.querySelector('[aria-label$="封单来源"]')),
   rows:[...list.querySelectorAll('li:not(.ty-auction-blank)')].map(node=>{const record=stock(node);const values=node.querySelector('.ty-auction-values');return {...record,amounts:[...(values?.children??[])].slice(0,-1).map(text),pct:text(values?.lastElementChild)};})})),
  funds:{intraday:(()=>{const label=text(root.querySelector('[aria-label="资金观察模式"] button[aria-pressed="true"]'));return label.includes('盘中')||label.includes('已存采样');})(),
   category:text(root.querySelector('[aria-label="资金板块分类"] button[aria-pressed="true"]')).includes('概念')?'concept':'industry',
   rows:[...root.querySelectorAll('.ty-funds-ranking tbody tr')].map(node=>({name:text(node.querySelector('.ty-sector-choice')),cells:[...node.querySelectorAll('td')].map(text)})),
   captions:[...root.querySelectorAll('.ty-funds-chart-panel p.ty-panel-caption')].map(text)},
  indices:[...root.querySelectorAll('.ty-index')].map(node=>({name:text(node.querySelector('b')),pct:text(node.querySelector('strong')),price:text(node.querySelector('small')),title:node.getAttribute('title')??''}))};
}
function checkPrimarySnapshot(data: WorkbenchData,view: TerminalView,dom: ReturnType<typeof readPrimarySnapshot> | null,allowRetained=false){
 const failures: {region?:string;reason:string}[]=[],regions: {name:string;expectedRows:number;observedRows:number;expectedCanonical:string;observedCanonical:string;passed:boolean}[]=[];
 let primaryNumeric=false;
 const region=(name: string,expected: unknown[],observed: unknown[])=>{
  const passed=JSON.stringify(expected)===JSON.stringify(observed);
  regions.push({name,expectedRows:expected.length,observedRows:observed.length,expectedCanonical:canonical(expected),observedCanonical:canonical(observed),passed});
  if(!passed)failures.push({region:name,reason:'complete_native_rows_or_values_do_not_match'});
 };
 if(!dom?.found||!dom.visible||dom.view!==view||dom.loading&&!allowRetained)return {passed:false,failures:[{reason:'primary_view_not_ready'}],regions};
 if(view==='overview'){
  const controls=dom.overviewControls;
  const pagination=/^(\d+)只股票，第(\d+)页，共(\d+)页$/.exec(controls.pagination);
  const sort=controls.sorts.length===1?controls.sorts[0]:null;
  if(controls.workspace&&controls.workspace!=='盘面'||controls.search!==null&&controls.search!==''||controls.scope!==null&&controls.scope!=='all'
   ||sort&&(sort.index!==3||sort.direction!=='descending')||pagination&&Number(pagination[2])!==1)
   return {passed:false,failures,regions,unverifiableReason:'overview_controls_outside_supported_default'};
  region('overview_controls',[{workspace:'盘面',visible:true,search:'',scope:'all',sorts:[{index:3,direction:'descending'}],tables:1}],
   [{workspace:controls.workspace,visible:controls.visible,search:controls.search,scope:controls.scope,sorts:controls.sorts,tables:dom.overview.length}]);
  region('overview_pagination',[{total:data.stocks.length,page:1,pages:Math.max(1,Math.ceil(data.stocks.length/50))}],
   [pagination?{total:Number(pagination[1]),page:Number(pagination[2]),pages:Number(pagination[3])}:null]);
  const rows=[...data.stocks].sort((a,b)=>{
   const missingA=a.pct_change==null||!Number.isFinite(a.pct_change),missingB=b.pct_change==null||!Number.isFinite(b.pct_change);
   if(missingA||missingB)return missingA===missingB?a.code.localeCompare(b.code):missingA?1:-1;
   return (b.pct_change!-a.pct_change!)||a.code.localeCompare(b.code);
  }).slice(0,50),observedRows=dom.overview[0]??[];
  primaryNumeric=rows.some(stock=>[stock.pct_change,stock.speed_5m,stock.amount_cny].some(value=>value!=null&&Number.isFinite(value)));
  for(const metric of ['pct_change','speed_5m'] as const){
   const expected=rows.map(stock=>({code:stock.code,title:stockTitle(stock),value:percent(stock[metric]),secondary:metric==='speed_5m'?percent(stock.pct_change):money(stock.amount_cny,1)+(stock.amount_cny!=null?'亿':'')}));
   const observed=observedRows.map(row=>({code:row.code,title:row.title,value:row.cells[metric==='speed_5m'?2:3],secondary:row.cells[metric==='speed_5m'?3:4]}));
   region(metric,expected,observed);
  }
  region('overview_columns',[{headers:['排名','名称','5分涨速',data.breadth.basis==='daily_close'?'收盘涨幅':'涨幅','成交额','同花顺题材']},
   ...rows.map((stock,index)=>({rank:String(index+1),code:stock.code,label:clean(stock.name),codeLabel:stock.code.slice(0,6),cells:6,themes:clean(stock.ths_themes?.join('、'))||'—'}))],
   [{headers:controls.headers},...observedRows.map(row=>({rank:row.cells[0],code:row.code,label:row.label,codeLabel:row.codeLabel,cells:row.cells.length,themes:row.cells[5]}))]);
 }else if(view==='auction'){
  if(dom.auction.length!==data.auction_days.length)failures.push({region:'auction_days',reason:'complete_day_set_does_not_match'});
  for(const day of data.auction_days){
   const list=dom.auction.find(item=>item.label===day.day+'封单滚动列表');
   if(!list){failures.push({region:'auction_day',reason:'native_day_list_missing'});continue;}
   const expected=day.items.map(stock=>({code:stock.code,title:stockTitle(stock),amounts:(['09:15','09:20','09:25'] as const).map(node=>money(stock.node_amounts?.[node],2)).concat(list.rolling?[money(stock.auction_current_seal_cny,2)]:[]),pct:percent(stock.auction_indicative_pct??stock.auction_pct)})).sort((a,b)=>a.code.localeCompare(b.code));
   primaryNumeric ||= day.items.some(stock=>Number.isFinite(stock.auction_indicative_pct??stock.auction_pct)||Object.values(stock.node_amounts??{}).some(Number.isFinite));
   const observed=list.rows.map(row=>({code:row.code,title:row.title,amounts:row.amounts,pct:row.pct})).sort((a,b)=>(a.code??'').localeCompare(b.code??''));
   region('auction:'+day.day,expected,observed);
   if(day.node_source&&!list.source.includes(clean(day.node_source+' · 采集 '+dateTime(day.node_received_at))))failures.push({region:'auction_source',reason:'source_or_receipt_clock_mismatch'});
  }
 }else if(view==='ladder'){
  const rows=data.stocks.filter(stock=>stock.previous_height!=null&&stock.previous_height>=1).sort((a,b)=>b.previous_height!-a.previous_height!);
  primaryNumeric=rows.some(stock=>Number.isFinite(stock.previous_height));
  const expected=rows.map(stock=>({code:stock.code,title:stockTitle(stock),
   status:clean((stock.status||'待观测')+(stock.seal_amount_cny==null?' '+percent(stock.pct_change):'')),
   turnover:number(stock.turnover_rate,1),
   seal:stock.seal_amount_cny!=null?money(stock.seal_amount_cny):stock.daily_pool?.seal_amount_cny!=null?money(stock.daily_pool.seal_amount_cny)+'日榜':'—'}));
  const observed=dom.ladder.map(row=>({code:row.code,title:row.title,status:row.cells[1],turnover:row.cells[3],seal:row.cells[4]}));
  region('ladder',expected,observed);
 }else if(view==='funds'){
  const all: IntradayFundSector[]=dom.funds.intraday?data.intraday_funds?.sectors??[]:data.sectors;
  const rows=all.filter(item=>dom.funds.category==='industry'?item.category==='industry'&&item.industry_level===2:item.category==='concept')
   .sort((a,b)=>(b.main_net_cny??-Infinity)-(a.main_net_cny??-Infinity)).slice(0,10);
  primaryNumeric=rows.some(item=>Number.isFinite(item.main_net_cny));
  if(new Set(rows.map(item=>item.name)).size!==rows.length)failures.push({region:'funds',reason:'ambiguous_visible_sector_names'});
  const expected=rows.map((item,index)=>({name:item.name,rank:String(index+1),net:signedMoney(item.main_net_cny),
   change:dom.funds.intraday?signedMoney(item.change_5m_cny):'—',rankChange:!dom.funds.intraday||item.rank_change_5m==null?'—':item.rank_change_5m>0?'↑'+number(item.rank_change_5m):item.rank_change_5m<0?'↓'+number(Math.abs(item.rank_change_5m)):'0'}));
  const observed=dom.funds.rows.map(row=>({name:row.name,rank:row.cells[0],net:row.cells[2],change:row.cells[3],rankChange:row.cells[4]}));
  region('funds:'+dom.funds.category,expected,observed);
  if(dom.funds.intraday&&!dom.funds.captions.some(value=>value.includes(clean((data.intraday_funds?.source_name||'盘中资金')+' · 数据截至 '+dateTime(data.intraday_funds?.latest_source_time)))))failures.push({region:'funds_source',reason:'source_or_native_clock_mismatch'});
 }
 if(view==='overview'||view==='funds'||dom.indices.length){
  const codes=['000001.SH','399001.SZ','399006.SZ'];
  const expected=codes.map(code=>{const row=data.indices.find(item=>item.code===code);return {pct:percent(row?.pct_change),price:number(row?.price,2),title:clean(`${row?.source_name?'实际供数：'+row.source_name+'\n':''}${row?quoteTiming(row):'行情时点待核对'}`)};});
  region('indices',expected,dom.indices.map(row=>({pct:row.pct,price:row.price,title:clean(row.title)})));
 }
 if(!primaryNumeric||!regions.some(item=>item.name!=='indices'&&item.expectedRows>0))failures.push({reason:'no_valid_primary_native_data'});
 return {passed:!failures.length,failures,regions,fundsMode:view==='funds'?(dom.funds.intraday?'intraday':'daily'):null,
  nativeFundPoints:data.intraday_funds?.points?.length??0};
}
export {readPrimarySnapshot,checkPrimarySnapshot};
