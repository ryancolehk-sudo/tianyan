import { useState } from 'react';
import { Icon } from '../../shared/icons';
import { features, featureModules, needs } from './featureCatalog';
import type { FeatureModule, NeedId, SystemFeature } from './featureCatalog';
import { coverageReader } from './featureCoverage';
import type { Candidate, CoverageInput, NeedCoverage } from './featureCoverage';
import { dateTime } from './shared';
import { CoverageRepair } from './CoverageRepair';

type Filter = 'interface' | 'all' | 'gap' | 'unavailable' | 'verify' | 'connected' | 'local';
const filters: [Filter, string][] = [['all', '全部功能'], ['gap', '数据有缺口'], ['interface', '缺少接口的数据项'], ['unavailable', '功能未提供'], ['verify', '待核验'], ['connected', '已接入'], ['local', '本机功能']];
type Requirement = NeedCoverage & { id: NeedId; name: string };
function summarize(feature: SystemFeature, required: Requirement[]): { filter: Filter; label: string; detail: string } {
  if (!required.length) return { filter: 'local', label: '本机功能', detail: feature.detail ?? '使用本机记录' };
  const unavailable = required.find(item => item.featureUnavailable);
  if (unavailable) return { filter: 'unavailable', label: '功能未提供', detail: unavailable.detail ?? unavailable.label };
  const gaps = required.filter(item => ['missing', 'setup', 'partial'].includes(item.state));
  const pending = required.filter(item => item.state === 'verify');
  const names = (items: Requirement[]) => items.map(item => item.name).join('、');
  if (gaps.length) {
    const some = required.some(item => item.state === 'connected' || item.state === 'partial');
    const missing = gaps.some(item => item.state === 'missing');
    const sameReason = gaps.length === required.length && gaps.every(item => item.label === gaps[0].label);
    return { filter: 'gap', label: some ? '部分接入' : gaps.every(item => item.interfaceGap) ? '缺少可用接口' : sameReason ? gaps[0].label : missing ? '缺少接口' : '待接入',
      detail: '待补齐 · ' + names(gaps) };
  }
  if (pending.length) return { filter: 'verify', label: '待核验', detail: names(pending) };
  return { filter: 'connected', label: '已接入', detail: names(required) };
}

function Sources({ need, choose, manage }: { need: Requirement; choose: (id: string) => void; manage: (item: Candidate) => void }) {
  const rows = need.candidates ?? [];
  const supplying = rows.filter(c => c.state === 'connected' || c.state === 'partial');
  const other = rows.filter(c => !supplying.includes(c));
  const shown = supplying.length ? supplying : rows;
  const route = needs[need.id].kind === 'ports' && rows[0]?.route;
  function source(item: Candidate) {
    return <li key={item.providerId + '/' + (item.route?.id ?? item.sourceId ?? '')}>
      <span><strong>{item.name}</strong> · {item.label}{item.option && <> · {item.option.billing_label}</>}</span>
      {item.option?.last_success_at && <small>最近成功 {dateTime(item.option.last_success_at)}</small>}
      {item.option?.scope && <small>{item.option.scope}</small>}
      {item.option?.implemented !== false && <button type="button" className="dc-link" onClick={() => manage(item)}>连接设置</button>}
    </li>;
  }
  return <div className="fc-need"><div className="fc-need-main"><strong>{need.name}</strong><span className={'fc-state fc-' + need.state}>{need.label}</span>
    {route && route.routable !== false && <button type="button" className="dc-link" onClick={() => choose(route.id)}>选择来源</button>}
    {need.href && <a href={need.href}>{needs[need.id].kind === 'model' ? 'AI 设置' : '查询设置'}</a>}
  </div>{need.detail && <small>{need.detail}</small>}
    {!!rows.length && <details className="fc-candidates"><summary>{shown.map(c => c.name).filter((name, i, all) => all.indexOf(name) === i).slice(0, 2).join('、')}{shown.length > 2 ? '等 ' + shown.length + ' 个来源' : ''}{supplying.length > 0 ? '供数' : ''}<Icon name="chevron"/></summary>
      <ul>{shown.map(source)}</ul>
      {supplying.length > 0 && other.length > 0 && <details><summary>其他候选来源（{other.length}）</summary><ul>{other.map(source)}</ul></details>}
    </details>}
  </div>;
}

type Props = { input: CoverageInput; manageSources: () => void; choose: (id: string) => void; manage: (row: Candidate) => void;
  refresh: () => void; busy: boolean; account: string; csrf?: string; active: boolean };
export function FunctionCoverage({ input, manageSources, choose, manage, refresh, busy, account, csrf, active }: Props) {
  const [module, setModule] = useState<FeatureModule | '全部模块'>('全部模块');
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState('');
  const [repairing, setRepairing] = useState(false);
  const resolve = coverageReader(input);
  const checks = new Map<NeedId, Requirement>((Object.keys(needs) as NeedId[]).map(id => [id, { id, name: needs[id].name, ...resolve(needs[id]) }]));
  const scoped = features.filter(f => module === '全部模块' || f.module === module).map(feature => {
    const required = feature.needs.map(id => checks.get(id)!);
    return { feature, required, ...summarize(feature, required) };
  });
  const missingInterfaces = new Set(scoped.flatMap(row => row.required.filter(need => need.interfaceGap).map(need => need.id)));
  const needle = query.trim().toLowerCase();
  const shown = scoped.filter(row => (filter === 'all' || row.filter === filter || filter === 'interface' && row.required.some(need => need.interfaceGap)) && (!needle ||
    [row.feature.module, row.feature.name, ...row.required.map(item => item.name)].join(' ').toLowerCase().includes(needle)));
  const overview = module === '全部模块' && filter === 'all' && !needle;
  return <section className="fc-dashboard" aria-labelledby="fc-title">
    <div className="dc-section-heading fc-heading"><div><h2 id="fc-title">功能数据覆盖</h2><p>按本机取数记录核对接入情况，具体交易日和标的范围可在功能内查看。</p></div>
      <div className="dc-port-actions"><button className="dc-primary" type="submit" form="fc-repair-form" disabled={!csrf || !input.matrix || !input.index || busy || repairing}
        title={csrf ? '检查全部功能并修复可恢复的数据缺口' : '需要管理员权限'}>{repairing ? '正在修复…' : '一键修复'}</button>
        <button type="button" onClick={manageSources}>管理数据来源</button>
        <button type="button" onClick={refresh} disabled={busy}><Icon name="refresh"/>刷新</button></div></div>
    {csrf && <CoverageRepair key={account} account={account} csrf={csrf} active={active} revision={input.matrix?.revision} checks={checks} manage={manage} choose={choose} refresh={refresh} onBusy={setRepairing}/>}
    <div className="fc-toolbar"><div className="fc-filters" role="group" aria-label="按接入情况筛选">
      {filters.map(([key, name]) => <button type="button" key={key} aria-pressed={key === filter} onClick={() => { setFilter(key); setExpanded(''); }}>
        {name}<b>{key === 'interface' ? missingInterfaces.size : key === 'all' ? scoped.length : scoped.filter(row => row.filter === key).length}</b></button>)}
    </div><div className="fc-lookup"><select aria-label="按系统模块筛选" value={module} onChange={event => { setModule(event.target.value as typeof module); setExpanded(''); }}>
      {(['全部模块', ...featureModules] as const).map(name => <option key={name}>{name}</option>)}</select>
      <label className="fc-search"><Icon name="search"/><input type="search" placeholder="查找功能或所需数据" aria-label="查找功能或所需数据" value={query} onChange={event => setQuery(event.target.value)}/></label></div></div>
    {busy && !input.matrix && <p className="fc-reading" role="status">正在读取本机接入状态…</p>}
    {overview ? <div className="fc-overview" role="group" aria-label="各模块功能覆盖">
      <div className="fc-module-head" aria-hidden="true"><span>系统模块</span><span>涉及的功能</span><span>覆盖情况</span></div>
      {featureModules.map(name => {
        const rows = scoped.filter(row => row.feature.module === name);
        const gaps = rows.filter(row => row.filter === 'gap');
        const unavailable = rows.filter(row => row.filter === 'unavailable');
        const pending = rows.filter(row => row.filter === 'verify');
        const affected = [...gaps, ...unavailable];
        const examples = affected.length ? affected : pending.length ? pending : rows;
        return <button key={name} type="button" className="fc-module-row" aria-label={'查看' + name + '的' + rows.length + '项功能'} onClick={() => setModule(name)}>
          <span className="fc-module-name"><strong>{name}</strong><small>{rows.length} 项功能</small></span>
          <span className="fc-detail-text">{examples.slice(0, 3).map(row => row.feature.name).join('、')}{examples.length > 3 ? ' 等' : ''}</span>
          <span className="fc-module-status">{gaps.length > 0 && <b className="fc-gap">{gaps.length} 项有缺口</b>}
            {unavailable.length > 0 && <small>{unavailable.length} 项未提供</small>}
            {pending.length > 0 && <small>{pending.length} 项待核验</small>}
            {!affected.length && !pending.length && (rows.every(row => row.filter === 'local') ? '本机功能' : '已接入')}
            <Icon name="arrow"/></span>
        </button>;
      })}
    </div> : shown.length > 0 ? <div className="fc-list">
      <div className="fc-columns" aria-hidden="true"><span>系统功能</span><span>所需数据或当前缺口</span><span>接入情况</span></div>
      {featureModules.map(group => {
        const rows = shown.filter(row => row.feature.module === group);
        return !!rows.length && <section key={group} aria-label={group + '功能'}><h3>{group}<small>{rows.length} 项</small></h3>
          {rows.map(({ feature, required, filter: state, label, detail }) => <div key={feature.id} className="fc-feature">
            <button type="button" aria-expanded={expanded === feature.id} aria-controls={'fc-detail-' + feature.id} className="fc-row"
              onClick={() => setExpanded(expanded === feature.id ? '' : feature.id)}>
              <span className="fc-name"><Icon name="chevron"/>{feature.name}</span><span className="fc-detail-text">{detail}</span><span className={'fc-state fc-' + state}>{label}</span>
            </button>
            {expanded === feature.id && <div id={'fc-detail-' + feature.id} className="fc-expanded">
              {required.map(item => <Sources key={item.id} need={item} choose={choose} manage={manage}/>)}
              <div className="fc-open">{required.length > 0 && feature.detail && <small>{feature.detail}</small>}<a href={feature.href}>打开{feature.module === '数据管理' && feature.id === 'data-connections' ? '接入设置' : '功能页面'} <Icon name="arrow"/></a></div>
            </div>}
          </div>)}
        </section>;
      })}
    </div> : <p className="fc-empty" role="status">{needle ? '没有匹配的功能或数据。' : '此范围内没有符合当前状态的功能。'} <button type="button" className="dc-link" onClick={() => { setQuery(''); setFilter('all'); }}>显示全部功能</button></p>}
  </section>;
}
