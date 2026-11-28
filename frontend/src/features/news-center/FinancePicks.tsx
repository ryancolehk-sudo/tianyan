import { useState } from 'react';
import type { FinancePick, PickScores } from '../../generated/schema';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { chinaDay, chinaTime } from './model';
import { newsApi } from './newsApi';
import './financePicks.css';

const pickCategories = ['产业催化', '订单与中标', '供应变化', '宏观政策', '公司动态', '市场与资金'] as const;
const dimensions: [keyof PickScores, string, number][] = [
  ['impact', '影响范围', 35], ['evidence', '事实依据', 30], ['novelty', '信息增量', 20], ['timeliness', '时效性', 15],
];

function PickCard({ item, day, model, onSelect }: { item: FinancePick; day: string; model: string; onSelect: (id: string) => void }) {
  const latest = item.reports[0] ?? item;
  const clock = item.published_at ? chinaTime(item.published_at) : item.published_day ? '仅日期' : '入库 ' + chinaTime(item.received_at);
  const when = item.published_at || item.published_day || item.received_at || undefined;
  const full = item.published_at ? chinaTime(item.published_at, true) : item.published_day ?? chinaTime(item.received_at, true);
  const sourceDay = item.published_at ? chinaDay(item.published_at) : item.published_day ?? (item.received_at && chinaDay(item.received_at));
  return <li className="nc-pick-story" id={'pick-' + item.id}>
    <time className="nc-pick-time" dateTime={when} title={full}>{sourceDay && sourceDay !== day && <span>{sourceDay.slice(5).replace('-', '/')} </span>}{clock}</time>
    <article className="nc-pick-card">
      <div className="nc-pick-byline"><span>{item.source_name}</span><span className="nc-pick-category">{item.category}</span>
        <details className="nc-pick-score"><summary aria-label={'AI 评分 ' + item.score + '，查看评分明细'}>AI 评分 <b>{item.score}</b></summary>
          <div className="nc-pick-breakdown"><strong>阅读价值评分</strong><dl>{dimensions.map(([key,label,max]) =>
            <div key={key}><dt>{label}</dt><dd>{item.scores[key]} / {max}</dd></div>)}</dl><small>{model}</small></div></details>
      </div>
      <h3><button type="button" className="nc-pick-title" id={'pick-title-' + item.id} onClick={() => onSelect(item.id)}>{item.title}</button></h3>
      <p className="nc-pick-summary">{item.summary}</p>
      <p className="nc-pick-reason"><strong>推荐理由</strong>{item.reason}</p>
      <footer>{item.reports.length > 1 && <details className="nc-pick-reports"><summary>{item.reports.length} 篇报道</summary>
        {item.reports.map(report => <button type="button" key={report.id} onClick={() => onSelect(report.id)}>
          {report.source_name} · {report.published_at ? chinaTime(report.published_at, true) : report.published_day ?? '发布时间未知'}</button>)}</details>}
        <span>最新来源 {latest.source_name}</span></footer>
    </article>
  </li>;
}

export function FinancePicks({ day, onDay, onSelect }: {
  day: string | null; onDay: (day: string | null) => void; onSelect: (id: string) => void;
}) {
  const read = useCachedLoader(() => newsApi.topicPicks(day), 'topic-picks:' + (day ?? 'latest'));
  const [category, setCategory] = useState<string>('全部');
  const [search, setSearch] = useState('');
  const [unread, setUnread] = useState(false);
  const [rankBy, setRankBy] = useState<'sources' | 'score'>('sources');
  const data = read.data?.data, edition = data?.edition;
  const query = search.trim().toLocaleLowerCase();
  const visible = (data?.items ?? []).filter(item => (category === '全部' || item.category === category) &&
    (!unread || !item.is_read) && (!query || [item.title, item.summary, item.reason].some(text => text.toLocaleLowerCase().includes(query))));
  const ranked = visible.map(item => ({
    item,
    sources: [...new Set([item.source_name, ...item.reports.map(report => report.source_name)])],
  })).filter(entry => rankBy === 'score' || entry.sources.length >= 2)
    .sort((a,b) => rankBy === 'sources' ? b.sources.length - a.sources.length : b.item.score - a.item.score)
    .slice(0,5);
  const follow = (id: string) => {
    document.getElementById('pick-' + id)?.scrollIntoView({block:'center'});
    document.getElementById('pick-title-' + id)?.focus({preventScroll:true});
  };
  return <div className="nc-picks">
    <div className="nc-picks-tools">
      <div role="group" aria-label="精选分类" className="nc-picks-categories">
        {['全部', ...pickCategories].map(name => <button key={name} type="button" aria-pressed={category === name}
          onClick={() => setCategory(name)}>{name}</button>)}
      </div>
      <input type="search" aria-label="搜索精选" placeholder="搜索标题、摘要或推荐理由" value={search} onChange={e => setSearch(e.target.value)}/>
    </div>
    {read.error && <div role="alert" className="nc-board-status">精选读取失败。<button type="button" onClick={read.reload}>重新读取</button></div>}
    {read.busy && !data && <p role="status">正在读取精选…</p>}
    {visible.length > 0 && <section aria-label="本期排行" className="nc-picks-rank">
      <header><h2>{rankBy === 'sources' ? '本期热点' : '本期高分'}</h2>
        <div role="group" aria-label="排行依据" className="nc-picks-rank-switch">
          <button type="button" aria-pressed={rankBy === 'sources'} onClick={() => setRankBy('sources')}>多家报道</button>
          <button type="button" aria-pressed={rankBy === 'score'} onClick={() => setRankBy('score')}>AI 评分</button>
        </div></header>
      {ranked.length ? <ol>{ranked.map(({item,sources},i) => <li key={item.id}><button type="button" onClick={() => follow(item.id)}>
        <span className="nc-pick-position" aria-hidden="true">{i+1}</span><span>{item.title}</span>
        <span className="nc-pick-rank-source">{rankBy === 'sources' ? sources.join(' · ') : item.source_name}</span>
        <strong>{rankBy === 'sources' ? sources.length + ' 家' : item.score}</strong></button></li>)}</ol>
        : <p className="nc-rank-empty">当前精选中还没有两家来源共同报道的事件。</p>}
    </section>}
    {data && <div className="nc-picks-datebar">
      <label><span className="sr-only">精选刊期</span><select aria-label="精选刊期" value={day ?? ''} onChange={e => onDay(e.target.value || null)}>
        <option value="">{!day && edition ? edition.day.replaceAll('-', '/') : '最新一期'}</option>
        {day && !data.days.includes(day) && <option value={day}>{day}</option>}
        {data.days.filter(value => day || value !== edition?.day).map(value => <option key={value} value={value}>{value.replaceAll('-', '/')}</option>)}
      </select></label>
      {edition && <span>{visible.length} 条 · 截至 {chinaTime(edition.cutoff)}</span>}
      <label className="nc-picks-unread"><input type="checkbox" checked={unread} onChange={e => setUnread(e.target.checked)}/>只看未读</label>
    </div>}
    {edition && visible.length > 0 && <ol aria-label="财经精选时间线" className="nc-picks-timeline">
      {visible.map(item => <PickCard key={item.id} item={item} day={edition.day} model={edition.model} onSelect={onSelect}/>)}</ol>}
    {data && !visible.length && <div role="status" className="nc-picks-empty">{!edition ? '该刊期尚未发布。' :
      data.items.length ? '当前筛选没有匹配的精选。' : '本期暂无可展示的入选事件。'}
      {edition && data.items.length > 0 && <button type="button" onClick={() => {setSearch(''); setCategory('全部'); setUnread(false);}}>清除筛选</button>}
      {!edition && day && <button type="button" onClick={() => onDay(null)}>查看最新一期</button>}</div>}
  </div>;
}
