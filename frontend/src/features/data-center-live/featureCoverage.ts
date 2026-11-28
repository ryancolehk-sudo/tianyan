import { credentialError } from './api';
import type { AiSettings, Capability, ConnectionIndex } from './api';
import type { CapabilityMatrix, CapabilityOption, RouteCapability, Supplier } from './capabilityApi';
import type { DataNeed } from './featureCatalog';

export type Access = 'connected' | 'partial' | 'setup' | 'missing' | 'verify';
export type Check = { state: Access; label: string; detail?: string; awaitingSelection?: boolean };
export type Candidate = Check & { name: string; providerId: string; route?: RouteCapability; sourceId?: string; option?: CapabilityOption };
export type NeedCoverage = Check & { candidates?: Candidate[]; href?: string; featureUnavailable?: boolean; interfaceGap?: boolean };
export type Archive = { day: string; received_at: string };
export type CoverageInput = { matrix?: CapabilityMatrix; index?: ConnectionIndex; ai?: AiSettings; archive?: Archive | null };
const unchecked: Check = { state: 'verify', label: '状态待核验' };
const blockers: Record<string, string> = {
  disabled: '采集已关闭', paused: '采集已暂停', not_configured: '待配置',
  points_not_enabled: '待授权积分', permission_missing: '缺少权限', credentials_missing: '待填写密钥',
  empty: '未返回数据', error: '最近取数失败', failed: '最近检测失败', unavailable: '来源不可用',
};
function coveragePriority(c: Candidate) {
  if (c.state === 'connected') return 0;
  if (c.state === 'partial') return 1;
  if (c.awaitingSelection) return 2;
  if (c.state === 'verify') return 3;
  return c.state === 'setup' ? 4 : 5;
}

function checkPort(supplier: Supplier, option: CapabilityOption, inUse: boolean, exact?: Capability): Check {
  if (!option.implemented) return { state: 'missing', label: '尚未适配' };
  if (option.entitlement === 'locked') return { state: 'setup', label: '缺少权限' };
  if (supplier.credential_required && !supplier.credential_configured) return { state: 'setup', label: '待填写密钥' };
  if (!supplier.enabled) return { state: 'setup', label: supplier.connection_id ? '来源已停用' : '可配置来源' };
  if (exact?.enabled === false || exact?.collection_enabled === false) return { state: 'setup', label: '采集已关闭' };
  const blocked = blockers[option.state] || (exact && blockers[exact.state]);
  if (blocked) return { state: 'setup', label: blocked };
  if (!option.source_ids.length) return { state: 'setup', label: '待配置采集范围' };
  if (option.entitlement !== 'verified' || !option.last_success_at || (exact && !exact.last_success_at))
    return { state: 'verify', label: '待取数验证' };
  if (option.state === 'time_unverified') return { state: inUse ? 'partial' : 'verify', label: '行情时点待核', detail: option.last_success_at };
  if (option.state === 'partial' || exact?.state === 'partial')
    return { state: inUse ? 'partial' : 'verify', label: '部分数据可用', detail: exact?.last_success_at ?? option.last_success_at };
  if (option.state !== 'ready' || (exact && exact.state !== 'ready'))
    return { state: 'verify', label: '待核验当前状态', detail: exact?.last_success_at ?? option.last_success_at };
  // A multi-family route may have a receipt for a sibling. Require proof for the exact family.
  if (exact && option.source_ids.length > 1 && Date.parse(exact.last_success_at!) !== Date.parse(option.last_success_at))
    return { state: 'verify', label: '待单项核验', detail: exact.last_success_at ?? undefined };
  if (!inUse) return { state: 'setup', label: '已验证，待选用', awaitingSelection: true };
  return { state: 'connected', label: '已接入', detail: exact?.last_success_at ?? option.last_success_at };
}

export function coverageReader({ matrix, index, ai, archive }: CoverageInput) {
  const suppliers = new Map(matrix?.providers.map(item => [item.id, item]));
  const routes = new Map(matrix?.capabilities.map(item => [item.id, item]));
  function candidates(route: RouteCapability): Candidate[] {
    return route.options.flatMap(option => {
      const supplier = suppliers.get(option.provider_id);
      if (!supplier) return [];
      const inUse = route.effective_provider_id === supplier.id && (route.routable !== false || option.selected);
      return [{ ...checkPort(supplier, option, inUse), name: supplier.name, route, providerId: supplier.id, option,
        sourceId: option.source_ids[0] }];
    });
  }
  function forFamily(id: string): Candidate[] {
    const direct = routes.get('source:' + id);
    if (direct) return candidates(direct);
    if (!index || !matrix) return [];
    // Some exact families are folded into a routed capability (e.g. the post-close Tushare daily series).
    return index.templates.filter(t => t.source_families?.includes(id)).flatMap(template => {
      const supplier = matrix.providers.find(item => item.template === template.id);
      if (!supplier) return [];
      const bound = index.items.flatMap(item => item.template === template.id ? item.capabilities ?? [] : [])
        .find(cap => cap.family === id);
      const route = bound && matrix.capabilities.find(item =>
        item.options.some(option => option.provider_id === supplier.id && option.source_ids.includes(bound.source_id)));
      const option = route?.options.find(item => item.provider_id === supplier.id);
      const result: Check = bound && option ? checkPort(supplier, option, true, bound)
        : bound ? unchecked : { state: 'setup', label: '可配置来源' };
      return [{ ...result, route, option, name: supplier.name, providerId: supplier.id, sourceId: bound?.source_id }];
    });
  }
  function checkSources(need: Extract<DataNeed, { ids: string[] }> | { kind: 'news' }): NeedCoverage {
    if (!matrix) return unchecked;
    let choices: Candidate[];
    if (need.kind === 'news') {
      choices = matrix.capabilities.filter(item => item.id.startsWith('source:') &&
        !['source:cninfo_announcements', 'source:review_cls_news'].includes(item.id) &&
        item.options.some(option => suppliers.get(option.provider_id)?.kind === 'news')).flatMap(candidates);
    } else if (need.kind === 'families') {
      choices = need.ids.flatMap(forFamily);
      if (!choices.length && !index) return unchecked;
    } else {
      choices = need.ids.flatMap(id => { const route = routes.get(id); return route ? candidates(route) : []; });
    }
    choices.sort((a, b) => coveragePriority(a) - coveragePriority(b));
    const best = choices[0];
    if (!index && need.kind === 'families' && need.ids.some(id => !routes.has('source:' + id))
        && best?.state !== 'connected' && best?.state !== 'partial') return unchecked;
    if (!best) return { state: 'missing', label: '未登记接口', interfaceGap: true };
    // An unused verified alternative and a pending verification both prevent
    // an assertion that every known interface is unavailable.
    const interfaceGap = choices.every(c => c.state === 'missing' || c.state === 'setup' && !c.awaitingSelection);
    const names = [...new Set(choices.filter(c => c.option?.implemented !== false).map(c => c.name))];
    const detail = interfaceGap ? names.length === 1 ? '当前仅登记 ' + names[0] + ' · ' + best.label
      : '当前已登记的来源均无法提供这项数据。' : undefined;
    return { state: best.state, label: interfaceGap ? '缺少可用接口' : best.label, candidates: choices, interfaceGap, detail };
  }
  return (need: DataNeed): NeedCoverage => {
    switch (need.kind) {
      case 'ports': case 'families': return checkSources(need);
      case 'news': return checkSources({ kind: 'news' });
      case 'unsupported': return { state: 'missing', label: '尚未接入', interfaceGap: true, featureUnavailable: true, detail: '系统尚无逐笔委托与撤单接口。' };
      case 'anomaly': {
        if (archive === undefined) return unchecked;
        if (archive === null) return { state: 'setup', label: '待首次归档', detail: '东方财富盘后异动资料由本机定时归档。' };
        return archive.day && archive.received_at ? { state: 'connected', label: '已接入', detail: '东方财富 · 已归档至 ' + archive.day } : unchecked;
      }
      case 'ifind': {
        const href = '/data?view=datasets#data-ifind';
        if (!index) return { ...unchecked, href };
        const items = index.items.filter(item => item.template === 'ifind_mcp');
        const ready = items.find(item => item.enabled && item.tested_revision === item.revision && item.last_test?.status === 'success');
        if (ready) return { state: 'connected', label: '连接已验证', detail: '工具与字段权限按本次查询核对。', href };
        return { state: items.some(item => item.enabled) ? 'verify' : 'setup', label: items.length ? '连接待启用或验证' : '可配置连接', href };
      }
      case 'model': {
        const href = '/data?view=connections#data-ai';
        const binding = ai?.features.find(item => item.id === need.binding);
        if (!ai || !binding) return { ...unchecked, href };
        if (!binding.implemented) return { state: 'missing', label: '功能待实现', featureUnavailable: true, href };
        if (!ai.enabled || !binding.enabled) return { state: 'setup', label: 'AI 功能未启用', href };
        if (!binding.effective_connection_id) return { state: 'setup', label: '待分配模型', href };
        const connection = index?.items.find(item => item.id === binding.effective_connection_id);
        const ready = binding.effective_connection_id === ai.personal_connection_id ? ai.personal_model_ready
          : connection?.enabled && !credentialError(connection) && connection.last_test?.status === 'success' &&
            (connection.tested_revision ?? connection.last_test.revision) === connection.revision;
        return ready ? { state: 'connected', label: '模型已验证', detail: connection?.name ?? '本人 ChatGPT', href }
          : { state: 'verify', label: '模型待验证', href };
      }
    }
  };
}
