import type { PropsWithChildren, ReactNode, KeyboardEvent } from 'react';
import { Icon } from './icons';
import { ApiError } from './api';
import { notifiedReadError } from './BackgroundRefresh';

export function Card({ title, children, action, className = '' }: PropsWithChildren<{ title: string; action?: ReactNode; className?: string }>) {
  return <section className={`card ${className}`}><div className="card-heading"><h2>{title}</h2>{action}</div>{children}</section>;
}
export function Empty({ title = '尚未接入', children }: PropsWithChildren<{ title?: string }>) {
  return <div className="empty"><div className="empty-symbol"><Icon name="chart" /></div><strong>{title}</strong><p>{children ?? '数据接入后将在这里显示。'}</p></div>;
}

export function ErrorState({ error, retry }: { error: Error; retry?: () => void }) {
  if (notifiedReadError(error)) return null;
  return <Notice retry={retry}><span>{error.message}</span>{error instanceof ApiError && error.requestId && <small className="request-id">参考编号：{error.requestId}</small>}</Notice>;
}

export function Segmented({ label, options, value, onChange }: { label: string; options: readonly string[]; value: string; onChange: (value: string) => void }) {
  function move(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const next = event.key === 'ArrowRight' ? (index + 1) % options.length : event.key === 'ArrowLeft' ? (index + options.length - 1) % options.length : event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : null;
    if (next === null) return;
    event.preventDefault(); onChange(options[next]);
    (event.currentTarget.parentElement?.children[next] as HTMLButtonElement)?.focus();
  }
  return <div className="segmented" role="radiogroup" aria-label={label}>{options.map((option, i) => <button type="button" role="radio" aria-checked={value === option} tabIndex={value === option ? 0 : -1} onKeyDown={e => move(e, i)} className={value === option ? 'selected' : ''} onClick={() => onChange(option)} key={option}>{option}</button>)}</div>;
}

export function EmptyTable({ columns, title, children }: PropsWithChildren<{ columns: string[]; title: string }>) {
  return <div className="table-wrap" tabIndex={0} role="region" aria-label={`${title}表格`}><table><thead><tr>{columns.map(column => <th scope="col" key={column}>{column}</th>)}</tr></thead><tbody><tr><td colSpan={columns.length}><Empty title={title}>{children}</Empty></td></tr></tbody></table></div>;
}

export function Brand({ artwork = false }: { artwork?: boolean }) {
  return <div className={artwork ? 'brand-artwork' : 'brand'}><div><strong>天衍系统</strong><small>TIANYAN · 量化研究系统</small></div></div>;
}
export function Notice({ children, retry }: PropsWithChildren<{ retry?: () => void }>) {
  return <div className="notice" role="alert">{children}{retry && <button onClick={retry}>重新加载</button>}</div>;
}
