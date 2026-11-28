import type { ReactNode } from 'react';
import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import type { RuntimeSource, SourceAdapter, SourceConfiguration, SourceIndex, SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { useSubmit } from '../../shared/useSubmit';
import { Card, Empty, ErrorState } from '../../shared/ui';

const rightsText = { allowed: '允许', denied: '不允许', unknown: '待核实' };
const states = { disabled: '未启用', permission_missing: '使用范围未核实', credentials_missing: '待本机填写 Key', not_checked: '尚未采集', waiting_focus: '等待关注标的', waiting: '等待采集条件', ready: '完整返回', partial: '部分返回', empty: '无数据', unavailable: '不可用', error: '采集失败' };
const results = { complete: '完整返回', partial: '部分返回', empty: '无数据', unavailable: '不可用', error: '失败' };
const capabilities: Record<string, string> = { 'bar.daily': '历史日K（三种复权口径）', 'valuation.daily': '历史估值字段', 'corporate.factors': '复权因子', 'company.financial': '财务指标（非完整三表）', 'corporate.dividends': '分红资料', 'company.basic': '证券基础资料', 'classification.industry': '行业分类', 'index.constituents': '三指数成分', 'fx.reference': '每日参考汇率', 'macro.annual': '中国年度宏观', 'rates.annual': '年度利率参考', 'news.public': '官方RSS条目', 'news.announcement': '公开公司公告', 'quote.eod': '沪深 A 股与部分指数盘后收盘（非实时）', 'quote.l1': '少量标的基础报价', 'calendar.month': '深市月度开闭市日期', 'instrument.catalog': '公开标的代码与简称目录' };
const reasons: Record<string, string> = { intraday_window_wait: '等待对应交易时段，批次尚未完成', source_budget_wait: '等待本机请求预算', local_request_wait: '等待共享请求许可', backfill_conflict_existing_observation: '补录与已有观察冲突，原记录保留', backfill_day_closed: '所选日期未核为开市日，未请求补采', backfill_day_out_of_range: '日期超出允许补采范围', backfill_unsupported: '该来源尚不支持按日期补采', history_page_limit_reached: '历史窗口达到单次行数上限，未证明完整', history_window_empty: '该历史窗口返回空', history_response_out_of_range: '历史返回超出所选范围', diagnostic_only: '仅预检接口，尚未写入行情',  permission_missing: '上游拒绝访问或权限不足', schema_changed: '上游格式变化，已暂停', response_too_large: '响应超过安全上限', upstream_redirect: '上游发生重定向', rate_limited: '上游限流，等待后重试', timeout: '请求超时', transport_error: '网络连接失败', upstream_unavailable: '上游暂不可用', source_disabled: '来源已关闭', daily_data_not_published: '盘后数据尚未发布', collection_disabled: '本机采集开关已关闭' };
export const sourceTime = (value: string | null | undefined) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
const reason = (value: string | null) => value ? reasons[value] ?? '该次请求未完成，请核查来源配置' : '—';
Object.assign(capabilities, { 'quote.live': '免费实时快照（需本人Key）', 'quote.delayed': '五分钟盘中参考（非实时）' });
Object.assign(capabilities, { 'market.breadth.reference': '供应商涨跌家数与区间分布参考' });
Object.assign(capabilities, { 'market.sector.attention': '供应商热门板块关注排名' });
Object.assign(capabilities, { 'market.funds.reference': '行业与个股盘后资金参考', 'market.pools.reference': '涨停、炸板与跌停原股池' });
Object.assign(capabilities, { 'quote.snapshot': '个股行情快照（按实际行情时间核验）', 'reference.tushare': '已购权益参考表（原字段与日期）' });
Object.assign(reasons, { calendar_not_ready: '已核沪深日历不足或不一致，未猜测请求日期', unsafe_upstream_response: '来源返回包含凭据的异常内容，已丢弃并暂停' });
Object.assign(reasons, { credentials_missing: '待本机安全填写行情Key', credentials_invalid: '本机凭据格式不正确' });
Object.assign(reasons, { replaced_by_fuyao: '已改用同花顺竞价，旧记录保留', auction_data_not_ready: '竞价数据尚未就绪', auction_limits_unavailable: '等待当日涨跌停价', auction_snapshot_stale: '竞价响应日期或新鲜度不符', auction_quantity_unit_changed: '竞价数量单位校验未通过' });
Object.assign(capabilities, { 'market.auction.native': '竞价未匹配量与双侧封单' });
Object.assign(capabilities, { 'bar.minute': '本人研究原生分钟（来源时标与最终性分别核验）', 'bar.1m': '1分钟', 'bar.5m': '5分钟', 'bar.15m': '15分钟', 'bar.30m': '30分钟', 'bar.60m': '60分钟' });
type Family = SourceConfiguration['family'];
const selectionFamilies = ['tencent_web', 'tickflow_live', 'ashare_public', 'baostock_history', 'baostock_fundamental', 'eastmoney_auction', 'tencent_auction', 'tushare_rt_k','tushare_daily','tushare_index_daily','tushare_daily_basic','tushare_adj_factor','tushare_moneyflow','tushare_income','tushare_balancesheet','tushare_cashflow','tushare_fina_indicator','tushare_stk_limit','tushare_dividend'];
selectionFamilies.push('sina_minute_1m', 'sina_minute_5m', 'sina_minute_15m', 'sina_minute_30m', 'sina_minute_60m');
Object.assign(capabilities, { 'market.auction.summary': '竞价批次汇总（元/股）', 'market.auction.stocks': '竞价批次明细（逐标的）' });
Object.assign(capabilities, { 'market.duanxianxia.auction.summary': '网站三阶段封单总额', 'market.duanxianxia.auction.stocks': '网站三阶段封单明细' });
Object.assign(reasons, { decoder_changed: '公开解析脚本变化，已暂停', endpoint_not_allowed: '数据域名不在核验范围，已暂停' });
const auctionFamilies = ['eastmoney_auction', 'tencent_auction'];
const auctionTableFamilies = ['duanxianxia_auction', 'duanxianxia_auction_history'];
const quoteFamilies = ['tencent_web', 'tickflow_live', 'ashare_public'];

function SourceEditor({ adapter, current, session, done, cancel }: { adapter: SourceAdapter; current?: RuntimeSource; session: SessionResponse; done: () => void; cancel: () => void }) {
  const [symbols, setSymbols] = useState(current?.symbols.map(value => `${value.exchange}:${value.asset_type}:${value.symbol}`).join('\n')
    ?? (adapter.family === 'tushare_index_daily' ? 'SSE:index:000001' : quoteFamilies.includes(adapter.family) ? 'SSE:index:000001\nSZSE:index:399001\nSZSE:index:399006\nSSE:index:000688' : selectionFamilies.includes(adapter.family) ? 'SZSE:stock:000001' : ''));
  const [interval, setInterval] = useState(current?.interval_seconds ?? Math.max(adapter.family === 'tencent_web' ? 60 : adapter.minimum_interval, adapter.minimum_interval));
  const [focused, setFocused] = useState(current?.collection_mode==='focused');
  const [basis, setBasis] = useState(current?.rights_basis ?? '');
  const [entitlement, setEntitlement] = useState<SourceConfiguration['entitlement_status']>(current?.provider.entitlement_status ?? 'unknown');
  const [rights, setRights] = useState<SourceConfiguration['rights']>(current?.provider.rights ?? { display: 'unknown', cache: 'unknown', export: 'unknown', redistribute: 'unknown' });
  const [enabled, setEnabled] = useState(current?.provider.enabled ?? false);
  const [sectorCodes, setSectorCodes] = useState((current?.sector_codes?.length ? current.sector_codes : (current?.board_codes ?? []).map(code => code.endsWith('.DC') ? code : code + '.DC')).join('\n'));
  const [validation, setValidation] = useState('');
  const write = useSubmit(session.data.user.id, 'source:configure:' + adapter.family);
  async function save(event: FormEvent) {
    event.preventDefault(); setValidation('');
    const selected: SourceConfiguration['symbols'] = [];
    if (selectionFamilies.includes(adapter.family)) {
      const lines = symbols.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
      if ((!focused && lines.length < 1) || lines.length > (adapter.family === 'tencent_web' ? 50 : auctionFamilies.includes(adapter.family) ? 20 : quoteFamilies.includes(adapter.family) ? 5 : 3) || lines.some(value => !/^(SSE|SZSE|BSE):(stock|index):\d{6}$/.test(value))) {
        setValidation('请按“市场:类型:六位代码”逐行填写；竞价快照最多20项且只接受沪深股票，免费实时/盘中参考最多5项，腾讯最多50项，历史与财务最多3项。'); return;
      }
      for (const line of lines) {
        const [exchange, asset_type, symbol] = line.split(':');
        selected.push({ exchange: exchange as 'SSE' | 'SZSE' | 'BSE', asset_type: asset_type as 'stock' | 'index', symbol });
      }
    } else if (adapter.family === 'cninfo' && symbols.trim()) {
      setValidation('标的目录来源不接受手工标的列表。'); return;
    }
    const sectors = adapter.family === 'tushare_dc_member' ? sectorCodes.split(/[\s,，]+/).filter(Boolean) : [];
    if (adapter.family === 'tushare_dc_member' && (sectors.length < 1 || sectors.length > 10 || new Set(sectors).size !== sectors.length || sectors.some(code => !/^BK\d{4,8}\.DC$/.test(code)))) {
      setValidation('请填写1至10个不重复的DC板块代码，每行一个，例如BK1184.DC。'); return;
    }
    const body: SourceConfiguration = { family: adapter.family as Family, symbols: selected, interval_seconds: interval,
      rights_basis: basis.trim(), rights, entitlement_status: entitlement, enabled, sector_codes: sectors,
      ...(adapter.family === 'tencent_web' && focused ? { collection_mode: 'focused' as const } : {}),
      ...(['fuyao_auction','tencent_market_rt'].includes(adapter.family) ? { collection_mode: 'full_market' as const } : {}) };
    const result = await write.submit(body, key => api.configureSource(body, session.data.csrf_token, key));
    if (result) done();
  }
  const permitted = entitlement === 'verified' && rights.display === 'allowed' && rights.cache === 'allowed';
  return <form className="source-config-form" aria-label={`配置${adapter.name}`} onSubmit={save}>
    <p>保存配置后查看实际采集结果。数据使用范围由管理员依据已有许可填写，请勿填写密码、令牌或其他密钥。</p>
    {adapter.family==='tencent_web' && <label className="source-enable"><input type="checkbox" checked={focused} onChange={event=>setFocused(event.target.checked)}/>跟随当前个股、可见榜单与自选（每窗口50只，合并单批200只）</label>}
    {adapter.family==='tencent_market_rt' && <p>按已存目录分批采集，逐页保留接收时间；目录不保证当日在市全集。与关注报价分开调度，共享限流冷却，仅供本人本机研究。</p>}
    {adapter.family === 'tushare_dc_member' && <label>观察板块代码<textarea aria-label="观察板块代码" rows={4} value={sectorCodes} maxLength={200} onChange={event => setSectorCodes(event.target.value)}/><small>每行一个DC板块代码，最多10个；按实际交易日取得成分，避免全市场成分超过单次返回范围。</small></label>}
    {adapter.family.startsWith('tushare_') && <p>已购 Tushare 优先。标的接口本批每连接只选择一个明确身份；指数日线使用指数，其他标的接口使用股票。财务保留所选字段与公告日，日期数据不会补成实时快照。Token 仅在本机私密环境配置。</p>}
    {adapter.capabilities.includes('bar.minute') && <p>本人非商业研究分钟来源：每条连接一个沪深股票，按真实日历在交易时段采集。五周期共享至少15秒、每日500次本机上限（非供应商额度承诺），限流统一等待。原生量额单位及尾柱最终性尚未确认；不补缺口、不开放导出或再分发。配置成功不代表真实分钟已返回，请查看调用记录及工作台图表。</p>}
    {selectionFamilies.includes(adapter.family) && <label>采集标的<textarea aria-label="采集标的" rows={4} value={symbols} maxLength={1800} onChange={event => setSymbols(event.target.value)}/><small>例：SSE:index:000001 与 SZSE:stock:000001 是不同标的。报价最多50项，历史/财务最多3项，财务仅沪深股票。历史源回补近180自然日，财务为最近四报告期指标。</small></label>}
    {auctionFamilies.includes(adapter.family) && <p>范围：1–20 个沪深股票的公开盘前快照，价格元、量股、额元；只有来源时点落在 09:15/09:20/09:25 ±60 秒才标记检查点，其余保持未知。不推断未匹配量、撤单或委托队列，也不写入人工竞价观察。</p>}
    {auctionTableFamilies.includes(adapter.family) && <p>范围：短线侠网站三阶段封单表，按来源日期保存全部表内标的。当前表65秒、历史表3600秒，两路共享至少65秒任务间隔；来源更新时间未知。仅个人本机参考，封单不是成交额、封单变化不代表真实撤单。</p>}
    {adapter.family === 'szse_public' && <p>范围：深市当月每日开闭市标记，不包含盘中阶段，不代填沪市和北交所日历。</p>}
    {adapter.family === 'cninfo' && <p>范围：巨潮公开 A 股/CDR 代码与简称目录（文件名含 szse，内容含沪深北）。导入后顶部搜索可查本机目录；不含行情价格，B 股跳过。四指数身份一并保留供检索。</p>}
    {adapter.news && <p>范围：{adapter.news.publisher} · {adapter.news.region} · {adapter.news.category}。读取标题、已有摘要与原始出处，保留原文语言和时间精度；本机个人阅读，不抓取文章正文或图片。</p>}
    {adapter.family === 'hk_gov_finance' && <p>范围：香港政府财经RSS的标题、原始链接与发布日期；仅本机个人阅读和缓存，保留署名，不抓取正文或图片。所有合法发布日期均只保留日期，不按字段钟面推定精确发布时间。公开或商业使用范围需另外核实。</p>}
    <label>刷新间隔（秒）<input aria-label="刷新间隔（秒）" type="number" min={adapter.minimum_interval} max={86400} required value={interval} onChange={event => setInterval(Number(event.target.value))}/></label>
    <label>使用依据<textarea aria-label="使用依据" rows={3} minLength={10} maxLength={2000} required value={basis} onChange={event => setBasis(event.target.value)} placeholder="记录允许使用的用途、范围及依据链接"/></label>
    <div className="source-rights-fields"><label>连接权益<select aria-label="连接权益" value={entitlement} onChange={event => setEntitlement(event.target.value as typeof entitlement)}><option value="unknown">待核实</option><option value="verified">已核实</option><option value="denied">不可用</option></select></label>
      {(['display', 'cache', 'export', 'redistribute'] as const).map(key => <label key={key}>{({ display: '展示', cache: '缓存', export: '导出', redistribute: '再分发' })[key]}<select aria-label={`数据${({ display: '展示', cache: '缓存', export: '导出', redistribute: '再分发' })[key]}权利`} value={rights[key]} onChange={event => setRights({ ...rights, [key]: event.target.value as typeof rights[typeof key] })}>{(['unknown', 'allowed', 'denied'] as const).map(value => <option key={value} value={value}>{rightsText[value]}</option>)}</select></label>)}
    </div>
    <label className="source-enable"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)}/>启用此来源</label>
    {enabled && !permitted && <p role="alert">启用需要先核实连接权益、展示与缓存权利；也可以关闭来源后先保存配置。</p>}
    {validation && <p role="alert">{validation}</p>}{write.error && <ErrorState error={write.error}/>}
    <div className="form-actions"><button className="primary" disabled={write.busy || (enabled && !permitted)}>{write.busy ? '正在保存…' : '保存来源配置'}</button><button type="button" disabled={write.busy} onClick={cancel}>取消</button></div>
  </form>;
}

export function SourceRuntime({ data, session, reload, connectionAction, requestedSourceId = '', families }: {
  data: SourceIndex; session: SessionResponse; reload: () => void; connectionAction?: (sourceId: string) => ReactNode; requestedSourceId?: string; families?: Set<string>;
}) {
  const [query, setQuery] = useState('');
  const [chosen, setChosen] = useState('');
  const search = query.trim().toLowerCase();
  const available = data.available_adapters.filter(adapter => !families || families.has(adapter.family));
  const adapters = [...available]
    .sort((a, b) => Number(b.family.startsWith('tushare_')) - Number(a.family.startsWith('tushare_')))
    .filter(adapter => (adapter.name + ' ' + adapter.family).toLowerCase().includes(search));
  const requested = data.items.find(item => item.provider.id === requestedSourceId);
  const unavailable = !!requestedSourceId && !available.some(adapter => adapter.family === requested?.family);
  const needsSelection = !!families && available.length > 1 && !chosen && !requested && !search;
  const selected = adapters.find(adapter => adapter.family === (chosen || requested?.family))
    ?? (needsSelection ? undefined : adapters[0]);
  const [editing, setEditing] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const write = useSubmit(session.data.user.id, 'source:refresh');
  const admin = session.data.user.role === 'admin';
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    const remaining = data.items.map(row => row.cooldown_until ? Date.parse(row.cooldown_until) - Date.now() : 0).filter(value => value > 0);
    if (!remaining.length) return;
    const timer = window.setTimeout(() => setClock(Date.now()), Math.min(...remaining) + 50);
    return () => window.clearTimeout(timer);
  }, [data, clock]);
  async function refresh(row: RuntimeSource) {
    const result = await write.submit({ source: row.provider.id }, key => api.refreshSource(row.provider.id, session.data.csrf_token, key));
    if (result) { setNotice('采集任务已排队。请刷新状态查看实际结果，排队不表示采集成功。'); reload(); }
  }
  if (unavailable) return <p role="status">找不到指定的采集接口，请核对来源是否仍已登记。</p>;
  return <Card title="采集接口" className="source-runtime" action={<span className="badge muted">{data.collection_enabled ? '本机采集已开启' : '本机采集已关闭'}</span>}>
    {!families && <details className="data-inline-detail source-policy"><summary>采集口径与来源优先级</summary>
      <p>报价优先比较行情日期与时效，同等条件下优先已购 Tushare 权益。部分返回的记录以实际数量为准，时间均为北京时间。</p>
      <p aria-label="Tushare接入概况">Tushare 已提供 {data.available_adapters.filter(row => row.family.startsWith('tushare_')).length} 条有界连接，已配置 {data.items.filter(row => row.family.startsWith('tushare_')).length} 条，完整返回 {data.items.filter(row => row.family.startsWith('tushare_') && row.collection_state === 'ready').length} 条。</p>
    </details>}
    {notice && <p className="source-feedback" role="status">{notice}</p>}{write.error && <ErrorState error={write.error}/>}
    {available.length > (families ? 1 : 0) && <div className="data-source-pick">
      <label>查找采集来源<input type="search" value={query} placeholder="搜索名称或接口代码" onChange={event => setQuery(event.target.value)}/></label>
      <label>采集来源<select aria-label="选择采集来源" value={selected?.family ?? ''} onChange={event => setChosen(event.target.value)}>
        {needsSelection && <option value="">选择接口</option>}
        {adapters.map(adapter => <option key={adapter.family} value={adapter.family}>{adapter.name}</option>)}
      </select></label><span>{adapters.length} 项</span>
    </div>}
    {!selected && !needsSelection && <p className="feature-note" role="status">{query ? '没有匹配的来源，请更换关键词。' : '当前没有可配置的采集来源。'}</p>}
    <div className="source-runtime-grid">{(selected ? [selected] : []).map(adapter => {
      const current = requested?.family === adapter.family ? requested : data.items.find(row => row.family === adapter.family);
      const newsCoverage = current?.news_coverage as { markets?: Array<{ market: string; returned: number; total: number | null; status: string }>; partial?: boolean } | null | undefined;
      const last = current?.last_attempt;
      const deniedBySupplier = current?.collection_state === 'unavailable' && current.paused_reason === 'permission_missing'
        && last?.reason === 'permission_missing' && last.request_count > 0;
      const permissionHint = deniedBySupplier ? '请核对绑定账号是否有此接口的权限'
        : current?.collection_state === 'permission_missing' ? '请核对这个来源在本机登记的使用范围' : null;
      const cooling = !!current?.cooldown_until && Date.parse(current.cooldown_until) > Math.max(clock, Date.now());
      return <article className="source-runtime-item" key={adapter.family} aria-label={adapter.name}>
        <div className="source-runtime-title"><h3>{adapter.name}</h3><span className="badge muted">{deniedBySupplier ? '供应商拒绝访问' : current ? states[current.collection_state] : '未配置'}</span></div>
        <p>{adapter.capabilities.map(value => capabilities[value] ?? value).join(' / ')}</p>
        {adapter.family === 'chinanews_finance' && <p className="feature-note">官方财经滚动RSS，每15分钟一轮。只保留订阅提供的标题、摘要与出处；往期从采集起累积，未配置AI时按规则汇编。</p>}
        {adapter.family === 'cninfo_announcements' && <p className="feature-note">沪、深、北分别读取最近7日，每市场每轮最多5页、每页30条；达到上限时显示覆盖有限。公告只有日期时不补时钟。</p>}
        {newsCoverage?.markets?.length ? <p className="feature-note" aria-label="公告采集覆盖">{newsCoverage.markets.map(value => `${({sh:'沪市',sz:'深市',bj:'北交所'} as Record<string,string>)[value.market] ?? value.market}：本轮${value.returned}条 / 查询窗口${value.total ?? '未知'}条${value.status === 'complete' ? '' : '（覆盖有限）'}`).join('；')}</p> : null}
        {adapter.family.startsWith('tushare_') && <p className="feature-note">已购首选 · 用户指定的兼容服务 · {current?.credential_configured ? '本机凭据已配置' : '待本机配置凭据'}。{adapter.family === 'tushare_moneyflow_dc' ? '个股资金每任务最多两页，每页均计次；' : '本批每任务最多一次请求；'}共用两秒本地请求间隔；已取消程序自设的每日2000次上限。供应商额度尚未提供，实际限流继续退避。具体数据、字段和日期见已入库数据。</p>}
        {['tushare_limit_list_d','tushare_top_list'].includes(adapter.family) && <p className="feature-note" aria-label="盘后自动采集策略">自动检查窗口：北京时间 {adapter.family === 'tushare_top_list' ? '16:30' : '16:00'}–21:30，按当前配置间隔且至少15分钟，仅在已核当日开市日历下请求。窗口是本机轮询策略，不保证上游发布时间；尚未返回当日表时保留旧表及原日期，空表不等于零。手动采集仍受原权益、间隔及共用额度限制。</p>}
        {auctionFamilies.includes(adapter.family) && <p className="feature-note">公开网页快照，无需Key，每60秒最多一轮；每次明确选择1–20个沪深股票。仅当来源时点命中检查点±60秒才标09:15/09:20/09:25，接收时间不冒充行情时点；汇总与明细按批次哈希配对，失败时保留上次成功批次并在竞价页标旧缓存。</p>}
        {auctionTableFamilies.includes(adapter.family) && <p className="feature-note">网站阶段封单参考，{adapter.family === 'duanxianxia_auction' ? '当前表每65秒最多一轮，最多3次GET，先核对数据域名与公开解析脚本指纹。' : '历史表每3600秒最多一轮，一次POST返回最近交易日。'}两路共享65秒持久化任务间隔，失败也占用；封单金额不是成交额，不代表真实撤单，上游行情供应商未知。来源未配置时不会采集。</p>}
        {cooling && <p className="source-cooldown" role="status">{current?.cooldown_scope === 'supplier' ? '供应商共用冷却' : '来源限流冷却'}至 {sourceTime(current?.cooldown_until)}。可继续查看已保存数据；到期后按计划重试，不代表上游保证恢复。</p>}
        {adapter.family === 'hk_gov_finance' && <p className="feature-note">香港政府新聞網原生财经RSS，每15分钟最多一轮，最多100条。仅个人本机标题/原始链接/日期阅读；不抓正文或图片。合法发布日期均显示“仅日期”，保留繁体原题，不按钟面补成精确发布时间；内地商业快讯继续单独接入。</p>}
        {adapter.family === 'baostock_daily' && <p className="feature-note">官方免费匿名接口，每6小时最多运行一轮。仅盘后未复权收盘；北交所、科创50、ETF和转债暂缺。覆盖数量包含报价与沪深日历条目，非实时报价数量。</p>}
        {adapter.family === 'tickflow_live' && <p className="feature-note">官方免费注册档，每次最多5标的，本机每30秒最多一轮。Key只在本机安全配置，页面不接收或显示；{current?.credential_configured ? '本机已配置凭据，实际权益以返回结果为准。' : '尚未配置本机Key，不会发起请求。'}盘中时延需用实际交易时段持续核验。</p>}
        {adapter.family === 'ashare_public' && <p className="feature-note">无需Key，每次最多5标的、每300秒一轮。供应商免费档约5分钟更新；源仅日期时不填时分秒、不称实时，不进入严格历史。</p>}
        {adapter.family === 'ashare_breadth' && <p className="feature-note">无需Key，每300秒读取供应商沪深涨跌家数和区间。源未提供行情日期/时间、完整分母或成交额，不称实时全市场；炸板与晋级未知。与其他AShare免费连接共享35秒请求间隔，等待预算不算上游失败。</p>}
        {adapter.family === 'ashare_sector_heat' && <p className="feature-note">无需Key，每300秒最多一轮/30条热门板块，含海外范围，关注排名并非涨幅/资金排名。榜单时间原文时区未明，不补行情时点，不创建本地证券身份。与其他AShare连接共享35秒预算；覆盖数按表版本计，板块条数见已入库表格。</p>}
        {['mairui_funds', 'mairui_pools'].includes(adapter.family) && <p className="feature-note">本人免费证书仅在本机安全配置，{current?.credential_configured ? '证书已安全配置' : '尚未检测到证书'}。官网500次/日，两连接共享本机450次/日预算，其他本人应用可能共用上游额度。{adapter.family === 'mairui_funds' ? '每小时2次，行业/个股盘后参考；原更新时间时区未明，概念资金暂不启用。' : '每15分钟3次，涨停/炸板/跌停池；按已核沪深日历选请求日期，来源无独立更新时间。'}覆盖数按表版本计，行数和全部字段见已入库数据；仅本人研究，禁止导出、转售与再分发。</p>}
        <dl className="data-source-facts">
          <div><dt>来源身份</dt><dd>{adapter.family}</dd></div>
          <div><dt>采集开关</dt><dd>{!current ? '未配置' : !current.provider.enabled ? '未启用' : !data.collection_enabled ? '本机总开关关闭' : current.paused_reason ? '已暂停' : '已启用'}</dd></div>
          <div><dt>费用记录</dt><dd>{current?.measured_cost_cny ?? '—'}</dd></div>
          <div><dt>最近尝试</dt><dd>{sourceTime(last?.received_at)}</dd></div>
          <div><dt>最近成功返回</dt><dd>{sourceTime(current?.last_success_at)}</dd></div>
          <div><dt>下次自动尝试</dt><dd>{current?.provider.enabled && data.collection_enabled && !current.paused_reason ? sourceTime(current.next_run_at) : '—'}</dd></div>
          <div><dt>最近覆盖</dt><dd>{last ? `${last.accepted ?? '—'} 新增 / ${last.duplicates ?? '—'} 重复 / ${last.expected ?? '—'} 预期` : '—'}</dd></div>
          <div><dt>未纳入记录</dt><dd>{last?.rejected ?? '—'}</dd></div>
          <div><dt>原因</dt><dd>{permissionHint ?? reason(current?.paused_reason ?? last?.reason ?? null)}</dd></div>
        </dl>
        {deniedBySupplier && current && connectionAction?.(current.provider.id)}
        {['tushare_market_rt','tushare_rt_indices','tushare_market_limits'].includes(adapter.family) && <p aria-label="Tushare批量行情范围">{adapter.family === 'tushare_market_rt' ? '沪深京A股通配符批量请求，每轮一个请求，不逐只配置或逐只轮询；报价保留每个股票自己的行情时间。涨跌停以当日最新价精确对照当日限制价，旧日、缺值、无正限制价不当成零；可比样本与未判定数在工作台和竞价页显示。' : adapter.family === 'tushare_rt_indices' ? '一次请求上证指数、深证成指、创业板指和科创50；不把指数当股票，不用股票代码首位猜交易所。' : '当日全市场价格限制原表；价格限制不等于曾触板或当前封板数量。当天表尚未取得时不借用前一天的限制价。自动检查安排在已核沪深开市日09:00–15:00；未发布可重试，最少间隔15分钟，实际配置见下方；非空表不等于全市场齐备，窗口内继续有界刷新。'}批量行情和四指数仅在已核沪深开市日的09:30–11:30、13:00–15:00采集，午休与收盘暂停；这是轮询窗口，不代表北交所日历已验。采集间隔、来源时点及实际覆盖分别核验，旧免费来源保留补缺。</p>}
        {['tushare_daily','tushare_index_daily'].includes(adapter.family) && <p>工作台按所选证券获取历史日K；单次窗口不超过五年、最多6000行，可继续向前获取。调用记录注明证券、请求范围与实际结果；原固定标的的自动计划保持。仅不复权历史已实现按需采集，不能把资料连接数当全市场历史覆盖。</p>}
        {adapter.family === 'tushare_rt_k' && <p aria-label="盘中行情采集策略">实时采集时段：北京时间09:30至11:30、13:00至15:00；11:30午休暂停、15:00收盘停止。仅在已核当日开市日历下请求一个明确沪深股票，实际采集间隔见当前配置。盘后查看已保存行情和发布后的日频资料；北交所等未核日历范围等待补齐。采集间隔不等于行情延迟，页面读取本机缓存，源行情时间与接收时间分别保留。</p>}
        <details><summary>查看来源范围与配置依据</summary><p>已实现版本：{adapter.version}；本机最低间隔 {adapter.minimum_interval} 秒。</p><p>{current ? `当前间隔 ${current.interval_seconds} 秒；标的 ${current.symbols.length} 项。` : '未配置，没有发起采集。'}</p><p>{current?.rights_basis ?? '使用范围尚未核实。'}</p>{adapter.references.map(url => <p key={url}><a href={url} target="_blank" rel="noopener noreferrer">来源资料 ↗</a></p>)}</details>
        {admin && <div className="source-actions"><button type="button" onClick={() => { setEditing(editing === adapter.family ? null : adapter.family); setNotice(''); }}>{editing === adapter.family ? '收起配置' : current ? '修改配置' : '配置来源'}</button><button type="button" disabled={write.busy || !current || !data.collection_enabled || !current.provider.enabled || current.provider.entitlement_status !== 'verified' || current.credential_configured === false || !!current.paused_reason || cooling} onClick={() => current && refresh(current)}>{write.busy ? '正在提交…' : cooling ? '等待冷却' : '采集一次'}</button></div>}
        {editing === adapter.family && <SourceEditor key={adapter.family + (current?.configured_at ?? '')} adapter={adapter} current={current} session={session} cancel={() => setEditing(null)} done={() => { setEditing(null); setNotice('来源配置已保存；尚不能据此判定连接成功。'); reload(); }}/>}</article>;
    })}</div>
    {!admin && <p className="feature-note">当前账号可查看状态；来源配置与采集操作由管理员执行。</p>}
  </Card>;
}

export function CallsDesk({ sources }: { sources?: RuntimeSource[] }) {
  const [source, setSource] = useState('');
  const [cursor, setCursor] = useState<string | null>(null);
  const read = useRead(useCallback(() => api.fetchRuns(source, cursor), [source, cursor]));
  const rows = read.data?.data.items ?? [];
  return <Card title="调用记录" className="source-calls" action={<button type="button" disabled={read.busy} onClick={read.reload}>刷新调用记录</button>}>
    <label className="source-call-filter">数据来源<select aria-label="调用记录来源" value={source} onChange={event => { setSource(event.target.value); setCursor(null); }}><option value="">全部来源</option>{sources?.map(row => <option value={row.provider.id} key={row.provider.id}>{row.provider.name}</option>)}</select></label>
    <p className="feature-note">每次实际采集尝试单独保存。时间为北京时间；请求目标日期、证券与接收时间分别显示，补录不代表当时已知；未返回的数量与未记账的费用显示 —，不会按零计算。</p>
    {read.busy ? <div className="resource-loading" role="status">正在读取调用记录…</div> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !rows.length ? <Empty title="尚无采集记录">已成功读取本实例记录。配置或浏览清单不会产生采集调用。</Empty> : <div className="table-wrap" role="region" tabIndex={0} aria-label="来源调用记录表格"><table><thead><tr>{['采集时间', '来源 / 能力', '用途 / 目标日期', '结果 / 原因', '新增 / 重复 / 预期', '请求 / 耗时', '费用（元）'].map(value => <th key={value} scope="col">{value}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.id}><td>{sourceTime(row.received_at)}</td><td>{sources?.find(value => value.provider.id === row.source_id)?.provider.name ?? row.source_id}<small>{capabilities[row.capability] ?? row.capability}</small></td><td>{row.acquisition_mode === 'daily_history' ? <><span>按需日K</span><small>{row.requested_instrument_code ?? '证券身份未知'}</small><small>{row.requested_start_day ?? '未知'} 至 {row.requested_end_day ?? '未知'}</small></> : row.acquisition_mode === 'backfill' ? <><span>{row.capability.split('+').includes('bar.minute') ? '分钟补录' : '按日期补采'}</span><small>{row.requested_day ?? '目标日期未知'}</small></> : <><span>常规采集</span><small>{row.requested_day ?? '随实际响应日期'}</small></>}{row.job_id && <small>任务 <code>{row.job_id}</code></small>}</td><td>{row.status === 'error' && ['intraday_window_wait','source_budget_wait','local_request_wait'].includes(row.reason ?? '') && !row.accepted && !row.duplicates ? '未完成' : results[row.status]}<small>{reason(row.reason)}</small></td><td>{row.accepted ?? '—'} / {row.duplicates ?? '—'} / {row.expected ?? '—'}<small>拒收 {row.rejected ?? '—'}</small></td><td>{row.request_count} 次 / {row.elapsed_ms === null ? '—' : `${row.elapsed_ms} ms`}</td><td>{row.cost_cny ?? '—'}</td></tr>)}</tbody></table></div>}
    <div className="resource-pagination"><button type="button" disabled={!cursor || read.busy} onClick={() => setCursor(null)}>回到最新</button><span>当前分页 · 非总数</span><button type="button" disabled={read.busy || !read.data?.data.next_cursor} onClick={() => setCursor(read.data?.data.next_cursor ?? null)}>下一页</button></div>
  </Card>;
}
