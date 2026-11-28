import { useEffect, useState } from 'react';
import { tomorrowApi } from './api';
import { displayTime, slotLabel } from './model';
import type { RunView } from './model';

export function ReportHistory({ back, openDay, openReport }: {
  back: () => void; openDay: (day: string) => void; openReport: (run: RunView) => void;
}) {
  const [day, setDay] = useState('');
  const [retry, setRetry] = useState(0);
  const [items, setItems] = useState<RunView[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setItems(null); setError('');
    tomorrowApi.runs(day).then(value => {
      if (active) setItems(value.data.items.filter(run => run.report_id));
    }).catch(err => { if (active) setError(err instanceof Error ? err.message : '历史预案读取失败'); });
    return () => { active = false; };
  }, [day, retry]);
  return <div className="pr-ground pr-reader"><article className="pr-paper">
    <div className="pr-heading"><h1>历史预案</h1><button className="pr-history" onClick={back}>返回预案</button></div>
    <div className="pr-search-date"><label>按收盘日期查找 <input type="date" value={day} onChange={e => setDay(e.target.value)}/></label>
      {day && <button onClick={() => setDay('')}>显示最近预案</button>}</div>
    {error ? <p role="alert">{error} <button onClick={() => setRetry(n => n + 1)}>重新读取</button></p>
      : items === null ? <p role="status">正在读取预案…</p>
      : items.length ? <ul className="pr-archive">{items.map(item =>
        <li key={item.id}><button onClick={() => openReport(item)}>
          <strong>{item.target_trade_day || '待确定交易日'} 交易预案</strong>
          <span>{item.source_trade_day} 收盘 · {slotLabel[item.slot]} · {displayTime(item.created_at)}</span>
          <b aria-hidden="true">阅读 ›</b></button></li>)}</ul>
      : <div className="pr-status"><h2>{day ? '这天还没有预案' : '暂无已生成的预案'}</h2>
        {day && <button onClick={() => openDay(day)}>前往这一天</button>}</div>}
  </article></div>;
}
