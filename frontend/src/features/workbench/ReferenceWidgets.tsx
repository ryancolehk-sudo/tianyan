import { useCallback, useState } from 'react';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Card, Empty } from '../../shared/ui';
import { PoolsReference } from '../market/MairuiReference';
import { useTradingContext } from './TradingContext';
import { DatasetMeta, DatasetRaw, DatasetStatus, LeaderboardWidget, TradingDatasetView, useTradingDataset } from './TradingWidgets';
import type { ReferenceWidgetDefinition } from './widgetCatalog';

function DatasetWidget({ definition, selectedOnly = false }: { definition: ReferenceWidgetDefinition; selectedOnly?:boolean }) {
  const { instrumentId, day } = useTradingContext();
  const [filter,setFilter] = useState('全部股票');
  const selected = selectedOnly || definition.scope === 'selected' || filter === '所选股票';
  const identityRead = useRead(useCallback(() => instrumentId ? api.marketQuotes(instrumentId) : Promise.resolve(null),[instrumentId]));
  const identity = !identityRead.busy && !identityRead.error ? identityRead.data?.data.items.find(row => row.instrument.id === instrumentId)?.instrument : undefined;
  const history = ['shibor','cpi','margin-detail'].includes(definition.id);
  const read = useTradingDataset(definition.dataset, definition.family, definition.scope === 'selected' ? instrumentId : undefined,undefined,history,definition.scope !== 'selected' || !!instrumentId);
  const record = read.record;
  const expected = definition.assetType ?? 'stock';
  const suffix = identity ? ({ SSE:'SH',SZSE:'SZ',BSE:'BJ' } as const)[identity.exchange] : '';
  const code = identity ? `${identity.symbol}.${suffix}` : '';
  const cutoff = day || read.data?.trade_day || new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());
  const rows = (record?.view_rows ?? record?.payload.rows ?? []).filter(row => {
    if (selected && (!identity || row.ts_code !== code)) return false;
    if (!history) return true;
    const date = (row.trade_date ?? row.date ?? row.month ?? '').replaceAll('-','');
    return /^\d{6}(?:\d{2})?$/.test(date) && date <= cutoff.replaceAll('-','').slice(0,date.length);
  });
  const unavailable = selected && (!instrumentId || !identity || identity.asset_type !== expected);
  return <div className="trading-widget" aria-label={definition.title + '资料组件'} data-testid={'workbench-widget-' + definition.id}>
    {definition.filterSelected && !selectedOnly && <div className="trade-toolbar"><label>范围 <select aria-label={definition.title + '查看范围'} value={filter} onChange={event => setFilter(event.target.value)}><option>全部股票</option><option>所选股票</option></select></label></div>}
    {unavailable ? <Empty title={!instrumentId ? '先选择一个标的' : identityRead.busy ? '正在核对所选标的…' : identityRead.error ? '标的身份读取失败' : `请选择${expected === 'index' ? '指数' : '股票'}`}>在首页搜索或点击股票名单，资料会随所选标的切换。{identityRead.error && <button onClick={identityRead.reload}>重试</button>}</Empty> : <>
      {identity && selected && <p className="trade-meta">{identity.name} · {code}</p>}
      <DatasetMeta record={record} kind={definition.id === 'auction' ? '竞价成交参考' : definition.id === 'ths-index' || definition.id.startsWith('index-basic') ? '基础目录' : '盘后资料'} warning={read.warning}/>
      <DatasetStatus busy={read.busy} error={read.error} empty={!record || !rows.length} retry={read.reload}>{record && <TradingDatasetView key={`${read.requestKey}:${selected ? code : ''}:${filter}`} definition={definition} record={record} rows={rows}/>}</DatasetStatus>
      {record && <DatasetRaw record={record}/>}
    </>}
    <details className="trade-details"><summary>使用说明</summary><p>{definition.description}</p><p>按实际数据日期展示，缓存刷新不触发外部采集。—为来源未提供，0为真实零值；原单位未知时不换算。</p></details>
  </div>;
}

export function ReferenceWidget({ definition, selectedOnly = false }: { definition: ReferenceWidgetDefinition; selectedOnly?:boolean }) {
  return <Card title={definition.title} className="reference-widget trading-reference-card">{definition.id === 'top-list' ? <LeaderboardWidget/> : definition.id === 'limit-list' ? <PoolsReference/> : <DatasetWidget definition={definition} selectedOnly={selectedOnly}/>}</Card>;
}
