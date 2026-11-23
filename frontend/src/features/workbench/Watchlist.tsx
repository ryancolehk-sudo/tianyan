import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { InstrumentListResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { useTradingContext, useWatchlist } from './TradingContext';

export function Watchlist() {
  const watch = useWatchlist();
  const trading = useTradingContext();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<InstrumentListResponse['data']['items']>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const request = useRef(0);
  const ids = watch?.items.map(item => item.instrument.id).filter((id): id is string => !!id) ?? [];
  const key = ids.join(',');
  const quotes = useRead(useCallback(() => key ? api.marketQuotesBatch(key.split(',')) : Promise.resolve(null), [key]));
  useEffect(() => { if (!key) return;
    const refresh = () => { if (document.visibilityState === 'visible') quotes.reload(); };
    const timer = setInterval(refresh, 30_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [key, quotes.reload]);
  useEffect(() => () => { request.current++; }, []);
  async function search(event: FormEvent) {
    event.preventDefault(); if (!query.trim()) return;
    const ticket = ++request.current; setSearching(true); setError(''); setResults([]);
    try { const value = await api.instruments(query.trim()); if (ticket === request.current) setResults(value.data.items); }
    catch (reason) { if (ticket === request.current) setError(reason instanceof Error ? reason.message : '搜索失败，请重试。'); }
    finally { if (ticket === request.current) setSearching(false); }
  }
  async function add(id: string) { await watch?.add(id); setResults([]); setQuery(''); }
  function move(index: number, delta: number) { const order = [...ids]; [order[index], order[index + delta]] = [order[index + delta], order[index]]; void watch?.reorder(order); }
  return <aside className="trading-watchlist" aria-label="自选股票">
    <div className="trading-watch-heading"><strong>自选</strong><small>{ids.length}</small><button type="button" onClick={() => setEditing(value => !value)} aria-pressed={editing}>{editing ? '完成' : '管理'}</button></div>
    <form className="watch-search" onSubmit={event => void search(event)}><label className="sr-only" htmlFor="watch-search">添加自选股票</label><input id="watch-search" placeholder="代码 / 名称" value={query} maxLength={80} onChange={event => setQuery(event.target.value)}/><button type="submit" disabled={searching || !query.trim()}>添加</button></form>
    {searching && <p role="status">正在搜索…</p>}
    {!!results.length && <div className="watch-search-results" role="region" aria-label="添加自选搜索结果">{results.map(item => <button type="button" key={item.id} disabled={watch?.saving || ids.includes(item.id)} onClick={() => void add(item.id)}><strong>{item.name}</strong><small>{item.symbol} · {item.exchange} · {item.asset_type === 'index' ? '指数' : '证券'}</small><span>{ids.includes(item.id) ? '已添加' : '+'}</span></button>)}<button type="button" onClick={() => setResults([])}>关闭结果</button></div>}
    {(watch?.error || error) && <p className="watch-error" role="alert">{watch?.error || error}<button type="button" onClick={() => watch?.reload()}>重试</button></p>}
    {watch?.busy ? <p role="status">读取自选…</p> : !ids.length ? <div className="watch-empty">添加经常观察的股票<br/><small>支持代码或名称搜索</small></div> : <div className="watch-rows" role="list" aria-label="自选列表">{watch?.items.map((item, index) => {
      const row = !quotes.error ? quotes.data?.data.items.find(row => row.instrument.id === item.instrument.id) : undefined;
      const pct = row?.change_percent == null ? null : Number(row.change_percent);
      return <div className={'watch-row' + (trading.instrumentId === item.instrument.id ? ' is-current' : '')} role="listitem" key={item.instrument.id}>
        <button type="button" className="watch-select" onClick={() => item.instrument.id && trading.selectInstrument(item.instrument.id)} aria-pressed={trading.instrumentId === item.instrument.id}>
          <span><strong>{item.instrument.name}</strong><small>{item.instrument.symbol}</small></span><span className={pct == null ? '' : pct > 0 ? 'quote-positive' : pct < 0 ? 'quote-negative' : ''}><strong>{row?.quote?.price == null ? '—' : Number(row.quote.price).toFixed(item.instrument.asset_type === 'etf' ? 3 : 2)}</strong><small>{pct == null ? '暂无报价' : (pct > 0 ? '+' : '') + pct.toFixed(2) + '%'}</small></span>
        </button>{editing && <div className="watch-order"><button type="button" disabled={!index || watch.saving} aria-label={'上移' + item.instrument.name} onClick={() => move(index, -1)}>↑</button><button type="button" disabled={index === ids.length - 1 || watch.saving} aria-label={'下移' + item.instrument.name} onClick={() => move(index, 1)}>↓</button><button type="button" disabled={watch.saving} aria-label={'删除自选' + item.instrument.name} onClick={() => item.instrument.id && void watch.remove(item.instrument.id)}>删除</button></div>}
        {row && <small className="watch-quote-time">{row.quote?.trade_day ?? '日期未知'} · {row.state === 'daily_close' ? '盘后' : row.state === 'delayed_reference' ? '延迟' : row.state === 'stale' ? '较早' : row.quote?.quote_time ? new Date(row.quote.quote_time).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '保存报价'}</small>}
      </div>;
    })}</div>}
    {quotes.error && <p className="watch-error" role="status">报价读取失败<button type="button" onClick={quotes.reload}>重试</button></p>}
    <small className="watch-caption">{watch?.saving ? '正在保存…' : '按账号保存 · 列表显示最新报价'}</small>
  </aside>;
}
