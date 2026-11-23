import { useCallback, useState } from 'react';
import type { Provider, SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Card, Empty, ErrorState, Segmented } from '../../shared/ui';
import { Icon } from '../../shared/icons';
import { CalendarDesk, EvidenceDesk, SnapshotDesk } from './Foundation';
import { CallsDesk, SourceRuntime } from './SourceRuntime';
import { IntegrationCatalog } from './IntegrationCatalog';
import { ConnectedData, CoverageDesk } from './ConnectedData';
import { discardPrivateDraft, retainAttempt } from '../../shared/privateDrafts';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { DataCenterLive } from '../data-center-live/DataCenterLive';
import { ModuleGuide } from './ModuleGuide';
import './data-tools.css';

const kinds: Record<Provider['kind'], string> = { manual: '人工导入', market: '行情数据', news: '资讯数据', calendar: '交易日历', catalog: '标的目录', model: 'AI 模型' };
const rights = { allowed: '允许', denied: '不允许', unknown: '待核实' };
const dataViews = [['sources', '数据源管理'], ['datasets', '已入库数据'], ['progress', '接入进度'], ['materials', '研究材料'], ['snapshots', '冻结快照'], ['calendar', '交易日历'], ['capabilities', '能力范围'], ['calls', '调用记录'], ['guide', '接入说明']] as const;

function SourceTable({ items }: { items: Provider[] }) {
  const [kind, setKind] = useState('全部');
  const visible = kind === '全部' ? items : items.filter(item => kinds[item.kind] === kind);
  return <><Segmented label="数据源类型" options={['全部', '人工导入', '行情数据', '资讯数据', '交易日历', '标的目录', 'AI 模型']} value={kind} onChange={setKind}/><div className="table-wrap" tabIndex={0} role="region" aria-label="数据源状态表格"><table><thead><tr>{['数据能力', '类型', '配置状态', '启用状态', '授权状态', '适配器版本', '声明能力'].map(name => <th scope="col" key={name}>{name}</th>)}</tr></thead><tbody>{visible.map(source => <tr key={source.id}><td><span className="source-name"><Icon name={source.kind === 'news' ? 'news' : source.kind === 'calendar' ? 'auction' : 'data'}/>{source.name}</span></td><td>{kinds[source.kind]}</td><td><span className={source.configured ? 'badge' : 'badge muted'}>{source.configured ? '已配置' : '未配置'}</span></td><td>{source.enabled ? '已启用' : '未启用'}</td><td>{source.kind === 'manual' ? '随材料声明' : source.entitlement_status === 'verified' ? '已核实' : source.entitlement_status === 'denied' ? '不可用' : '待核实'}</td><td><span className="version-label">{source.adapter_version === 'not-installed' ? '未安装' : source.adapter_version}</span></td><td>{source.capabilities.length ? source.capabilities.map(cap => ({ 'evidence.manual': '人工材料保存', 'calendar.import': '人工日历导入' }[cap] ?? cap)).join(' / ') : '尚未声明'}</td></tr>)}</tbody></table>{!visible.length && <Empty title="该类型尚未登记数据源">已成功读取本实例状态，没有此分类的登记项。</Empty>}</div></>;
}

function CapabilityTable({ items }: { items: Provider[] }) {
  return <div className="table-wrap" tabIndex={0} role="region" aria-label="数据权利与能力表格"><table><thead><tr>{['数据能力', '声明能力', '展示', '缓存', '导出', '再分发'].map(name => <th scope="col" key={name}>{name}</th>)}</tr></thead><tbody>{items.map(source => <tr key={source.id}><td>{source.name}</td><td>{source.capabilities.length ? source.capabilities.map(cap => ({ 'evidence.manual': '人工材料保存', 'calendar.import': '人工日历导入' }[cap] ?? cap)).join(' / ') : '尚未声明'}</td>{(['display', 'cache', 'export', 'redistribute'] as const).map(key => <td key={key}><span className={source.rights[key] === 'allowed' ? 'badge' : 'badge muted'}>{rights[source.rights[key]]}</span></td>)}</tr>)}</tbody></table></div>;
}

export function LegacyDataCenter({ session }: { session: SessionResponse }) {
  const navigation = useNavigation();
  const view = dataViews.find(([key]) => key === new URLSearchParams(navigation.search).get('view'))?.[1] ?? '数据源管理';
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [evidenceId, setEvidenceId] = useState<string | null>(() => new URLSearchParams(location.search).get('evidence'));
  function changeView(label: string) {
    const url = new URL(location.href);
    url.searchParams.set('view', dataViews.find(([, name]) => name === label)?.[0] ?? 'sources');
    updateLocation(url);
  }
  const read = useRead(api.providers);
  const runtime = useRead(api.dataSources);
  const coverage = useRead(api.dataCoverage);
  const quotes = useRead(api.marketQuotes);
  const instruments = useRead(useCallback(() => api.instruments(''), []));
  // Background revalidation keeps this owner's data tools mounted so saved
  // notices, selected versions and unsaved input survive provider refreshes.
  const items = read.data?.data.items;
  const sources = !runtime.error && !runtime.busy ? runtime.data?.data : undefined;
  function reload() { read.reload(); runtime.reload(); quotes.reload(); instruments.reload(); coverage.reload(); }
  return <>
    <div className="data-toolbar"><Segmented label="数据管理工具" options={dataViews.map(([, name]) => name)} value={view} onChange={changeView}/><button onClick={reload} disabled={read.busy || runtime.busy}><Icon name="refresh"/>刷新状态</button></div>
    <div className="data-summary">{[
      ['已登记能力', items?.length, '本实例登记项，不含候选'],
      ['已配置', items?.filter(item => item.configured).length, '配置不等于已验证连接'],
      ['已启用', items?.filter(item => item.enabled).length, '来源开关，总采集状态另列'],
      ['今日调用', sources?.today_request_count, '北京时间当天的实际请求次数'],
      ['预计费用', sources?.today_cost_cny, '以费用记录为准，未记账为 —'],
    ].map(([label, value, note]) => <div key={label}><span>{label}</span><strong data-testid={`summary-${label}`}>{value ?? '—'}</strong><small>{note}</small></div>)}</div>
    <div className="research-status-note"><Icon name="info"/><span>人工材料、冻结快照和日历导入可用。外部来源按实际状态逐项接通；未配置或未取得使用范围的来源不自动采集。题材归纳与新闻摘要可在配置模型后手动生成，其余 AI 功能见接入状态。</span></div>
    {view === '数据源管理' && <>
      {runtime.busy ? <Card title="外部数据连接"><div className="resource-loading" role="status">正在读取连接状态…</div></Card> : runtime.error ? <Card title="外部数据连接"><ErrorState error={runtime.error} retry={runtime.reload}/></Card> : sources && <SourceRuntime data={sources} session={session} reload={reload}/>}
      <IntegrationCatalog coverage={!coverage.busy && !coverage.error ? coverage.data?.data : undefined} providers={items} runtime={sources} quotes={!quotes.busy && !quotes.error ? quotes.data?.data : undefined} instruments={!instruments.busy && !instruments.error ? instruments.data ?? undefined : undefined}/>
    </>}
    {read.error && items && <ErrorState error={read.error} retry={read.reload}/>}
    {view === '已入库数据' ? <ConnectedData/> : view === '接入进度' ? <CoverageDesk open={() => changeView('已入库数据')}/> : view === '调用记录' ? <CallsDesk sources={sources?.items}/> : read.busy && !items ? <Card title="数据源状态"><div className="resource-loading" role="status">正在读取数据源状态…</div></Card> : read.error && !items ? <Card title="数据源状态"><ErrorState error={read.error} retry={read.reload}/></Card> :
      view === '研究材料' ? <EvidenceDesk session={session} source={items?.find(item => item.kind === 'manual')} initialId={evidenceId} freeze={keys => { discardPrivateDraft(session.data.user.id, 'snapshot:create'); retainAttempt(session.data.user.id, 'snapshot:create', null); setSelectedKeys(keys); changeView('冻结快照'); }}/>:
      view === '冻结快照' ? <SnapshotDesk session={session} selectedKeys={selectedKeys} showEvidence={id => { setEvidenceId(id); changeView('研究材料'); }}/>:
      view === '交易日历' ? <CalendarDesk session={session}/>:
      view === '数据源管理' ? <div className="data-layout"><Card title="本机数据源登记" className="data-sources"><SourceTable items={items ?? []}/><p className="feature-note">配置和启用来自本机登记；实际采集结果见上方连接状态。</p></Card><ModuleGuide/><Card title="数据权利与能力范围" className="data-capabilities"><CapabilityTable items={items ?? []}/><p className="feature-note">数据源登记不自动赋予展示、缓存、导出或再分发权利。</p></Card><Card title="调用记录" className="data-calls"><p>按来源回查每次实际采集的结果、覆盖、耗时和费用；没有记录时保持明确的空状态。</p><button type="button" onClick={() => changeView('调用记录')}>查看调用记录</button></Card></div>:
      view === '能力范围' ? <Card title="数据权利与能力范围"><CapabilityTable items={items ?? []}/><p className="feature-note">权利范围分别登记；来源配置仅对管理员开放，密钥在接入管理中安全保存。</p></Card>:
      <><Card title="接入说明"><div className="integration-steps">{[['核实具体使用范围', '已购Tushare权益内优先、免费来源补缺，分别确认展示、缓存、导出和再分发权利。'], ['配置已实现的连接', '在数据源管理中选择来源、范围与间隔，保存配置不代表连接成功。'], ['核查实际结果', '来源与本机总采集开关开启后，由后台采集；调用记录显示实际返回与失败原因。']].map(([title, detail], i) => <div key={title}><span>{i + 1}</span><h3>{title}</h3><p>{detail}</p></div>)}</div></Card><ModuleGuide/></>}
  </>;
}

export function DataCenter({ session }: { session: SessionResponse }) {
  return <DataCenterLive session={session} legacyTools={<LegacyDataCenter session={session}/>}/>;
}

export function System({ session }: { session: SessionResponse }) {
  const read = useRead(api.health);
  const role = { admin: '管理员', researcher: '研究员', viewer: '查看者' }[session.data.user.role];
  return <><div className="system-layout">
    <Card title="当前工作空间"><dl className="session-details"><dt>系统名称</dt><dd>天衍系统</dd><dt>当前账号</dt><dd>{session.data.user.username}</dd><dt>账号角色</dt><dd>{role}</dd><dt>实例模式</dt><dd>{session.meta.app_mode === 'demo' ? '演示环境' : '真实环境'}</dd><dt>数据接入</dt><dd><a href="/data">查看数据管理实际状态</a></dd></dl><p className="feature-note">客户端不能切换真实/演示数据库。管理员密码不在页面显示。</p></Card>
    <Card title="基础服务状态" action={<button onClick={read.reload} disabled={read.busy}>重新检查</button>}>{read.busy ? <div className="resource-loading" role="status">正在检查基础服务…</div> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : <div className="system-ready"><Icon name="shield"/><strong>{read.data?.status === 'ok' ? '基础服务就绪' : '部分基础服务未就绪'}</strong><p>数据库、迁移与后台心跳的状态，不代表业务数据已接入。</p></div>}</Card>
    <Card title="账户与配置"><div className="system-control-list"><div><span>连接与密钥配置</span><a href="/data?view=connections">进入数据管理</a></div>{['用户管理', '审计查询'].map(name => <div key={name}><span>{name}</span><button disabled>尚未开放</button></div>)}</div><p className="feature-note">当前支持安全登录/退出；配置、研究材料与日历按现有角色限制写入，用户管理界面尚未开放。</p></Card>
    <Card title="实例与数据迁移"><div className="migration-boundary"><Icon name="data"/><h3>独立实例 · 独立数据</h3><p>代码通过 GitHub 同步，数据库与研究记录通过独立备份恢复迁移。不会自动合并两台电脑的数据。</p><span className="badge muted">备份不在页面公开下载</span></div></Card>
  </div></>;
}
