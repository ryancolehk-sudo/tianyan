import { useMemo } from 'react';
import type { NewsItem } from '../../generated/schema';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { useRead } from '../../shared/useRead';
import { Empty, ErrorState, Segmented } from '../../shared/ui';
import { sourceTime } from '../data/SourceRuntime';
import { digestPeriods, editionsFor, formatDay, headlineFor, loadDigest, periodEnd, periodKey, publicationDay, publisherFor, sectionName, sectionOrder, validDay } from './financeDigest';
import type { DigestPeriod } from './financeDigest';
import './financeDaily.css';

function SourceLine({ item }: { item: NewsItem }) {
  return <div className="digest-source-line"><span>{item.source_name}</span><span aria-hidden="true">·</span>{item.published_at ? <time dateTime={item.published_at}>{sourceTime(item.published_at)}</time> : <time dateTime={item.payload.published_day ?? undefined}>{item.payload.published_day} · 仅日期</time>}</div>;
}

function SourceDetails({ item }: { item: NewsItem }) {
  return <details className="digest-source-details"><summary>来源记录</summary><dl><dt>本机接收</dt><dd>{sourceTime(item.received_at)}</dd><dt>记录版本</dt><dd>{item.id}</dd><dt>内容校验</dt><dd><code>{item.content_hash}</code></dd></dl></details>;
}

function EditionCalendar({ day, days, period, onSelect }: { day: string; days: Set<string>; period: DigestPeriod; onSelect: (day: string) => void }) {
  const first = day.slice(0, 7) + '-01';
  const date = new Date(first + 'T00:00:00Z');
  const offset = (date.getUTCDay() + 6) % 7;
  const length = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  return <div className="digest-date-card"><div className="digest-date-sheet"><span>{period === 'day' ? '本期日期' : period === 'week' ? '本周起始' : '本月起始'}</span><strong>{day.slice(-2)}</strong><time dateTime={day}>{formatDay(day, { year: 'numeric', month: 'long' })}<br/>{formatDay(day, { weekday: 'long' })}</time></div><div className="digest-calendar"><div className="digest-calendar-title"><strong>{formatDay(day, { month: 'long' })}</strong><small>点选日期</small></div><div className="digest-calendar-grid">{['一', '二', '三', '四', '五', '六', '日'].map(label => <span className="digest-weekday" key={label}>{label}</span>)}{Array.from({ length: offset }, (_, index) => <span key={'blank-' + index}/>)}{Array.from({ length }, (_, index) => {
    const value = first.slice(0, 8) + String(index + 1).padStart(2, '0');
    const active = days.has(value);
    return <button key={value} type="button" disabled={!active} aria-label={value + (active ? ' 有资讯' : ' 无资讯')} aria-pressed={periodKey(value, period) === day && active} onClick={() => onSelect(value)} title={value}><span>{index + 1}</span></button>;
  })}</div></div></div>;
}

export function FinanceDaily() {
  const navigation = useNavigation();
  const params = new URLSearchParams(navigation.search);
  const period: DigestPeriod = digestPeriods.find(([key]) => key === params.get('period'))?.[0] ?? 'day';
  const selected = params.get('date');
  const read = useRead(loadDigest, { retainOnTransientFailure: true });
  const sources = read.data?.sources ?? [];
  const scope = params.get('scope') === 'announcements' ? '公司公告' : params.get('scope') === 'all' ? '全部资讯' : '综合要闻';
  const items = useMemo(() => (read.data?.items ?? []).filter(item => scope === '全部资讯' || (scope === '公司公告' ? item.payload.category === '公告' : item.payload.category !== '公告')), [read.data, scope]);
  const editions = useMemo(() => editionsFor(items, period), [items, period]);
  const requested = validDay(selected) ? periodKey(selected, period) : null;
  const day = requested ?? editions[0]?.day;
  const edition = editions.find(value => value.day === day);
  const headline = edition ? headlineFor(edition.items) : undefined;
  const groups = sectionOrder.map(name => ({ name, items: edition?.items.filter(item => item.id !== headline?.id && sectionName(item) === name) ?? [] })).filter(group => group.items.length);
  const highlights = groups.flatMap(group => group.items.slice(0, 1));
  for (const item of edition?.items ?? []) if (item.id !== headline?.id && !highlights.some(row => row.id === item.id)) highlights.push(item);
  const sourceCount = new Set(edition?.items.map(item => publisherFor(item, sources))).size;
  const connected = sources.filter(source => source.readable && source.count > 0);
  const publisherCount = new Set(connected.map(source => source.publisher)).size;
  const days = new Set(items.map(publicationDay).filter((value): value is string => value !== null));
  const unknownDates = items.filter(item => !publicationDay(item)).length;
  const readingMinutes = Math.max(1, Math.ceil((edition?.items.reduce((total, item) => total + item.payload.title.length + (item.payload.summary?.length ?? 0), 0) ?? 0) / 350));
  const periodLabel = digestPeriods.find(([key]) => key === period)![1];
  const selectScope = (value: string) => {
    const url = new URL(location.href);
    url.searchParams.set('scope', value === '公司公告' ? 'announcements' : value === '全部资讯' ? 'all' : 'headlines');
    url.searchParams.delete('date'); url.hash = ''; updateLocation(url);
  };
  const selectDate = (value: string, nextPeriod = period) => {
    const url = new URL(location.href);
    url.searchParams.set('view', 'daily');
    url.searchParams.set('period', nextPeriod);
    if (value) url.searchParams.set('date', periodKey(value, nextPeriod)); else url.searchParams.delete('date');
    url.hash = ''; updateLocation(url);
  };
  const monthGroups = [...new Set(editions.map(value => value.day.slice(0, 7)))];
  const index = editions.findIndex(value => value.day === day);
  return <section className="finance-daily" aria-label="财经报刊"><aside className="digest-archive" aria-label="财经往期目录">
    <Segmented label="报刊周期" options={digestPeriods.map(([, label]) => label)} value={periodLabel} onChange={value => selectDate(day ?? '', digestPeriods.find(([, label]) => label === value)![0])}/>
    <div className="digest-archive-heading"><h2>往期</h2><span>{editions.length} 期可读</span></div>
    <label className="digest-date-picker">选择日期<input type="date" aria-label="报刊日期" value={day ?? ''} onChange={event => selectDate(event.target.value)}/></label>
    <nav className="digest-archive-list" aria-label="往期报刊">{monthGroups.map(month => <details key={month} open><summary>{month.slice(0, 4)} 年 {Number(month.slice(5))} 月 <span>{editions.filter(value => value.day.startsWith(month)).length}</span></summary>{editions.filter(value => value.day.startsWith(month)).map(value => <button type="button" className={'digest-edition-link' + (value.day === day ? ' is-current' : '')} aria-current={value.day === day ? 'date' : undefined} key={value.day} onClick={() => selectDate(value.day)}><span className="digest-edition-day"><strong>{value.day.slice(-2)}</strong><small>{period === 'day' ? formatDay(value.day, { weekday: 'short' }) : period === 'week' ? '周报' : '月报'}</small></span><span className="digest-edition-title">{headlineFor(value.items).payload.title}<small>{value.items.length} 篇资讯</small></span></button>)}</details>)}</nav>
    <nav className="digest-mobile-editions" aria-label="往期报刊快捷选择">{editions.map(value => <button type="button" key={value.day} aria-current={value.day === day ? 'date' : undefined} onClick={() => selectDate(value.day)}><strong>{formatDay(value.day, { month: 'numeric', day: 'numeric' })}</strong><span>{headlineFor(value.items).payload.title}</span></button>)}</nav>
    <div className="digest-archive-foot"><span>TIANYAN FINANCE</span><p>从每日资讯，读懂市场脉络。</p><button type="button" onClick={read.reload} disabled={read.busy}>{read.busy ? '正在读取…' : '刷新报刊'} <span aria-hidden="true">↻</span></button></div>
  </aside><div className="digest-paper" aria-busy={read.busy}>
    {read.busy ? <div className="digest-state" role="status"><span className="digest-kicker">TIANYAN · FINANCE</span><h2>正在整理财经{periodLabel}…</h2><p>读取全球信源与已采集资讯</p></div> : read.error ? <div className="digest-state"><ErrorState error={read.error} retry={read.reload}/></div> : <>
      <div className="digest-coverage-toolbar"><Segmented label="日报内容范围" options={['综合要闻', '公司公告', '全部资讯']} value={scope} onChange={selectScope}/><details className="digest-network"><summary>全球信源 <strong>{publisherCount}</strong> 家 · {connected.length} 个频道 <span aria-hidden="true">⌄</span></summary><div className="digest-network-grid">{sources.map(source => <div key={source.family}><strong>{source.name}</strong><span>{source.region} · {source.publisher}</span><small>{source.failed ? '读取失败，可刷新重试' : !source.readable ? '未启用 / 使用范围未核实' : source.count ? `${source.count} 条已存资讯` : '暂无已存条目'}{source.readable && ['error', 'unavailable'].includes(source.state) ? ' · 采集异常' : ''}</small></div>)}</div><a href="/data">管理来源与查看采集状态 ›</a></details></div>
      {read.data?.failed.length ? <div className="digest-partial" role="status">{read.data.failed.join('、')}暂时读取失败，本期仅显示其余来源。<button type="button" onClick={read.reload}>重试</button></div> : null}
      <header className="digest-masthead"><div className="digest-dateline"><span>{day ? period === 'day' ? formatDay(day, { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' }) : `${day} — ${periodEnd(day, period)}` : '每日财经 · 有据可循'}</span><span>全球财经 · 市场 · 产业</span><span>基于已采集资讯</span></div><div className="digest-brand-row"><div><h2><span>财经</span>{periodLabel}</h2><p>天衍系统 <span>·</span> TIANYAN GLOBAL FINANCE</p></div>{day && <EditionCalendar day={day} days={days} period={period} onSelect={selectDate}/>}</div></header>
      {!edition || !headline ? <div className="digest-state"><Empty title={requested ? '本期暂无已采集资讯' : '暂无可编排的财经资讯'}>{requested ? '可选择左侧其他日期，或返回最近一期。' : '查看数据中心的资讯来源状态，采集后即可在这里阅读。'}</Empty>{editions.length ? <button type="button" onClick={() => selectDate(editions[0].day)}>阅读最近一期</button> : <a href="/data">查看资讯来源 ›</a>}</div> : <>
        <div className="digest-statistics"><div><span><strong>{edition.items.length}</strong> 篇资讯</span><span><strong>{sourceCount}</strong> 个来源</span><span><strong>{new Set(edition.items.map(sectionName)).size}</strong> 个主题</span></div><span>约 {readingMinutes} 分钟读完</span></div>
        <div className="digest-columns"><div className="digest-main-column"><section className="digest-lead" aria-labelledby="digest-lead-label"><h3 className="digest-section-label" id="digest-lead-label">头条</h3><div className="digest-lead-story"><span className="digest-lead-category">{sectionName(headline)} <span> / </span> {formatDay(publicationDay(headline)!, { month: '2-digit', day: '2-digit' })}</span><h3><a href={headline.payload.source_url} target="_blank" rel="noopener noreferrer">{headline.payload.title}<span className="digest-story-arrow" aria-hidden="true">↗</span></a></h3>{headline.payload.summary && <p className="digest-lead-summary">{headline.payload.summary}</p>}<div className="digest-lead-bottom"><SourceLine item={headline}/><a className="digest-original" href={headline.payload.source_url} target="_blank" rel="noopener noreferrer">阅读原文 ↗</a></div></div><SourceDetails item={headline}/></section>
          {groups.map((group, groupIndex) => <section className="digest-section" id={'digest-section-' + groupIndex} tabIndex={-1} key={group.name} aria-labelledby={'digest-heading-' + groupIndex}><div className="digest-section-heading"><span>{String(groupIndex + 1).padStart(2, '0')}</span><h3 id={'digest-heading-' + groupIndex}>{group.name}</h3><small>{group.items.length} 篇</small></div>{group.items.map(item => <article className="digest-article" id={'digest-article-' + item.id} tabIndex={-1} key={item.id}><SourceLine item={item}/><h3><a href={item.payload.source_url} target="_blank" rel="noopener noreferrer">{item.payload.title}</a></h3>{item.payload.summary && <p>{item.payload.summary}</p>}<div className="digest-article-bottom"><a className="digest-original" href={item.payload.source_url} target="_blank" rel="noopener noreferrer">原文 ↗</a><SourceDetails item={item}/></div></article>)}</section>)}
        </div><aside className="digest-reading-rail"><section><h3 className="digest-section-label">{period === 'day' ? '今日看点' : '本期看点'}</h3>{highlights.length ? <ol className="digest-highlights">{highlights.slice(0, 3).map((item, number) => <li key={item.id}><span>{number + 1}</span><a href={'#digest-article-' + item.id}><strong>{item.payload.title}</strong><small>{item.source_name}</small></a></li>)}</ol> : <p className="digest-quiet">本期 {edition.items.length} 篇资讯，见左侧头条。</p>}</section>
          <section className="digest-contents"><h3 className="digest-section-label">本期版面</h3>{groups.length ? <ol>{groups.map((group, groupIndex) => <li key={group.name}><a href={'#digest-section-' + groupIndex}><span>{String(groupIndex + 1).padStart(2, '0')}</span><strong>{group.name}</strong><small>{group.items.length} 篇</small></a></li>)}</ol> : <p className="digest-quiet">财经要闻 · 1 篇</p>}</section>
          <section className="digest-source-directory"><h3 className="digest-section-label">本期信源</h3>{[...new Set(edition.items.map(item => publisherFor(item, sources)))].map(name => <p key={name}><span aria-hidden="true">●</span>{name}</p>)}<a href="/data">查看来源状态 ›</a></section>
        </aside></div>
        <nav className="digest-pagination" aria-label="报刊翻期"><button type="button" disabled={index < 0 || index >= editions.length - 1} onClick={() => selectDate(editions[index + 1].day)}>← 上一期</button><span>本期完</span><button type="button" disabled={index <= 0} onClick={() => selectDate(editions[index - 1].day)}>下一期 →</button></nav>
      </>}
      <div className="digest-colophon"><p>天衍财经{periodLabel} · 全球信源，原文可查。</p><p>每个已启用频道读取最近 100 条，往期与周月汇总仅覆盖这些已存内容；同一媒体多个频道按一家信源计数。保留原文语言和出处，未经 AI 改写。{unknownDates > 0 && `${unknownDates} 条发布时间未知，未归入日期报刊。`}</p><a href="/news?view=feed">返回实时列表 ›</a></div>
    </>}
  </div></section>;
}
