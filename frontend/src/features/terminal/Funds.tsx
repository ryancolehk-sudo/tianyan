import { useWorkspaceState } from '../../shared/workspaceState';
import { useEffect } from 'react';
import { useNavigation } from '../../shared/navigation';
import { currentDayContext, marketPhaseLabel, useMarketSession } from '../../shared/marketSession';
import { AIObservation, Change, Empty, MarketRibbon, Panel, StockName } from './Components';
import type { PageProps } from './Components';
import type { IntradayFundSector } from './types';
import { FundsChart, sectorColors } from './Charts';
import { KplStrengthWorkspace } from './KplStrengthWorkspace';
import { dateTime, money, number, percent, shortDay, signedMoney, time, tone } from './data';
import { fundChartCoverage, latestFundSector } from './sourceTiming';
import { SectorStrengthFilter } from './SectorStrengthFilter';
import { toggleFundComparison } from './strengthSelection';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { loadWorkbench } from './data';

export function Funds(props: PageProps) {
  const { data, selected, selectStock, selectSector, asOfTime } = props;
  const navigation = useNavigation();
  const explicitDay = new URLSearchParams(navigation.search).get('trade_day') ?? '';
  const { market } = useMarketSession(data.market_session);
  const liveFunds = data.intraday_funds;
  const ths = liveFunds?.source_family === 'ths_board_funds';
  const metric = ths ? '资金净额' : '主力净额';
  const historyMode = !!asOfTime || !!explicitDay && (!market || explicitDay !== market.trade_day);
  const automatic = ths || !historyMode && (currentDayContext(market) || data.mode === 'live'
    || !!liveFunds?.sectors.length && liveFunds.trade_day === data.trade_day && data.funds_trade_day !== data.trade_day);
  // Manual choices last for this market context; the next opening or trade day resumes automatic mode.
  const context = `${market?.trade_day ?? data.trade_day}:${market?.automatic_live ?? data.mode}:${explicitDay}:${asOfTime}`;
  const key=`${props.session.data.user.id}:funds:${ths ? 'ths' : 'eastmoney'}:${context}`;
  const [choice, setChoice] = useWorkspaceState<{ context: string; intraday: boolean } | null>(key+':mode',null);
  const intraday = choice?.context === context ? choice.intraday : automatic;
  const [allMembers, setAllMembers] = useWorkspaceState(key+':members',false);
  const [showScope, setShowScope] = useWorkspaceState(key+':scope',false);
  const [display, setDisplay] = useWorkspaceState<'strength'|'funds'>(key+':display','strength');
  const strengthMode = display === 'strength';
  const availableSectors: IntradayFundSector[] = intraday ? liveFunds?.sectors ?? [] : data.sectors;
  const selectedCategory = [...availableSectors, ...(liveFunds?.focused_sectors ?? []), ...data.sectors]
    .find(item => item.code === data.selected_sector_code)?.category
    ?? (ths ? data.selected_sector_code?.split(':')[1] : undefined);
  const [categoryChoice, setCategoryChoice] = useWorkspaceState<{ context: string; selected: string | null; value: 'industry' | 'concept' } | null>(key+':category',null);
  // Derive the category before the selection effect, including on a direct
  // link or a return from another page. A manual tab choice lasts until the
  // selected object or market context changes.
  const category = categoryChoice?.context === context && categoryChoice.selected === data.selected_sector_code
    ? categoryChoice.value : selectedCategory === 'concept' ? 'concept' : 'industry';
  const setCategory = (value: 'industry' | 'concept') => setCategoryChoice({ context, selected: data.selected_sector_code, value });
  const categoryLabel = category === 'industry' ? ths ? '同花顺行业' : '二级行业' : '概念板块';
  const inCategory = (item: {category?:string|null;industry_level?:number|null}) => category === 'industry'
    ? item.category === 'industry' && (ths || item.industry_level === 2) : item.category === 'concept';
  const allSectors = availableSectors.filter(inCategory)
    .sort((a, b) => (b.main_net_cny ?? -Infinity) - (a.main_net_cny ?? -Infinity));
  const sectors = allSectors.slice(0, 10);
  const rankedSelection = allSectors.find(item => item.code === data.selected_sector_code);
  const liveSelection = intraday ? latestFundSector(liveFunds, data.selected_sector_code, liveFunds?.trade_day ?? data.trade_day) : undefined;
  const directorySelection = intraday ? data.sectors.find(item => item.code === data.selected_sector_code && inCategory(item)) : undefined;
  const sector = (liveSelection && inCategory(liveSelection) ? liveSelection : rankedSelection)
    ?? (directorySelection ? { ...directorySelection, main_net_cny: null, pct_change: null, amount_cny: null, rank: null } : undefined) ?? allSectors[0];
  const [comparison, setComparison] = useWorkspaceState<string[] | null>(key+':compare:'+category,null);
  const checked = comparison ?? [];
  const strengthRows = (data.sector_strength ?? []).filter(inCategory);
  const compareQuery = new URLSearchParams({view:'funds', ...(explicitDay ? {day:explicitDay} : {}),
    ...(asOfTime ? {as_of_time:asOfTime} : {}), fund_compare:[...checked].sort().join(',')});
  const compareRead = useCachedLoader(()=> !ths && comparison?.length ? loadWorkbench(compareQuery) : Promise.resolve(null),
    key+':curves:'+(comparison?.length ? compareQuery.toString() : 'default'), '所选板块资金曲线');
  const compareData = ths || comparison == null ? data : compareRead.data?.data ?? data;
  const chartSectors = comparison == null
    ? sector && !sectors.some(item => item.code === sector.code) ? [...sectors, sector] : sectors
    : checked.map(code => [...availableSectors,...data.sectors,...strengthRows].find(item=>item.code===code))
      .filter((item): item is NonNullable<typeof item>=>!!item).map(item=>({ main_net_cny:null, pct_change:null, amount_cny:null, rank:null, ...item }));
  const chartFunds = compareData?.intraday_funds;
  const chartHistory = compareData?.funds_history ?? [];
  const toggleComparison = (code:string) => {
    const next = toggleFundComparison(checked,code);
    setComparison(next);
    if(next.includes(code)) selectSector(code);
  };
  const matchedSelection = !!sector && sector.code === data.selected_sector_code;
  useEffect(() => {
    if (sector && !matchedSelection) selectSector(sector.code);
  }, [sector?.code, matchedSelection, selectSector]);
  const dailyDay = data.funds_trade_day === undefined ? data.trade_day : data.funds_trade_day;
  const fundsDay = intraday ? liveFunds?.trade_day : dailyDay;
  const membersDay = data.members_trade_day === undefined ? data.trade_day : data.members_trade_day;
  const stockFunds = new Map((liveFunds?.stocks ?? []).map(item => [item.code, item]));
  const targetStates = intraday ? (liveFunds?.target_states ?? []).filter(item => item.code === selected?.code || item.code === sector?.code) : [];
  const targetLabel = { available: '已更新', stale: '等待更新', preparing: '正在准备', recovering: '正在自动恢复', unavailable: '暂未取得有效数据' };
  const members = [...(matchedSelection ? data.selected_members : [])].map(stock => intraday ? { ...stock, main_net_cny: stockFunds.get(stock.code)?.value ?? null } : stock)
    .sort((a, b) => (b.main_net_cny ?? -Infinity) - (a.main_net_cny ?? -Infinity));
  const history = data.funds_history.filter(item => item.sector_code === sector?.code).sort((a, b) => a.day.localeCompare(b.day)).slice(-3);
  const known = members.filter(stock => stock.pct_change != null);
  const up = known.filter(stock => stock.pct_change! > 0).length;
  const down = known.filter(stock => stock.pct_change! < 0).length;
  const flat = known.length - up - down;
  const phase = market ? marketPhaseLabel[market.phase] : '';
  const closed = market?.phase === 'closed';
  const coverage = fundChartCoverage(liveFunds, chartSectors, fundsDay);
  const closingMissing = !ths && !historyMode && closed && market?.is_trading_day === true && fundsDay === market.trade_day
    && !coverage.closingComplete;
  const closingStatus = closingMissing ? `收盘数据不完整 · 截至 ${time(coverage.sourceTime)}` : `已收盘 · 截至 ${time(coverage.sourceTime)}`;
  const curveTime = coverage.curveTime;
  const curveIsLater = !!curveTime && !!coverage.latestRankTime && Date.parse(curveTime) > Date.parse(coverage.latestRankTime);
  const categoryCoverage = liveFunds?.coverage?.find(item => item.category === category);
  const coverageNames = { stock: '个股', industry: ths ? '同花顺行业' : '二级行业', concept: '概念' };
  const captureCoverage = liveFunds?.coverage?.map(item => `${coverageNames[item.category]} ${number(item.represented_count)}/${item.expected_count == null ? '待核' : number(item.expected_count)}${item.complete === false ? `（本批${item.latest_batch_count === item.represented_count ? '' : number(item.latest_batch_count)}未齐）` : ''}`).join(' · ');
  const scope = liveFunds?.scope || '同一来源的当日主力累计净额；近5分钟和名次变化仅在同一时段具备真实前值后计算。';
  const missingTitle = !ths && category === 'industry' && availableSectors.some(item => item.category === 'industry' && item.industry_level == null) && !allSectors.length ? '已有行业样本，分类目录待更新' : liveFunds?.state === 'permission_denied' ? '盘中资金使用范围待核实' : closed ? '当日盘中资金尚未保存' : '等待当日盘中资金';
  const ladderQuery = new URLSearchParams({ view: 'ladder', ...(explicitDay ? { trade_day: explicitDay } : {}),
    ...(selected ? { stock: selected.code } : {}), ...(sector ? { sector: sector.code } : {}), ...(asOfTime ? { as_of_time: asOfTime } : {}) });
  return <>
    <div className="ty-funds-controls" role="group" aria-label="资金板块分类">
      <button type="button" className={'ty-button' + (category === 'industry' ? ' ty-button-gold' : '')} aria-pressed={category === 'industry'} onClick={() => setCategory('industry')}>{ths ? '同花顺行业' : '二级行业'}</button>
      <button type="button" className={'ty-button' + (category === 'concept' ? ' ty-button-gold' : '')} aria-pressed={category === 'concept'} onClick={() => setCategory('concept')}>概念板块</button>
      <span>{ths ? (category === 'industry' ? '同花顺行业分类' : '同花顺概念分类 · 成分可能重叠') : category === 'industry' ? '东方财富行业分类 · 仅二级行业' : '东方财富概念分类 · 成分可能重叠'} · 各类独立排名</span>
    </div>
    <MarketRibbon data={data} funds intradayFunds={intraday} fundsCount={allSectors.length} fundsCategory={categoryLabel}
      fundsExpected={categoryCoverage?.expected_count} fundsComplete={categoryCoverage?.complete} asOfTime={asOfTime}/>
    {ths && <p className="ty-panel-caption">上方为同花顺当前分类的资金覆盖，行业与概念分别统计；下方开盘啦题材强度独立覆盖，不受行业板块数量限制。</p>}
    {intraday && <p className="ty-panel-caption" role="status">{ths ? '采样已存覆盖' : '盘中已存覆盖'}：{captureCoverage || '各类实际总数待核'}。{ths ? '来源未提供行情日期和时刻，以下按接收时间记录；分页完整不代表时效已核实。' : '分页未齐不能代表全市场；排行与曲线各保留自己的来源时点。'}</p>}
    {ths && liveFunds?.category_notes?.[category] && <p className="ty-panel-caption" role="status">{liveFunds.category_notes[category]}</p>}
    <div className="ty-workspace ty-funds-workspace"><div className="ty-main-column"><div className="ty-funds-top">
      <Panel title={strengthMode ? '板块强度' : '板块资金脉络'} subtitle={strengthMode ? '开盘啦 · 原始强度' : `${categoryLabel} · ` + (intraday ? `${closed ? '当日已存' : '盘中'}${ths ? '采样资金净额' : '累计主力净额'} · 亿元` : `每日${metric} · 亿元 · 近10交易日`)} action={<><span className={intraday && closingMissing ? 'ty-gold' : undefined}>{fundsDay || '—'} {intraday ? closed ? ths ? '已存接收采样' : closingStatus : phase : '盘后'}</span><button type="button" className="ty-link" onClick={() => setShowScope(!showScope)} aria-expanded={showScope}>查看口径</button></>} className="ty-funds-chart-panel">
        <div className="ty-funds-controls" role="group" aria-label="板块图表指标"><button type="button" className={'ty-button'+(strengthMode?' ty-button-gold':'')} aria-pressed={strengthMode} onClick={()=>setDisplay('strength')}>板块强度</button><button type="button" className={'ty-button'+(!strengthMode?' ty-button-gold':'')} aria-pressed={!strengthMode} onClick={()=>setDisplay('funds')}>{ths ? '资金净额' : '主力流入'}</button></div>
        {showScope && <div className="ty-scope-note">{strengthMode ? '开盘啦原始题材强度，按接收记录画线；不是涨停家数，也不等同同花顺资金净额。' : intraday ? scope : '每日净额是该交易日资金流入减流出；不同交易日的数据不相减计算近5分钟变化。'} 题材成员可能重叠，板块资金不直接相加。实际来源及缺项见页面底部。</div>}
        {strengthMode ? <KplStrengthWorkspace day={explicitDay || liveFunds?.trade_day || data.trade_day} cutoff={asOfTime} user={props.session.data.user.id} csrf={props.session.data.csrf_token}/> : <>
        <div className="ty-strength-chart-workspace">
          <SectorStrengthFilter rows={strengthRows} checked={checked} defaultMode={comparison==null} onToggle={toggleComparison} onReset={()=>setComparison(null)} stateKey={key+':strength:'+category} source={ths ? 'ths' : 'eastmoney'}/>
          <div className="ty-strength-plot">
            {!checked.length && comparison != null ? <Empty title="勾选板块查看资金曲线" detail="可同时对比最多10个板块"/> :
              <FundsChart sectors={chartSectors} history={chartHistory} selected={sector?.code ?? ''} intraday={intraday} intradayPoints={chartFunds?.points}
                comparison={comparison!=null} metric={metric} timeBasis={ths ? 'received_at' : 'source_time'} missingTitle={compareRead.busy && comparison!=null ? '正在读取所选板块已存采样' : missingTitle}/>}
            {comparison!=null && compareRead.error && <p className="ty-panel-caption" role="alert">所选曲线读取失败 <button type="button" className="ty-link" onClick={compareRead.reload}>重试</button></p>}
            <div className="ty-fund-legend" aria-label="资金曲线图例">{chartSectors.map((item,index)=> {
              const points = (intraday ? chartFunds?.points ?? [] : chartHistory).filter(point=>point.sector_code===item.code&&point.value!=null);
              return <button type="button" key={item.code} className="ty-link" onClick={()=>selectSector(item.code)} title={item.name+' · '+(points.length?'点击查看该板块详情':'无可用已存采样')}>
                <i style={{background:item.code===sector?.code?'#f7d76b':sectorColors[index%sectorColors.length]}}/>{item.name}{!points.length&&<small> · {compareRead.busy&&comparison!=null?'读取中':'缺采样'}</small>}</button>;
            })}</div>
          </div>
        </div>
        {targetStates.map(item => <p className="ty-panel-caption" key={item.code} role="status">所选{item.category === 'stock' ? '个股' : '板块'}资金 · {targetLabel[item.state]}{item.source_time ? ` · 来源 ${dateTime(item.source_time)}` : ' · 缺测不补零'}{item.category !== 'stock' ? ' · 单独观察，不计入资金榜排名' : ''}</p>)}
        <div className="ty-funds-controls" role="group" aria-label="资金观察模式">
          <button type="button" className={'ty-button' + (!intraday ? ' ty-button-gold' : '')} aria-pressed={!intraday} onClick={() => setChoice({ context, intraday: false })}>盘后 · 近10日</button>
          <button type="button" className={'ty-button' + (intraday ? ' ty-button-gold' : '')} aria-pressed={intraday} onClick={() => setChoice({ context, intraday: true })}>{closed ? '当日 · 已存采样' : '盘中 · 当日'}</button>
          {choice?.context === context ? <button type="button" className="ty-link" onClick={() => setChoice(null)}>跟随交易时段</button> : <span>{intraday ? closed ? ths ? '已存接收采样' : closingStatus : `${phase ? phase + ' · ' : ''}自动跟随` : '同图保留其他板块 · 点击排名关联高亮'}</span>}
        </div>
        {intraday && ths && <p className="ty-panel-caption">同花顺公开资金表 · 本类最近接收 {dateTime(categoryCoverage?.received_at)} · 约5分钟采集一轮，接收时间不等于行情时间；长缺口不连线</p>}
        {intraday && !ths && <p className={'ty-panel-caption' + (closingMissing ? ' ty-gold' : '')} role={closingMissing ? 'status' : undefined}>{liveFunds?.source_name || '盘中资金'} · 排行来源截至 {dateTime(coverage.latestRankTime)} · 所选曲线最新点 {dateTime(curveTime)}{curveIsLater ? ' · 完整样本齐备后更新排名' : ''}{closingMissing ? ' · 15:00收盘样本未齐或本批更新不完整，曲线保留真实缺口；收盘后的排行来源不能补成15:00曲线' : liveFunds?.state === 'stale' ? closed ? ' · 已保存的较早采样' : ' · 数据待更新' : ''}{liveFunds?.state === 'missing' ? closed ? ' · 当日未保存采样' : ' · 等待首批真实采样' : ''}</p>}
        </>}
      </Panel>
      <Panel title={intraday ? closed ? '当日采样排名' : '盘中同步排名' : '盘后资金排名'} subtitle={categoryLabel} action={<span>{fundsDay || '—'}</span>} className="ty-funds-ranking">
        <div className="ty-table-scroll"><table className="ty-table"><thead><tr><th>#</th><th>板块</th><th>当日净额<br/><small>亿元</small></th><th>近5分变化<br/><small>亿元</small></th><th>名次变化</th></tr></thead><tbody>{sectors.map((item, index) => <tr key={item.code} data-current={item.code === sector?.code || undefined} onClick={() => selectSector(item.code)}>
          <td>{index + 1}</td><td><button type="button" className="ty-sector-choice" onClick={event => { event.stopPropagation(); selectSector(item.code); }}><i style={{ background: item.code === sector?.code ? '#f7d76b' : sectorColors[index % sectorColors.length] }}/>{item.name}</button></td>
          <td className={tone(item.main_net_cny)}>{signedMoney(item.main_net_cny)}</td><td className={tone(intraday ? item.change_5m_cny : null)}>{intraday ? signedMoney(item.change_5m_cny) : '—'}</td>
          <td className={tone(intraday ? item.rank_change_5m : null)}>{!intraday || item.rank_change_5m == null ? '—' : item.rank_change_5m > 0 ? `↑${number(item.rank_change_5m)}` : item.rank_change_5m < 0 ? `↓${number(Math.abs(item.rank_change_5m))}` : '0'}</td>
        </tr>)}</tbody></table>{!sectors.length && <Empty title={intraday ? missingTitle : '等待板块资金记录'} detail={intraday ? scope : '排名只使用同源、同日的实际资金'}/>}</div>
        <div className="ty-panel-caption">{intraday ? liveFunds?.sectors.length ? `${closed ? '最后保存样本的变化 · ' : ''}缺少同源5分钟前值或可比范围时显示 —` : closed ? '当日未保存采样' : '首批当日采样到达后自动展示' : '日频数据不计算分钟变化'} · 题材可能重叠</div>
      </Panel>
    </div><div className="ty-funds-bottom">
      <Panel title={<>{sector?.name || '选择板块'} · 成分股表现</>} subtitle={`已采集${members.length}只 / 成分${(matchedSelection ? props.sector?.member_count ?? sector?.member_count : sector?.member_count) ?? '—'}只 · 成分日期 ${membersDay || '—'}`} className="ty-members-panel">
        <div className="ty-table-scroll"><table className="ty-table"><thead><tr><th>名称</th><th>当日净额<small>（亿）</small></th><th>近5分<small>（亿）</small></th><th>涨幅</th><th>成交额</th><th>当前状态</th></tr></thead><tbody>{(allMembers ? members : members.slice(0, 6)).map(stock => <tr key={stock.code} data-current={stock.code === selected?.code || undefined} onClick={() => selectStock(stock.code)}>
          <td><StockName stock={stock} onSelect={selectStock}/></td><td className={tone(stock.main_net_cny)}>{signedMoney(stock.main_net_cny, 2)}</td><td className={tone(intraday ? stockFunds.get(stock.code)?.change_5m_cny : null)}>{intraday ? signedMoney(stockFunds.get(stock.code)?.change_5m_cny, 2) : '—'}</td><td><Change value={stock.pct_change}/></td><td>{money(stock.amount_cny, 1)}{stock.amount_cny != null && '亿'}</td><td><span className={stock.seal_amount_cny != null && stock.seal_amount_cny > 0 ? 'ty-gold' : 'ty-neutral'}>{stock.status || '待观测'}</span></td>
        </tr>)}</tbody></table>{!members.length && (ths || !props.sectorBusy && !props.sectorError) && <Empty title={sector ? '该板块成分记录待采集' : '选择板块查看成分股'} detail={ths ? '同花顺成员映射与个股资金尚缺，保留此区等待同源数据' : '成分记录与行情日期分别保留，个股资金缺测不补零'}/>}</div>
        <div className="ty-members-footer"><button type="button" className="ty-link" disabled={members.length <= 6} onClick={() => setAllMembers(!allMembers)}>{allMembers ? '收起成分' : '查看全部成分'} ›</button><a className="ty-link" href={ths ? undefined : '/workbench?' + ladderQuery} aria-disabled={ths || undefined} title={ths ? '同花顺成分映射待接入' : undefined}>与连板观察联动 ›</a><span>资金日期 {fundsDay || '—'}</span></div>
      </Panel>
      <Panel title="资金与表现对照" subtitle={sector?.name} className="ty-funds-compare"><div className="ty-table-scroll"><table className="ty-table"><thead><tr><th>日期</th><th>{metric}<small>（亿）</small></th><th>板块涨幅</th><th>成交额</th></tr></thead><tbody>{history.map(item => <tr key={item.day} data-current={item.day === fundsDay || undefined}><td>{shortDay(item.day)}</td><td className={tone(item.value)}>{signedMoney(item.value)}</td><td><Change value={item.day === dailyDay ? data.sectors.find(value => value.code === sector?.code)?.pct_change : null}/></td><td>{money(item.day === dailyDay ? data.sectors.find(value => value.code === sector?.code)?.amount_cny : null, 1)}</td></tr>)}</tbody></table>{!history.length && <Empty compact title="等待同口径历史记录"/>}</div>
        <div className="ty-funds-context"><span>当日板块涨幅 <b className={tone(sector?.pct_change)}>{percent(sector?.pct_change)}</b></span><span>当日成交额 <b>{money(sector?.amount_cny, 1)}{sector?.amount_cny != null && '亿'}</b></span><small>资金日 {fundsDay || '—'} · 行情日 {data.trade_day || '—'}</small></div>
        <div className="ty-member-breadth"><div><span>成分涨跌分布</span><span className="ty-up">上涨 {known.length ? up : '—'}</span><span className="ty-down">下跌 {known.length ? down : '—'}</span><span>平盘 {known.length ? flat : '—'}</span></div><div className="ty-distribution"><i style={{ width: known.length ? `${100 * up / known.length}%` : '0%' }}/><i style={{ width: known.length ? `${100 * down / known.length}%` : '0%' }}/><i style={{ width: known.length ? `${100 * flat / known.length}%` : '0%' }}/></div><small>已采集且有涨幅的 {known.length} 只成分 · 不代表完整题材</small></div>
      </Panel>
    </div></div><AIObservation data={data} session={props.session} tradeDay={data.trade_day} asOfTime={props.asOfTime} selected={selected} sector={sector} view="funds" intradayFunds={intraday}/></div>
  </>;
}
