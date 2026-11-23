import { useCallback, useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Card, ErrorState, Segmented } from '../../shared/ui';
import { Icon } from '../../shared/icons';
import { Watchlist } from '../workbench/Watchlist';
import { useTradingContext, useWatchlist } from '../workbench/TradingContext';
import { StockDetails } from '../workbench/StockDetails';
import { HistoryChart } from './HistoryChart';
import '../workbench/tradingWorkbench.css';

const decimal = (value: string | null | undefined) => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 4 });
const compact = (value: string | null | undefined) => value == null ? '—' : Math.abs(Number(value)) >= 1e8 ? (Number(value) / 1e8).toFixed(2) + '亿' : Math.abs(Number(value)) >= 1e4 ? (Number(value) / 1e4).toFixed(2) + '万' : decimal(value);
const signed = (value: string | null | undefined, suffix = '') => value == null ? '—' : (Number(value) > 0 ? '+' : '') + Number(value).toFixed(2) + suffix;
const direction = (value: string | null | undefined) => Number(value) > 0 ? 'quote-positive' : Number(value) < 0 ? 'quote-negative' : '';
const exchanges = { SSE: '沪市', SZSE: '深市', BSE: '北交所' };
const assets = { stock: '股票', index: '指数', etf: 'ETF', convertible: '转债' };
const states = { not_configured: '行情未接入', disabled: '来源未启用', permission_missing: '使用范围未核实', empty: '暂无报价', unavailable: '报价不可用', partial: '字段不完整', stale: '较早报价', available: '已保存报价', daily_close: '盘后收盘 · 非实时', delayed_reference: '盘中参考 · 约5分钟更新' };

export function TradingQuote() {
  const trading = useTradingContext();
  const watch = useWatchlist();
  const selected = trading.instrumentId;
  const read = useRead(useCallback(() => api.marketQuotes(selected), [selected]));
  useEffect(() => { const refresh = () => { if (document.visibilityState === 'visible') read.reload(); }; const timer = setInterval(refresh, 30_000); document.addEventListener('visibilitychange', refresh); return () => { clearInterval(timer); document.removeEventListener('visibilitychange', refresh); }; }, [read.reload]);
  const [period, setPeriod] = useState('日K');
  const [details, setDetails] = useState(false);
  useEffect(() => setDetails(false), [selected]);
  const data = read.data?.data;
  const row = !read.error && (selected ? data?.selection === 'instrument' && data.items[0]?.instrument.id === selected : data?.selection === 'indices') ? data?.items[0] : undefined;
  const identity = row?.instrument;
  const id = selected ?? identity?.id ?? undefined;
  const key = JSON.stringify([id, trading.day]);
  const basics = useRead(useCallback(async () => ({ key, value: id ? await api.tradingDatasets('tushare.daily_basic', 'tushare_daily_basic', trading.day || undefined, id) : null }), [key, id, trading.day]));
  const limits = useRead(useCallback(async () => ({ key, value: id ? await api.tradingDatasets('tushare.stk_limit', 'tushare_stk_limit', trading.day || undefined, id) : null }), [key, id, trading.day]));
  const history = useRead(useCallback(async () => ({ key, value: id && trading.day ? await api.marketBars(id, 'unadjusted') : null }), [key, id, trading.day]));
  const bars = !history.error && history.data?.key === key && history.data.value?.data.instrument.id === id ? history.data.value?.data.items : undefined;
  const barItem = bars?.find(item => item.payload.trade_day === trading.day);
  const bar = barItem?.payload;
  const previous = bars?.filter(item => item.payload.trade_day && item.payload.trade_day < trading.day).sort((a,b) => a.payload.trade_day!.localeCompare(b.payload.trade_day!)).at(-1)?.payload.close;
  const payload = trading.day ? bar : row?.quote;
  const price = trading.day ? bar?.close : row?.quote?.price;
  const historicalChange = bar && previous != null && Number(previous) > 0 ? Number(bar.close) - Number(previous) : null;
  const change = trading.day ? historicalChange?.toString() : row?.change;
  const changePercent = trading.day ? historicalChange == null ? null : (historicalChange / Number(previous) * 100).toString() : row?.change_percent;
  const label = read.error ? '报价读取失败' : row ? states[row.state] : read.busy ? '正在读取…' : '暂无报价';
  const basicData = !basics.error && basics.data?.key === key ? basics.data?.value?.data.items.find(item => item.instrument_id === id) : undefined;
  const basicDay = trading.day || basicData?.trade_day;
  const basicRow = (basicData?.view_rows ?? basicData?.payload.rows)?.filter(item => item.ts_code?.startsWith(identity?.symbol ?? '\0') && (!basicDay || item.trade_date?.replaceAll('-','') === basicDay.replaceAll('-','')))
    .sort((a, b) => (b.trade_date ?? '').localeCompare(a.trade_date ?? ''))[0];
  const limitData = !limits.error && limits.data?.key === key ? limits.data?.value?.data.items.find(item => item.instrument_id === id) : undefined;
  const date = trading.day || row?.quote?.trade_day || '';
  const limitRow = limitData?.payload.rows.find(item => item.ts_code?.startsWith(identity?.symbol ?? '\0') && item.trade_date?.replaceAll('-', '') === date.replaceAll('-', ''));
  const fields: [string, string][] = [['今开', decimal(payload?.open)], ['最高', decimal(payload?.high)], ['最低', decimal(payload?.low)], ['成交量（股）', compact(payload?.volume_shares)], ['成交额（元）', compact(payload?.amount_cny)]];
  const previousClose = trading.day ? previous : row?.quote?.previous_close;
  if (previousClose != null) fields.unshift(['昨收', decimal(previousClose)]);
  if (basicRow?.turnover_rate != null) fields.push(['换手率', decimal(basicRow.turnover_rate) + '%']);
  if (limitRow?.up_limit != null) fields.push(['涨停价', decimal(limitRow.up_limit)]);
  if (limitRow?.down_limit != null) fields.push(['跌停价', decimal(limitRow.down_limit)]);
  const saved = watch?.items.some(item => item.instrument.id === id);
  const stamp = row?.quote?.quote_time ? new Date(row.quote.quote_time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : (row?.quote?.trade_day ?? '日期未知') + (row?.state === 'delayed_reference' ? ' · 源时分秒未知' : ' · 盘后收盘');
  return <Card title="行情与个股观察" className="quote-card trading-quote" action={<span className="badge muted">{trading.day ? trading.day + ' 回看' : label}</span>}>
    <div className="trading-quote-body"><Watchlist/><div className="trading-instrument">
      <div className="trading-instrument-header"><div className="trading-symbol"><h3>{identity?.name ?? (selected ? '所选标的' : '上证指数')}</h3><small>{identity ? exchanges[identity.exchange] + ' · ' + assets[identity.asset_type] + ' · ' + identity.symbol : '正在读取'}</small></div>
        <div className="trading-quote-actions"><button type="button" disabled={!selected} onClick={() => setDetails(true)}>个股资料</button><button type="button" disabled={!id || !watch || watch.saving || watch.busy} onClick={() => id && void (saved ? watch?.remove(id) : watch?.add(id))}>{saved ? '已加自选 ✓' : '＋ 自选'}</button><button type="button" aria-label="刷新报价" onClick={read.reload} disabled={read.busy}><Icon name="refresh"/></button></div></div>
      {read.error && <ErrorState error={read.error} retry={read.reload}/>}
      <div className="trading-quote-ticker"><div className={'quote-price ' + direction(change)}><strong>{price == null ? '—' : Number(price).toLocaleString('en-US', { minimumFractionDigits: identity?.asset_type === 'etf' ? 3 : 2, maximumFractionDigits: identity?.asset_type === 'etf' ? 3 : 2 })}<small>{price != null && (identity?.asset_type === 'index' ? ' 点' : ' 元')}</small></strong><span>{signed(change) + '　' + signed(changePercent, '%')}{trading.day && <small>当日收盘 · 未复权</small>}</span></div><dl>{fields.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl></div>
      <div className="trading-quote-meta"><span>{trading.day ? (bar ? trading.day + ' · 日线保存记录' : '所选日期没有保存的日线报价') : stamp + ' · ' + (row?.provenance?.source_name ?? label)}</span>{basicRow && <span>换手及估值：{basicRow.trade_date}</span>}</div>
      <div className="chart-toolbar trading-periods"><Segmented label="图表周期" options={['分时', '日K', '周K', '月K']} value={['分时', '日K', '周K', '月K'].includes(period) ? period : ''} onChange={setPeriod}/><label>分钟K<select aria-label="分钟K周期" value={period.endsWith('分钟') ? period : ''} onChange={event => event.target.value && setPeriod(event.target.value)}><option value="">选择周期</option>{['1分钟', '5分钟', '15分钟', '30分钟', '60分钟'].map(item => <option key={item}>{item}</option>)}</select></label></div>
      <HistoryChart instrument={id} period={period} day={trading.day} onDayChange={trading.setDay}/>
      <details className="trading-quote-details"><summary>报价来源与数据详情</summary><div className="trading-fundamental-summary">{[['市盈率', basicRow?.pe], ['市净率', basicRow?.pb], ['每股收益', basicRow?.eps]].filter(([, value]) => value != null).map(([name, value]) => <span key={name}>{name} <b>{decimal(value)}</b></span>)}<a href={'/research/plans' + (id ? '?instrument=' + encodeURIComponent(id) : '')}>笔记与预案 ›</a></div>{!trading.day && row?.provenance && <div><p>来源：{row.provenance.source_name} · 数据时点：{stamp} · 接收：{new Date(row.provenance.received_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</p><p>来源发布时间：{row.provenance.published_at ? new Date(row.provenance.published_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未知'} · 只能用于本机接收之后的研究</p><p>来源版本：{row.provenance.source_version_id} · {row.provenance.adapter_version}</p><p>记录校验：<code>{row.provenance.content_hash}</code></p><p>原始开盘：{row.quote?.open ?? '—'} · 原始最高：{row.quote?.high ?? '—'} · 原始最低：{row.quote?.low ?? '—'}</p><a href={'/data?view=materials&evidence=' + encodeURIComponent(row.provenance.evidence_version_id)}>查看原始数据版本 ›</a></div>}{trading.day && barItem && <div><p>日线来源：{history.data?.value?.data.source_name ?? '未知'} · 数据日：{bar?.trade_day} · 接收：{new Date(barItem.received_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</p><p>原始开盘：{bar?.open} · 原始最高：{bar?.high} · 原始最低：{bar?.low}</p><p>记录校验：<code>{barItem.content_hash}</code></p><a href={'/data?view=materials&evidence=' + encodeURIComponent(barItem.id)}>查看原始日线版本 ›</a></div>}<p>报价、量能与估值按各自数据日期展示。未提供的字段保持缺失。</p>{!trading.day && row?.state === 'delayed_reference' && <p>约5分钟更新的参考报价，源时分秒未知，不能作为实时快照或严格历史依据。</p>}</details>
    </div></div>{details && <StockDetails name={identity?.name ?? '所选股票'} close={() => setDetails(false)}/>}
  </Card>;
}
