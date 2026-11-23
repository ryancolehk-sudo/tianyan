import { useEffect, useRef, useState } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { ApiError } from '../../shared/api';
import { dateTime } from '../terminal/data';
import { screeningApi } from './api';
import type { Catalogue, Condition, Metric, ObservationEvent, Operator, ResultState, Rule, RuleSave, RunResult, RunSummary } from './types';
import './screening.css';

type Props = { session: SessionResponse; day: string | null; asOfTime: string; onSelect: (code: string) => void; defaultOpen?: boolean };
type Draft = Omit<RuleSave, 'id' | 'expected_version' | 'cooldown_seconds'> & { cooldown_seconds: string };
const METRICS: Record<Metric, string> = {
  price: '现价', pct_change: '涨跌幅', speed_5m: '近5分钟涨速', amount_cny: '成交额',
  turnover_rate: '换手率', float_mv_cny: '流通市值', open_pct: '开盘涨幅', previous_height: '昨日连板高度', volume_ratio: '竞价成交量比',
};
const UNITS: Record<Metric, string> = { price: '元', pct_change: '%', speed_5m: '%', amount_cny: '元', turnover_rate: '%', float_mv_cny: '元', open_pct: '%', previous_height: '板', volume_ratio: '倍' };
const OPERATORS: Record<Operator, string> = { gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=' };
const STATES: Record<ResultState, string> = { matched: '命中', not_matched: '未命中', insufficient: '数据不足' };
const MODES = { review: '历史回看', observed: '当时已知', monitor: '自动观察' };
const decimal = /^-?\d{1,18}(?:\.\d{1,12})?$/;
const newDraft = (): Draft => ({ name: '', scope: 'market', definition: { conditions: [{ field: 'pct_change', operator: 'gte', value: '' }], sort_field: 'amount_cny', sort_direction: 'desc' }, enabled: false, cooldown_seconds: '300' });
const fromRule = (rule: Rule): Draft => ({ name: rule.name, scope: rule.scope, definition: structuredClone(rule.definition), enabled: rule.enabled, cooldown_seconds: String(rule.cooldown_seconds) });
const message = (error: unknown) => error instanceof Error ? error.message : '读取失败，请重试。';

export function ScreeningPanel(props: Props) {
  // Private drafts and results never survive an account, permission or CSRF session change.
  const sessionKey = [props.session.data.user.id, props.session.data.user.role, props.session.data.csrf_token].join(':');
  return <ScreeningPanelBody key={sessionKey} {...props}/>;
}

function ScreeningPanelBody({ session, day, asOfTime, onSelect, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [loadedRule, setLoadedRule] = useState<Rule | null>(null);
  const [draft, setDraft] = useState<Draft>(newDraft);
  const [baseline, setBaseline] = useState(() => JSON.stringify(newDraft()));
  const [pendingSelection, setPendingSelection] = useState<string | null>(null);
  const [history, setHistory] = useState<RunSummary[]>([]);
  const [events, setEvents] = useState<ObservationEvent[]>([]);
  const [result, setResult] = useState<{ context: string; data: RunResult } | null>(null);
  const [filter, setFilter] = useState<ResultState>('matched');
  const [saving, setSaving] = useState(false);
  const [resultBusy, setResultBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState('');
  const [historyError, setHistoryError] = useState('');
  const [eventsError, setEventsError] = useState('');
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [blocked, setBlocked] = useState(false);
  const live = useRef(true);
  const blockedRef = useRef(false);
  const context = `${day ?? ''}|${asOfTime}`;
  const contextRef = useRef(context); contextRef.current = context;
  const selectedRef = useRef(loadedRule?.id ?? ''); selectedRef.current = loadedRule?.id ?? '';
  const resultSequence = useRef(0);
  const rulesSequence = useRef(0);
  const historySequence = useRef(0);
  const eventsSequence = useRef(0);
  const savePending = useRef(false);
  const saveRetry = useRef<{ body: string; key: string } | null>(null);
  const runRetry = useRef<{ body: string; key: string } | null>(null);
  const canWrite = session.data.user.role !== 'viewer' && !blocked;
  const dirty = JSON.stringify(draft) !== baseline;
  const activeResult = result?.context === context ? result.data : null;
  const valid = () => live.current && !blockedRef.current;
  const contextInput = () => ({ ...(day ? { day } : {}), ...(asOfTime ? { as_of_time: asOfTime } : {}) });
  const metricLabel = (field: Metric) => catalogue?.metrics.find(item => item.field === field)?.label ?? METRICS[field];
  const metricUnit = (field: Metric) => catalogue?.metrics.find(item => item.field === field)?.unit ?? UNITS[field];
  const metricText = (field: Metric, value: string | null) => value == null ? '缺失' : `${value}${metricUnit(field) ? ` ${metricUnit(field)}` : ''}`;

  useEffect(() => {
    live.current = true;
    const deny = () => {
      blockedRef.current = true; resultSequence.current++; rulesSequence.current++; historySequence.current++; eventsSequence.current++;
      setBlocked(true); setResult(null); setRules([]); setLoadedRule(null); setHistory([]); setEvents([]); setCatalogue(null);
      const empty = newDraft(); setDraft(empty); setBaseline(JSON.stringify(empty)); setPendingSelection(null);
      setActionError(''); setReadError(''); setHistoryError(''); setEventsError(''); setNotice(''); setResultBusy(false); setSaving(false);
    };
    window.addEventListener('session-changed', deny);
    window.addEventListener('session-expired', deny);
    window.addEventListener('read-permission-denied', deny);
    return () => {
      live.current = false; resultSequence.current++; rulesSequence.current++; historySequence.current++; eventsSequence.current++;
      window.removeEventListener('session-changed', deny); window.removeEventListener('session-expired', deny); window.removeEventListener('read-permission-denied', deny);
    };
  }, []);

  useEffect(() => {
    resultSequence.current++; setResult(null); setResultBusy(false); setActionError(''); setNotice(''); setFilter('matched');
  }, [context]);

  async function readRules() {
    const ticket = ++rulesSequence.current;
    try {
      const response = await screeningApi.rules();
      if (valid() && ticket === rulesSequence.current) { setRules(response.data.items); setReadError(''); }
    } catch (error) { if (valid() && ticket === rulesSequence.current) setReadError(message(error)); }
  }
  async function readEvents(ruleId = selectedRef.current) {
    const ticket = ++eventsSequence.current;
    try {
      const response = await screeningApi.events(ruleId || undefined);
      if (valid() && ticket === eventsSequence.current && selectedRef.current === ruleId) { setEvents(response.data.items); setEventsError(''); }
    } catch (error) { if (valid() && ticket === eventsSequence.current && selectedRef.current === ruleId) setEventsError(message(error)); }
  }
  async function readHistory(ruleId: string) {
    const ticket = ++historySequence.current;
    if (!ruleId) { setHistory([]); setHistoryError(''); return; }
    try {
      const response = await screeningApi.runs(ruleId);
      if (valid() && ticket === historySequence.current && selectedRef.current === ruleId) { setHistory(response.data.items); setHistoryError(''); }
    } catch (error) { if (valid() && ticket === historySequence.current && selectedRef.current === ruleId) setHistoryError(message(error)); }
  }
  async function readInitial() {
    setReading(true);
    const responses = await Promise.allSettled([screeningApi.catalogue(), readRules(), readEvents()]);
    if (!valid()) return;
    if (responses[0].status === 'fulfilled') setCatalogue(responses[0].value.data);
    else setReadError(message(responses[0].reason));
    setReading(false);
  }
  useEffect(() => {
    if (!open || blocked) return;
    void readInitial();
    const timer = window.setInterval(() => {
      if (document.hidden || !valid()) return;
      // Observation is evaluated by the server worker, never by a page GET or timer POST.
      void readRules(); void readEvents();
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [open, blocked]);
  useEffect(() => {
    if (!open || blocked) return;
    setHistory([]); setHistoryError(''); setEvents([]); setEventsError('');
    void readHistory(loadedRule?.id ?? ''); void readEvents(loadedRule?.id ?? '');
  }, [loadedRule?.id, open, blocked]);

  function edit(next: Draft) {
    resultSequence.current++; setResultBusy(false); setResult(null); setDraft(next); setActionError(''); setNotice('');
  }
  function changeCondition(index: number, change: Partial<Condition>) {
    edit({ ...draft, definition: { ...draft.definition, conditions: draft.definition.conditions.map((item, i) => i === index ? { ...item, ...change } : item) } });
  }
  function loadRule(id: string) {
    if (savePending.current) return;
    const rule = rules.find(item => item.id === id) ?? null;
    if (id && !rule) { setActionError('这条规则已不在当前列表，请刷新规则。'); return; }
    resultSequence.current++; selectedRef.current = id; setResult(null); setResultBusy(false); setLoadedRule(rule);
    const next = rule ? fromRule(rule) : newDraft(); setDraft(next); setBaseline(JSON.stringify(next));
    setPendingSelection(null); setActionError(''); setNotice(''); setFilter('matched');
  }
  function chooseRule(id: string) {
    if (savePending.current) return;
    if (dirty) setPendingSelection(id); else loadRule(id);
  }
  function validate(save: boolean): boolean {
    let error = '';
    if (save && (!draft.name.trim() || draft.name.trim().length > 80)) error = '请输入 1–80 字的规则名称。';
    else if (!draft.definition.conditions.length || draft.definition.conditions.length > 12) error = '请设置 1–12 个同时满足的条件。';
    else if (draft.definition.conditions.some(item => !decimal.test(item.value))) error = '条件值请输入普通十进制数，最多 18 位整数、12 位小数。';
    else if (save && (!/^\d+$/.test(draft.cooldown_seconds) || Number(draft.cooldown_seconds) < 60 || Number(draft.cooldown_seconds) > 86400)) error = '冷却时间请输入 60–86400 之间的整数秒。';
    setActionError(error); return !error;
  }
  async function saveRule(toggle?: boolean) {
    if (!canWrite || savePending.current || !validate(true)) return;
    const payload: RuleSave = {
      ...draft, name: draft.name.trim(), cooldown_seconds: Number(draft.cooldown_seconds), enabled: toggle ?? draft.enabled,
      ...(loadedRule ? { id: loadedRule.id, expected_version: loadedRule.version } : {}),
    };
    savePending.current = true; setSaving(true); setActionError(''); setNotice('');
    const fingerprint = JSON.stringify(payload);
    if (saveRetry.current?.body !== fingerprint) saveRetry.current = { body: fingerprint, key: crypto.randomUUID() };
    try {
      const response = await screeningApi.save(payload, session.data.csrf_token, saveRetry.current.key);
      if (!valid()) return;
      saveRetry.current = null;
      rulesSequence.current++; resultSequence.current++; setResult(null); setResultBusy(false);
      const saved = response.data; selectedRef.current = saved.id;
      setRules(current => [saved, ...current.filter(item => item.id !== saved.id)]); setLoadedRule(saved);
      const next = fromRule(saved); setDraft(next); setBaseline(JSON.stringify(next));
      setPendingSelection(null);
      setNotice(`规则已保存为 v${saved.version}，自动观察${saved.enabled ? '已启用' : '未启用'}。`);
    } catch (error) {
      if (valid()) setActionError(error instanceof ApiError && error.status === 409 ? '保存版本已变化。草稿已保留，请重新载入最新规则核对后再保存。' : `${message(error)} 草稿已保留。`);
    } finally { savePending.current = false; if (valid()) setSaving(false); }
  }
  async function evaluate(preview: boolean) {
    if (!canWrite || saving || resultBusy || (preview ? !validate(false) : !loadedRule || dirty)) return;
    const ticket = ++resultSequence.current; const at = context; const ruleId = loadedRule?.id;
    setResultBusy(true); setActionError(''); setNotice(''); setResult(null); setFilter('matched');
    const fingerprint = JSON.stringify({ ruleId, version: loadedRule?.version, ...contextInput() });
    if (!preview && runRetry.current?.body !== fingerprint) runRetry.current = { body: fingerprint, key: crypto.randomUUID() };
    try {
      let response = preview
        ? await screeningApi.preview({ ...contextInput(), definition: draft.definition, scope: draft.scope, state: 'matched', offset: 0, limit: 100 }, session.data.csrf_token)
        : await screeningApi.run(ruleId!, contextInput(), session.data.csrf_token, runRetry.current!.key);
      if (!valid() || ticket !== resultSequence.current || contextRef.current !== at) return;
      if (!preview && ruleId) {
        runRetry.current = null;
        void readHistory(ruleId); void readEvents(ruleId); void readRules();
        if (response.data.id) response = await screeningApi.result(response.data.id, 'matched');
      }
      if (valid() && ticket === resultSequence.current && contextRef.current === at) setResult({ context: at, data: response.data });
    } catch (error) { if (valid() && ticket === resultSequence.current && contextRef.current === at) setActionError(message(error)); }
    finally { if (valid() && ticket === resultSequence.current && contextRef.current === at) setResultBusy(false); }
  }
  async function openResult(id: string, state: ResultState = 'matched', offset = 0) {
    const ticket = ++resultSequence.current; const at = context; const ruleId = selectedRef.current;
    setResultBusy(true); setActionError(''); setFilter(state);
    // A different run or category must not temporarily display the previous category's rows.
    setResult(null);
    try {
      const response = await screeningApi.result(id, state, offset);
      if (valid() && ticket === resultSequence.current && contextRef.current === at && selectedRef.current === ruleId) setResult({ context: at, data: response.data });
    } catch (error) { if (valid() && ticket === resultSequence.current && contextRef.current === at) setActionError(message(error)); }
    finally { if (valid() && ticket === resultSequence.current && contextRef.current === at) setResultBusy(false); }
  }
  function changeFilter(state: ResultState) {
    if (activeResult) void pageResult(activeResult, state);
  }
  async function pageResult(current: RunResult, state: ResultState, offset = 0) {
    if (current.id) { await openResult(current.id, state, offset); return; }
    if (!canWrite) return;
    const ticket = ++resultSequence.current; const at = context;
    setResultBusy(true); setActionError(''); setFilter(state); setResult(null);
    try {
      const response = await screeningApi.preview({ ...contextInput(), definition: current.definition, scope: current.scope, state, offset, limit: 100 }, session.data.csrf_token);
      if (valid() && ticket === resultSequence.current && contextRef.current === at) setResult({ context: at, data: response.data });
    } catch (error) { if (valid() && ticket === resultSequence.current && contextRef.current === at) setActionError(message(error)); }
    finally { if (valid() && ticket === resultSequence.current && contextRef.current === at) setResultBusy(false); }
  }
  const rows = activeResult?.items.filter(item => item.state === filter) ?? [];
  const latestVersion = rules.find(item => item.id === loadedRule?.id)?.version;
  const versionChanged = !!loadedRule && latestVersion != null && latestVersion !== loadedRule.version;
  const currentSaved = rules.find(item => item.id === loadedRule?.id) ?? loadedRule;

  return <section className="ty-panel ty-screening" aria-labelledby="ty-screening-title">
    <div className="ty-panel-head ty-screening-head"><div className="ty-panel-label"><h2 id="ty-screening-title">条件选股与观察</h2><span className="ty-panel-subtitle">已存样本 · 条件研究</span></div>
      <button type="button" className="ty-button ty-button-gold" aria-expanded={open} aria-controls="ty-screening-body" onClick={() => setOpen(value => !value)}>{open ? '收起条件选股' : '展开条件选股'}</button></div>
    {open && <div id="ty-screening-body" className="ty-screening-body">
      {blocked ? <p role="alert" className="ty-screening-error">会话或读取权限已变化，当前规则和结果已清空。请重新登录或刷新页面。</p> : <>
        <p className="ty-screening-boundary">范围：{catalogue?.scope || '当前已保存且有权读取的股票样本，不代表全市场完整覆盖'}。条件只用于研究观察。</p>
        <div className="ty-screening-selector"><label>已保存规则<select aria-label="已保存规则" value={loadedRule?.id ?? ''} disabled={saving} onChange={event => chooseRule(event.target.value)}><option value="">{canWrite ? '新建规则' : '选择规则回看'}</option>{rules.map(rule => <option value={rule.id} key={rule.id}>{rule.name} · v{rule.version} · {rule.enabled ? '观察中' : '已暂停'}</option>)}</select></label>
          <button type="button" className="ty-button" disabled={reading || saving} onClick={() => { void readInitial(); if (loadedRule) void readHistory(loadedRule.id); }}>刷新规则与记录</button>
          {loadedRule && canWrite && <button type="button" className="ty-button" disabled={saving} onClick={() => chooseRule('')}>新建规则</button>}
          <span className="ty-screening-draft" role="status">{dirty ? '未保存草稿' : loadedRule ? `已保存 v${loadedRule.version}` : '新规则 · 尚未保存'}</span></div>
        {reading && <p role="status" className="ty-muted">正在读取规则…</p>}
        {readError && <p role="alert" className="ty-screening-error">{readError}</p>}
        {pendingSelection !== null && <div className="ty-screening-confirm" role="alert"><span>当前草稿未保存，载入规则会替换这些修改。</span><button type="button" className="ty-button" disabled={saving} onClick={() => setPendingSelection(null)}>保留草稿</button><button type="button" className="ty-button" disabled={saving} onClick={() => loadRule(pendingSelection)}>放弃草稿并载入</button></div>}
        {versionChanged && <p className="ty-screening-error" role="status">服务器已有 v{latestVersion}。当前草稿基于 v{loadedRule?.version}，保存前请核对。<button type="button" className="ty-text-button" onClick={() => chooseRule(loadedRule!.id)}>重新载入规则</button></p>}
        {!canWrite && <p className="ty-screening-boundary">当前账号只读，可查看已保存规则、历史运行和观察事件。</p>}
        <fieldset className="ty-screening-editor" disabled={!canWrite || saving}>
          <legend className="ty-screening-sr">规则条件</legend>
          <div className="ty-screening-grid"><label>规则名称<input aria-label="规则名称" value={draft.name} maxLength={80} placeholder="例如：成交活跃样本" onChange={event => edit({ ...draft, name: event.target.value })}/></label>
            <label>样本范围<select aria-label="样本范围" value={draft.scope} onChange={event => edit({ ...draft, scope: event.target.value as Draft['scope'] })}><option value="market">当前已存样本</option><option value="watchlist">我的自选样本</option></select></label></div>
          <div className="ty-screening-condition-title"><strong>全部条件同时满足（AND）</strong><span>{draft.definition.conditions.length}/12</span></div>
          <div className="ty-screening-conditions">{draft.definition.conditions.map((condition, index) => <div className="ty-screening-condition" key={index}>
            <label><span className="ty-screening-sr">条件 {index + 1} 指标</span><select aria-label={`条件 ${index + 1} 指标`} value={condition.field} onChange={event => changeCondition(index, { field: event.target.value as Metric })}>{(Object.keys(METRICS) as Metric[]).map(field => <option value={field} key={field}>{metricLabel(field)}{metricUnit(field) ? `（${metricUnit(field)}）` : ''}</option>)}</select></label>
            <label><span className="ty-screening-sr">条件 {index + 1} 比较</span><select aria-label={`条件 ${index + 1} 比较`} value={condition.operator} onChange={event => changeCondition(index, { operator: event.target.value as Operator })}>{Object.entries(OPERATORS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label><span className="ty-screening-sr">条件 {index + 1} 数值</span><input aria-label={`条件 ${index + 1} 数值`} inputMode="decimal" autoComplete="off" placeholder="输入数值" value={condition.value} onChange={event => changeCondition(index, { value: event.target.value })}/></label>
            <button type="button" className="ty-button" aria-label={`删除条件 ${index + 1}`} disabled={draft.definition.conditions.length <= 1} onClick={() => edit({ ...draft, definition: { ...draft.definition, conditions: draft.definition.conditions.filter((_, i) => i !== index) } })}>删除</button>
            <small>{catalogue?.metrics.find(item => item.field === condition.field)?.description}</small>
          </div>)}</div>
          <button type="button" className="ty-button" disabled={draft.definition.conditions.length >= 12} onClick={() => edit({ ...draft, definition: { ...draft.definition, conditions: [...draft.definition.conditions, { field: 'pct_change', operator: 'gte', value: '' }] } })}>＋ 添加条件</button>
          <div className="ty-screening-grid ty-screening-options"><label>结果排序<select aria-label="结果排序" value={draft.definition.sort_field} onChange={event => edit({ ...draft, definition: { ...draft.definition, sort_field: event.target.value as Metric } })}>{(Object.keys(METRICS) as Metric[]).map(field => <option key={field} value={field}>{metricLabel(field)}</option>)}</select></label>
            <label>排序方向<select aria-label="排序方向" value={draft.definition.sort_direction} onChange={event => edit({ ...draft, definition: { ...draft.definition, sort_direction: event.target.value as 'asc' | 'desc' } })}><option value="desc">从高到低</option><option value="asc">从低到高</option></select></label>
            <label>事件冷却（秒）<input aria-label="事件冷却秒数" type="number" min={60} max={86400} step={1} value={draft.cooldown_seconds} onChange={event => edit({ ...draft, cooldown_seconds: event.target.value })}/></label></div>
          <label className="ty-screening-check"><input type="checkbox" checked={draft.enabled} onChange={event => edit({ ...draft, enabled: event.target.checked })}/>保存后启用自动观察</label>
        </fieldset>
        <p className="ty-screening-boundary">自动观察由后台按当前交易日处理；回看日期不会改变观察目标。页面每 60 秒只读取规则和事件。</p>
        <div className="ty-screening-context"><span>本次预览 / 手动运行：<strong>{day || '服务端当前交易日'}</strong>{asOfTime ? ` · 截止 ${dateTime(asOfTime)}` : ' · 读取该日当前可见资料'}</span><small>手动运行会保存本次结果；预览不保存。</small></div>
        <div className="ty-screening-actions"><button type="button" className="ty-button" disabled={!canWrite || saving || resultBusy} onClick={() => void evaluate(true)}>预览草稿</button><button type="button" className="ty-button ty-button-gold" disabled={!canWrite || saving} onClick={() => void saveRule()}>{saving ? '正在保存…' : '保存规则'}</button>
          <button type="button" className="ty-button" disabled={!canWrite || !loadedRule || dirty || versionChanged || saving || resultBusy} onClick={() => void evaluate(false)}>运行并保存结果</button>
          {loadedRule && <button type="button" className="ty-button" disabled={!canWrite || dirty || versionChanged || saving} onClick={() => void saveRule(!loadedRule.enabled)}>{loadedRule.enabled ? '暂停自动观察' : '启用自动观察'}</button>}
          {dirty && loadedRule && <small>运行和启停使用已保存版本，请先保存草稿。</small>}</div>
        {actionError && <p role="alert" className="ty-screening-error">{actionError}</p>}
        {notice && <p role="status" className="ty-screening-notice">{notice}</p>}
        {currentSaved?.last_run_at && <p className="ty-screening-boundary">最近运行 {dateTime(currentSaved.last_run_at)}（北京时间）</p>}
        {currentSaved?.last_error && <p className="ty-screening-error">后台最近错误：{currentSaved.last_error}</p>}
        {resultBusy && <p role="status" className="ty-screening-loading">正在读取本次条件结果…</p>}
        {activeResult && <div className="ty-screening-results" aria-label="条件结果">
          <div className="ty-screening-result-head"><h3>{activeResult.id ? '已保存运行' : '草稿预览 · 未保存'}</h3><span>{activeResult.rule_name || '当前草稿'}{activeResult.rule_version != null ? ` · v${activeResult.rule_version}` : ''} · {MODES[activeResult.mode]}</span></div>
          <p className="ty-screening-boundary">数据日 {activeResult.trade_day} · 截止 {dateTime(activeResult.as_of_time)} · 计算 {dateTime(activeResult.created_at)}（北京时间）</p>
          {!!activeResult.reasons.length && <ul className="ty-screening-reasons">{activeResult.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul>}
          <div className="ty-screening-tabs" aria-label="结果分类">{(Object.keys(STATES) as ResultState[]).map(state => <button key={state} type="button" aria-pressed={filter === state} className={filter === state ? 'ty-button ty-button-gold' : 'ty-button'} onClick={() => changeFilter(state)}>{STATES[state]} {activeResult.counts[state]}</button>)}<span>总样本 {activeResult.counts.total}</span></div>
          {!activeResult.id && <p className="ty-screening-boundary">预览分类和翻页会按相同条件重新读取资料。需要固定结果时，请保存规则并运行。</p>}
          {rows.length ? <div className="ty-screening-result-list">{rows.map(item => <article className="ty-screening-result-row" key={item.code}>
            <div className="ty-screening-stock"><button type="button" className="ty-stock-name" onClick={() => onSelect(item.code)}>{item.name}<small>{item.code}</small></button><span data-state={item.state}>{STATES[item.state]}</span><span>{metricLabel(activeResult.definition.sort_field)} {metricText(activeResult.definition.sort_field, item.metrics[activeResult.definition.sort_field] ?? null)}</span></div>
            <p className="ty-screening-stamp">行情时点 {item.source_time ? dateTime(item.source_time) : '未知'} · 接收 {item.received_at ? dateTime(item.received_at) : '未知'}</p>
            <details><summary>逐条件原因（{item.checks.length} 项）</summary><ul className="ty-screening-checks">{item.checks.map((check, index) => <li key={index}><strong>{metricLabel(check.field)} {OPERATORS[check.operator]} {metricText(check.field, check.target)}</strong><span data-state={check.state}>{STATES[check.state]} · 实际 {metricText(check.field, check.actual)}</span><span>{check.reason}</span></li>)}</ul></details>
          </article>)}</div> : <p className="ty-screening-empty">{activeResult.counts[filter] ? '本页没有可显示记录，请返回上一页或重新预览。' : `本次没有${STATES[filter]}的样本。`}</p>}
          <div className="ty-screening-pagination"><span>{STATES[filter]} · 已显示 {activeResult.offset + 1 > activeResult.counts[filter] ? 0 : activeResult.offset + 1}–{activeResult.offset + rows.length} / {activeResult.counts[filter]}</span><button type="button" className="ty-button" disabled={activeResult.offset === 0} onClick={() => void pageResult(activeResult, filter, Math.max(0, activeResult.offset - activeResult.limit))}>上一页</button><button type="button" className="ty-button" disabled={!activeResult.has_more} onClick={() => void pageResult(activeResult, filter, activeResult.offset + activeResult.limit)}>下一页</button></div>
          <details className="ty-screening-evidence"><summary>查看规则快照与来源引用（{activeResult.source_refs.length}）</summary><p>引擎 {activeResult.engine_version} · 样本范围 {activeResult.scope === 'market' ? '当前已存样本' : '我的自选样本'}</p><ul>{activeResult.definition.conditions.map((condition, i) => <li key={i}>{metricLabel(condition.field)} {OPERATORS[condition.operator]} {metricText(condition.field, condition.value)}</li>)}</ul><p>输入 hash <code>{activeResult.input_hash}</code></p><p>数据版本 <code>{activeResult.data_revision}</code></p>{activeResult.source_refs.map(ref => <div key={ref.evidence_id}><p>证据 {ref.evidence_id} · 来源 {ref.source_id}</p><p>接收 {dateTime(ref.received_at)} · hash <code>{ref.content_hash}</code></p></div>)}</details>
        </div>}
        <div className="ty-screening-records"><section aria-labelledby="ty-screening-history"><h3 id="ty-screening-history">过去运行</h3>{historyError && <p role="alert" className="ty-screening-error">{historyError}</p>}{history.length ? <ul>{history.map(run => <li key={run.id}><button type="button" className="ty-text-button" disabled={saving || resultBusy} onClick={() => void openResult(run.id)}>{run.trade_day} · {dateTime(run.created_at)} · v{run.rule_version}</button><span>{MODES[run.mode]} · 命中 {run.counts.matched} / {run.counts.total}</span></li>)}</ul> : <p className="ty-screening-empty">{loadedRule ? '尚无已保存运行。' : '选择已保存规则后查看历史运行。'}</p>}</section>
          <section aria-labelledby="ty-screening-events"><h3 id="ty-screening-events">近期观察事件 <small>{loadedRule ? loadedRule.name : '所有规则'} · 所有日期</small></h3>{eventsError && <p role="alert" className="ty-screening-error">{eventsError}</p>}{events.length ? <ul>{events.map(event => <li key={event.id}><div><span className="ty-screening-event" data-direction={event.direction}>{event.direction === 'entered' ? '进入' : '退出'}</span><button type="button" className="ty-text-button" onClick={() => onSelect(event.code)}>{event.name} {event.code}</button></div><span>{event.rule_name} · v{event.rule_version} · {dateTime(event.created_at)}</span><small>行情时点 {event.source_time ? dateTime(event.source_time) : '未知'}</small></li>)}</ul> : <p className="ty-screening-empty">暂无观察事件；数据不足不视为退出。</p>}</section></div>
        {catalogue && <details className="ty-screening-evidence"><summary>指标与数据边界</summary><ul>{catalogue.boundaries.map((boundary, i) => <li key={i}>{boundary}</li>)}</ul><p>引擎 {catalogue.engine_version} · 后台检查间隔 {catalogue.interval_seconds} 秒</p></details>}
      </>}
    </div>}
  </section>;
}
