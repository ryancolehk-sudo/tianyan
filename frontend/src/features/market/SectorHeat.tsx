import { useCallback, useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Empty, ErrorState, Segmented } from '../../shared/ui';
import { Amount, DatasetMeta, DatasetRaw, DatasetStatus, StockCell, TradingTable, Value, useTradingDataset } from '../workbench/TradingWidgets';

const dataset = 'market.ashare.sector_heat';

export function SectorHeat() {
  const read = useRead(useCallback(() => api.datasets(dataset), []));
  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible') read.reload(); };
    const timer = window.setInterval(refresh, 60_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [read.reload]);
  const record = !read.error ? read.data?.data.items.find(row => row.family === 'ashare_sector_heat' && row.payload.dataset === dataset) : undefined;
  const rows = record?.payload.rows ?? [];
  return <section className="sector-heat" aria-label="热门板块关注排名" aria-busy={read.busy}>
    <div className="sector-heat-heading"><strong>供应商关注排名</strong><button type="button" disabled={read.busy} onClick={read.reload}>刷新缓存</button></div>
    <p className="feature-note">关注热度辅助参考 · 包含海外板块 · 不作为涨幅或资金排名</p>
    {read.error ? <ErrorState error={read.error} retry={read.reload}/> : read.busy && !record ? <p role="status">正在读取板块参考…</p> : !record ? <Empty title="热门板块尚未采集">接入后读取本机缓存，配置与采集状态见数据中心。</Empty> : !rows.length ? <Empty title="该轮榜单没有条目">供应商返回空列表，未补造板块或排名。</Empty> : <>
      <ol className="sector-heat-list">{rows.slice(0, 8).map(row => <li key={row.symbol}>
        <span className="sector-heat-rank" aria-label={'原始排名 ' + row.rank}>{row.rank}</span>
        <span className="sector-heat-name"><strong>{row.name}</strong><small>{row.symbol} · {row.source_type}</small></span>
        <span className="sector-heat-change"><small>排名变化原值</small>{row.rank_delta ?? '—'}</span>
      </li>)}</ol>
      <p className="feature-note">显示 {Math.min(rows.length, 8)} / {rows.length} 条已存记录；顺序按供应商关注排名。</p>
      <details className="sector-heat-source"><summary>查看来源与时间</summary><p>{record.payload.attribution}</p><p>榜单时间原文：{[...new Set(rows.map(row => row.ranking_updated_raw ?? '未知'))].join(' / ')}（时区未注明）</p><p>服务器首次接收：{new Date(record.received_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}（北京时间）</p><p>{record.payload.scope}</p></details>
    </>}
    <a className="more-link" href="/data?view=datasets">查看完整表格与来源状态 ›</a>
  </section>;
}

export function SectorMarkets() {
  const [kind,setKind] = useState('行业');
  const [chosen,setChosen] = useState<string | null>(null);
  const [query,setQuery] = useState('');
  const [descending,setDescending] = useState(true);
  const boards = useTradingDataset(`market.eastmoney.${kind === '行业' ? 'industry' : 'concept'}`,'eastmoney_boards');
  const members = useTradingDataset('market.eastmoney.constituents','eastmoney_boards');
  const record = boards.record;
  const rows = (record?.payload.rows ?? []).filter(row => !query.trim() || [row.name,row.code,row.symbol].some(value => value?.includes(query.trim()))).sort((a,b) => {
    const av = a.change_percent == null ? null : Number(a.change_percent), bv = b.change_percent == null ? null : Number(b.change_percent);
    if (av === null || !Number.isFinite(av)) return 1;
    if (bv === null || !Number.isFinite(bv)) return -1;
    return (av - bv) * (descending ? -1 : 1);
  });
  const board = rows.find(row => (row.code ?? row.symbol) === chosen);
  const memberRecord = members.items.find(item => item.source_id === record?.source_id && item.payload.rows.some(row => row.board_code === chosen));
  const memberRows = (memberRecord?.payload.rows ?? []).filter(row => row.board_code === chosen);
  return <section className="trading-widget" aria-label="行业与概念行情"><div className="trade-toolbar"><Segmented label="板块类型" options={['行业','概念']} value={kind} onChange={value => { setKind(value); setChosen(null); }}/><button onClick={boards.reload} disabled={boards.busy}>刷新</button></div><DatasetMeta record={record} kind="板块行情快照" warning={boards.warning}/><div className="trade-toolbar"><input type="search" aria-label="查找板块" placeholder="板块名称 / 代码" value={query} onChange={event => setQuery(event.target.value)}/></div><DatasetStatus busy={boards.busy} error={boards.error} empty={!record} retry={boards.reload}>{record && <><div className="trade-table-scroll trade-core-table" tabIndex={0} role="region" aria-label="板块涨跌排名"><table className="trade-table"><thead><tr><th className="trade-left">板块</th><th aria-sort={descending ? 'descending' : 'ascending'}><button onClick={() => setDescending(!descending)}>涨幅 {descending ? '↓' : '↑'}</button></th><th>成交额</th><th>涨 / 跌家数</th><th className="trade-left">领涨股</th></tr></thead><tbody>{rows.map((row,index) => <tr key={row.code ?? row.symbol ?? index}><td className="trade-left"><button className="trade-reason-button" onClick={() => setChosen(row.code ?? row.symbol)}>{row.name}<small>{row.code ?? row.symbol} · 查看成分 ›</small></button></td><td><Value value={row.change_percent} signed suffix="%"/></td><td><Amount record={record} field="amount_cny" value={row.amount_cny}/></td><td>{row.up_count ?? '—'} / {row.down_count ?? '—'}</td><td className="trade-left">{row.lead_symbol ? <StockCell row={{symbol:row.lead_symbol,name:row.lead_name}}/> : '—'}</td></tr>)}</tbody></table></div><DatasetRaw record={record}/></>}</DatasetStatus>{chosen && <section className="trade-board-members"><div className="trade-toolbar"><strong>{board?.name ?? chosen} · 已存成分</strong><button onClick={() => setChosen(null)}>收起</button></div><DatasetStatus busy={members.busy} error={members.error} empty={!memberRows.length} retry={members.reload}>{memberRecord && <TradingTable key={chosen} record={memberRecord} rows={memberRows} label="板块成分股" columns={[{key:'symbol',label:'股票',type:'stock'},{key:'change_percent',label:'涨幅',type:'percent'},{key:'amount_cny',label:'成交额',type:'amount'}]} initialSort="change_percent"/>}</DatasetStatus><p className="trade-meta">仅展示来源已采集、明确关联当前板块的成分；缺失不代表没有成分股。</p></section>}<details className="trade-details"><summary>关注热度辅助</summary><SectorHeat/></details></section>;
}
