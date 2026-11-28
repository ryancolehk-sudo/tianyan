import type { IconName } from './icons';
import { updateLocation } from './navigation';

export type DataPage = 'connections' | 'datasets' | 'monitor';
type DataTopic = { id: string; label: string; detail: string; icon: IconName; aliases?: string[] };
type DataPageDefinition = { id: DataPage; label: string; icon: IconName; topics: DataTopic[] };

export const dataPages: DataPageDefinition[] = [
  { id: 'connections', label: '接入设置', icon: 'sliders', topics: [
    { id: 'connections', label: '数据来源', detail: '按系统功能查看数据缺口、管理来源与采集参数', icon: 'data', aliases: ['功能数据覆盖', '数据缺口', '供应商连接', '接入管理', '采集设置', '数据源管理', '采集范围', '采集间隔', '采集开关'] },
    { id: 'ai', label: 'AI 设置', detail: '模型、ChatGPT 与功能分配', icon: 'spark' },
    { id: 'capabilities', label: '能力与权限', detail: '来源登记与数据使用权', icon: 'shield', aliases: ['能力范围'] },
    { id: 'guide', label: '接入说明', detail: '使用步骤、模块与接入计划', icon: 'info', aliases: ['使用帮助'] },
  ] },
  { id: 'datasets', label: '数据资料', icon: 'layers', topics: [
    { id: 'datasets', label: '已入库数据', detail: '查表、筛选与来源核对', icon: 'data' },
    { id: 'materials', label: '研究材料', detail: '录入、更正与版本回查', icon: 'research' },
    { id: 'snapshots', label: '冻结快照', detail: '按截止时间保存材料版本', icon: 'snapshot' },
    { id: 'calendar', label: '交易日历', detail: '导入日历与查询交易日', icon: 'calendar' },
    { id: 'ifind', label: 'iFinD 查询', detail: '八个金融领域的手动查询', icon: 'search' },
  ] },
  { id: 'monitor', label: '运行监控', icon: 'pulse', topics: [
    { id: 'monitor', label: '服务与任务', detail: '运行状态、排队与异常', icon: 'pulse', aliases: ['运行状态'] },
    { id: 'recovery', label: '数据补漏', detail: '缺口、补采与核验结果', icon: 'shield' },
    { id: 'progress', label: '接入进度', detail: '各类数据的覆盖情况', icon: 'chart' },
    { id: 'service-calls', label: '服务调用', detail: '模型与数据服务的调用明细', icon: 'list' },
    { id: 'calls', label: '采集记录', detail: '按来源查采集批次与费用', icon: 'calendar', aliases: ['调用记录'] },
  ] },
];

export function dataDestination(view: string | null) {
  // Old collection links now open the inline controls inside 数据来源.
  if (view === 'sources') return { page: dataPages[0], topic: 'sources' };
  const topic = view === 'data-sources' || !view ? 'connections' : view;
  const page = dataPages.find(item => item.topics.some(entry => entry.id === topic));
  return page ? { page, topic } : { page: dataPages[0], topic: 'sources' };
}

export function libraryTopic(search: string, hash: string) {
  const params = new URLSearchParams(search);
  const { page, topic } = dataDestination(params.get('view'));
  const anchor = page.topics.find(item => hash === '#data-' + item.id);
  if (anchor) return anchor.id;
  if (topic !== 'datasets') return topic;
  return params.has('evidence') ? 'materials' : params.has('snapshot') ? 'snapshots' : 'datasets';
}

export function dataHref(topic: string, search = '', anchor = false) {
  const { page } = dataDestination(topic);
  const params = new URLSearchParams(search);
  params.set('view', topic === 'ai' ? 'ai' : page.id === 'datasets' ? topic : page.id);
  return '/data?' + params + (anchor || topic !== page.id ? '#data-' + topic : '');
}

export function collectionParams(search: string, sourceId?: string, providerId?: string) {
  const params = new URLSearchParams(search);
  params.delete('collection_source');
  params.delete('collection_provider');
  if (sourceId) params.set('collection_source', sourceId);
  if (providerId) params.set('collection_provider', providerId);
  return params;
}

export function goToDataTopic(topic: string, params = new URLSearchParams(location.search)) {
  const url = new URL(dataHref(topic, params.toString(), true), location.origin);
  if (url.href === location.href) {
    const target = document.getElementById('data-' + topic);
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: 'start' });
  } else updateLocation(url);
}
