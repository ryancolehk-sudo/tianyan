import { useState } from 'react';
import { api } from '../../shared/api';
import { Card, Table, Empty, Loading, Failure, RefButton, Value, Money, useBoundRead } from './components';
import { amount, number, pct, slots, text, tone } from './model';
import type { Row, Theme } from './model';
import type { ReviewProps } from './context';

export function ThemeTable(p: ReviewProps & { compact?: boolean }) {
  const [search, setSearch] = useState(''); const [sort, setSort] = useState('pct'); const [limit, setLimit] = useState(12);
  const chosen = new Set(p.notebook.content.directions.map(d => d.sector_code));
  p.notebook.content.core_stocks.forEach(c => c.sector_code && chosen.add(c.sector_code));
  const all = p.data.themes.items.filter(t => (!p.mine || chosen.has(t.ts_code)) && `${t.name} ${t.ts_code}`.toLowerCase().includes(search.toLowerCase())).sort((a, b) => {
    const value = (t: Theme) => sort === 'amount' ? number(t.member_sample.known_turnover_yuan) : sort === 'funds' ? number(t.fund_records[0]?.net_amount) : number(t.provider_record.pct_change);
    return (value(b) ?? -Infinity) - (value(a) ?? -Infinity);
  });
  return <><div className="dr-table-tools">{!p.compact && <input aria-label="搜索题材" placeholder="搜索题材 / 板块代码" value={search} onChange={e => setSearch(e.target.value)}/>}<div className="dr-mini-tabs">{[['pct', '涨幅'], ['amount', '成员成交'], ['funds', '板块资金']].map(([key, label]) => <button key={key} className={sort === key ? 'selected' : ''} aria-pressed={sort === key} onClick={() => setSort(key)}>{label}</button>)}</div></div><Table label="题材表现与联动" className={p.compact ? '' : 'dr-medium-scroll'}><thead><tr><th>题材</th><th>原涨幅</th><th>{sort === 'funds' ? '板块净流入' : '已披露成员成交'}</th><th>涨停 / 炸板</th><th>我的定位</th><th>证据</th></tr></thead><tbody>{all.slice(0, p.compact ? 6 : limit).map(t => <tr key={t.ts_code} className={p.sector === t.ts_code ? 'chosen' : ''}><td><button className="dr-text-button" onClick={() => p.select({ sector: t.ts_code })}>{t.name}</button></td><td><Value value={t.provider_record.pct_change} percentage/></td><td><Money value={sort === 'funds' ? t.fund_records[0]?.net_amount : t.member_sample.known_turnover_yuan}/></td><td>{text(t.member_sample.pool_up)} / {text(t.member_sample.pool_broken)}</td><td>{p.notebook.content.directions.find(d => d.sector_code === t.ts_code)?.slot ?? '未定位'}</td><td><RefButton row={sort === 'funds' ? t.fund_records[0] : t.provider_record} onOpen={p.openRef} label={`查看${t.name}原始证据`}/></td></tr>)}</tbody></Table>{!all.length && <Empty>{p.mine ? '尚未指定研究题材。可先在方向定位中填写。' : '没有匹配的原日题材。'}</Empty>}{!p.compact && all.length > limit && <button className="dr-load-more" onClick={() => setLimit(limit + 30)}>查看更多（{Math.min(limit, all.length)}/{all.length}）</button>}<p className="dr-note">原涨幅为供应商板块范围；成员成交与涨停/炸板为同日已披露沪深样本，非全题材总量。</p></>;
}

export function CoreTable(p: ReviewProps & { compact?: boolean }) {
  const rows = p.notebook.content.core_stocks.filter(c => !p.mine || p.watched.has(c.code));
  return rows.length ? <Table label="核心个股跟踪"><thead><tr><th>#</th><th>个股</th><th>表现</th><th>研究角色</th><th>待核查</th><th>标记</th><th>证据</th></tr></thead><tbody>{rows.slice(0, p.compact ? 6 : 30).map((c, i) => { const value = p.tracked.find(s => s.ts_code === c.code); const pool = value?.pool_record; const sealed = pool?.limit === 'U'; return <tr key={c.code} className={p.stock === c.code ? 'chosen' : ''}><td>{i + 1}</td><td><button className="dr-text-button" onClick={() => p.select({ stock: c.code, ...(c.sector_code ? { sector: c.sector_code } : {}), record_key: null })}>{c.name}<small>{c.code}</small></button></td><td><Value value={value?.quote?.pct_chg} percentage/>{sealed && <small className="gold">{text(number(pool?.limit_times))}板</small>}</td><td><button className="dr-text-button" disabled={!p.notebook.writable} onClick={() => p.select({ stock: c.code, edit: 'core' })}>{c.role || '未设角色'} ✎</button></td><td className="dr-wrap-cell">{c.rationale || '待补充依据'}</td><td><span className={`dr-tag ${c.tag === '风险提示' ? 'danger' : ''}`}>{c.tag}</span></td><td><RefButton row={value?.quote} onOpen={p.openRef} label={`查看${c.name}当日日线证据`}/></td></tr>; })}</tbody></Table> : <Empty>从题材内部梯队选择个股，点击“加入核心跟踪”，再填写自己的研究角色。</Empty>;
}

export function WatchQuestions(p: ReviewProps) {
  return <>{p.notebook.content.observations.length ? <Table label="我的观察清单"><thead><tr><th>#</th><th>关注问题</th><th>标记</th></tr></thead><tbody>{p.notebook.content.observations.map((o, i) => <tr key={i}><td>{i + 1}</td><td className="dr-wrap-cell">{o.text}</td><td><span className={`dr-tag ${o.tag === '风险提示' ? 'danger' : ''}`}>{o.tag}</span></td></tr>)}</tbody></Table> : <Empty>记录下一步需要核对的问题，避免把观察直接当作结论。</Empty>}<button className="dr-load-more" disabled={!p.notebook.writable} onClick={() => p.edit('watch')}>编辑观察清单 ✎</button></>;
}

export function Catalysts(p: ReviewProps) {
  const [expanded, setExpanded] = useState(false);
  const rows = p.data.news.records;
  function link(row: Row) {
    const ref = row.row_reference;
    if (!ref || ref.source_row === undefined || !p.sector || !p.notebook.writable) return;
    if (p.notebook.content.catalyst_links.some(c => c.evidence_id === ref.id && c.source_row === ref.source_row && c.sector_code === p.sector)) return;
    if (p.notebook.content.catalyst_links.length >= 20) return;
    p.notebook.update({ ...p.notebook.content, catalyst_links: [...p.notebook.content.catalyst_links, { evidence_id: ref.id, source_row: ref.source_row, sector_code: p.sector, comment: '' }] });
  }
  return <><div className="dr-news-window">{rows.slice(0, expanded ? 20 : 5).map(row => { const links = p.notebook.content.catalyst_links.filter(c => c.evidence_id === row.row_reference?.id && c.source_row === row.row_reference?.source_row); return <article key={String(row.id)}><time>{String(row.published_at).slice(11, 16)}</time><div><a href={String(row.source_url)} target="_blank" rel="noreferrer">{text(row.title)} ↗</a>{expanded && <p>{text(row.summary)}</p>}<div className="dr-news-links">{links.map(c => <span key={c.sector_code}>{p.data.themes.items.find(t => t.ts_code === c.sector_code)?.name ?? c.sector_code}<button aria-label="移除催化关联" disabled={!p.notebook.writable} onClick={() => p.notebook.update({ ...p.notebook.content, catalyst_links: p.notebook.content.catalyst_links.filter(a => a !== c) })}>×</button></span>)}</div></div><RefButton row={row} onOpen={p.openRef} label="查看电报原始记录"/><button disabled={!p.notebook.writable || !p.sector || p.notebook.content.catalyst_links.length >= 20} onClick={() => link(row)} title="人工关联当前选中题材，不自动认定价格因果">关联题材</button></article>; })}</div>{!rows.length && <Empty>所选日期没有已保存的电报窗口。</Empty>}{rows.length > 5 && <button className="dr-load-more" onClick={() => setExpanded(!expanded)}>{expanded ? '收起' : `查看窗口全部${rows.length}条`}</button>}<p className="dr-note">{p.data.news.scope_note} · 仅人工建立关联，不自动判断催化因果。</p></>;
}

export function SelectedStock(p: ReviewProps) {
  const selected = p.tracked.find(s => s.ts_code === p.stock);
  const core = p.notebook.content.core_stocks.find(c => c.code === p.stock);
  return p.stock ? <div className="dr-stock-context"><b>{core?.name ?? selected?.name ?? String(p.selectedTheme?.member_records.find(r => r.con_code === p.stock)?.name ?? p.stock)}</b><small>{p.stock}</small><span className={tone(selected?.quote?.pct_chg)}>{pct(selected?.quote?.pct_chg)}</span><span>收盘 {text(selected?.quote?.close)}</span><span>成交 {selected?.quote?.amount == null ? '—' : amount(Number(selected.quote.amount) * 1000)}</span><RefButton row={selected?.quote} onOpen={p.openRef}/><button disabled={!p.notebook.writable || !!core || !selected?.quote || p.notebook.content.core_stocks.length >= 30} onClick={() => p.addCore(p.stock, selected?.name ?? p.stock)}>加入核心跟踪 ＋</button><button onClick={() => p.go('lhb')}>核对龙虎榜 →</button></div> : <p className="dr-note">点击梯队个股可联动报价、核心跟踪和龙虎榜。</p>;
}

export function LhbPreview(p: ReviewProps) {
  const query = new URLSearchParams({ trade_day: p.day, ...(p.asOf ? { as_of_time: p.asOf } : {}), ...(p.stock ? { stock: p.stock } : {}) });
  const read = useBoundRead(`lhb-preview:${query}`, () => p.stock ? api.reviewLhb(query) : Promise.resolve(null));
  const record = read.data?.data.records[0];
  return <>{read.busy && p.stock && !record ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : record ? <><h3>{text(record.source_rows[0]?.name)} · {record.ts_code}</h3><p className="dr-note">{record.reason}</p><div className="dr-three-values"><section><small>买入侧前五合计</small><b className="rise">{amount(record.buy_top5.amount_yuan)}</b></section><section><small>卖出侧前五合计</small><b className="fall">{amount(record.sell_top5.amount_yuan)}</b></section><section><small>原榜净额</small><b className={tone(record.headline.net_amount)}>{amount(record.headline.net_amount)}</b></section></div><button onClick={() => p.select({ view: 'lhb', record_key: record.record_key })}>核对这条披露 →</button></> : <Empty>{p.stock ? '所选股票当日没有已披露的龙虎榜记录。' : '先选择个股，再查看相应龙虎榜证据。'}</Empty>}<p className="dr-note">不同原因和区间分开查看，席位标签不等于唯一交易者。</p></>;
}

function ThemeEvolution(p: ReviewProps) {
  const value = p.themeHistory;
  const days = value?.items[0]?.records.map(r => r.trade_day) ?? [];
  return <>{p.themeHistoryBusy && !value ? <Loading/> : p.themeHistoryError ? <Failure error={p.themeHistoryError} retry={p.reloadThemeHistory}/> : value?.items.length ? <Table label="题材近五日演变"><thead><tr><th>题材</th>{days.map(d => <th key={d}>{d.slice(5)}</th>)}</tr></thead><tbody>{value.items.map(item => <tr key={item.ts_code}><td><button className="dr-text-button" onClick={() => p.select({ sector: item.ts_code })}>{p.data.themes.items.find(t => t.ts_code === item.ts_code)?.name ?? item.ts_code}</button></td>{item.records.map(r => { const stage = p.themeStagesByDay[r.trade_day]?.find(s => s.sector_code === item.ts_code)?.stage ?? '未标记'; return <td key={r.trade_day}><button className="dr-stage" onClick={() => p.select({ trade_day: r.trade_day, sector: item.ts_code, edit: p.notebook.writable ? 'directions' : null })} disabled={!!p.asOf}>{stage}</button><small className={tone(r.catalog_record?.pct_change)}>{pct(r.catalog_record?.pct_change)}</small></td>; })}</tr>)}</tbody></Table> : <Empty>先选择题材或填写方向定位。</Empty>}<p className="dr-note">涨幅来自原日目录；阶段为人工标记。尚未标记时不自动推断“启动”或“发酵”。</p></>;
}

function InternalLadder(p: ReviewProps) {
  const [limit, setLimit] = useState(10);
  const theme = p.selectedTheme;
  const quotes = new Map((theme?.stock_records ?? []).map(r => [String(r.ts_code), r]));
  const pools = new Map(p.data.pools.records.map(r => [String(r.ts_code), r]));
  const members = (theme?.member_records ?? []).filter(r => /^(?:6\d{5}\.SH|[03]\d{5}\.SZ)$/.test(String(r.con_code)) && (!p.mine || p.watched.has(String(r.con_code)))).sort((a, b) => {
    const height = (row: Row) => { const pool = pools.get(String(row.con_code)); return pool?.limit === 'U' ? number(pool.limit_times) ?? 0 : 0; };
    return height(b) - height(a) || (number(quotes.get(String(b.con_code))?.pct_chg) ?? -Infinity) - (number(quotes.get(String(a.con_code))?.pct_chg) ?? -Infinity);
  });
  return <>{p.detailBusy && !theme ? <Loading/> : !theme?.detail_available ? <Empty>该原日题材成员尚未取得。不会用当前成员替代历史。</Empty> : <Table label="选中题材内部梯队" className="dr-medium-scroll"><thead><tr><th>高度</th><th>个股</th><th>涨幅</th><th>研究角色</th><th>操作</th><th>证据</th></tr></thead><tbody>{members.slice(0, limit).map(r => { const code = String(r.con_code); const pool = pools.get(code); const quote = quotes.get(code); const core = p.notebook.content.core_stocks.find(c => c.code === code); return <tr key={code} className={p.stock === code ? 'chosen' : ''}><td className="gold">{pool?.limit === 'U' ? `${text(number(pool.limit_times))}板` : '—'}</td><td><button className="dr-text-button" onClick={() => p.select({ stock: code, record_key: null })}>{text(r.name)}<small>{code}</small></button></td><td><Value value={quote?.pct_chg} percentage/></td><td>{core?.role || '未设角色'}</td><td><button disabled={!p.notebook.writable || !!core || p.notebook.content.core_stocks.length >= 30} onClick={() => p.addCore(code, String(r.name ?? code))}>{core ? '已跟踪' : '加入跟踪 ＋'}</button></td><td><RefButton row={quote ?? r} onOpen={p.openRef} label={`查看${text(r.name)}原日证据`}/></td></tr>; })}</tbody></Table>}{members.length > limit && <button className="dr-load-more" onClick={() => setLimit(limit + 30)}>查看更多成员（{Math.min(limit, members.length)}/{members.length}）</button>}<p className="dr-note">已披露沪深成员 {text(theme?.member_sample.disclosed_member_n)} · 日线匹配 {text(theme?.member_sample.matched_daily_n)} · 缺报价 {text(theme?.member_sample.missing_quote_n)}{theme?.truncated ? ' · 详情已截断，请核对原表' : ''}</p><SelectedStock {...p}/></>;
}

export function Direction(p: ReviewProps) {
  const d = p.notebook.content;
  return <><div className="dr-direction-top"><Card number="01" title="方向定位" action={<button disabled={!p.notebook.writable} onClick={() => p.edit('directions')}>编辑 ✎</button>}><div className="dr-position-grid">{slots.map(slot => { const item = d.directions.find(a => a.slot === slot); return <button key={slot} className={`dr-position ${item?.sector_code === p.sector ? 'chosen' : ''}`} disabled={!item} onClick={() => item && p.select({ sector: item.sector_code })}><span>{slot}</span><strong>{item?.name ?? '尚未指定'}</strong><small>{item?.rationale || '由本人选择并填写依据'}</small></button>; })}</div><div className="dr-judgment"><b>整体判断</b><p className="dr-preserved">{d.judgment || '先核对题材表现与内部梯队，再记录判断。'}</p><button disabled={!p.notebook.writable} onClick={() => p.edit('directions')}>✎</button></div></Card><Card number="02" title="题材演变"><ThemeEvolution {...p}/></Card><Card number="03" title="题材表现与联动"><ThemeTable {...p}/></Card></div>
  <div className="dr-direction-middle"><Card number="04" title={`题材内部梯队${p.selectedTheme ? ' · ' + p.selectedTheme.name : ''}`}><InternalLadder {...p}/><div className="dr-evidence-pair"><div><h3>支持证据</h3><p className="dr-preserved">{d.support_evidence || '尚未填写。'}</p></div><div><h3>相反证据</h3><p className="dr-preserved">{d.refuting_evidence || '尚未填写。'}</p></div></div><button className="dr-load-more" disabled={!p.notebook.writable} onClick={() => p.edit('directions')}>补充支持与反证 ✎</button></Card><Card number="05" title="核心个股跟踪" action={<small>人工角色 · 与龙虎榜联动</small>}><CoreTable {...p}/></Card></div>
  <div className="dr-direction-bottom"><Card number="06" title="候选催化与题材映射"><Catalysts {...p}/></Card><Card number="07" title="龙虎榜证据引用"><LhbPreview {...p}/></Card><Card number="08" title="我的观察清单"><WatchQuestions {...p}/></Card></div></>;
}
