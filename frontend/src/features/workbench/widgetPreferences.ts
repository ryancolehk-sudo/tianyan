import { useState } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { widgetCatalog } from './widgetCatalog';

export const existingWidgets = [
  { id: 'quote', title: '行情与个股观察', description: '所选标的报价与已采集历史图表', defaultVisible: true },
  { id: 'rules', title: '规则观察', description: '规则定义与运行状态', defaultVisible: false },
  { id: 'sectors', title: '市场环境与板块联动', description: '已采集热度参考；热度不等于涨幅', defaultVisible: true },
  { id: 'funds', title: '资金流向参考', description: '供应商行业与已配置个股资料', defaultVisible: true },
  { id: 'news', title: '市场资讯', description: '已保存的资讯摘要', defaultVisible: true },
  { id: 'signals', title: '规则信号 · 全市场', description: '已运行规则的信号结果', defaultVisible: false },
  { id: 'pools', title: '连板观察池', description: '供应商股池参考，保留原数据日', defaultVisible: true },
  { id: 'research', title: '研究记录', description: '已保存的笔记、预案与复盘；自选在行情区管理', defaultVisible: true },
] as const;

export const allWidgets = [...existingWidgets, ...widgetCatalog];
const allowed = new Set(allWidgets.map(item => item.id));
const defaults = () => allWidgets.filter(item => item.defaultVisible).map(item => item.id);

function read(key: string) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return { visible: defaults(), saved: true };
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid');
    const pref = value as Record<string, unknown>;
    if (Object.keys(pref).sort().join(',') !== 'version,visible' || pref.version !== 1 || !Array.isArray(pref.visible)
      || pref.visible.length > allowed.size || !pref.visible.every(id => typeof id === 'string' && allowed.has(id))
      || new Set(pref.visible).size !== pref.visible.length) throw new Error('invalid');
    const previousDefault = ['quote', 'rules', 'sectors', 'funds', 'news', 'signals', 'pools', 'research', 'top-list'];
    const visible = pref.visible as string[];
    return { visible: visible.length === previousDefault.length && previousDefault.every(id => visible.includes(id)) ? defaults() : visible, saved: true };
  } catch { return { visible: defaults(), saved: false }; }
}

// Only display IDs live in browser storage. No market rows, form values or credentials.
export function useWidgetPreferences(session: SessionResponse) {
  const key = `tianyan.workbench.widgets.v1:${session.meta.app_mode}:${session.data.user.id}`;
  const [state, setState] = useState(() => ({ key, ...read(key) }));
  const current = state.key === key ? state : { key, ...read(key) };
  function save(visible: string[]) {
    let saved = true;
    try { localStorage.setItem(key, JSON.stringify({ version: 1, visible })); }
    catch { saved = false; }
    setState({ key, visible, saved });
  }
  return {
    visible: new Set(current.visible), saved: current.saved,
    toggle: (id: string, checked: boolean) => {
      if (allowed.has(id)) save(allWidgets.filter(item => item.id === id ? checked : current.visible.includes(item.id)).map(item => item.id));
    },
    reset: () => save(defaults()),
  };
}
