import { useEffect, useRef } from 'react';
import type { PropsWithChildren } from 'react';

export function RecordDialog({ title, busy = false, onClose, children }: PropsWithChildren<{ title: string; busy?: boolean; onClose: () => void }>) {
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!dialog.current?.open) {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      dialog.current?.showModal();
    }
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = previous; };
  }, []);
  function closed() {
    onClose();
    // React removes the dialog after its close event. Restore focus after that
    // commit, including when the original list button was replaced by a read.
    requestAnimationFrame(() => {
      const target = opener.current;
      const usable = target?.isConnected && target !== document.body && !target.closest('dialog');
      if (usable) target.focus();
      if (!usable || document.activeElement !== target) document.querySelector<HTMLButtonElement>('[data-new-research]')?.focus();
    });
  }
  return <dialog ref={dialog} className="research-record-dialog" aria-label={title} onClose={closed} onCancel={event => { if (busy) event.preventDefault(); }}>
    <header className="record-dialog-heading"><div><span className="eyebrow">人工研究 · 保留版本</span><h2>{title}</h2></div><button type="button" disabled={busy} onClick={() => dialog.current?.close()}>收起</button></header>
    <div className="record-dialog-body">{children}</div>
  </dialog>;
}
