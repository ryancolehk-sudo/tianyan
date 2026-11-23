import { useId } from 'react';
import type { CalendarLookup, ScenarioInput, Snapshot } from '../../generated/schema';
import { ErrorState } from '../../shared/ui';
import { EvidencePicker, InstrumentPicker } from './ReferencePickers';

export function TargetDay({ day, calendarId, value, fixed, change, lookup }: {
  day: string; calendarId: string; value: string; fixed: boolean; change: (day: string) => void;
  lookup: { busy: boolean; error: Error | null; data: CalendarLookup | null; retry: () => void };
}) {
  const next = lookup.data?.session?.trade_day;
  return <section className="reference-picker plan-target" aria-label="预案目标日期"><h3>目标交易日</h3><label>目标日期<input type="date" aria-label="目标日期" value={value} readOnly={fixed} min={day} required onChange={event => change(event.target.value)}/></label>
    {fixed && <p className="field-help">目标日随文档固定，追加修订仍指向这一天。需要其他目标日时新建预案。</p>}
    {!calendarId ? <p className="permission-note">尚未绑定交易日历。可手动指定晚于来源日的目标日期保存草稿；这个日期不代表已确认开市，暂不能发布。</p> : lookup.busy ? <p role="status">正在按所选日历核对下一开市日…</p> : lookup.error ? <ErrorState error={lookup.error} retry={lookup.retry}/> : !next ? <p className="permission-note">所选日历覆盖内没有可确认的后续开市日。{lookup.data?.reason} 不猜测“明天”，请补充明确日历版本。</p> : <div className="next-session-answer"><span>所选日历声明的下一开市日 <strong>{next}</strong></span>{!fixed && <button type="button" onClick={() => change(next)}>采用此日为目标日</button>}
      {value && value !== next ? <p className="permission-note">目标日期与此日历的下一开市日不一致，不能保存。{fixed ? '目标日已固定，请换用匹配的日历版本或新建预案。' : '请明确采用核对结果，或重新选择日历。'}</p> : <p className="field-help">查询依据是明确选择的人工日历；保存与发布时服务器再次验证。</p>}
    </div>}
  </section>;
}

export function ScenarioEditor({ index, item, snapshot, change, remove, removable }: { index: number; item: ScenarioInput; snapshot: Snapshot | null; change: (value: ScenarioInput) => void; remove: () => void; removable: boolean }) {
  const triggerId = useId(); const invalidationId = useId();
  return <section className="direction-editor scenario-editor" aria-label={`情景 ${index + 1}`}><div className="subsection-heading"><h4>条件情景 {index + 1}</h4><button type="button" disabled={!removable} onClick={remove}>移除此情景</button></div>
    <label>情景名称<input aria-label={`情景 ${index + 1} 名称`} value={item.label} maxLength={300} required onChange={event => change({ ...item, label: event.target.value })}/></label>
    <label><span id={triggerId}>情景 {index + 1} 触发条件</span><textarea aria-labelledby={triggerId} rows={4} maxLength={20000} required value={item.trigger_text} placeholder="写明需要观察的事实条件与证据，不执行表达式或程序" onChange={event => change({ ...item, trigger_text: event.target.value })}/></label>
    <label><span id={invalidationId}>情景 {index + 1} 失效条件</span><textarea aria-labelledby={invalidationId} rows={4} maxLength={20000} required value={item.invalidation_text} placeholder="写明何时应重新复核或不再沿用本判断" onChange={event => change({ ...item, invalidation_text: event.target.value })}/></label>
    <InstrumentPicker value={item.instrument_ids} change={ids => change({ ...item, instrument_ids: ids })}/>
    <EvidencePicker key={snapshot?.id ?? ''} snapshot={snapshot} value={item.evidence_refs} change={ids => change({ ...item, evidence_refs: ids })} label={`情景 ${index + 1} 材料引用`}/>
  </section>;
}
