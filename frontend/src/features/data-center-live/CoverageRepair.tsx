import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from '../../shared/api';
import { monitorHref } from '../../shared/dataMonitorLink';
import type { RepairReceipt } from '../../generated/schema';
import type { Envelope } from './api';
import { features, needs } from './featureCatalog';
import type { NeedId } from './featureCatalog';
import type { Candidate, NeedCoverage } from './featureCoverage';
import { dateTime, messageOf } from './shared';

export type CoverageRequirement = NeedCoverage & { id: NeedId; name: string };
type Target = { need_id: NeedId; port_ids: string[]; source_ids: string[]; gap: boolean };
type Input = { expected_revision: number; targets: Target[] };
type Report = { started_at: string; by_need: Partial<Record<NeedId, string[]>>; actions: RepairReceipt[] };
type Attempt = { key: string; body: Input };
type Group = 'manual' | 'pending' | 'limited' | 'verified';
const labels: Record<Group, string> = { manual: '需要你处理', pending: '后台处理中', limited: '当前受限', verified: '已核验' };
const groups = Object.keys(labels) as Group[];
const repairUrl = '/api/v1/data-center/coverage-repair';
const usedNeeds = [...new Set(features.flatMap(f => f.needs))];

function targets(checks: Map<NeedId, CoverageRequirement>): Target[] {
  return usedNeeds.map(id => {
    const need = needs[id], check = checks.get(id)!;
    const gap = check.state !== 'connected';
    const candidates = (check.candidates ?? []).filter(c => c.option?.implemented && (gap || c.state === 'connected'));
    const sources = candidates.flatMap(c => need.kind === 'ports' || need.kind === 'news'
      ? c.option?.source_ids ?? [] : c.sourceId ? [c.sourceId] : []);
    return { need_id: id, port_ids: gap && need.kind === 'ports' ? need.ids : [],
      source_ids: [...new Set(sources)], gap };
  });
}

function classify(check: CoverageRequirement, actions: RepairReceipt[]): Group {
  if (actions.some(a => a.area === 'history' && a.state === 'manual')) return 'manual';
  const incomplete = check.state !== 'connected';
  if (incomplete && (check.featureUnavailable || check.state === 'missing')) return 'limited';
  const data = actions.filter(a => a.area !== 'history');
  if (incomplete && (!data.length || data.every(a => a.state === 'manual' || a.state === 'limited'))) {
    return data.length && data.every(a => a.state === 'limited') ? 'limited' : 'manual';
  }
  if (actions.some(a => ['working', 'waiting', 'available'].includes(a.state))) return 'pending';
  if (actions.some(a => a.state === 'limited')) return 'limited';
  return incomplete || actions.some(a => a.state === 'manual') ? 'manual' : 'verified';
}

function nextStep(check: CoverageRequirement) {
  if (check.featureUnavailable) return check.detail ?? '此功能尚未提供可用接口。';
  if (check.label.includes('权限')) return '先在供应商账号开通此项权限。若采集已因权限暂停，在该项「采集参数」核对并保存，然后点「检测单项能力」。';
  if (check.label.includes('密钥')) return '在连接设置填写本人的密钥，保存后检测此数据项。';
  if (check.label.includes('积分')) return '到连接设置核对计费与积分授权，再决定是否启用。';
  if (check.label.includes('关闭') || check.label.includes('停用') || check.label.includes('暂停')) return '在来源设置核对停用原因，按需恢复采集。';
  if (needs[check.id].kind === 'model') return check.label + '。在 AI 设置选择模型并检测连接。';
  if (needs[check.id].kind === 'ifind') return '在查询设置保存 iFinD 连接，测试本人账号的查询权限。';
  return check.detail ?? check.label;
}

function previousAttempt(storageKey: string): Attempt | null {
  try {
    const raw = sessionStorage.getItem(storageKey);
    if (!raw) return null;
    const a = JSON.parse(raw);
    return typeof a.key === 'string' && Array.isArray(a.body?.targets) ? a : null;
  } catch { return null; }
}

type Props = { account: string; csrf: string; active: boolean; revision?: number; checks: Map<NeedId, CoverageRequirement>;
  manage: (row: Candidate) => void; choose: (port: string) => void; refresh: () => void; onBusy: (value: boolean) => void };
export function CoverageRepair({ account, csrf, active, revision, checks, manage, choose, refresh, onBusy }: Props) {
  const storageKey = 'coverage-repair-attempt:' + account;
  const attempt = useRef<Attempt | null>(previousAttempt(storageKey));
  const [report, setReport] = useState<Report | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<Group | null>(null);
  const [open, setOpen] = useState(false);
  const [version, setVersion] = useState(0);
  const submittingRef = useRef(false);
  const sequence = useRef(0);
  const fingerprint = useRef('');
  const currentRefresh = useRef(refresh);
  currentRefresh.current = refresh;
  function received(value: Report | null, submitted = false) {
    if (!value) { setReport(null); return; }
    const state = JSON.stringify([value.started_at, value.actions.map(a => [a.id, a.state])]);
    if (submitted || (fingerprint.current && state !== fingerprint.current)) currentRefresh.current();
    fingerprint.current = state;
    setReport(value);
  }
  const tracking = report?.actions.some(a => ['working', 'waiting'].includes(a.state));
  useEffect(() => {
    if (!active) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      const seq = sequence.current;
      try {
        const res = await request<Envelope<{ last_repair: Report | null }>>(repairUrl);
        if (live && seq === sequence.current && !submittingRef.current) {
          received(res.data.last_repair ?? null);
          if (!attempt.current) setError('');
        }
      } catch (reason) {
        if (live && !attempt.current && seq === sequence.current) setError(messageOf(reason));
      } finally {
        if (live && tracking) timer = setTimeout(read, 20000);
      }
    }
    void read();
    return () => { live = false; clearTimeout(timer); };
  }, [active, tracking, version]);
  async function submit() {
    if (!revision || submittingRef.current || !active) return;
    submittingRef.current = true; sequence.current++;
    onBusy(true); setSubmitting(true); setError(''); setOpen(true); setSelected(null);
    const next = attempt.current ?? { key: crypto.randomUUID(), body: { expected_revision: revision, targets: targets(checks) } };
    attempt.current = next;
    try { sessionStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* the in-memory attempt still covers retries */ }
    function clearAttempt() {
      attempt.current = null;
      try { sessionStorage.removeItem(storageKey); } catch { /* storage is optional */ }
    }
    try {
      const response = await request<Envelope<Report>>(repairUrl, { method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': next.key }, body: JSON.stringify(next.body) });
      clearAttempt(); received(response.data, true);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500) {
        clearAttempt();
        if (reason.status === 409) currentRefresh.current();
      }
      setError(attempt.current ? '提交结果尚未确认。点击一键修复可安全重试。' : messageOf(reason));
    } finally { submittingRef.current = false; setSubmitting(false); onBusy(false); }
  }
  const effects = new Map((report?.actions ?? []).map(a => [a.id, a]));
  const rows = (Object.keys(report?.by_need ?? {}) as NeedId[]).flatMap(id => {
    const check = checks.get(id);
    if (!check) return [];
    const actions = (report?.by_need[id] ?? []).flatMap(key => effects.get(key) ?? []);
    return [{ check, actions, group: classify(check, actions) }];
  });
  return <form id="fc-repair-form" className="fc-repair" onSubmit={event => { event.preventDefault(); void submit(); }}>
    {error && <p className="fc-repair-error" role="alert">{error} <button type="button" className="dc-link" onClick={() => setVersion(v => v + 1)}>重读回执</button></p>}
    {submitting && <p role="status">正在核对来源、采集窗口和历史缺口…</p>}
    {report && <section aria-label="修复结果"><div className="fc-repair-head">
      <button type="button" className="dc-link" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起修复结果' : '查看上次修复'}</button>
      <small>{dateTime(report.started_at)} · {rows.length} 项数据</small>
      <button type="button" className="dc-link" onClick={() => { setVersion(v => v + 1); currentRefresh.current(); }}>更新结果</button></div>
      {open && <><div className="fc-repair-groups" role="group" aria-label="按修复结果筛选">
        {groups.map(g => <button type="button" key={g} aria-pressed={selected === g} onClick={() => setSelected(selected === g ? null : g)}>
          {labels[g]} <b>{rows.filter(r => r.group === g).length}</b></button>)}</div>
        <div className="fc-repair-list">{rows.filter(r => !selected || r.group === selected).sort((a, b) => groups.indexOf(a.group) - groups.indexOf(b.group)).map(({ check, group, actions }) => {
          const candidate = check.candidates?.find(c => c.option?.implemented !== false);
          const port = needs[check.id].kind === 'ports' && candidate?.route?.routable !== false ? candidate?.route?.id : null;
          const affected = features.filter(f => f.needs.includes(check.id));
          return <details key={check.id}><summary><strong>{check.name}</strong><span>{labels[group]}</span><small>{affected.length} 项功能</small></summary>
            <p>{affected.map(f => f.name).join('、')}</p>
            {check.state !== 'connected' && <p>{nextStep(check)}</p>}
            {actions.map(a => <div key={a.id} className="fc-repair-action"><b>{a.title}{(a.count ?? 1) > 1 ? '（' + a.count + ' 条记录）' : ''}</b> · {a.advice}
              {a.retry_at && a.state === 'waiting' && <small>下次核对 {dateTime(a.retry_at)}</small>}
              {a.area === 'history' && a.source_id && <a href={monitorHref({ sourceId: a.source_id, tradeDay: a.trade_day, context: 'history' })}>查看历史记录</a>}
            </div>)}
            {candidate && !check.featureUnavailable && <button type="button" className="dc-link" onClick={() => manage(candidate)}>{candidate.name}连接设置</button>}
            {port && <button type="button" className="dc-link" onClick={() => choose(port)}>选择来源</button>}
            {check.href && !check.featureUnavailable && <a href={check.href}>{needs[check.id].kind === 'model' ? '打开 AI 设置' : '打开查询设置'}</a>}
            {needs[check.id].kind === 'anomaly' && <a href="/data?view=monitor#data-monitor">查看归档调度状态</a>}
          </details>;
        })}</div>
        {!rows.length && <p role="status">本次核对未发现需要修复的数据项。</p>}
        {selected && !rows.some(r => r.group === selected) && <p>本次没有此类数据项。</p>}
      </>}
    </section>}
  </form>;
}
