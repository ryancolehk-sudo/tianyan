import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { AIOverview, Coverage, MarketRibbon } from './Components';
import type { PageProps } from './Components';
import type { WorkbenchData } from './types';
import { Overview } from './Overview';
const ResearchWorkspace = lazy(() => import('./ResearchWorkspace').then(module => ({ default: module.ResearchWorkspace })));
const ScreeningPanel = lazy(() => import('../screening/ScreeningPanel').then(module => ({ default: module.ScreeningPanel })));

const panes = [['monitor','盘面'],['research','板块与个股'],['screen','条件选股'],['ai','AI 观察'],['coverage','数据覆盖']] as const;
type Pane = typeof panes[number][0];

export function OverviewWorkspace(props: PageProps & {fundsData: WorkbenchData; selectFund: (code: string) => void; selectCutoff: (value: string) => void}) {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  const requested = query.get('workspace') || (location.hash === '#board-direction' ? 'research' : 'monitor');
  const pane: Pane = panes.find(([key]) => key === requested)?.[0] ?? 'monitor';
  // Keep visited panes mounted: switching away must not discard a screening draft or list position.
  const [visited, setVisited] = useState<Set<Pane>>(() => new Set(['monitor', pane]));
  const researchPane = useRef<HTMLElement>(null);
  useEffect(() => { setVisited(previous => previous.has(pane) ? previous : new Set([...previous, pane])); }, [pane]);
  useEffect(() => { if (pane === 'research') researchPane.current?.focus({preventScroll:true}); }, [pane, query.get('stock')]);
  function choose(value: Pane) {
    const url = new URL(location.href); url.searchParams.set('workspace', value); url.hash = ''; updateLocation(url);
  }
  function inspect(code: string) {
    if (!code) { props.selectStock(''); return; }
    const url = new URL(location.href);
    url.searchParams.set('stock', code); url.searchParams.set('workspace', 'research');
    url.searchParams.set('research_scope', 'all'); url.searchParams.set('research_detail', '1');
    url.searchParams.delete('instrument'); url.hash = ''; updateLocation(url);
  }
  return <div className="ty-overview-screen">
    <MarketRibbon data={props.data} asOfTime={props.asOfTime}/>
    <nav className="ty-workspace-switch" aria-label="综合盯盘工作区">{panes.map(([key,label]) => <button key={key} type="button" aria-pressed={pane === key} aria-controls={'overview-pane-'+key} onClick={() => choose(key)}>{label}</button>)}</nav>
    <div className="ty-workspace-panes">
      <section id="overview-pane-monitor" className="ty-workspace-pane" aria-label="盘面工作区" hidden={pane !== 'monitor'}><Overview {...props}/></section>
      <section ref={researchPane} tabIndex={-1} id="overview-pane-research" className="ty-workspace-pane" aria-label="板块与个股工作区" hidden={pane !== 'research'}>{(visited.has('research') || pane === 'research') && <Suspense fallback={<p role="status">正在载入板块与个股…</p>}><ResearchWorkspace {...props} embedded/></Suspense>}</section>
      <section id="overview-pane-screen" className="ty-workspace-pane ty-workspace-scroll" aria-label="条件选股工作区" hidden={pane !== 'screen'}>{(visited.has('screen') || pane === 'screen') && <Suspense fallback={<p role="status">正在载入条件选股…</p>}><ScreeningPanel session={props.session} day={props.data.trade_day} asOfTime={props.asOfTime} onSelect={inspect} defaultOpen/></Suspense>}</section>
      <section id="overview-pane-ai" className="ty-workspace-pane ty-workspace-scroll" aria-label="AI 观察工作区" hidden={pane !== 'ai'}>{(visited.has('ai') || pane === 'ai') && <AIOverview session={props.session} tradeDay={props.data.trade_day} asOfTime={props.asOfTime}/>}</section>
      <section id="overview-pane-coverage" className="ty-workspace-pane ty-workspace-scroll" aria-label="数据覆盖工作区" hidden={pane !== 'coverage'}>{(visited.has('coverage') || pane === 'coverage') && <Coverage data={props.data} asOfTime={props.asOfTime} selectCutoff={props.selectCutoff}/>}</section>
    </div>
  </div>;
}
