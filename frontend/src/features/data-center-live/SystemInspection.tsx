import { useCallback, useEffect, useRef, useState } from 'react';
import { dataCenterApi } from './api';
import type { ConnectionView, InspectionFinding, InspectionState, SystemInspection } from './api';
import { dateTime, messageOf } from './shared';
import { Icon } from '../../shared/icons';
import { monitorHref } from '../../shared/dataMonitorLink';
import { collectionParams, dataHref } from '../../shared/dataNavigation';
import { ApiError } from '../../shared/api';
import { InspectionActivity, useInspectionRun } from './InspectionActivity';

const states: Record<InspectionState, string> = {
  available: '可自动修复', manual: '需要你处理', working: '处理中', waiting: '等待条件', limited: '覆盖说明', verified: '已核验恢复',
};
const areas = { source: '采集', history: '历史补漏', capability: '接入进度', runtime: '后台服务', cache: '页面数据', call: '调用', route: '备用来源' };
const order: InspectionState[] = ['available', 'manual', 'working', 'waiting', 'limited'];
type Props = { csrf: string; account: string; refreshVersion: number; connections: ConnectionView[]; configure: (id: string | null, kind?: 'market' | 'model', source?: string) => void };

function groupPermissions(findings: InspectionFinding[], connections: ConnectionView[]) {
  const result: InspectionFinding[] = [];
  const groups = new Map<ConnectionView, InspectionFinding[]>();
  for (const finding of findings) {
    const matches = finding.state === 'manual' && finding.reason === 'permission_missing'
      ? connections.filter(c => finding.connection_id ? c.id === finding.connection_id : !!finding.source_id && c.source_ids.includes(finding.source_id)) : [];
    if (matches.length !== 1) { result.push(finding); continue; }
    const group = groups.get(matches[0]) || [];
    group.push(finding);
    groups.set(matches[0], group);
  }
  for (const [connection, items] of groups) {
    if (items.length === 1) { result.push(items[0]); continue; }
    const names = [...new Set(items.map(row => row.title))];
    result.push({ ...items[0], id: 'permission:' + connection.id, connection_id: connection.id,
      source_id: null, title: connection.name + ' · 接入授权', count: items.length, trade_day: null,
      advice: '在这一个连接内核对所需数据的权限，保存后检测连接。涉及' + names.slice(0, 2).join('、') + '等。' });
  }
  return result;
}

function Findings({ items, connections, configure }: Pick<Props, 'connections' | 'configure'> & { items: InspectionFinding[] }) {
  const [expanded, setExpanded] = useState(false);
  return <><ul className="inspection-items">{(expanded ? items : items.slice(0, 4)).map(item => {
    const connection = connections.find(v => v.id === item.connection_id || !!item.source_id && v.source_ids.includes(item.source_id));
    return <li key={item.id}><span className={'inspection-mark inspection-' + item.state} aria-hidden="true"/>
      <div><strong>{item.title}</strong><small>{areas[item.area]}{item.count > 1 ? ' · ' + item.count + ' 条记录' : ''}</small>
        <p>{item.advice}{item.retry_at && <span> 下次核对 {dateTime(item.retry_at)}</span>}</p></div>
      {connection && (item.state === 'manual' || item.area === 'capability') ?
        <button type="button" className="dc-link" onClick={() => configure(connection.id, connection.kind === 'model' ? 'model' : 'market', item.source_id || undefined)}>接入配置</button> :
        item.source_id && (item.area === 'history' || item.area === 'call' && item.trade_day) ?
        <a className="dc-link" href={monitorHref({ sourceId: item.source_id, tradeDay: item.trade_day, context: 'history' })}>核对记录</a> :
        item.source_id && item.state === 'manual' &&
        <a className="dc-link" href={dataHref('connections', collectionParams('', item.source_id).toString(), true)}>查看来源设置</a>}</li>;
  })}</ul>{items.length > 4 && <button type="button" className="dc-link inspection-more" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起列表' : '展开其余 ' + (items.length - 4) + ' 项'}</button>}</>;
}

export function InspectionPanel({ csrf, account, refreshVersion, connections, configure }: Props) {
  const [scan, setScan] = useState<SystemInspection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState(true);
  const [selected, setSelected] = useState<InspectionState | 'receipt' | null>(null);
  const mounted = useRef(false);
  const generation = useRef(0);
  const readLock = useRef(false);
  const read = useCallback(async () => {
    if (readLock.current) return;
    const version = generation.current;
    readLock.current = true;
    try {
      const { data } = await dataCenterApi.inspection();
      if (!data?.checks || !Array.isArray(data.findings)) throw new Error('本机服务未返回有效巡检记录。');
      if (mounted.current && version === generation.current) { setScan(data); setError(null); }
    } catch (reason) {
      if (mounted.current && version === generation.current) {
        setError(messageOf(reason));
        if (reason instanceof ApiError && (reason.status === 401 || reason.status === 403)) setScan(null);
      }
    } finally { if (version === generation.current) { readLock.current = false; if (mounted.current) setReading(false); } }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void read();
    return () => { mounted.current = false; generation.current++; readLock.current = false; };
  }, [read]);
  const activity = useInspectionRun(account, csrf, () => { setSelected('receipt'); void read(); });
  const batch = activity.busy || activity.uncertain ? null : activity.run?.batch ?? (!activity.status && !activity.statusError ? scan?.last_repair : null);
  const refreshStatus = useRef(activity.refresh);
  refreshStatus.current = activity.refresh;
  const previousRefresh = useRef(refreshVersion);
  useEffect(() => {
    if (previousRefresh.current !== refreshVersion) { previousRefresh.current = refreshVersion; refreshStatus.current(); void read(); }
  }, [refreshVersion, read]);
  useEffect(() => {
    const poll = () => { if (!document.hidden) void read(); };
    const timer = window.setInterval(poll, 60000);
    document.addEventListener('visibilitychange', poll);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', poll); };
  }, [read]);
  const findings = groupPermissions(scan?.findings || [], connections);
  const selectedState = (selected === 'receipt' && !batch ? null : selected) || (batch ? 'receipt' : order.find(state => findings.some(item => item.state === state))) || 'available';
  const shown = selectedState === 'receipt' ? batch?.actions || [] : findings.filter(item => item.state === selectedState);
  return <section className="inspection-panel" aria-labelledby="inspection-title">
    <div className="inspection-heading"><div><h3 id="inspection-title"><Icon name="shield"/>全系统检修</h3>
      <p>{scan ? '上次巡检 ' + dateTime(scan.checked_at) : reading ? '正在核对本机运行记录…' : '巡检清单等待更新'}</p></div>
      <button type="button" className="inspection-primary" disabled={activity.busy || activity.health === 'offline'} onClick={() => { setSelected('receipt'); void activity.start(); }}><Icon name="system"/>{activity.health === 'offline' ? '服务启动后可检修' : activity.busy ? '正在检修…' : activity.uncertain ? '重试本次修复' : '一键检修并修复'}</button></div>
    <InspectionActivity activity={activity} batch={batch ?? null}/>
    {error && <div role="alert" className="inspection-error"><strong>巡检清单暂未更新</strong> <button type="button" onClick={() => { void read(); }}>重新读取巡检</button></div>}
    {scan && <details className="inspection-scope"><summary>巡检范围</summary><p>{Object.entries(scan.checks).map(([label, n]) => <span key={label}>{label} <b>{n}</b></span>)}</p></details>}
    {(scan || batch) && <><div className="inspection-filters" aria-label="检修结果筛选">
      {batch && <button type="button" aria-pressed={selectedState === 'receipt'} onClick={() => setSelected('receipt')}>本次修复 <b>{batch.actions.length}</b></button>}
      {order.map(state => <button type="button" key={state} aria-pressed={selectedState === state} onClick={() => setSelected(state)}>{states[state]} <b>{findings.filter(item => item.state === state).length}</b></button>)}</div>
      {selectedState === 'receipt' && batch && <p className="inspection-receipt">本次修复登记于 {dateTime(batch.started_at)}</p>}
      {shown.length ? selectedState === 'receipt' ? <div className="inspection-batch">{(['verified','working','waiting','manual','limited'] as const).map(state => {
        const rows = shown.filter(row => row.state === state);
        return rows.length ? <div key={state}><h4>{states[state]} · {rows.length}</h4><Findings items={rows} connections={connections} configure={configure}/></div> : null;
      })}</div> : <Findings key={selectedState} items={shown} connections={connections} configure={configure}/>
        : selectedState !== 'receipt' && <p className="inspection-empty">本次巡检没有归入「{states[selectedState]}」的项目。</p>}
    </>}
  </section>;
}
