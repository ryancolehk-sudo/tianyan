import { useEffect, useRef, useState } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { usePrivateDraft } from '../../shared/privateDrafts';
import { useSubmit } from '../../shared/useSubmit';
import { useBoundRead } from './components';
import { blankNotes } from './model';
import type { NotesContent, Ref } from './model';

type Draft = { head: string | null; content: NotesContent; dirty: boolean; version: number; saved_at: string | null };
export function useNotes(session: SessionResponse, day: string, asOf: string, evidence: { current: Ref[] }) {
  const owner = session.data.user.id;
  const key = `daily-review:${day}:${asOf}`;
  const [draft, setDraft] = usePrivateDraft<Draft>(owner, key, () => ({ head: null, content: blankNotes(), dirty: false, version: 0, saved_at: null }), false);
  const read = useBoundRead(key, () => api.reviewNotes(new URLSearchParams({ trade_day: day, ...(asOf ? { as_of_time: asOf } : {}) })));
  const write = useSubmit(owner, key + ':save');
  const pending = useRef(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const current = useRef(draft); current.current = draft;
  const [notice, setNotice] = useState('');
  const [bindingError, setBindingError] = useState<Error | null>(null);
  useEffect(() => { const value = read.data?.data; if (value && !current.current.dirty && (value.revision?.version_number ?? 0) >= current.current.version) setDraft({ head: value.head_id, content: value.content, dirty: false, version: value.revision?.version_number ?? 0, saved_at: value.revision?.received_at ?? null }, false); }, [read.data]);
  const writable = session.data.user.role !== 'viewer' && !asOf && !!read.data && !read.busy && read.data.data.writable !== false && !read.error;
  function update(content: NotesContent) { if (!writable || pending.current) return; setNotice(''); setBindingError(null); setDraft({ ...draft, content: { ...content, confirmed_sections: [], data_evidence_ids: [] }, dirty: true }, true); }
  async function save(content = current.current.content, confirmed?: string) {
    if (!writable || pending.current) return false;
    const ids = [...new Set(evidence.current.map(r => r.id))];
    if (confirmed && ids.length > 100) { setBindingError(new Error('本次资料超过100件，暂不确认。请先保存草稿，再缩小题材或个股范围。')); return false; }
    const value = confirmed ? { ...content, data_evidence_ids: ids, confirmed_sections: [...new Set([...content.confirmed_sections, confirmed])] } : content;
    setBindingError(null);
    const body = { trade_day: day, expected_head: current.current.head, content: value };
    pending.current = true;
    try {
      const result = await write.submit(body, attempt => api.saveReviewNotes(body, session.data.csrf_token, attempt));
      if (!result || !active.current) return false;
      const saved = result.data;
      setDraft({ head: saved.head_id, content: saved.content, dirty: false, version: saved.revision?.version_number ?? 0, saved_at: saved.revision?.received_at ?? null }, false);
      setNotice(`已保存第${saved.revision?.version_number ?? '—'}版${confirmed ? '并人工确认' : ''}，原版本保留。`);
      return true;
    } finally { pending.current = false; }
  }
  function reloadLatest() { setBindingError(null); setDraft({ ...draft, dirty: false }, false); read.reload(); write.clearError(); setNotice('正在重新读取服务器版本。'); }
  return { ...draft, writable, loading: read.busy, busy: write.busy, error: bindingError ?? write.error ?? read.error, notice, update, save, reloadLatest };
}
export type Notebook = ReturnType<typeof useNotes>;
