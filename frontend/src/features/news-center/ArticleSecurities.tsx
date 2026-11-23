import { chinaTime, safeOriginal } from './model';
import type { NewsArticle } from './model';

export function ArticleSecurities({ article }: { article: NewsArticle }) {
  const subjects = article.securities ?? [];
  if (!subjects.length && article.category !== '公司公告' && article.family !== 'cninfo_announcements') return null;
  return <div className="nc-securities" aria-label="证券主体">{subjects.length ? <>{subjects.map((subject, index) =>
    <span className="nc-security" key={subject.evidence_id + ':' + index}><strong>{subject.name || '简称未提供'}</strong><span className="nc-security-code">{subject.code || '代码未提供'}</span></span>)}
    {article.security_status !== 'source_provided' && <span className="nc-security-pending">主体待核</span>}</>
    : <span className="nc-security-missing">{article.security_status === 'missing' ? '证券主体未提供' : '证券主体待核'}</span>}</div>;
}

export function SecurityEvidence({ article }: { article: NewsArticle }) {
  const subjects = article.securities ?? [];
  if (!subjects.length) return null;
  return <><dt>主体依据</dt><dd>{article.security_status !== 'source_provided' && <p>来源字段已留存，可用时点待核；不作为历史已确认关联。</p>}<ul className="nc-security-evidence">{subjects.map((subject, index) =>
    <li key={subject.evidence_id + ':' + index}><span>{subject.name || '简称未提供'} · {subject.code || '代码未提供'}{subject.exchange && ' · ' + subject.exchange}</span>
      <span>{subject.basis === 'archived_announcement' ? '同公告 ID 的已存来源档案' : '公告原始结构字段'} · {subject.source_name}</span>
      <small>接收 {chinaTime(subject.received_at, true)} · 证据 {subject.evidence_id}</small><small>{subject.source_fields.join('、')}</small><small>校验 {subject.content_hash}</small>
      {safeOriginal(subject.source_url) && <a href={safeOriginal(subject.source_url)} target="_blank" rel="noopener noreferrer">核对主体来源 ↗</a>}</li>)}</ul></dd></>;
}
