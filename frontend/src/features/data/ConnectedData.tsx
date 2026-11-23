import { useCallback, useState } from 'react';
import { useNavigation } from '../../shared/navigation';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Card, Empty, ErrorState } from '../../shared/ui';
import { sourceTime } from './SourceRuntime';

export function CoverageDesk({ open }: { open: () => void }) {
  const read = useRead(api.dataCoverage);
  const rows = read.data?.data.items ?? [];
  return <Card title="31类数据接入进度" action={<button type="button" onClick={open}>查看已入库数据</button>}>
    <p className="feature-note">每类按实际留存数据和使用范围核对。“部分数据可用”只覆盖说明中列出的内容；候选或网页可达不等于整类接通。</p>
    {read.busy ? <p role="status">正在核对全部数据能力…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : <>
      <p className="coverage-count">已有部分数据 <strong>{rows.filter(row => row.state === 'partial').length}</strong> / {rows.length} 类</p>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="全部数据接入进度"><table><thead><tr><th>能力</th><th>实际状态</th><th>覆盖与剩余缺口</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><small>{row.id}</small><strong>{row.name}</strong></td><td><span className={row.state === 'partial' ? 'badge' : 'badge muted'}>{row.state === 'partial' ? '部分数据可用' : row.state === 'blocked' ? '缺少具体条件' : '待接入'}</span></td><td>{row.detail}</td></tr>)}</tbody></table></div>
    </>}
  </Card>;
}

export function ConnectedData() {
  const navigation = useNavigation();
  const family = new URLSearchParams(navigation.search).get('family') || undefined;
  const load = useCallback(() => api.datasets(undefined, family), [family]);
  const read = useRead(load);
  const [selected, select] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const rows = read.data?.data.items ?? [];
  const item = rows.find(row => row.id === selected) ?? rows[0];
  const matching = item?.payload.rows.filter(row => !query.trim() || Object.values(row).some(value => value?.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))) ?? [];
  const current = Math.min(page, Math.max(0, Math.ceil(matching.length / 50) - 1));
  return <Card title="已入库数据" className="connected-data" action={<button type="button" onClick={read.reload}>刷新已存数据</button>}>
    <p className="feature-note">查看实际来源表格、报告期、单位和出处。这里只读本机缓存；空值表示来源未提供，不补零。资料接收于现在，不代表过去已知。</p>
    {read.busy ? <p role="status">正在读取已采集资料…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !item ? <Empty title="尚无已保存的参考表格">先查看来源连接和调用记录；没有数据不表示市场上没有这类资料。</Empty> : <>
      <div className="resource-filter"><label>数据表<select aria-label="选择已入库数据表" value={item.id} onChange={event => { select(event.target.value); setQuery(''); setPage(0); }}>{rows.map(row => <option key={row.id} value={row.id}>{row.payload.title} · {row.source_key.replace('reference:', '')} · {row.source_name}</option>)}</select></label><label>表内查找<input type="search" aria-label="参考数据关键词" maxLength={80} value={query} onChange={event => { setQuery(event.target.value); setPage(0); }}/></label></div>
      <div className="dataset-provenance"><strong>{item.payload.title}</strong><p>{item.payload.scope}</p><p>来源：{item.payload.attribution} · 服务器接收：{sourceTime(item.received_at)} · 已存 {item.payload.rows.length} 行</p><a href={item.payload.source_url} target="_blank" rel="noopener noreferrer">查看原始出处 ↗</a><details><summary>查看该版校验信息</summary><p>来源身份：{item.family}</p><p>版本：{item.id}</p><code>{item.content_hash}</code></details></div>
      {!matching.length ? <Empty title={item.payload.rows.length ? '没有匹配的记录' : '来源本次未返回记录'}>调整表内关键词或查看其他报告期；不把空记录当成零值。</Empty> : <div className="table-wrap reference-table" tabIndex={0} role="region" aria-label="已入库参考数据"><table><thead><tr>{item.payload.columns.map(column => <th key={column.key}>{column.label}{column.unit && <small>{column.unit}</small>}</th>)}</tr></thead><tbody>{matching.slice(current * 50, (current + 1) * 50).map((row, i) => <tr key={i}>{item.payload.columns.map(column => <td key={column.key}>{row[column.key] ?? '—'}</td>)}</tr>)}</tbody></table></div>}
      <div className="catalog-pagination"><span>匹配 {matching.length} 行 · 第 {current + 1} / {Math.max(1, Math.ceil(matching.length / 50))} 页</span><button disabled={current === 0} onClick={() => setPage(current - 1)}>上一页</button><button disabled={(current + 1) * 50 >= matching.length} onClick={() => setPage(current + 1)}>下一页</button></div>
    </>}
  </Card>;
}
