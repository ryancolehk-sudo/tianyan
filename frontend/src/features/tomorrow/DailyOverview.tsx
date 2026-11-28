import { useState } from 'react';
import { records, text } from './model';
import type { ReportView } from './model';

type Row = Record<string, unknown>;

function field(row: Row, path: string): unknown {
  let current: unknown = row;
  for (const key of path.split('.')) {
    if (!current || typeof current !== 'object') return null;
    current = (current as Row)[key];
  }
  return current;
}
function number(value: unknown) {
  if (value == null || value === '' || typeof value === 'boolean') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function formatted(value: unknown, suffix = '', multiplier = 1) {
  const n = number(value);
  return n == null ? '—' : (n * multiplier).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + suffix;
}
const views = ['涨停行业', '连板梯队', '非涨停强势股', '概念与标的', '仓位与现金'] as const;
export function DailyOverview({ report }: { report: ReportView }) {
  const [view, setView] = useState<typeof views[number]>('涨停行业');
  const data = report.dossier;
  const stocks = records(data.stocks);
  const market = (data.market ?? {}) as Row;
  const pool = (field(market, 'limits.supplier_reported') ?? {}) as Row;
  const cash = (data.portfolio ?? {}) as Row;
  const sealed = stocks.filter(s => field(s, 'facts.pool.value.limit') === 'U');
  const stockCode = (s: Row) => text(s.code);
  const stockName = (s: Row) => text(s.name ?? s.code);
  const gain = (s: Row) => number(field(s, 'facts.pct_chg.value'));
  const height = (s: Row) => number(field(s, 'facts.pool.value.limit_times'));
  const chips = [
    ['上涨 / 下跌', formatted(field(market, 'breadth.up')) + ' / ' + formatted(field(market, 'breadth.down'))],
    ['成交额', formatted(field(market, 'turnover.amount_yuan'), ' 亿', 0.00000001)],
    ['涨停 / 跌停', formatted(pool.up) + ' / ' + formatted(pool.down)],
    ['最高连板', formatted(pool.max_height, ' 板')],
    ['炸板率', formatted(pool.broken_rate_pct, '%')],
  ];
  let columns: string[];
  let rows: string[][];
  let note: string;
  if (view === '涨停行业') {
    const groups = new Map<string, Row[]>();
    for (const s of sealed) {
      const industry = String(field(s, 'facts.pool.value.industry') || '行业未披露');
      const group = groups.get(industry) ?? [];
      group.push(s);
      groups.set(industry, group);
    }
    columns = ['行业', '涨停家数', '池内占比', '涨停标的'];
    rows = [...groups].sort((a,b) => b[1].length - a[1].length || a[0].localeCompare(b[0], 'zh-CN'))
      .map(([industry, members]) => [industry, String(members.length), formatted(members.length / sealed.length, '%', 100), members.map(stockName).join('、')]);
    note = '按供应商涨停池的行业字段分组，每只股票计入一个行业。';
  } else if (view === '连板梯队') {
    columns = ['高度', '标的', '代码', '首次封板', '开板次数', '成交额（亿元）'];
    rows = sealed.filter(s => (height(s) ?? 0) >= 2).sort((a,b) => height(b)! - height(a)!)
      .map(s => [formatted(height(s), ' 板'), stockName(s), stockCode(s), text(field(s,'facts.pool.value.first_time')), text(field(s,'facts.pool.value.open_times')), formatted(field(s,'facts.amount.value'),'',0.00001)]);
    note = '按供应商披露的连续涨停高度降序。首板和炸板见涨停池全景。';
  } else if (view === '非涨停强势股') {
    columns = ['标的', '代码', '涨幅', '5 日表现', '量比（对前五日均量）', '历史支撑', '历史压力'];
    rows = stocks.filter(s => (gain(s) ?? 0) > 0 && field(s,'calculations.price_verified_limit_up') === false)
      .sort((a,b) => gain(b)! - gain(a)!).slice(0,20).map(s => [stockName(s), stockCode(s), formatted(gain(s),'%'), formatted(field(s,'calculations.returns_pct.5.value'),'%'), formatted(field(s,'calculations.volume_to_prior5_mean.value')), formatted(field(s,'calculations.support_resistance.support_candidate')), formatted(field(s,'calculations.support_resistance.resistance_candidate'))]);
    note = '取底稿内收盘上涨、经当日限制价核实未封板的前 20 只，按当日涨幅排序。';
  } else if (view === '概念与标的') {
    columns = ['概念', '当日涨幅', '5 日表现', '成分涨停数', '观察标的'];
    rows = records(data.sectors).slice(0,10).map(sector => [text(sector.name ?? sector.code), formatted(field(sector,'facts.pct_change.value'),'%'), formatted(field(sector,'calculations.returns_pct.5.value'),'%'), formatted(field(sector,'calculations.member_limit_up_n')), stocks.filter(s => Array.isArray(s.sectors) && s.sectors.includes(sector.code)).slice(0,5).map(stockName).join('、') || '—']);
    note = '按当日概念涨幅取前十。标的来自本次底稿中已保存的概念成分。';
  } else {
    columns = ['组合项目', '本稿数值'];
    rows = [['总仓位', formatted(cash.invested_weight,'%',100)], ['现金占比', formatted(cash.cash_weight,'%',100)], ['现金余额', formatted(cash.cash,' 元')]];
    const policy = (cash.constraints ?? {}) as Row;
    if (policy.approved === true) {
      rows.push(['已确认总仓位上限', formatted(policy.max_total_weight,'%',100)], ['单股上限', formatted(policy.max_stock_weight,'%',100)], ['板块上限', formatted(policy.max_sector_weight,'%',100)]);
    }
    for (const a of records(cash.allocations)) rows.push([text(a.code), formatted(a.weight,'%',100)]);
    note = Array.isArray(cash.limitations) && cash.limitations.length ? cash.limitations.map(text).join(' ') : '按本稿冻结的现金、持仓与价格计算。';
  }
  return <section className="tp-panel tp-daily" aria-label="盘后速览"><div className="tp-actions"><h2>盘后速览</h2><small>来源交易日 {report.source_trade_day}</small></div>
    <dl className="tp-daily-metrics">{chips.map(([name,value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
    <div className="tp-actions" aria-label="切换盘后明细">{views.map(v => <button type="button" key={v} aria-pressed={v === view} onClick={() => setView(v)}>{v}</button>)}</div>
    <div className="tp-table-wrap"><table><caption className="tp-sr">{view}</caption><thead><tr>{columns.map(c => <th key={c}>{c}</th>)}</tr></thead><tbody>{rows.map((row,i) => <tr key={i}>{row.map((cell,j) => <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>
    {!rows.length && <p className="tp-muted">本稿没有符合条件的记录。</p>}<small>{note}</small></section>;
}
