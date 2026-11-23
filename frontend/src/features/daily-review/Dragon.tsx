import { StockCandles } from './ReviewAnalysis';
import { useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { useNavigation } from '../../shared/navigation';
import { Card, Table, Empty, Loading, Failure, RefButton, Money, Value, useBoundRead } from './components';
import { amount, number, text, tone } from './model';
import type { Listing, Row, Side } from './model';
import type { ReviewProps } from './context';

type HistoryRecord = { trade_day: string; ts_code: string; reason: string; record_key: string; net_amount: string | null; state: string; source_rows: Row[] };
function SeatTable({ side, type, select, openRef, selected }: { side: Side; type: 'buy' | 'sell'; select: ReviewProps['select']; openRef: ReviewProps['openRef']; selected: string }) {
  const rows = [...side.records, ...side.extra_rows];
  return <><Table label={type === 'buy' ? '买入侧原始席位' : '卖出侧原始席位'}><thead><tr><th>#</th><th>席位名称</th><th>{type === 'buy' ? '买入额' : '卖出额'}</th><th>证据</th></tr></thead><tbody>{rows.map((row, i) => <tr key={i} className={selected === row.exalter ? 'chosen' : ''}><td>{i + 1}</td><td><button className="dr-text-button" onClick={() => select({ seat: String(row.exalter) })}>{text(row.exalter)}</button></td><td className={type === 'buy' ? 'rise' : 'fall'}>{amount(row[type === 'buy' ? 'buy' : 'sell'])}</td><td><RefButton row={row} onOpen={openRef} label="核对该席位原始行"/></td></tr>)}</tbody></Table>{!rows.length && <Empty>没有该侧可匹配的席位披露。</Empty>}{side.state !== 'complete' && <p className="dr-warning">{side.state === 'ambiguous_disclosure' ? '同股票、同原因存在多条披露，席位归属待核；金额不合计。' : `该侧披露${side.disclosed_row_count}行，前五合计尚未核全。`}</p>}</>;
}

function SeatHistory(p: ReviewProps & { seat: string }) {
  const query = new URLSearchParams(location.search);
  const [minimum, setMinimum] = useState(query.get('min_amount_yuan') ?? '');
  const lookback = query.get('seat_days') ?? '5';
  const params = new URLSearchParams({ trade_day: p.day, seat_name: p.seat, lookback, ...(query.get('min_amount_yuan') ? { min_amount_yuan: query.get('min_amount_yuan')! } : {}), ...(p.asOf ? { as_of_time: p.asOf } : {}) });
  const read = useBoundRead(`seat:${params}`, () => p.seat ? api.reviewSeat(params) : Promise.resolve(null));
  return <Card title={p.seat ? `${p.seat} · 近期买入侧披露` : '席位近期披露'} action={<form className="dr-seat-filters" onSubmit={e => { e.preventDefault(); p.select({ min_amount_yuan: minimum || null }); }}><label>时间<select aria-label="席位披露时间范围" value={lookback} onChange={e => p.select({ seat_days: e.target.value })}>{[5, 10, 20].map(n => <option value={n} key={n}>近{n}交易日</option>)}</select></label><label>最低买入额（元）<input aria-label="席位最低买入额" type="number" min="0" step="0.01" value={minimum} onChange={e => setMinimum(e.target.value)}/></label><button disabled={!p.seat}>应用筛选</button></form>}>
    {read.busy && p.seat ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : !p.seat ? <Empty>点击上方席位名称，查看相同披露名称的历史记录。</Empty> : <><Table label="席位近期买入记录"><thead><tr><th>股票</th><th>披露日</th><th>原涨幅</th><th>买入额</th><th>原因</th><th>查看</th></tr></thead><tbody>{read.data?.data.records.map((row, i) => <tr key={i}><td>{row.name ?? row.ts_code}<small>{row.ts_code}</small></td><td>{row.trade_day.slice(5)}</td><td><Value value={row.listing_record?.pct_change} percentage/></td><td><Money value={row.amount_yuan}/></td><td className="dr-wrap-cell">{row.reason}</td><td><button onClick={() => p.select({ trade_day: row.trade_day, stock: row.ts_code, record_key: row.lhb_record_key })}>详情 ↗</button></td></tr>)}</tbody></Table>{!read.data?.data.records.length && <Empty>当前筛选下没有已披露的买入侧记录。</Empty>}{read.data?.data.truncated && <p className="dr-warning">当前显示最多500条，请收窄时间或金额。</p>}</>}
    <p className="dr-note">相同席位名称不代表唯一交易者；仅买入侧前五披露，不是该席位全部买卖。不同原因与区间逐条保留，不累计金额。</p>
  </Card>;
}

function StockHistory(p: ReviewProps & { code: string }) {
  const params = new URLSearchParams({ trade_day: p.day, stock: p.code, ...(p.asOf ? { as_of_time: p.asOf } : {}) });
  const read = useBoundRead(`lhb-history:${params}`, () => p.code ? api.reviewLhb(params) : Promise.resolve(null));
  const records = (read.data?.data as unknown as { history_records?: HistoryRecord[]; history_truncated?: boolean } | undefined)?.history_records ?? [];
  return <Card title="个股历次上榜" action={<small>有权已存窗口</small>}>{read.busy ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : records.length ? <Table label="个股历史披露"><thead><tr><th>日期</th><th>披露原因</th><th>原榜净额</th><th>记录</th></tr></thead><tbody>{records.map(row => <tr key={row.record_key} className={row.trade_day === p.day ? 'chosen' : ''}><td>{row.trade_day.slice(5)}</td><td className="dr-wrap-cell">{row.reason}</td><td><Money value={row.net_amount}/>{row.state !== 'available' && <small>归属待核</small>}</td><td><button onClick={() => p.select({ trade_day: row.trade_day, stock: row.ts_code, record_key: row.record_key, seat: null })}>查看 ↗</button></td></tr>)}</tbody></Table> : <Empty>没有可访问的历史披露记录。</Empty>}<p className="dr-note">统计区间分别记录，不累计重叠金额。最多显示200条。</p></Card>;
}

function ResearchExcerpt(p: ReviewProps & { record: Listing | null }) {
  const { record, notebook: n } = p;
  const note = n.content.lhb_notes.find(row => row.record_key === record?.record_key);
  function updated(value: string) {
    if (!record || !n.writable || (!note && n.content.lhb_notes.length >= 20)) return;
    const refs = [...record.source_rows, ...record.buy_top5.records, ...record.sell_top5.records].map(row => row.row_reference?.id).filter((id): id is string => !!id);
    const next = { record_key: record.record_key, stock_code: record.ts_code, text: value, confirmed: false, evidence_ids: [...new Set(refs)].slice(0, 20) };
    n.update({ ...n.content, lhb_notes: [...n.content.lhb_notes.filter(row => row.record_key !== record.record_key), next] });
  }
  async function confirm() {
    if (!note || !record || !note.text.trim()) return;
    const content = { ...n.content, lhb_notes: n.content.lhb_notes.map(row => row.record_key === record.record_key ? { ...row, confirmed: true } : row) };
    await n.save(content, 'lhb');
  }
  return <Card title="研究摘记与引用" action={<span className="dr-ai">人工摘记 · {note?.confirmed && !n.dirty ? '已确认' : '待确认'}</span>}>
    <label className="dr-sr" htmlFor="lhb-research-note">龙虎榜研究摘记</label><textarea id="lhb-research-note" value={note?.text ?? ''} onChange={e => updated(e.target.value)} maxLength={2000} rows={7} disabled={!n.writable || !record || (!note && n.content.lhb_notes.length >= 20)} placeholder="逐条记录席位披露、仍待核实的问题和相反证据。"/>
    {record ? <p className="dr-reference-chip">{text(record.source_rows[0]?.name)} · {p.day.slice(5)} · 原因分组<RefButton row={record.source_rows[0]} onOpen={p.openRef}/></p> : <Empty>先选择一条具体披露。</Empty>}
    <div className="dr-card-actions"><button disabled={!n.writable || !record} onClick={() => record && p.addCore(record.ts_code, String(record.source_rows[0]?.name ?? record.ts_code))}>加入方向跟踪</button><button disabled title="明日预案后续添加">加入明日观察</button></div><div className="dr-form-actions"><button disabled={!n.writable || !n.dirty || n.busy} onClick={() => n.save()}>保存摘记</button><button className="primary" disabled={!n.writable || n.busy || !note?.text.trim() || (!!note?.confirmed && !n.dirty)} onClick={confirm}>确认并引用</button></div><p className="dr-note">保存与确认均新增笔记版本，并绑定这条披露的原证据；不改变原榜单。</p>
  </Card>;
}

export function Dragon(p: ReviewProps) {
  const nav = useNavigation(); const query = new URLSearchParams(nav.search);
  const [search, setSearch] = useState(''); const [scope, setScope] = useState('all'); const [netOrder, setNetOrder] = useState(false);
  const params = new URLSearchParams({ trade_day: p.day, ...(p.asOf ? { as_of_time: p.asOf } : {}) });
  const read = useBoundRead(`lhb:${params}`, () => api.reviewLhb(params));
  const records = read.data?.data.records ?? [];
  const key = query.get('record_key');
  const record = key ? records.find(r => r.record_key === key) ?? null : (p.stock ? records.find(r => r.ts_code === p.stock) : records[0]) ?? null;
  useEffect(() => { if (record && !key) p.select({ stock: record.ts_code, record_key: record.record_key }, true); }, [record?.record_key, key]);
  const memberCodes = new Set(p.selectedTheme?.member_records.map(row => String(row.con_code)) ?? []);
  const filtered = records.filter(r => (!p.mine || p.watched.has(r.ts_code)) && (scope !== 'mine' || p.watched.has(r.ts_code)) && (scope !== 'sector' || memberCodes.has(r.ts_code)) && `${r.ts_code} ${text(r.source_rows[0]?.name)} ${r.reason}`.toLowerCase().includes(search.toLowerCase()));
  if (netOrder) filtered.sort((a, b) => (number(b.headline.net_amount) ?? -Infinity) - (number(a.headline.net_amount) ?? -Infinity));
  const seat = query.get('seat') ?? '';
  return <><div className="dr-lhb-top"><Card title="上榜对象" action={<small>{records.length}组沪深披露</small>}><div className="dr-table-tools"><input aria-label="搜索上榜对象" value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索股票 / 原披露原因"/><select aria-label="龙虎榜筛选范围" value={scope} onChange={e => setScope(e.target.value)}><option value="all">全部披露</option><option value="mine">我的关注</option><option value="sector">所选题材已返回成员</option></select><button aria-pressed={netOrder} onClick={() => setNetOrder(!netOrder)}>{netOrder ? '按净额' : '原披露顺序'}</button></div>
    {read.busy ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : <Table label="龙虎榜上榜对象" className="dr-lhb-list"><thead><tr><th>#</th><th>个股</th><th>原因 / 区间</th><th>原榜净额</th><th>状态</th></tr></thead><tbody>{filtered.map((r, i) => <tr key={r.record_key} className={record?.record_key === r.record_key ? 'chosen' : ''}><td>{i + 1}</td><td><button className="dr-text-button" onClick={() => p.select({ stock: r.ts_code, record_key: r.record_key, seat: null })}>{text(r.source_rows[0]?.name)}<small>{r.ts_code}</small></button></td><td className="dr-wrap-cell" title={r.reason}>{r.reason}</td><td><Money value={r.headline.net_amount}/></td><td>{r.headline_state === 'available' ? '已载入' : '归属待核'}</td></tr>)}</tbody></Table>}
    {scope === 'sector' && p.selectedTheme?.truncated && <p className="dr-warning">原日成员详情已截断，此处仅筛选已返回成员，不能代表题材全部披露。</p>}{!read.busy && !filtered.length && <Empty>{scope === 'sector' && !p.selectedTheme?.detail_available ? '所选原日题材成员未取得，不能确认关联范围。' : scope === 'sector' && p.selectedTheme?.truncated ? '已返回成员中未匹配到披露；完整题材范围尚不可判断。' : '当前筛选下没有已披露记录。'}</Empty>}<div className="dr-link-line"><span>关注关联：{p.notebook.content.core_stocks.find(c => c.code === record?.ts_code)?.role ?? '尚未设置研究角色'}</span><button onClick={() => p.go('direction')}>返回板块轮动 →</button></div><p className="dr-note">按具体披露原因分别查看，不合并单日与多日金额。</p></Card>
    <Card title={`席位明细${record ? ' · ' + text(record.source_rows[0]?.name) : ''}`} action={<RefButton row={record?.source_rows[0]} onOpen={p.openRef} label="核对榜单原始披露"/>}>
      {record ? <><p className="dr-listing-reason">上榜原因：{record.reason}</p>{record.headline_state !== 'available' && <p className="dr-warning">同代码、同原因有多条来源记录，无法确认唯一披露区间。原行均保留，净额和席位合计不拼加。</p>}<div className="dr-three-values dr-lhb-totals"><section><small>买入侧前五合计</small><b className="rise">{amount(record.buy_top5.amount_yuan)}</b></section><section><small>卖出侧前五合计</small><b className="fall">{amount(record.sell_top5.amount_yuan)}</b></section><section><small>原榜净额</small><b className={tone(record.headline.net_amount)}>{amount(record.headline.net_amount)}</b></section></div><div className="dr-seats-grid"><Card title="买入侧前五"><SeatTable side={record.buy_top5} type="buy" select={p.select} openRef={p.openRef} selected={seat}/></Card><Card title="卖出侧前五"><SeatTable side={record.sell_top5} type="sell" select={p.select} openRef={p.openRef} selected={seat}/></Card></div><p className="dr-note">点击席位查看近期同名披露。原榜净额直接引用统计单，不用两侧前五之差替代；机构同名行各自保留。</p></> : <Empty>{key ? '该具体披露未在所选日期返回，请从左侧重新选择。' : p.stock ? '所选股票当日没有可访问的龙虎榜披露。' : '请选择一条上榜记录。'}</Empty>}
    </Card></div><StockCandles day={p.day} asOf={p.asOf} stock={record?.ts_code ?? p.stock} select={p.select}/><div className="dr-lhb-bottom"><StockHistory {...p} code={record?.ts_code ?? p.stock}/><SeatHistory {...p} seat={seat}/><ResearchExcerpt {...p} record={record}/></div></>;
}
