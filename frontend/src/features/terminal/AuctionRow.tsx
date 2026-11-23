import type { PropsWithChildren } from 'react';
import { useTextHint } from '../../shared/ReadableText';
import { hintDetail } from './details';

export function AuctionRow({ code, name, description, selected, onSelect, children }: PropsWithChildren<{
  code: string; name: string; description: string; selected: boolean; onSelect: (code: string) => void;
}>) {
  const hint = useTextHint<HTMLButtonElement>(description);
  return <><button ref={hint.anchor} type="button" className="ty-auction-row" aria-label={name}
    aria-pressed={selected} aria-describedby={hint.describedBy} {...hint.handlers}
    onMouseEnter={() => hintDetail(code)} onFocus={() => { hint.show(); hintDetail(code); }}
    onClick={() => { hint.show(); onSelect(code); }}>{children}</button>{hint.popup}</>;
}
