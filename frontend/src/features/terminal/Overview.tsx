import { Panel } from './Components';
import type { PageProps } from './Components';
import { BreadthChart } from './Charts';
import { number, time } from './data';
import type { WorkbenchData } from './types';
import { SectorTracking } from './SectorTracking';
import { useNavigation } from '../../shared/navigation';
import { useMarketSession } from '../../shared/marketSession';
import { latestSourceTime, missingClosingSample } from './sourceTiming';
import { useWorkspaceState } from '../../shared/workspaceState';
import { StockRanking } from './StockRanking';
import { KplStrengthWorkspace } from './KplStrengthWorkspace';
import { OverviewFunds } from './OverviewFunds';
import { MarketStatus } from './MarketStatus';
import { OverviewBoards } from './OverviewBoards';
import { OverviewLadder } from './OverviewLadder';

export function Overview(props: PageProps & {fundsData: WorkbenchData; selectFund: (code: string) => void}) {
  const { data, selected, selectStock, busy, asOfTime } = props;
  const [notesOpen, setNotesOpen] = useWorkspaceState(`${props.session.data.user.id}:overview-notes:${data.trade_day}:${asOfTime}`, false);
  const [marketContext, setMarketContext] = useWorkspaceState<'radar' | 'boards' | 'funds'>(props.session.data.user.id + ':overview:market-context', 'radar');
  const navigation = useNavigation();
  const explicitDay = new URLSearchParams(navigation.search).get('trade_day') ?? '';
  const { market } = useMarketSession(data.market_session);
  const historical = !!asOfTime || !!explicitDay && (!market || explicitDay !== market.trade_day);
  const stockTime = latestSourceTime(data.stocks.map(item => item.source_time));
  const dailyClose = data.breadth.basis === 'daily_close';
  const historicalBreadth = data.breadth.historical_series;
  const breadthPoints = historicalBreadth?.points.length ? historicalBreadth.points : data.breadth.points;
  const breadthTime = latestSourceTime(breadthPoints.filter(item => item.up != null || item.down != null).map(item => item.time));
  const historicalLast = historicalBreadth?.points.filter(item => item.up != null || item.down != null).at(-1);
  const lastPoint = breadthPoints.filter(item => item.up != null || item.down != null).at(-1);
  const curveSource = historicalLast ? historicalBreadth!.source_family === 'xgb_breadth_history' ? '选股宝历史' : historicalBreadth!.source_name : undefined;
  const seriesLabel = curveSource || (dailyClose ? '收盘日线' : data.breadth.source_family === 'tencent_market_rt' ? '腾讯' : undefined);
  const closingMissing = !historical && missingClosingSample(data.trade_day, breadthTime, market, 360);
  return <OverviewFunds data={props.fundsData} user={props.session.data.user.id} cutoff={asOfTime ?? ''} onSelect={props.selectFund}
    render={(balance, ranking) => <div className="ty-overview-unified">
      <div className="ty-overview-market-grid">
        <KplStrengthWorkspace stocks={data.stocks} day={explicitDay || data.trade_day} cutoff={asOfTime ?? ''} user={props.session.data.user.id} csrf={props.session.data.csrf_token} compact overview/>
        {balance}
        <Panel title="涨跌停家数走势" subtitle={<>{seriesLabel && <span>{seriesLabel}</span>}<span className="ty-series-legend"><i className="ty-dot-up"/>涨停 <b className="ty-up">{number(lastPoint?.up)}</b><i className="ty-dot-down"/>跌停 <b className="ty-down">{number(lastPoint?.down)}</b></span></>} action={<span>{curveSource ? '历史' : '采样'} {time(breadthTime)}</span>} className="ty-breadth-panel">
          <BreadthChart points={breadthPoints} busy={busy} sourceName={seriesLabel}/>
          {closingMissing && <p className="ty-panel-caption ty-gold" role="status">{dailyClose ? '曲线缺收盘样本 · 图例为最后采样' : '缺少收盘样本'}</p>}
          <details className="ty-breadth-notes" open={notesOpen} onToggle={event => setNotesOpen(event.currentTarget.open)}><summary>数据说明</summary>
          {historicalLast && <p className="ty-panel-caption" title={`${historicalBreadth!.source_name} · ${historicalBreadth!.trade_day} · 接收 ${historicalBreadth!.received_at} · ${historicalBreadth!.scope}`}>{curveSource}原始统计，范围按来源说明 · {dailyClose ? '收盘日线核验' : '已存报价核验'}：涨停 {number(data.breadth.limit_up)} / 跌停 {number(data.breadth.limit_down)} · 两种口径独立显示</p>}
          <div className="ty-event-strip">{data.events.slice(-3).map((event, i) => <span key={`${event.time}-${i}`}><i/><b>{time(event.time)}</b>{event.label}</span>)}{!data.events.length && <span className="ty-muted"><i/>实际快照积累后记录盘面变化</span>}</div>
          </details>
        </Panel>
        <OverviewLadder {...props}/>
      </div>
      <div className="ty-overview-stock-row" role="group" aria-label="个股与板块榜单">
        <StockRanking readingKey={`${props.session.data.user.id}:overview-rank:${data.trade_day}:${asOfTime}:gain`} rows={data.stocks} selected={selected?.code ?? null} onSelect={selectStock} dailyClose={dailyClose} sourceLabel={dailyClose ? `${data.trade_day} 收盘` : `行情 ${time(stockTime)}`} watch={props.watch}
          focus={{session:props.session,day:data.trade_day,cutoff:asOfTime,enabled:data.capabilities.some(item=>item.key==='focused_quotes'&&item.status==='available')}}/>
        <SectorTracking {...props} compact/>
        <div className="ty-market-context">
          <div className="ty-market-context-switch" role="group" aria-label="市场概览">
            <button type="button" aria-pressed={marketContext === 'radar'} onClick={() => setMarketContext('radar')}>盘面结构</button>
            <button type="button" aria-pressed={marketContext === 'boards'} onClick={() => setMarketContext('boards')}>板块列表</button>
            <button type="button" aria-pressed={marketContext === 'funds'} onClick={() => setMarketContext('funds')}>资金流向</button>
          </div>
          {marketContext === 'radar' && <MarketStatus {...props}/>}
          {marketContext === 'boards' && <OverviewBoards {...props}/>}
          {marketContext === 'funds' && ranking}
        </div>
      </div>
    </div>}/>;
}
