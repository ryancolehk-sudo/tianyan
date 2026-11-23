export type ResearchKind = 'review' | 'direction' | 'plan';
export const researchNames = { review: '每日复盘', direction: '方向研究', plan: '条件预案' };
export const publicationNames = { draft: '草稿', published: '已发布', withdrawn: '已撤回' };
export function recordedTime(value?: string | null) { return value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(new Date(value)) : '尚未发布'; }
export function researchSlot(kind: ResearchKind, day: string, documentId: string | null) { return 'research:edit:' + (documentId ?? kind + ':' + day + ':new'); }
