import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { tomorrowApi } from './api';
import type { Holding, RiskPolicy, SettingsView } from './model';
type HoldingInput = { code: string; quantity: string; sellable: string; cost: string; sectors: string };
const blankHolding = (): HoldingInput => ({ code: '', quantity: '', sellable: '', cost: '', sectors: '' });
function percent(value?: number | null) { return value == null ? '' : String(Math.round(value * 10000) / 100); }
export function PlanSettings({ settings, csrf, writable, saved, showSources, expanded, section }: { settings: SettingsView; csrf: string; writable: boolean; saved: (value: SettingsView) => void; showSources?: boolean; expanded?: boolean; section?: 'sources'|'schedule'|'portfolio' }) {
  const details = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (showSources && details.current) {
      details.current.open = true;
      details.current.querySelector('.tp-source-options')?.scrollIntoView({block: 'center'});
    }
  }, [showSources]);
  const [current, setCurrent] = useState(settings);
  const [enabled, setEnabled] = useState(settings.enabled);
  const [useAI, setUseAI] = useState(settings.use_ai);
  const initialScheduleModel = (value: SettingsView) => value.use_ai ? value.model_connection_id || 'default' : 'rule';
  const [scheduleModel, setScheduleModel] = useState(() => initialScheduleModel(settings));
  const [cash, setCash] = useState(settings.cash === null || settings.cash === undefined ? '' : String(settings.cash));
  const [holdings, setHoldings] = useState<HoldingInput[]>(settings.holdings.map(h => ({ code: h.code, quantity: String(h.quantity), sellable: String(h.sellable_quantity), cost: h.cost == null ? '' : String(h.cost), sectors: h.sector_codes?.join('、') ?? '' })));
  const [approved, setApproved] = useState(settings.risk_policy.approved);
  const [priceRule, setPriceRule] = useState(settings.risk_policy.price_rule);
  const [limits, setLimits] = useState({ max_total_weight: percent(settings.risk_policy.max_total_weight), max_stock_weight: percent(settings.risk_policy.max_stock_weight), max_sector_weight: percent(settings.risk_policy.max_sector_weight), risk_per_trade: percent(settings.risk_policy.risk_per_trade) });
  const [sourceIds, setSourceIds] = useState(settings.ai_source_ids ?? []);
  const [sourceFilter, setSourceFilter] = useState('');
  const sources = settings.sources ?? [];
  const visibleSources = sources.filter(source => source.name.toLowerCase().includes(sourceFilter.trim().toLowerCase()));
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  function updateHolding(index: number, patch: Partial<HoldingInput>) { setHoldings(items => items.map((item, i) => i === index ? { ...item, ...patch } : item)); }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!writable || busy) return;
    setError(''); setNotice('');
    try {
      if (section==='sources' || section==='schedule') {
        setBusy(true);
        const modelChanged = scheduleModel !== initialScheduleModel(current);
        const patch = section === 'sources' ? {ai_source_ids: sourceIds} : {enabled, ...(modelChanged && {
          use_ai: scheduleModel !== 'rule', model_connection_id: ['rule','default'].includes(scheduleModel) ? null : scheduleModel,
        })};
        const value=(await tomorrowApi.saveSettings({expected_revision:current.revision,...patch},csrf)).data;
        setCurrent(value);saved(value);setNotice('已保存');return;
      }
      const positions: Holding[] = holdings.filter(h => h.code.trim() || h.quantity || h.sellable || h.cost).map(h => {
        const quantity = Number(h.quantity); const sellable = Number(h.sellable);
        if (!/^(?:6\d{5}\.SH|[03]\d{5}\.SZ)$/.test(h.code.trim().toUpperCase()) || !h.quantity || !h.sellable || !Number.isInteger(quantity) || !Number.isInteger(sellable) || quantity < 0 || sellable < 0 || sellable > quantity || (h.cost !== '' && (!Number.isFinite(Number(h.cost)) || Number(h.cost) <= 0))) throw new Error('请填写完整股票代码、持股数量和可卖数量；可卖数量不能超过持股数量。');
        return { code: h.code.trim().toUpperCase(), quantity, sellable_quantity: sellable, ...(h.cost === '' ? {} : { cost: Number(h.cost) }), sector_codes: h.sectors.split(/[、,，\s]+/).filter(Boolean) };
      });
      if (new Set(positions.map(h => h.code)).size !== positions.length) throw new Error('同一股票请合并为一条持仓。');
      if (cash !== '' && (!Number.isFinite(Number(cash)) || Number(cash) < 0)) throw new Error('可用现金应为不小于零的金额。');
      const risk: RiskPolicy = { approved, price_rule: priceRule };
      for (const [key, value] of Object.entries(limits)) {
        if (value === '') continue;
        const number = Number(value);
        if (!Number.isFinite(number) || number < 0 || (key !== 'max_total_weight' && number === 0) || number > (key === 'risk_per_trade' ? 10 : 100)) throw new Error('仓位上限在 0% 至 100% 之间；单股、板块比例须大于零，单笔风险预算不超过 10%。');
        (risk as unknown as Record<string, unknown>)[key] = number / 100;
      }
      if (risk.max_total_weight != null && ((risk.max_stock_weight ?? 0) > risk.max_total_weight || (risk.max_sector_weight ?? 0) > risk.max_total_weight)) throw new Error('单股和板块上限不能超过总仓位上限。');
      setBusy(true);
      const value = (await tomorrowApi.saveSettings({ expected_revision: current.revision, ...(!section && {enabled, use_ai:useAI, ai_source_ids:sourceIds}), cash: cash === '' ? null : Number(cash), holdings: positions, risk_policy: risk }, csrf)).data;
      setCurrent(value); saved(value); setNotice('设置已保存，下次生成使用这些设置。');
    } catch (e) { setError(e instanceof Error ? e.message : '设置保存失败。'); }
    finally { setBusy(false); }
  }
  return <details ref={details} open={expanded} className="tp-panel tp-settings"><summary>定时、资料与风险设置 <span>{current.enabled ? '定时已开启' : '定时未开启'}</span></summary>
    <form onSubmit={submit}>
      <fieldset hidden={!!section && section!=='schedule'} disabled={!writable || busy || (!!section && section!=='schedule')}><legend>生成安排</legend>
        <label className="tp-check"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)}/>交易日自动生成预案</label>
        {section === 'schedule' && <label>定时复盘模型<select aria-label="定时复盘模型" value={scheduleModel} onChange={e => setScheduleModel(e.target.value)}>
          <option value="rule">规则复盘</option><option value="default" disabled={!settings.default_connection_id}>{settings.default_model ? '默认模型 · ' + settings.default_model : '默认 AI 模型尚未就绪'}</option>
          {settings.model_connection_id && !settings.model_options?.some(item => item.id === settings.model_connection_id) &&
            <option value={settings.model_connection_id} disabled>原定时模型当前不可用</option>}
          {settings.model_options?.map(item => <option key={item.id} value={item.id}>{item.source === 'api' ? '我的 API' : 'Codex 账号'} · {item.model} · {item.name}</option>)}
        </select></label>}
        {section === 'schedule' && scheduleModel !== 'rule' && (settings.model_gate || !settings.ai_source_ids.length) &&
          <p className="tp-muted">{settings.model_gate ? settings.model_gate + '。请先在预案页开启。' : '请在预案页选择允许 AI 使用的复盘资料。'}</p>}
        <p className="tp-muted">交易日 {settings.evening_time || '22:30'} 生成晚间完整稿，下一交易日 {settings.overnight_time || '08:30'} 追加隔夜变化。时间均为北京时间，电脑和后台服务需保持运行。</p>
        <label hidden={!!section} className="tp-check"><input type="checkbox" checked={useAI} onChange={e => setUseAI(e.target.checked)}/>定时任务使用 AI 分阶段研究</label>
        <p hidden={!!section} className="tp-muted">AI 未就绪时会说明原因。仅规则稿保留事实、计算与缺口，不生成 AI 判断。</p>
      </fieldset>
      <fieldset hidden={!!section && section!=='sources'} disabled={!writable || busy || (!!section && section!=='sources')}><legend>允许发送给模型的资料</legend>
        <p className="tp-muted">勾选的资料可用于本次及后续 AI 预案，Codex 和 API 共用这份授权。</p>
        {section === 'sources' && sources.length > 8 && <label className="pr-source-filter">查找资料<input type="search" value={sourceFilter} onChange={e => setSourceFilter(e.target.value)} placeholder="输入资料名称"/></label>}
        <div className="tp-source-options">{sources.length ? visibleSources.map(source => <label key={source.id} className="tp-check"><input type="checkbox" disabled={!source.ai_allowed} checked={sourceIds.includes(source.id)} onChange={e => setSourceIds(ids => e.target.checked ? [...new Set([...ids, source.id])] : ids.filter(id => id !== source.id))}/>{source.name}{!source.ai_allowed && <small>暂不允许 AI 使用</small>}</label>) : <p>尚无可选择的来源。可到<a href="/data?view=connections">接入管理</a>检查资料与 AI 权限。</p>}
          {sources.length > 0 && !visibleSources.length && <p>没有匹配的资料。</p>}</div>
        {!sourceIds.length && <p className="tp-warning">尚未选择来源，正文不会发送给 AI。</p>}
      </fieldset>
      <fieldset hidden={!!section && section!=='portfolio'} disabled={!writable || busy || (!!section && section!=='portfolio')}><legend>本人资金与持仓</legend>
        <label className="tp-cash">可用现金（元）<input type="number" min="0" step="0.01" value={cash} onChange={e => setCash(e.target.value)} placeholder="未填写"/></label>
        <div className="tp-table-wrap"><table><caption className="tp-sr">本人持仓</caption><thead><tr><th>股票代码</th><th>持股数量</th><th>可卖数量</th><th>成本（元）</th><th>所属板块代码</th><th>操作</th></tr></thead><tbody>{holdings.map((h, i) => <tr key={i}><td><input aria-label={`持仓 ${i + 1} 股票代码`} value={h.code} placeholder="600000.SH" maxLength={12} onChange={e => updateHolding(i, { code: e.target.value })}/></td><td><input aria-label={`持仓 ${i + 1} 持股数量`} type="number" min="0" step="1" value={h.quantity} onChange={e => updateHolding(i, { quantity: e.target.value })}/></td><td><input aria-label={`持仓 ${i + 1} 可卖数量`} type="number" min="0" step="1" value={h.sellable} onChange={e => updateHolding(i, { sellable: e.target.value })}/></td><td><input aria-label={`持仓 ${i + 1} 成本`} type="number" min="0" step="0.01" value={h.cost} onChange={e => updateHolding(i, { cost: e.target.value })}/></td><td><input aria-label={`持仓 ${i + 1} 所属板块`} value={h.sectors} onChange={e => updateHolding(i, { sectors: e.target.value })} placeholder="多个用逗号分隔"/></td><td><button type="button" aria-label={`移除持仓 ${i + 1}`} onClick={() => setHoldings(items => items.filter((_, index) => index !== i))}>移除</button></td></tr>)}</tbody></table></div>
        {!holdings.length && <p className="tp-muted">尚未维护持仓。缺少账户输入时，不生成个性化仓位数字。</p>}
        <button type="button" onClick={() => setHoldings(items => [...items, blankHolding()])}>添加持仓</button>
      </fieldset>
      <fieldset hidden={!!section && section!=='portfolio'} disabled={!writable || busy || (!!section && section!=='portfolio')}><legend>仓位与价位规则</legend>
        <div className="tp-form-grid">{([['max_total_weight', '总仓位上限'], ['max_stock_weight', '单股仓位上限'], ['max_sector_weight', '板块仓位上限'], ['risk_per_trade', '单笔风险预算']] as const).map(([key, title]) => <label key={key}>{title}（%）<input type="number" min={key === 'max_total_weight' ? 0 : 0.01} max={key === 'risk_per_trade' ? 10 : 100} step="0.01" placeholder="未确认" value={limits[key]} onChange={e => setLimits(value => ({ ...value, [key]: e.target.value }))}/></label>)}
        <label>支撑压力依据<select value={priceRule} onChange={e => setPriceRule(e.target.value as RiskPolicy['price_rule'])}><option value="none">暂不计算价位</option><option value="prior_range_20">过去 20 个交易日高低区间</option></select></label></div>
        <p className="tp-muted">区间仅是历史价格观察依据。历史不足、复权不一致或输入缺失时，相应价位和仓位留空。</p>
        <label className="tp-check"><input type="checkbox" checked={approved} onChange={e => setApproved(e.target.checked)}/>我已确认本页填写的规则，允许后续预案采用</label>
      </fieldset>
      {error && <div className="tp-error" role="alert">{error}<small>输入已保留。若设置已被更新，请重读页面核对后再保存。</small></div>}
      {notice && <p role="status" className="tp-success">{notice}</p>}
      <div className="tp-actions">{section === 'sources' && <span>已选 {sourceIds.length} 项</span>}
        <button className="primary" disabled={!writable || busy || (section === 'sources' && !sourceIds.length && !current.ai_source_ids.length)}>{busy ? '正在保存…' : section === 'sources' ? '保存资料选择' : '保存设置'}</button>{!writable && <small>当前账号为只读</small>}</div>
    </form>
  </details>;
}
