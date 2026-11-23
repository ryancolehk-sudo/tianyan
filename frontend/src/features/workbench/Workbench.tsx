import { useLayoutEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Card } from '../../shared/ui';
import { Icon } from '../../shared/icons';
import { InstrumentQuote } from '../market/Quotes';
import { NewsPreview } from '../market/News';
import { SectorMarkets } from '../market/SectorHeat';
import { FundsReference, PoolsReference } from '../market/MairuiReference';
import type { SessionResponse } from '../../generated/schema';
import { ReferenceWidget } from './ReferenceWidgets';
import { Dashboard } from './Dashboard';
import { widgetCatalog } from './widgetCatalog';
import { allWidgets, existingWidgets, useWidgetPreferences } from './widgetPreferences';
import { TradingProvider, useTradingContext } from './TradingContext';
import { ResearchSummary } from './TradingWidgets';
import './workbenchWidgets.css';
import './tradingWorkbench.css';

export function Workbench({ session }: { session: SessionResponse }) {
  return <TradingProvider key={session.data.user.id} session={session}><TradingWorkbench session={session}/></TradingProvider>;
}
function TradingWorkbench({ session }: { session: SessionResponse }) {
  const trading = useTradingContext();
  const [query, setQuery] = useState('');
  const [header, setHeader] = useState<HTMLElement | null>(null);
  const [layoutTarget, setLayoutTarget] = useState<HTMLDivElement | null>(null);
  const preferences = useWidgetPreferences(session);
  const { visible } = preferences;
  useLayoutEffect(() => { setHeader(document.getElementById('workbench-header')); }, []);
  const matches = (item: { title: string; description: string }) => !query.trim() || (item.title + item.description).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  const content: Record<string, ReactNode> = {
    quote: <InstrumentQuote/>,
    rules: <Card title="规则观察"><div className="trading-placeholder"><strong>尚无已运行规则</strong><p>已有人工预案可在研究记录中查看。</p><a href="/research/plans">查看预案 ›</a></div></Card>,
    sectors: <Card title="板块行情"><SectorMarkets/></Card>,
    funds: <Card title="资金流向"><FundsReference/></Card>,
    news: <Card title="市场资讯" action={<a className="more-link" href="/news">全部资讯 ›</a>}><NewsPreview/></Card>,
    signals: <Card title="规则信号"><div className="trading-placeholder"><strong>尚无已运行信号</strong><p>规则运行后在这里显示结果。</p></div></Card>,
    pools: <Card title="连板观察池"><PoolsReference/></Card>,
    research: <Card title="研究记录" action={<a className="more-link" href="/research/reviews">研究工作区 ›</a>}><ResearchSummary/></Card>,
  };
  widgetCatalog.forEach(item => { content[item.id] = <ReferenceWidget definition={item}/>; });
  const choices = (items: typeof allWidgets) => <div className="widget-choice-grid">{items.filter(matches).map(item => <label key={item.id}><input type="checkbox" checked={visible.has(item.id)} onChange={event => preferences.toggle(item.id, event.target.checked)}/><span><strong>{item.title}</strong><small>{item.description}</small></span></label>)}</div>;
  const controls = <div className="workbench-commandbar trading-commandbar">
    <div className="workbench-title-row"><h1>盘中工作台</h1><div className="trading-day-controls"><label>观察日期<input aria-label="观察交易日" type="date" value={trading.day} max={new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date())} onChange={event => trading.setDay(event.target.value)}/></label><button type="button" className={trading.mode === 'live' ? 'selected' : ''} onClick={() => trading.setDay('')}>{trading.mode === 'live' ? '最新数据' : '返回最新'}</button><span className="trading-mode-label">{trading.mode === 'history' ? '按保存交易日回看' : '按来源频率更新'}</span></div><div ref={setLayoutTarget} className="workbench-layout-controls"/></div>
    <details className="workbench-widget-manager">
      <summary><span>工作台组件</span><small>已显示 {visible.size} / {allWidgets.length}</small><b>管理组件 <Icon name="plus"/></b></summary>
      <div className="widget-manager-intro"><p>选择显示内容，在“编辑布局”中拖拽和调整大小。</p><button type="button" onClick={preferences.reset}>恢复默认组件</button></div>
      <label className="widget-library-search">查找组件<input type="search" value={query} maxLength={80} placeholder="名称，如龙虎榜、资金、财务" onChange={event => setQuery(event.target.value)}/></label>
      {!preferences.saved && <p role="status" className="widget-preference-warning">本次选择已生效，浏览器暂未能保存。</p>}
      <fieldset><legend>看盘与研究</legend>{choices([...existingWidgets])}</fieldset>
      <fieldset><legend>个股资料与市场数据</legend>{choices([...widgetCatalog])}</fieldset>
      {!allWidgets.some(matches) && <p className="widget-manager-empty">没有匹配的组件。</p>}
    </details>
  </div>;
  return <div className="trading-workbench">
    {header ? createPortal(controls, header) : controls}
    <div id="workbench-canvas" className="sr-only" tabIndex={-1}>看盘组件</div>
    <Dashboard owner={session.meta.app_mode + ':' + session.data.user.id} widgets={allWidgets.filter(item => visible.has(item.id)).map(item => ({ id: item.id, title: item.title, content: content[item.id] }))} onRemove={id => preferences.toggle(id, false)} toolbarTarget={layoutTarget}/>
  </div>;
}
