import { useEffect, useState } from 'react';
import { tomorrowApi } from './api';
import { References } from './Report';
import { displayTime, records, safeUrl, text } from './model';
import type { DossierKind, DossierPage, EvidenceRef } from './model';

const kinds: Array<[DossierKind, string]> = [['stock', '候选标的'], ['sector', '概念板块'], ['event', '事件']];
const fieldNames: Record<string, string> = { close: '收盘', open: '开盘', high: '最高', low: '最低', pre_close: '前收盘', pct_chg: '涨跌幅', pct_change: '涨跌幅', amount: '成交额', vol: '成交量', up_limit: '涨停价', down_limit: '跌停价', title: '标题', text: '正文', body: '正文', content: '正文', summary: '摘要', published_at: '发布时间', modified_at: '更正时间', turnover_rate: '换手率', up_num: '上涨数量', down_num: '下跌数量' };
function at(row: Record<string, unknown>, path: string): unknown {
  let value: unknown = row;
  for (const part of path.split('.')) { if (!value || typeof value !== 'object' || Array.isArray(value)) return null; value = (value as Record<string, unknown>)[part]; }
  return value;
}
function valueText(value: unknown) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const fact = value as Record<string, unknown>;
    if ('value' in fact) { const result = text(fact.value); return result === '—' ? result : `${result}${fact.unit ? ' ' + text(fact.unit) : ''}`; }
  }
  return text(value);
}
function title(item: Record<string, unknown>, kind: DossierKind) { return text(item.name ?? item.title ?? item.code ?? item.ts_code ?? (kind === 'event' ? '未命名事件' : '未命名对象')); }
function rowEvidence(item: Record<string, unknown>) {
  const rows = [...records(item.evidence)];
  if (item.row_reference && typeof item.row_reference === 'object') rows.push(item.row_reference as Record<string, unknown>);
  const facts = item.facts && typeof item.facts === 'object' ? Object.values(item.facts) : [];
  for (const fact of facts) if (fact && typeof fact === 'object') rows.push(...records((fact as Record<string, unknown>).evidence));
  for (const path of ['calculations.support_resistance', 'calculations.volume_to_prior5_mean', ...[1, 5, 20, 60].map(day => `calculations.returns_pct.${day}`)]) {
    const metric = at(item, path); if (metric && typeof metric === 'object') rows.push(...records((metric as Record<string, unknown>).evidence));
  }
  const seen = new Set<string>();
  return rows.filter(row => { const key = [row.id, row.source_row, Array.isArray(row.fields) ? row.fields.join(',') : ''].join(':'); if (seen.has(key)) return false; seen.add(key); return true; });
}

function ObjectDetail({ item, kind, refs }: { item: Record<string, unknown>; kind: DossierKind; refs: EvidenceRef[] }) {
  const [allEvidence, setAllEvidence] = useState(false);
  const evidence = rowEvidence(item);
  const ids = [...new Set([...(Array.isArray(item.evidence_refs) ? item.evidence_refs.filter((id): id is string => typeof id === 'string') : []), ...evidence.map(row => row.id).filter((id): id is string => typeof id === 'string')])];
  const refMap = new Map(refs.map(ref => [ref.id, ref]));
  const factFields = kind === 'stock' ? ['close', 'open', 'high', 'low', 'pre_close', 'pct_chg', 'amount', 'vol', 'up_limit', 'down_limit'] : ['pct_change', 'turnover_rate', 'up_num', 'down_num'];
  const limits = Array.isArray(item.limitations) ? item.limitations.map(text) : item.limitation ? [text(item.limitation)] : [];
  const body = item.content ?? item.summary ?? item.text;
  return <article className="tp-dossier-detail" aria-label="底稿对象详情"><h4>{title(item, kind)}</h4>{typeof item.code === 'string' && item.code !== item.name && <p className="tp-muted">{text(item.code)}</p>}
    {kind === 'event' ? <><dl className="tp-dossier-fields"><div><dt>发布时间</dt><dd>{item.published_at ? displayTime(String(item.published_at)) : text(item.published_day)}</dd></div><div><dt>正文充分性</dt><dd>{typeof item.content_sufficient === 'boolean' ? item.content_sufficient ? '已保存足够正文' : '正文不足' : '—'}</dd></div><div><dt>关联标的</dt><dd>{text(item.stock_codes)}</dd></div></dl>{body != null && <details><summary>查看已保存正文或摘要</summary><p className="tp-prose">{text(body)}</p></details>}{safeUrl(String(item.source_url ?? '')) && <a href={safeUrl(String(item.source_url))!} target="_blank" rel="noopener noreferrer">打开原始出处 ↗</a>}</> : <><h5>已有事实</h5><dl className="tp-dossier-fields">{factFields.map(field => <div key={field}><dt>{fieldNames[field]}</dt><dd>{valueText(at(item, 'facts.' + field))}</dd></div>)}</dl><h5>关键计算</h5><dl className="tp-dossier-fields">{[1, 5, 20, 60].map(day => <div key={day}><dt>{day} 日价格变化</dt><dd>{valueText(at(item, `calculations.returns_pct.${day}`))}</dd></div>)}{kind === 'stock' ? <><div><dt>量比（对前五日均量）</dt><dd>{valueText(at(item, 'calculations.volume_to_prior5_mean'))}</dd></div><div><dt>历史支撑参考</dt><dd>{valueText(at(item, 'calculations.support_resistance.support_candidate'))}</dd></div><div><dt>历史压力参考</dt><dd>{valueText(at(item, 'calculations.support_resistance.resistance_candidate'))}</dd></div></> : <><div><dt>已保存成分数量</dt><dd>{valueText(at(item, 'calculations.members'))}</dd></div><div><dt>成分上涨比例</dt><dd>{valueText(at(item, 'calculations.member_up_share_pct'))}</dd></div><div><dt>成分涨停数量</dt><dd>{valueText(at(item, 'calculations.member_limit_up_n'))}</dd></div></>}</dl>{Array.isArray(item.candidate_reasons) && <p className="tp-muted">候选原因：{text(item.candidate_reasons)}</p>}</>}
    {limits.length > 0 && <ul className="tp-limitations">{limits.map((limit, i) => <li key={i}>{limit}</li>)}</ul>}
    {evidence.length > 0 && <details className="tp-dossier-evidence"><summary>原始依据的位置（{evidence.length}）</summary><div className="tp-table-wrap"><table><thead><tr><th>来源</th><th>原件行号（从 0 开始）</th><th>对应字段</th></tr></thead><tbody>{(allEvidence ? evidence : evidence.slice(0, 20)).map((row, i) => <tr key={i}><td>{refMap.get(String(row.id))?.source_name ?? text(row.source_name)}</td><td>{text(row.source_row)}</td><td>{Array.isArray(row.fields) && row.fields.length ? [...new Set(row.fields.map(field => fieldNames[String(field)] ?? '其他来源字段'))].join('、') : '—'}</td></tr>)}</tbody></table></div>{evidence.length > 20 && !allEvidence && <button onClick={() => setAllEvidence(true)}>查看其余原始依据</button>}</details>}
    <References ids={ids} refs={refs}/>
  </article>;
}

export function DossierBrowser({ runId, refs }: { runId: string; refs: EvidenceRef[] }) {
  const [open, setOpen] = useState(false); const [kind, setKind] = useState<DossierKind>('stock');
  const [offset, setOffset] = useState(0); const [retry, setRetry] = useState(0); const [selected, setSelected] = useState<number | null>(null);
  const [page, setPage] = useState<{ key: string; data: DossierPage } | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const queryKey = `${runId}:${kind}:${offset}:${retry}`;
  const current = page?.key === queryKey ? page.data : null;
  useEffect(() => {
    if (!open) return;
    let active = true; setPage(null); setSelected(null); setError(''); setBusy(true);
    tomorrowApi.objects(runId, kind, offset).then(value => { if (active) setPage({ key: queryKey, data: value.data }); }).catch(error => { if (active) setError(error instanceof Error ? error.message : '底稿暂不可读取。'); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [open, queryKey, runId, kind, offset]);
  function chooseKind(value: DossierKind) { setSelected(null); setOffset(0); setKind(value); }
  return <details className="tp-panel tp-dossier" onToggle={event => { const next = event.currentTarget.open; setOpen(next); if (!next) { setPage(null); setSelected(null); } }}><summary>完整研究底稿 <span>按页查看本次冻结的全部对象</span></summary>{open && <><p className="tp-muted">这里保留本次冻结的全部候选、概念和事件。正文深研范围单独展示，暂未深研不代表缓存中没有该对象。</p><nav className="tp-dossier-kinds" aria-label="底稿对象类型">{kinds.map(([value, name]) => <button key={value} aria-pressed={kind === value} className={kind === value ? 'primary' : ''} onClick={() => chooseKind(value)}>{name}</button>)}</nav>{busy && <p className="tp-muted" role="status">正在读取本页底稿…</p>}{error && <div className="tp-error" role="alert">{error}<button onClick={() => setRetry(value => value + 1)}>重试本页底稿</button></div>}{current && <><div className="tp-dossier-count">{kinds.find(([value]) => value === kind)?.[1]}共 {current.total} 条<span>资料截止：{displayTime(current.as_of_time)}</span></div><div className="tp-dossier-list">{current.items.map((item, i) => <button key={i} aria-pressed={selected === i} onClick={() => setSelected(i)}><b>{title(item, kind)}</b>{kind !== 'event' && <small>{text(item.code ?? item.ts_code)}</small>}<span>查看详情 →</span></button>)}{!current.items.length && <p className="tp-muted">本页暂无可见对象。</p>}</div>{selected !== null && current.items[selected] && <ObjectDetail key={`${queryKey}:${selected}`} item={current.items[selected]} kind={kind} refs={refs}/>}</>}
    <div className="tp-actions tp-dossier-paging"><button disabled={busy || offset <= 0} onClick={() => { setSelected(null); setOffset(value => Math.max(0, value - 10)); }}>上一页底稿</button><span className="tp-muted">第 {Math.floor(offset / 10) + 1} 页{current ? ` · 本页 ${current.items.length} 条` : ''}</span><button disabled={busy || !current || offset + current.items.length >= current.total} onClick={() => { setSelected(null); setOffset(value => value + 10); }}>下一页底稿</button></div></>}</details>;
}
