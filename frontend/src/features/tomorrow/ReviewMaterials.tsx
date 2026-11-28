import { useState, type ReactNode } from 'react';
import type { ReportView, ReviewScope, SettingsView } from './model';

export type ReviewControls = { settings: SettingsView; report?: ReportView | null; busy: boolean; admin: boolean; editSources: () => void };
type Start = (submit: () => void) => void;

export function ReviewMaterials({settings, busy, admin, editSources, children}: ReviewControls & {
  children: (scope: ReviewScope, disabled: boolean, start: Start) => ReactNode;
}) {
  const saved = settings.ai_source_ids.length > 0;
  const sources = (settings.sources ?? []).filter(s => s.ai_allowed && (!saved || settings.ai_source_ids.includes(s.id)));
  const materials = settings.review_materials;
  const count = materials?.count ?? sources.length;
  const gaps = materials?.gaps ?? [];
  const [pending, setPending] = useState<{submit: () => void; digest?: string} | null>(null);
  const confirm = pending && pending.digest === materials?.digest;
  const disabled = busy || !count || (!!settings.model_gate && !admin);
  const scope: ReviewScope = {...(settings.model_gate && {enable_ai: true}), ...(materials
    ? {materials_digest: materials.digest} : !saved && {source_ids: sources.map(s => s.id)})};
  function start(submit: () => void) {
    if (disabled) return;
    if (gaps.length) setPending({submit, digest: materials?.digest});
    else submit();
  }
  return <>
    {count > 0 ? <p className="pr-material-summary">{saved ? '沿用已授权的' : '自动使用已有的'} {count} 个资料接口 · 点复盘后，模型将读取本次相关的归档资料。</p>
      : <p role="status">没有可用于模型复盘的资料接口。请在资料设置中检查接口和使用权限。 <button onClick={editSources}>查看资料设置</button></p>}
    {settings.model_gate && <p className="pr-direct-gate">{admin ? '开始复盘时将同时开启系统 AI 和预案 AI。' : '请联系管理员开启系统和预案的 AI 权限。'}</p>}
    {!!gaps.length && (confirm || !count) && <section className="pr-material-gaps" aria-label="复盘资料缺口">
      <strong>{count ? '这些内容缺少可用接口' : '当前缺少的资料接口'}</strong>
      <ul>{gaps.map(g => <li key={g.impact}>{g.interfaces}：无法核验{g.impact}</li>)}</ul>
      {confirm && <><p>{materials?.core_missing ? '核心行情接口不足，只能整理已有资料草稿。是否继续？' : '是否用其余资料继续复盘？'}</p>
        <button type="button" disabled={busy} onClick={() => setPending(null)}>取消</button>
        <button type="button" className="pr-generate" disabled={disabled} onClick={() => { setPending(null); pending.submit(); }}>
          {materials?.core_missing ? '继续整理草稿' : '用现有资料继续复盘'}</button></>}
    </section>}
    {!confirm && children(scope, disabled, start)}
  </>;
}
