import { useEffect, useState } from 'react';
import type { EditionSummary, NewsPeriod } from './model';
import { chinaDay, periodName } from './model';

export function EditionCalendar({ editions, selected, period = 'day', onSelect }: {
  editions: EditionSummary[]; selected: EditionSummary | null; period?: NewsPeriod; onSelect: (day: string) => void;
}) {
  const day = selected?.day ?? editions[0]?.day ?? chinaDay(new Date().toISOString());
  const [month, setMonth] = useState(day.slice(0, 7));
  useEffect(() => { setMonth(day.slice(0, 7)); }, [day]);
  const [year, monthNumber] = month.split('-').map(Number);
  const firstWeekday = (new Date(Date.UTC(year, monthNumber - 1, 1)).getUTCDay() + 6) % 7;
  const dayCount = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
  const monthEditions = editions.filter(edition => edition.day.startsWith(month));
  const days = new Map(monthEditions.map(edition => [edition.day, edition]));
  const moveMonth = (direction: number) => setMonth(new Date(Date.UTC(year, monthNumber - 1 + direction, 1)).toISOString().slice(0, 7));
  const weekday = new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', weekday: 'long' }).format(new Date(day + 'T00:00:00Z'));
  return <section className="nc-calendar" aria-label={`${periodName(period)}归档日历`}>
    <div className="nc-calendar-date">
      <span>{selected ? `第 ${String(selected.issue_number).padStart(3, '0')} 期` : '归档日历'}</span>
      <strong className="nc-dot-date" aria-hidden="true">{day.slice(-2)}</strong>
      <time dateTime={day}>{day.slice(0, 4)} 年 {Number(day.slice(5, 7))} 月</time>
      <span>{weekday}</span>
    </div>
    <div className="nc-calendar-month">
      <div className="nc-calendar-caption"><strong>{monthNumber}月</strong><small>{monthEditions.length ? `已载入 ${monthEditions.length} 期` : '暂无已出刊'}</small></div>
      <div className="nc-calendar-weekdays" aria-hidden="true">{['一', '二', '三', '四', '五', '六', '日'].map(value => <span key={value}>{value}</span>)}</div>
      <div className="nc-calendar-grid">
        {Array.from({ length: firstWeekday }, (_, index) => <span className="nc-calendar-blank" key={'blank-' + index}/>)}
        {Array.from({ length: dayCount }, (_, index) => {
          const date = month + '-' + String(index + 1).padStart(2, '0');
          const edition = days.get(date);
          return <button type="button" key={date} className={`${edition ? 'has-edition' : ''} ${selected?.day === date ? 'is-selected' : ''}`} disabled={!edition} onClick={() => onSelect(date)} aria-label={`${date} ${edition ? '阅读' + periodName(period) : '未出刊'}`} aria-pressed={selected?.day === date} title={`${date} ${edition ? edition.headline || periodName(period) : '未出刊'}`}><span aria-hidden="true">{index + 1}</span>{edition && <i aria-hidden="true"/>}</button>;
        })}
      </div>
      <div className="nc-calendar-move"><button type="button" aria-label="上一月归档" onClick={() => moveMonth(-1)}>‹</button><span>{year}年</span><button type="button" aria-label="下一月归档" onClick={() => moveMonth(1)}>›</button></div>
    </div>
  </section>;
}
