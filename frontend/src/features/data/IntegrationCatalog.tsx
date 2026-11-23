import { useState } from 'react';
import type { DataCoverage, InstrumentListResponse, Provider, QuoteBatch, SourceIndex } from '../../generated/schema';
import { Card, Empty, Segmented } from '../../shared/ui';
import { capabilities, sources, tools, catalogCheckedOn, catalogModules, catalogStages } from './integrationPlan';
import type { CatalogEntry } from './integrationPlan';

const views = ['数据能力', '候选来源', '开源工具'] as const;
const entries = { 数据能力: capabilities, 候选来源: sources, 开源工具: tools };
const pageSize = 8;

function status(item: CatalogEntry, view: typeof views[number], providers?: Provider[], runtime?: SourceIndex, quotes?: QuoteBatch, instruments?: InstrumentListResponse, coverage?: DataCoverage) {
  if (view === '开源工具') {
    if (item.id === 'akshare' && runtime?.available_adapters.some(row => row.capabilities.includes('bar.minute'))) return { label: '已复用分钟协议', note: '未安装SDK；本人研究薄适配，实际配置/返回/时效见连接状态', available: false };
    if (item.id === 'baostock' && runtime?.available_adapters.some(row => row.family === 'baostock_daily')) return { label: '已引入SDK', note: '0.9.4已冻结；本机盘后采集另见连接结果，非实时授权', available: true };
    return { label: '未引入', note: '仅列候选，未安装或复制', available: false };
  }
  if (view === '候选来源') {
    // The implemented daily subset belongs to the existing BaoStock supplier
    // entry; do not duplicate the supplier or mark its other capabilities done.
    const families = item.id === 'sina_web' ? (runtime?.available_adapters.filter(adapter => adapter.capabilities.includes('bar.minute')).map(adapter => adapter.family) ?? []) : item.id === 'tushare_pro' ? (runtime?.available_adapters.filter(adapter => adapter.family.startsWith('tushare_')).map(adapter => adapter.family) ?? []) : item.id === 'global_publisher_rss' ? (runtime?.available_adapters.filter(adapter => adapter.news && !['hk_gov_finance', 'ecb_news', 'fed_public', 'cninfo_announcements', 'chinanews_finance'].includes(adapter.family)).map(adapter => adapter.family) ?? []) : item.id === 'baostock' ? ['baostock_daily','baostock_history','baostock_fundamental','baostock_classification'] : item.id === 'cninfo' ? ['cninfo','cninfo_announcements'] : item.id === 'ecb_public' ? ['ecb_public','ecb_news'] : item.id === 'ashare_public' ? ['ashare_public','ashare_breadth','ashare_sector_heat'] : item.id === 'mairui' ? ['mairui_funds','mairui_pools'] : [item.id];
    const configured = runtime?.items.find(row => families.includes(row.family));
    if (configured) return { label: '已配置', note: !configured.collection_enabled ? '本机采集关闭；最近结果见连接状态' : configured.provider.enabled ? '结果与覆盖见上方连接状态' : '来源未启用', available: false };
    if (runtime?.available_adapters.some(adapter => families.includes(adapter.family))) return { label: '待配置', note: '接入能力已准备，使用范围另核', available: false };
    return { label: '未接入', note: item.id === 'cls_content' ? '待书面授权' : '待核验条件与实际能力', available: false };
  }
  const actual = providers && runtime && coverage?.items.find(row => row.id === item.id);
  if (actual && !['D01','D02','D03','D29','D31'].includes(item.id)) return { label: actual.state === 'partial' ? '部分可用' : actual.state === 'blocked' ? '缺少具体条件' : '未接入', note: actual.detail, available: actual.state === 'partial' };
  if (item.id === 'D01') {
    if (!instruments) return { label: '目录状态未读取', note: '尚不能确认本机目录，可刷新状态重试', available: false };
    const catalog = runtime?.items.find(row => row.provider.capabilities.includes('instrument.catalog'));
    // Historical accepted counts are audit history, never current availability.
    const imported = !!catalog && ((catalog.last_attempt?.accepted ?? 0) + (catalog.last_attempt?.duplicates ?? 0) > 0);
    const catalogAllowed = !!catalog && catalog.provider.configured && catalog.provider.entitlement_status === 'verified'
      && catalog.provider.rights.display === 'allowed' && catalog.provider.rights.cache === 'allowed';
    // The list is already filtered by current display rights on the server. A
    // disabled collection switch does not hide saved identities, while revoked
    // display/cache rights do.
    if (catalog && catalogAllowed && instruments.data.items.length) {
      return { label: imported ? '目录已导入' : '本机检索可用',
        note: imported ? '获权目录已写入本机；上市退市历史与别名仍待接入，搜索不是全市场实时查询' : '已留存标的可通过顶部搜索；完整市场目录、上市退市与别名待接入',
        available: true };
    }
    if (catalog && !catalogAllowed) {
      if (instruments.data.items.length) {
        return { label: '本机检索可用',
          note: '目录来源展示权限已撤回；人工记录或其他有权来源仍可检索，撤权目录不参与搜索', available: true };
      }
      return { label: imported ? '展示权限已撤回' : '权益待核实',
        note: imported ? '来源保存的目录不再参与检索；恢复展示与缓存权利后可查，原有标的未删除' : '连接、展示或缓存范围未核实，不能按历史采集次数判可用',
        available: false };
    }
    if (catalog) {
      return { label: catalog.provider.enabled ? '待采集' : '采集已关闭',
        note: '目录来源已配置；完成一次目录导入后顶部搜索可用，关闭采集不等于撤销展示', available: false };
    }
    if (instruments.data.items.length) {
      return { label: '本机检索可用', note: '已留存标的可通过顶部搜索；完整市场目录、上市退市与别名待接入', available: true };
    }
    if (runtime?.available_adapters.some(adapter => adapter.family === 'cninfo')) {
      return { label: '待配置', note: '巨潮标的目录适配器已实现；配置并采集后顶部搜索可用', available: false };
    }
    return { label: '未接入', note: '本机暂无留存标的；不根据空目录判断市场上不存在', available: false };
  }
  if (item.id === 'D03') {
    const priced = quotes?.items.filter(row => row.quote?.price !== null && row.quote?.price !== undefined && row.provenance) ?? [];
    if (priced.length) {
      const current = priced.some(row => row.state === 'available');
      const daily = priced.filter(row => row.state === 'daily_close');
      if (daily.length) return { label: '盘后参考可用', note: `四指数返回 ${daily.length} / 4；${daily[0].quote?.trade_day ?? '日期未知'} 收盘，非实时；沪深个股可搜索查看`, available: true };
      const timed = priced.some(row => row.quote?.quote_time !== null);
      return { label: current ? '部分可用' : timed ? '仅有较早报价' : '报价时点未知',
        note: '四指数已保存 ' + priced.length + ' 项；数据时点和缺失项见工作台，不代表全市场或实时', available: current };
    }
    const configured = runtime?.items.some(row => row.provider.capabilities.some(cap => ['quote.l1', 'quote.eod'].includes(cap)));
    if (configured) return { label: quotes ? '已配置待核对' : '报价状态未读取', note: quotes ? '四指数暂无可用报价，个股按所选标的读取' : '暂不能确认价格字段；不按采集次数判可用', available: false };
  }
  const cap = item.id === 'D02' ? 'calendar.month' : null;
  const observed = cap && runtime?.items.some(row => row.provider.enabled && row.provider.entitlement_status === 'verified' && row.provider.rights.display === 'allowed' && row.provider.rights.cache === 'allowed' && row.provider.capabilities.includes(cap) && (row.last_attempt?.accepted ?? 0) + (row.last_attempt?.duplicates ?? 0) > 0);
  if (observed) return { label: '部分可用', note: '已保存月度开闭市日期；BaoStock覆盖沪深，深交所源仅深市；具体月份见材料，交易时段另核', available: true };
  if (item.id === 'D31' && runtime) return { label: '部分可用', note: '来源状态和调用记录可查；费用未记账为 —', available: true };
  if (item.localCapability) {
    const declared = providers?.some(provider => provider.kind === 'manual' && provider.configured && provider.enabled
      && provider.entitlement_status === 'verified' && provider.capabilities.includes(item.localCapability!));
    return { label: providers ? declared ? '部分可用' : '未启用' : '状态未读取',
      note: declared ? item.localCapability === 'calendar.import' ? '人工导入可用；自动源未接入' : '人工材料可用；关注管理待做' : '本机部分以登记状态为准', available: !!declared };
  }
  return { label: '未接入', note: item.id === 'D31' ? '基础状态另列；调用与费用待做' : item.id === 'D30' ? '判断能力待开发' : '计划条目，尚未实际验证', available: false };
}

export function IntegrationCatalog({ providers, runtime, quotes, instruments, coverage }: { providers?: Provider[]; runtime?: SourceIndex; quotes?: QuoteBatch; instruments?: InstrumentListResponse; coverage?: DataCoverage }) {
  const [view, setView] = useState<typeof views[number]>('数据能力');
  const [keyword, setKeyword] = useState('');
  const [module, setModule] = useState('全部模块');
  const [stage, setStage] = useState('全部批次');
  const [page, setPage] = useState(0);
  const query = keyword.trim().toLocaleLowerCase();
  const filtered = entries[view].filter(item => (module === '全部模块' || item.modules.some(name => name === module))
    && (stage === '全部批次' || item.stage === stage)
    && (!query || [item.id, item.name, item.purpose, item.basis, item.cost, item.boundary, ...item.modules,
      ...(item.sourceIds ?? []).map(id => sources.find(source => source.id === id)?.name ?? id)].join(' ').toLocaleLowerCase().includes(query)));
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const current = Math.min(page, pages - 1);
  const visible = filtered.slice(current * pageSize, (current + 1) * pageSize);
  function clear() { setKeyword(''); setModule('全部模块'); setStage('全部批次'); setPage(0); }
  return <Card title="接入计划清单" className="integration-catalog" action={<span className="badge">免费优先 · 逐项落地</span>}>
    <div className="catalog-intro"><p>先把需要的数据列齐，再逐项接通。候选不计入本机已配置数量，接入后在这里跟进实际状态。</p><small>计划资料核对：{catalogCheckedOn} · 非实时检测</small></div>
    <div className="catalog-counts" aria-label="规划清单数量"><span>数据能力 <strong>{capabilities.length}</strong></span><span>候选来源 <strong>{sources.length}</strong></span><span>开源工具 <strong>{tools.length}</strong></span><small>费用以实际使用记录为准，未知仍为 —</small></div>
    <p className="catalog-sequence"><strong>首批方向</strong><span>标的与日历</span><span>基础报价与日线</span><span>公告与基本资讯</span></p>
    <Segmented label="接入清单分类" options={views} value={view} onChange={value => { setView(value as typeof views[number]); setPage(0); }}/>
    <div className="catalog-filters">
      <label>搜索清单<input type="search" aria-label="清单关键词" value={keyword} maxLength={100} placeholder="数据、来源或工具名称" onChange={e => { setKeyword(e.target.value); setPage(0); }}/></label>
      <label>覆盖模块<select aria-label="覆盖模块" value={module} onChange={e => { setModule(e.target.value); setPage(0); }}>{['全部模块', ...catalogModules].map(name => <option key={name}>{name}</option>)}</select></label>
      <label>实施批次<select aria-label="实施批次" value={stage} onChange={e => { setStage(e.target.value); setPage(0); }}>{['全部批次', ...catalogStages].map(name => <option key={name}>{name}</option>)}</select></label>
      <button type="button" onClick={clear}>重置筛选</button>
    </div>
    <p className="catalog-result" aria-live="polite">{view}：匹配 {filtered.length} / {entries[view].length} 项{filtered.length > 0 && ` · 显示 ${current * pageSize + 1}–${Math.min((current + 1) * pageSize, filtered.length)} 项`}</p>
    <div className="table-wrap catalog-table" tabIndex={0} role="region" aria-label="接入计划表格"><table><thead><tr><th scope="col">{view}</th><th scope="col">用途与依据</th><th scope="col">安排与状态</th><th scope="col">范围与费用条件</th></tr></thead><tbody>{visible.map(item => {
      const state = status(item, view, providers, runtime, quotes, instruments, coverage);
      return <tr key={`${view}:${item.id}`}><td data-label="项目"><small className="catalog-id">{view === '数据能力' ? item.id : view === '候选来源' ? '来源候选' : '工具候选'}</small><strong className="catalog-name">{item.name}</strong><span className="catalog-modules">{item.modules.join(' / ')}</span></td><td data-label="用途"><p>{item.purpose}</p><small>{item.basis}</small></td><td data-label="状态"><span className="catalog-stage">{item.stage}</span><span className={state.available ? 'badge' : 'badge muted'}>{state.label}</span><small>{state.note}</small></td><td data-label="条件"><p>{item.cost}</p><details><summary aria-label={`查看${item.name}详情`}>查看范围与限制</summary><p>{item.boundary}</p>{view === '候选来源' && <p>展示、缓存、导出与再分发均需单独确认。具体配置与最近采集结果，以本机连接状态为准；本计划不代表全部能力已接通。</p>}{item.url && <a href={item.url} target="_blank" rel="noopener noreferrer">{view === '开源工具' ? '查看固定版本资料' : '查看核对依据'} ↗</a>}</details></td></tr>;
    })}</tbody></table>{!visible.length && <Empty title="没有匹配的计划条目">调整关键词、模块或实施批次。筛选结果为空不表示来源故障。</Empty>}</div>
    <div className="catalog-pagination"><span>第 {current + 1} / {pages} 页</span><button type="button" disabled={current === 0} onClick={() => setPage(current - 1)}>上一页</button><button type="button" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>下一页</button></div>
    <p className="feature-note">开源许可与数据使用权分别核实；试用和积分不等于永久免费。切换或筛选只查看清单，不会发起外部采集、安装工具或购买服务。配置、启用和权利状态见下方本机登记。</p>
  </Card>;
}
