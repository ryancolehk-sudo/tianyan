import type { ReactNode } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { Icon } from '../../shared/icons';
import { useRead } from '../../shared/useRead';
import { ErrorNotice, useResource } from '../data-center-live/shared';
import { SourceRuntime } from './SourceRuntime';

export function CollectionTools({ session, requestedSourceId, scope, connectionAction, onChanged }: {
  session: SessionResponse; requestedSourceId: string; scope?: { sourceIds: string[]; families: string[] };
  connectionAction: (sourceId: string) => ReactNode; onChanged: () => void;
}) {
  const providers = useResource(api.providers, !scope);
  const runtime = useRead(api.dataSources);
  const sources = runtime.error ? undefined : runtime.data?.data;
  const items = providers.error ? undefined : providers.data?.data.items;
  function reload() { if (!scope) providers.reload(); runtime.reload(); }
  function changed() { reload(); onChanged(); }
  const totals = [
    ['已登记能力', items?.length], ['已配置', items?.filter(item => item.configured).length],
    ['已启用', items?.filter(item => item.enabled).length], ['今日调用', sources?.today_request_count],
    ['预计费用', sources?.today_cost_cny == null ? null : '¥ ' + sources.today_cost_cny],
  ];
  const families = scope && new Set([...scope.families,
    ...(sources?.items.filter(item => scope.sourceIds.includes(item.provider.id)).map(item => item.family) ?? [])]);
  return <>
    <div className="data-area-heading data-stats-heading">
      {!scope &&
      <dl className="data-inline-counts" aria-label="采集统计">{totals.map(([label, value]) => <div key={label}><dt>{label}</dt><dd data-testid={'summary-' + label}>{value ?? '—'}</dd></div>)}</dl>
      }
      <button type="button" onClick={reload} disabled={runtime.busy || (!scope && providers.busy)}><Icon name="refresh"/>刷新状态</button>
    </div>
    {providers.error && !runtime.error && <ErrorNotice error={providers.error} retry={providers.reload} inline/>}
    {runtime.error ? <ErrorNotice error={runtime.error} retry={reload} inline/>
      : sources ? <SourceRuntime key={requestedSourceId} requestedSourceId={requestedSourceId} families={families} data={sources} session={session} reload={changed} connectionAction={connectionAction}/>
      : <p role="status">正在读取采集配置…</p>}
  </>;
}
