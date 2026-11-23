export type LadderBar = { day:string; open:number; high:number; low:number; close:number };
export type LadderRecord = {
  code:string; name:string; day:string; height:number; previous_height:number|null; status:string;
  themes:string[]; theme_source:string|null; limit_type:string|null; pct_change:number|null; price:number|null;
  auction_price:number|null; first_time:string|null; last_time:string|null;
  seal_amount:number|null; amount:number|null; actual_float:number|null; actual_turnover:number|null;
  source_time:string|null; received_at:string|null; price_basis:string; stale:boolean;
};
export type LadderDay = { day:string; previous_day:string|null; state:string; rows:LadderRecord[]; eliminated:LadderRecord[]; coverage:Record<string,unknown>[] };
export type LadderObservation = {trade_day:string|null; quote_day:string|null; available_days:string[]; days:LadderDay[];
  candles:Record<string,LadderBar[]>; latest_quotes:Record<string,LadderRecord>; state:string; as_of_time:string|null;
  built_at:string; can_export:boolean; scope:string};
export type LadderMode = 'single'|'multi'|'list'|'sector';
export type SortField = 'pct_change'|'first_time'|'last_time'|'seal_amount'|'amount'|'actual_float'|'actual_turnover';
export const sortLabels:Record<SortField,string> = {pct_change:'涨幅',first_time:'首封',last_time:'末封',seal_amount:'封单',amount:'成交额',actual_float:'实际流通',actual_turnover:'实际换手'};
export const statusLabel:Record<string,string> = {sealed:'涨停',touched:'触板',opened:'炸板',eliminated:'淘汰'};
export function pct(value:number|null|undefined) {return value==null?'—':`${value>0?'+':''}${value.toFixed(2)}%`;}
export function cash(value:number|null|undefined) {return value==null?'—':Math.abs(value)>=1e8?`${(value/1e8).toFixed(2)}亿`:Math.abs(value)>=1e4?`${(value/1e4).toFixed(0)}万`:value.toLocaleString('zh-CN',{maximumFractionDigits:0});}
export function tone(value:number|null|undefined) {return value==null||value===0?'neutral':value>0?'up':'down';}
export function localTimestamp(value:string|null|undefined) {
  if(!value)return '未披露';
  const date=new Date(value);return Number.isNaN(date.getTime())?'未披露':new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date);
}
export function compareRows(field:SortField, direction:'asc'|'desc') {
  return (a:LadderRecord,b:LadderRecord) => {
    const x=a[field],y=b[field];
    if(x==null||y==null)return x==null&&y==null?a.code.localeCompare(b.code):x==null?1:-1;
    return (typeof x==='string'&&typeof y==='string'?x.localeCompare(y):Number(x)-Number(y))*(direction==='asc'?1:-1)||a.code.localeCompare(b.code);
  };
}
export function barsThrough(bars:LadderBar[]|undefined, day:string) {return (bars??[]).filter(b=>b.day<=day).slice(-24);}
export function themeColor(name:string) {let hash=0;for(const char of name)hash=(hash*31+char.charCodeAt(0))|0;return ['#ff8d9d','#f3bf63','#9aafff','#a7d47c','#c5a0ed','#62cfc6','#e5a67a','#8bc6ec'][Math.abs(hash)%8];}
