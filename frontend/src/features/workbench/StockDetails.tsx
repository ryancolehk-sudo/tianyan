import { useEffect, useRef, useState } from 'react';
import { Segmented } from '../../shared/ui';
import { ReferenceWidget } from './ReferenceWidgets';
import { widgetCatalog } from './widgetCatalog';

const groups: Record<string, readonly string[]> = {
  '资金': ['moneyflow', 'margin-detail'],
  '财务': ['fina-indicator', 'income', 'balancesheet', 'cashflow'],
  '事件': ['dividend', 'block-trade', 'auction'],
  '行情资料': ['daily-basic', 'stk-limit', 'daily', 'adj-factor'],
};
export function StockDetails({ name, close }: { name: string; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [group, setGroup] = useState('财务');
  const [selected, setSelected] = useState('fina-indicator');
  useEffect(() => {
    const opener = document.activeElement;
    dialog.current?.showModal();
    return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus({ preventScroll: true }); };
  }, []);
  const definition = widgetCatalog.find(item => item.id === selected)!;
  return <dialog ref={dialog} className="trade-drawer stock-details-drawer" aria-label="个股资料" onClose={close} onCancel={close}>
    <header><strong>{name} · 个股资料</strong><button type="button" onClick={close}>关闭</button></header>
    <div className="trade-drawer-body"><Segmented label="个股资料分类" options={Object.keys(groups)} value={group} onChange={value => { setGroup(value); setSelected(groups[value][0]); }}/>
      <div className="stock-detail-tabs" aria-label="资料选择">{groups[group].map(id => <button type="button" key={id} className={selected === id ? 'selected' : ''} aria-pressed={selected === id} onClick={() => setSelected(id)}>{widgetCatalog.find(item => item.id === id)?.title}</button>)}</div>
      <ReferenceWidget key={selected} definition={definition} selectedOnly/>
    </div>
  </dialog>;
}
