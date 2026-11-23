import { useCallback, useEffect } from 'react';
import type { MarketQuote } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { ErrorState } from '../../shared/ui';
import { TradingQuote } from './TradingQuote';
import { MarketBreadth } from './Breadth';
import { BreadthHistory } from './BreadthHistory';
import './marketRibbon.css';
import './homepageNotes.css';

const indexTiles = [
  { name: '上证指数', exchange: 'SSE', symbol: '000001' },
  { name: '深证成指', exchange: 'SZSE', symbol: '399001' },
  { name: '创业板指', exchange: 'SZSE', symbol: '399006' },
  { name: '科创50', exchange: 'SSE', symbol: '000688' },
] as const;
const labels: Record<MarketQuote['state'], string> = { not_configured: '行情未接入', disabled: '来源未启用', permission_missing: '使用范围未核实', empty: '尚无保存报价', unavailable: '报价不可用', partial: '报价字段不完整', stale: '较早报价', available: '已保存报价', daily_close: '盘后收盘 · 非实时', delayed_reference: '盘中参考 · 约5分钟更新' };

function quoteTime(row: MarketQuote) {
  return row.quote?.quote_kind === 'daily_close' ? `${row.quote.trade_day ?? '日期未知'} 盘后` : row.quote?.quote_kind === 'delayed_reference' ? `${row.quote.trade_day ?? '日期未知'}（源时分秒未知）` : time(row.quote?.quote_time);
}

export function decimalText(value: string | null | undefined) {
  if (value === null || value === undefined || !/^-?\d+(\.\d+)?$/.test(value)) return '—';
  const [integer, fraction] = value.split('.');
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction !== undefined ? '.' + fraction : '');
}
function signed(value: string | null | undefined, suffix = '') {
  if (value === null || value === undefined) return '—';
  const prefix = !value.startsWith('-') && /[1-9]/.test(value) ? '+' : '';
  return prefix + decimalText(value) + suffix;
}
function direction(value: string | null | undefined) {
  return value && /[1-9]/.test(value) ? value.startsWith('-') ? 'quote-negative' : 'quote-positive' : '';
}
function time(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未知';
}
function useQuotes(instrument?: string, day = '') {
  const key = JSON.stringify([instrument, day]);
  const read = useRead(useCallback(async () => {
    const end = day ? Date.parse(day + 'T23:59:59+08:00') : null;
    return { key, response: await api.marketQuotes(instrument, end === null ? undefined : new Date(Math.min(end, Date.now())).toISOString()) };
  }, [instrument, day, key]));
  useEffect(() => {
    // Poll the local cache only, preserve existing values while it refreshes,
    // and do no hidden-tab polling. No browser contacts a market provider.
    const refresh = () => { if (document.visibilityState === 'visible') read.reload(); };
    const timer = window.setInterval(refresh, 30_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [read.reload]);
  // A selection change must not momentarily show the previous instrument's quote.
  const response = read.data?.key === key ? read.data.response : undefined;
  const data = response?.data;
  const matches = instrument ? data?.selection === 'instrument' && data.items[0]?.instrument.id === instrument : data?.selection === 'indices';
  return { ...read, data: matches ? data : undefined };
}

export function MarketOverview() {
  const navigation = useNavigation();
  const day = navigation.path === '/workbench' ? new URLSearchParams(navigation.search).get('trade_day') ?? '' : '';
  const read = useQuotes(undefined, /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : '');
  const compact = navigation.path === '/auction' || navigation.path === '/workbench';
  const tiles = indexTiles.map(tile => {
      const row = !read.error ? read.data?.items.find(item => item.instrument.exchange === tile.exchange && item.instrument.symbol === tile.symbol && item.instrument.asset_type === 'index') : undefined;
      const details = row?.provenance ? `来源：${row.provenance.source_name}；行情时点：${quoteTime(row)}；首次接收：${time(row.provenance.received_at)}。只读保存记录。` : undefined;
      return <div className={row?.quote ? 'index-tile index-quote-tile' : 'index-tile'} key={`${tile.exchange}:${tile.symbol}`} title={details ? `${row ? labels[row.state] : ''}；${details}` : read.error ? '报价读取失败' : row ? labels[row.state] : '报价尚未读取'}>
        {row?.instrument.id ? <a href={'/workbench?instrument=' + encodeURIComponent(row.instrument.id)} onClick={event => { if (navigation.path === '/workbench') { event.preventDefault(); const url = new URL(location.href); url.searchParams.set('instrument', row.instrument.id!); updateLocation(url); } }}>{tile.name}</a> : <span>{tile.name}</span>}
        <strong className={direction(row?.change)}>{decimalText(row?.quote?.price)}</strong>
        {(row?.quote || compact) && <span className={'index-price-change ' + direction(row?.change)}>{signed(row?.change)}　{signed(row?.change_percent, '%')}</span>}
        <small className="index-state homepage-index-meta">{read.error ? '报价读取失败' : row ? labels[row.state] : '正在读取报价…'}</small>
        {row?.quote && <small className="index-data-time homepage-index-meta">数据时点：{row.quote.quote_kind !== undefined && row.quote.quote_kind !== 'snapshot' ? quoteTime(row) : row.quote.quote_time ? new Date(row.quote.quote_time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '未知'}</small>}
        {!row?.quote && <div className="index-spark-placeholder" aria-hidden="true"/>}
      </div>;
    });
  return <section className={`market-strip ${compact ? 'market-strip-auction' : ''} ${navigation.path === '/workbench' ? 'market-strip-workbench' : ''}`} aria-label="市场概览">
    {compact ? <div className="index-ribbon" aria-label="指数报价横栏">{tiles}</div> : tiles}
    <MarketBreadth compact={compact} workbench={navigation.path === '/workbench'} day={day}/>
    {compact && <details className="auction-market-meta"><summary>指数来源与时间</summary>{indexTiles.map(tile => {
      const row = !read.error ? read.data?.items.find(i => i.instrument.exchange === tile.exchange && i.instrument.symbol === tile.symbol) : undefined;
      return <p key={tile.symbol}>{tile.name}：{row ? labels[row.state] : read.error ? '报价读取失败' : '暂无可核验报价'} · {row ? quoteTime(row) : '行情时点未知'} · {row?.provenance?.source_name ?? '来源未知'} · 接收 {time(row?.provenance?.received_at ?? null)}</p>;
    })}</details>}
    {navigation.path === '/workbench' && <details className="workbench-market-history"><summary>涨跌停历史</summary><BreadthHistory/></details>}
    {read.error && <div className="market-overview-error"><ErrorState error={read.error} retry={read.reload}/></div>}
  </section>;
}

export function InstrumentQuote() { return <TradingQuote/>; }
