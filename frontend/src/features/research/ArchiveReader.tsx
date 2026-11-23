import { useCallback, useState } from 'react';
import type { ArchiveFileInfo, ReviewArchive } from '../../generated/schema';
import { api } from '../../shared/api';
import { Card, Empty, ErrorState, Segmented } from '../../shared/ui';
import { useRead } from '../../shared/useRead';
import { recordedTime } from './recordTypes';
import { ArchiveText } from './ArchiveText';
import { updateLocation, useNavigation } from '../../shared/navigation';

export function useReviewLocation() { return useNavigation().search; }
export function updateReviewLocation(values: Record<string, string | null>) {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(values)) {
    if (value === null) url.searchParams.delete(key); else url.searchParams.set(key, value);
  }
  updateLocation(url);
}

export function ArchiveDatePicker() {
  const day = new URLSearchParams(useReviewLocation()).get('archive_date') ?? '';
  const change = (value: string) => updateReviewLocation({ workspace: 'archive', archive_date: value || null, archive_cursor: null, archive: null, paragraph: null });
  return <><label className="date-picker archive-date-picker">复盘适用日<input type="date" aria-label="归档适用日" value={day} onChange={event => change(event.target.value)}/></label>{day && <button className="archive-all-dates" onClick={() => change('')}>查看全部日期</button>}</>;
}

function archiveURL(params: URLSearchParams, id: string, mode: string) {
  const query = new URLSearchParams(params); query.set('workspace', 'archive'); query.set('archive', id); query.set('report', mode); query.delete('paragraph');
  return '?' + query;
}

function SourceFile({ label, source }: { label: string; source: ArchiveFileInfo }) {
  return <div className="archive-source-file"><h4>{label}</h4><a href={source.url} target="_blank" rel="noreferrer">查看固定来源文件</a><p>{source.path}</p><dl><dt>文件 SHA-256</dt><dd><code>{source.sha256}</code></dd><dt>Git blob</dt><dd><code>{source.blob}</code></dd></dl></div>;
}

function ArchiveReport({ value, mode }: { value: ReviewArchive; mode: string }) {
  const location = new URLSearchParams(useReviewLocation());
  const paragraph = location.has('paragraph') && location.get('archive') !== value.id ? 'unbound' : location.get('paragraph');
  const locate = (index: number | null) => updateReviewLocation({ archive: value.id, report: mode, workspace: 'archive', paragraph: index === null ? null : String(index) });
  const reader = (text: string) => <ArchiveText key={value.id + ':' + mode} text={text} archiveId={value.id} mode={mode} paragraph={paragraph} locate={locate}/>;
  return <>
    <header className="archive-report-header"><div><span className="eyebrow">已保存的研究产出</span><h2>{value.target_trade_day} · 第 {value.source_version} 版</h2><p>依据 {value.based_on_trade_day} 收盘资料 · {value.source_stage}</p></div><span className="badge muted">历史归档</span></header>
    <dl className="archive-times"><div><dt>原稿材料截止</dt><dd>{recordedTime(value.evidence_cutoff_at)}</dd></div><div><dt>原稿定稿时间</dt><dd>{recordedTime(value.source_generated_at)}</dd></div></dl>
    <Segmented label="归档报告视图" options={['原总结', '完整稿']} value={mode === 'full' ? '完整稿' : '原总结'} onChange={name => updateReviewLocation({ report: name === '完整稿' ? 'full' : 'summary', paragraph: null })}/>
    {mode === 'full' ? <>{!value.has_summary && <p className="field-help">此版未附总结，以下展示完整原稿。</p>}{reader(value.full_text)}</> : value.summary_text ? reader(value.summary_text) : <div className="archive-missing-summary"><Empty title="这一版没有附总结卡片">完整稿已保存。不会借用其他版本或生成新的摘要。</Empty><button onClick={() => updateReviewLocation({ report: 'full', paragraph: null })}>阅读这一版完整稿</button></div>}
    <details className="archive-source"><summary>来源、版本与本机导入记录</summary><p>来源仓库：{value.source_repository}</p><p>固定提交：<code>{value.source_commit}</code></p><p>本机导入：{recordedTime(value.imported_at)}</p><p>本机接收时间与原稿定稿时间分别保留。文件hash核对不代表市场事实或观点已被本系统核验。</p><SourceFile label="完整稿原文件" source={value.full_source}/>{value.summary_source && <SourceFile label="同版总结原文件" source={value.summary_source}/>}<p>导入依据：{value.rights_basis}</p></details>
  </>;
}

export function ArchiveReader() {
  const search = useReviewLocation();
  const params = new URLSearchParams(search);
  const day = params.get('archive_date') ?? '';
  const cursor = params.get('archive_cursor');
  const [previous, setPrevious] = useState<Array<string | null>>([]);
  const list = useRead(useCallback(() => api.reviewArchives(day, cursor), [day, cursor]));
  const items = !list.busy && !list.error ? list.data?.data.items ?? [] : [];
  const selected = params.get('archive') ?? items[0]?.id ?? null;
  const detail = useRead(useCallback(() => selected ? api.reviewArchive(selected) : Promise.resolve(null), [selected]));
  const mode = params.get('report') === 'full' ? 'full' : params.get('report') === 'summary' ? 'summary' : detail.data?.data.has_summary === false ? 'full' : 'summary';
  return <section className="archive-workspace" aria-label="旧项目复盘归档阅读">
    <div className="archive-boundary"><strong>历史复盘 · 原文回查</strong><p>阅读旧项目已保存的研究与同版总结。原条件适用于原报告日期，不是当前正式预案；本次阅读不生成分析、不调用模型。</p></div>
    <div className="archive-layout"><Card title="归档目录" className="archive-catalog" action={<button onClick={() => { list.reload(); detail.reload(); }} disabled={list.busy}>刷新归档</button>}>
      {list.busy ? <p role="status">正在读取复盘归档…</p> : list.error ? <ErrorState error={list.error} retry={list.reload}/> : !items.length ? <Empty title={day ? '这个适用日没有已导入的复盘' : '尚未导入历史复盘'}>这里仅列出当前账号已保存的归档。真实报告通过本机私下导入，代码更新不会自动生成研究内容。</Empty> : <nav className="archive-list" aria-label="已导入的复盘版本">{items.map(item => <a key={item.id} href={archiveURL(params, item.id, mode)} aria-current={selected === item.id ? 'true' : undefined} onClick={event => { if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); updateReviewLocation({ archive: item.id, workspace: 'archive', paragraph: null }); }}><span>{item.target_trade_day}<b>第 {item.source_version} 版</b></span><small>依据 {item.based_on_trade_day} · {item.source_stage}</small><small>{item.has_summary ? '附同版原总结' : '仅完整稿 · 未附总结'}</small></a>)}</nav>}
      <div className="archive-pager"><button disabled={!cursor || list.busy} onClick={() => { updateReviewLocation({ archive_cursor: previous.at(-1) ?? null, archive: null, paragraph: null }); setPrevious(previous.slice(0, -1)); }}>上一页</button><button disabled={!list.data?.data.next_cursor || list.busy} onClick={() => { setPrevious([...previous, cursor]); updateReviewLocation({ archive_cursor: list.data?.data.next_cursor ?? null, archive: null, paragraph: null }); }}>下一页</button></div><p className="field-help">按适用日、原版号排列。目录第一份不代表现在有效。</p>
    </Card><div className="card archive-reading" aria-label="复盘归档正文">
      {list.busy && !params.get('archive') ? <p role="status">正在读取归档目录…</p> : list.error && !params.get('archive') ? <Empty title="目录读取未完成">修复左侧错误后重新读取，不将故障当成没有归档。</Empty> : !selected ? <Empty title="选择一份已保存的复盘">导入后可阅读原总结、完整稿并核对来源。</Empty> : detail.error ? <ErrorState error={detail.error} retry={detail.reload}/> : detail.busy || detail.data?.data.id !== selected ? <p role="status">正在读取原稿…</p> : <ArchiveReport value={detail.data.data} mode={mode}/>}
    </div></div>
  </section>;
}
