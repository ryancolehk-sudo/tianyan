import type { PropsWithChildren } from 'react';
import { researchRoutes } from '../navigation';

export function ResearchToolbar({ path, day, dateLabel, dateInputLabel = dateLabel, onDayChange, children }: PropsWithChildren<{
  path: string; day: string; dateLabel?: string; dateInputLabel?: string; onDayChange: (value: string) => void;
}>) {
  return <div className="research-toolbar">
    <nav className="tabs" aria-label="复盘研究子页面">{researchRoutes.map(([href, name]) => <a key={href}
      href={`${href}?date=${encodeURIComponent(day)}`} className={href === path ? 'selected' : ''}
      aria-current={href === path ? 'page' : undefined}>{name}</a>)}</nav>
    <div className="research-toolbar-controls">{dateLabel && <label className="date-picker">{dateLabel}<input type="date"
      aria-label={dateInputLabel} value={day} onChange={event => { if (event.target.value) onDayChange(event.target.value); }}/></label>}{children}</div>
  </div>;
}
