import { useEffect, useState } from 'react';
import { Card, Table, Loading, Empty, Distribution, Dialog, Value } from './components';
import { amount, feedbackCount, isIntradaySnapshot, metricLabel, pct, snapshotTime, text, turnover, stages } from './model';
import { distributionBands, legacyDistributionBands, getDistribution, hasDetailedDistribution } from './distribution';
import type { Cohort } from './model';
import type { ReviewProps } from './context';
import { reducedLimitRatio, sealingRate, sealRateTitle, yesterdayPerformanceTitle, limitRatioTitle, openedLimitTitle } from './emotionMetrics';
import { EmotionHistory } from './EmotionHistory';
import { BoardRatio } from './BoardRatio';
import { boardRatioTitle } from './boardRatios';
import { reviewLookback, usePreferences } from '../../shared/preferences';

export function Relay({ data, select }: Pick<ReviewProps, 'data' | 'select'>) {
  const [samples, showSamples] = useState<{ title: string; group: Cohort } | null>(null);
  const relay = data.metrics.relay;
  const snapshot = isIntradaySnapshot(data.metrics);
  const groups = relay ? [['昨日首板', relay.first_board], ['昨日连板', relay.consecutive], ['昨日炸板', relay.broken], ['昨日断板（核验）', relay.failed]] as const : [];
  return <><Table label="昨日固定样本今日反馈"><thead><tr><th>固定样本</th><th>今开中位</th><th>{snapshot ? '现价中位' : '今收中位'}</th><th>晋级 / 负反馈</th></tr></thead><tbody>{groups.map(([label, group]) => <tr key={label}><td><button className="dr-text-button" onClick={() => showSamples({ title: label, group })}>{label} {text(group.N)}只</button></td><td><Value value={group.open_median_pct} percentage/></td><td><Value value={group.close_median_pct} percentage/></td><td>{label.includes('首板') || label.includes('连板') ? <><b className="rise">{feedbackCount(group, 'promotion')}</b> · {pct(group.promotion_pct, false)}</> : <>跌停 {feedbackCount(group, 'down_limit')}</>}</td></tr>)}</tbody></Table>
    <p className="dr-note">{relay?.previous_trade_day ?? '—'}样本 → 当日反馈。缺报价保留N；中位数基于已知样本。</p>{relay?.failed_classification && <p className="dr-note">断板候选 {text(relay.failed_classification.N)} · 确认未续板 {text(relay.failed_classification.confirmed_failed_n)} · 待核 {text(relay.failed_classification.unknown_n)}</p>}
    {samples && <Dialog title={`${samples.title} · 今日反馈样本`} close={() => showSamples(null)}><p>固定N {text(samples.group.N)} · 有效开收 {text(samples.group.valid_n)} · 缺报价 {text(samples.group.missing_n)}。完整等权{snapshot ? '现价' : '收盘'}均值 {pct(samples.group.close_mean_pct)}；已知样本均值 {pct(samples.group.known_close_mean_pct)}。</p>{samples.group.samples?.length ? <Table label="固定跨日样本"><thead><tr><th>股票</th><th>昨日高度</th><th>今开</th><th>{snapshot ? '现价' : '今收'}</th><th>{snapshot ? '采样时点状态' : '当日收盘'}</th></tr></thead><tbody>{samples.group.samples.map(row => <tr key={String(row.ts_code)}><td><button onClick={() => { select({ stock: String(row.ts_code), view: 'direction' }); showSamples(null); }}>{text(row.ts_code)}</button></td><td>{text(row.previous_height)}</td><td><Value value={row.open_pct} percentage/></td><td><Value value={row.close_pct} percentage/></td><td>{row.up_limit === true ? '封板' : row.down_limit === true ? '跌停' : row.up_limit === null ? '限制价待核' : '未封板'}</td></tr>)}</tbody></Table> : <Empty>这组样本尚未加载，或原池没有该类披露。</Empty>}</Dialog>}
  </>;
}

export function Quantification({ data }: Pick<ReviewProps, 'data'>) {
  const m = data.metrics; const supplier = m.limits?.supplier_reported;
  const verified = m.limits?.price_verified;
  const upTurnover = verified?.up_turnover as { amount_yuan?: string | null; share_pct?: string | null; valid_n?: number; N?: number | null } | undefined;
  return <div className="dr-quant-grid"><section><small>{metricLabel(m, '最大收盘封单金额')}</small><strong className="gold">{amount(supplier?.max_disclosed_seal_amount_yuan)}</strong><p>单位及收盘时点未核</p></section><section><small>核验涨停成交额</small><strong className="gold">{amount(upTurnover?.amount_yuan)}</strong><p>沪深含ST · {text(upTurnover?.valid_n)}/{text(upTurnover?.N)}只有效</p></section><section title={limitRatioTitle}><small>短线池涨跌停比</small><strong className="gold">{reducedLimitRatio(supplier?.up, supplier?.down)}</strong><p>{metricLabel(m, '收盘')}涨停 {text(supplier?.up)} / 跌停 {text(supplier?.down)}</p></section><section><small>核验涨停成交占比</small><strong className="gold">{pct(upTurnover?.share_pct, false)}</strong><p>相同沪深范围 · 分母为市场成交额</p></section></div>;
}

function SummaryCells({ metrics, tradeDay }: { metrics: ReviewProps['data']['metrics']; tradeDay: string }) {
  const pool = metrics.limits?.supplier_reported;
  const yesterday = metrics.relay?.yesterday_limit_up;
  return <><td title={metricLabel(metrics, sealRateTitle)}>{pct(sealingRate(metrics), false)}</td>
    <td title={metricLabel(metrics, `${yesterdayPerformanceTitle} 收盘有效 ${text(yesterday?.close_valid_n)}/${text(yesterday?.N)}。`)}><Value value={yesterday?.close_mean_pct} percentage/></td>
    <td title={`${limitRatioTitle} 涨停 ${text(pool?.up)} / 跌停 ${text(pool?.down)}。`}>{reducedLimitRatio(pool?.up, pool?.down)}</td>
    <td className="dr-board-ratio-column"><BoardRatio metrics={metrics} tradeDay={tradeDay}/></td></>;
}

function OpenedLimitCells({ metrics }: { metrics: ReviewProps['data']['metrics'] }) {
  const intraday = metrics.limits?.intraday;
  return <>{(['up', 'down'] as const).map(side => <td key={side} className={side === 'up' ? 'rise' : 'fall'} title={metricLabel(metrics, `${openedLimitTitle} 已知 ${text(intraday?.[side === 'up' ? 'known_opened_up' : 'known_opened_down'])}；待核 ${text(intraday?.[side === 'up' ? 'up_unknown_n' : 'down_unknown_n'])}。`)}>{text(intraday?.[side === 'up' ? 'opened_up' : 'opened_down'])}</td>)}</>;
}

export function Emotion(p: ReviewProps) {
  const { data, series, seriesBusy, day, notebook: n, select, go, edit, stagesByDay } = p;
  const [settings, showSettings] = useState(false);
  const [columns, setColumns] = useState({ indices: true, breadth: true, ladder: true, themes: true });
  const [stageFilter, setStageFilter] = useState('全部周期');
  const { preferences } = usePreferences();
  const lookback = reviewLookback(new URLSearchParams(location.search), preferences);
  const records = (series?.records ?? []).filter(r => stageFilter === '全部周期' || (stagesByDay[r.trade_day] ?? '未标记') === stageFilter);
  const supplier = data.metrics.limits?.supplier_reported;
  const detailed = (series?.records ?? []).some(r => hasDetailedDistribution(r.metrics));
  const bands = detailed ? distributionBands : legacyDistributionBands;
  const scopeGroups=[
    {label:'日期与人工记录',span:2},{label:'市场概况',span:columns.indices?2:1},
    {label:'非ST短线池',span:columns.indices?4:0},
    {label:'沪深日线 · 含ST',span:(columns.breadth?3+bands.length:0)+(columns.ladder?2:0)},
    {label:'非ST短线池',span:columns.ladder?5:0},{label:'供应商题材',span:columns.themes?1:0},
  ].filter(group=>group.span>0);
  useEffect(() => {
    const panel = document.querySelector<HTMLElement>('.dr-history-scroll');
    const row = panel?.querySelector<HTMLElement>('tr[aria-selected="true"]');
    if (!panel || !row) return;
    const top = row.getBoundingClientRect().top - panel.getBoundingClientRect().top + panel.scrollTop;
    const headHeight=panel.querySelector('thead')?.getBoundingClientRect().height??38;
    if (top < panel.scrollTop + headHeight || top + row.offsetHeight > panel.scrollTop + panel.clientHeight) panel.scrollTop = Math.max(0, top + row.offsetHeight - panel.clientHeight);
  }, [series, day, stageFilter]);
  return <><Card title="情绪周期表" className="dr-emotion-table" action={<div className="dr-card-actions"><span>{series?.trade_days[0] ?? '—'} — {series?.trade_days.at(-1) ?? '—'}</span><label className="dr-inline-label">截至<input aria-label="情绪统计截至日期" type="date" value={series?.trade_days.at(-1) ?? day} onChange={e => e.target.value && select({ range_end: e.target.value, trade_day: e.target.value, record_key: null })}/></label><button className={lookback === '20' ? 'selected' : ''} onClick={() => select({ lookback: '20' })}>近20日</button><button className={lookback === '60' ? 'selected' : ''} onClick={() => select({ lookback: '60' })}>近60日</button><button onClick={() => showSettings(true)}>字段设置</button><select aria-label="情绪周期筛选" value={stageFilter} onChange={e => setStageFilter(e.target.value)}><option>全部周期</option>{stages.map(s => <option key={s}>{s}</option>)}</select></div>}>
    {seriesBusy && <Loading text="正在核算历史统计，60日窗口可能需要约20秒…"/>}
    {series && <Table label="情绪周期每日统计" className="dr-history-scroll">{scopeGroups.map((group,index)=><colgroup key={index} span={group.span}/>)}<thead><tr className="dr-scope-head">{scopeGroups.map((group,index)=><th key={index} scope="colgroup" colSpan={group.span}><span>{group.label}</span></th>)}</tr><tr className="dr-column-head"><th scope="col">日期</th><th scope="col">情绪阶段 · 人工</th><th scope="col">成交额</th>{columns.indices && <><th scope="col">上证</th><th scope="col" title={sealRateTitle}>封板率</th><th scope="col" title={yesterdayPerformanceTitle}>昨日涨停表现</th><th scope="col" title={limitRatioTitle}>涨跌停比</th><th scope="col" className="dr-board-ratio-column" title={boardRatioTitle}>连板比值</th></>}{columns.breadth && <><th scope="col">上涨</th><th scope="col">下跌</th><th scope="col">平盘</th>{bands.map(b => <th scope="col" key={b.key} title={b.title}>{b.label}</th>)}</>}{columns.ladder && <><th scope="col" title={openedLimitTitle}>曾涨停</th><th scope="col" title={openedLimitTitle}>曾跌停</th><th scope="col" title="供应商非ST短线池披露的炸板数；与含ST的曾涨停日线范围不同。">炸板（池）</th><th scope="col">首板</th><th scope="col">二板</th><th scope="col">三板+</th><th scope="col">最高</th></>}{columns.themes && <th scope="col">领涨题材 · 供应商</th>}</tr></thead><tbody>{records.map(r => { const s = r.metrics.limits?.supplier_reported; const stage = stagesByDay[r.trade_day] ?? '未标记'; return <tr key={r.trade_day} className={r.trade_day === day ? 'chosen' : ''} aria-selected={r.trade_day === day}><td><button className="dr-text-button" onClick={() => select({ trade_day: r.trade_day, record_key: null })}>{r.trade_day.slice(0, 4)}/{r.trade_day.slice(5).replace('-', '/')} <small>{new Date(r.trade_day + 'T12:00:00+08:00').toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', weekday: 'short' })}</small>{isIntradaySnapshot(r.metrics) && <span className="dr-snapshot-badge">盘中 {snapshotTime(r.metrics)}</span>}</button></td><td><button className={`dr-stage stage-${stages.indexOf(stage as typeof stages[number])}`} disabled={p.session.data.user.role === 'viewer' || !!p.asOf} onClick={() => select({ trade_day: r.trade_day, record_key: null, edit: 'market' })}>{stage} <span>✎</span></button></td><td>{turnover(r.metrics.turnover?.amount_yuan)}</td>{columns.indices && <><td><Value value={r.indices.find(i => i.ts_code === "000001.SH")?.record?.pct_chg} percentage/></td><SummaryCells metrics={r.metrics} tradeDay={r.trade_day}/></>}{columns.breadth && <><td className="rise">{text(r.metrics.breadth?.up)}</td><td className="fall">{text(r.metrics.breadth?.down)}</td><td>{text(r.metrics.breadth?.flat)}</td>{getDistribution(r.metrics, detailed).bins.map(b => <td key={b.key} title={b.title} style={{ color: b.color }}>{text(b.value)}</td>)}</>}{columns.ladder && <><OpenedLimitCells metrics={r.metrics}/><td>{text(s?.broken)}</td><td className="rise">{text(s?.first_board)}</td><td>{text(s?.second_board)}</td><td>{text(s?.third_plus)}</td><td className="gold">{text(s?.max_height)}</td></>}{columns.themes && <td>{r.themes.records[0] ? <button className="dr-text-button" onClick={() => select({ trade_day: r.trade_day, sector: String(r.themes.records[0].ts_code), view: 'direction' })}>{text(r.themes.records[0].name)}</button> : '—'}</td>}</tr>; })}</tbody></Table>}
    {!seriesBusy && !records.length && <Empty>当前窗口没有匹配的记录。人工阶段不会由涨跌幅自动代填。</Empty>}<p className="dr-note">标有“盘中”的行是已保存时点的样本，当前价、封板与反馈不代表收盘结果。其余日频行中，分布的涨停/跌停为收盘封板；曾涨停/曾跌停为盘中触板、收盘已打开，均按沪深日线含ST核验。封板率、涨跌停比及连板高度使用非ST短线池；昨日涨停表现为固定昨日池的今日等权涨幅。{detailed && ' 分布为十一档：真实涨跌停优先，其余按 3%、5%、7% 分档；0% 为平盘。'}</p>
  </Card><div className="dr-selected-day"><b>选中：{day}</b><span>情绪 {n.content.stage}</span><button onClick={() => go('overview')}>查看当日复盘 →</button><small>短线池 {text(supplier?.N)}只记录 · {metricLabel(data.metrics, '日线')}有效 {text(data.metrics.breadth?.valid_n)}</small></div>
  <EmotionHistory {...p}/><div className="dr-three-bottom"><Card title="涨跌分布明细"><Distribution metrics={data.metrics}/></Card><Card title="接力反馈" action={<small>{data.metrics.relay?.previous_trade_day} → {day.slice(5)}</small>}><Relay data={data} select={select}/><div className="dr-manual-line"><span className="dr-ai">AI点评 · 预留</span><p>{n.content.summary || '先核对样本反馈，再填写自己的判断。'}</p><button disabled={!n.writable} onClick={() => edit('market')}>✎</button></div></Card><Card title="情绪量化指标"><Quantification data={data}/></Card></div>
  {settings && <Dialog title="情绪表字段" close={() => showSettings(false)}><p>日期、人工阶段和成交额始终显示。</p>{Object.entries(columns).map(([key, checked]) => <label className="dr-checkbox" key={key}><input type="checkbox" checked={checked} onChange={e => setColumns({ ...columns, [key]: e.target.checked })}/>{{ indices: '上证与四项情绪指标', breadth: '市场广度与分布', ladder: '涨跌停与连板高度', themes: '供应商领涨题材' }[key]}</label>)}</Dialog>}
  </>;
}
