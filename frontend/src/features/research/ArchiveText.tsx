import { useEffect, useMemo, useRef, useState } from 'react';

type Token = { text: string; match?: number };
type Block = { heading: Token[] | null; body: Token[]; preview: string };

function reading(text: string, query: string) {
  const matches: Array<{ block: number }> = [];
  const expression = query ? new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu') : null;
  function tokens(value: string, block: number): Token[] {
    if (!expression) return [{ text: value }];
    const parts: Token[] = []; let start = 0;
    for (const match of value.matchAll(expression)) {
      if (match.index > start) parts.push({ text: value.slice(start, match.index) });
      parts.push({ text: match[0], match: matches.length }); matches.push({ block });
      start = match.index + match[0].length;
    }
    parts.push({ text: value.slice(start) }); return parts;
  }
  const blocks: Block[] = text.replace(/\r\n/g, '\n').trim().split(/\n\s*\n/).map((block, index) => {
    const lines = block.split('\n');
    const heading = lines[0].match(/^(?:\*\*(.+)\*\*|#{1,4}\s+(.+))$/);
    const title = heading ? heading[1] ?? heading[2] : null;
    const original = (title ?? block).replace(/\s+/g, ' ');
    return { heading: title ? tokens(title, index) : null, body: tokens(title ? lines.slice(1).join('\n') : block, index),
      preview: original.length > 28 ? original.slice(0, 28) + '…' : original };
  });
  return { blocks, matches };
}

export function ArchiveText({ text, archiveId, mode, paragraph, locate }: {
  text: string; archiveId: string; mode: string; paragraph: string | null; locate: (value: number | null) => void;
}) {
  const [query, setQuery] = useState(''); const [active, setActive] = useState(-1);
  const [findAction, setFindAction] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const { blocks, matches } = useMemo(() => reading(text, query), [text, query]);
  const prefix = `archive-${archiveId}-${mode}`;
  const selected = paragraph !== null && /^\d{1,4}$/.test(paragraph) && Number(paragraph) < blocks.length ? Number(paragraph) : null;
  useEffect(() => {
    if (selected === null) return;
    const block = document.getElementById(`${prefix}-p${selected}`);
    block?.focus({ preventScroll: true }); block?.scrollIntoView({ block: 'start' });
  }, [selected, prefix]);
  useEffect(() => {
    if (active < 0) return;
    const match = document.getElementById(`${prefix}-m${active}`);
    match?.focus({ preventScroll: true }); match?.scrollIntoView({ block: 'center' });
  }, [active, findAction, prefix]);
  function find(direction: number) {
    if (!matches.length) return;
    const next = (active < 0 ? direction > 0 ? 0 : matches.length - 1 : (active + direction + matches.length) % matches.length);
    setActive(next); setFindAction(action => action + 1); locate(matches[next].block);
  }
  function top() { locate(null); root.current?.focus({ preventScroll: true }); root.current?.scrollIntoView({ block: 'start' }); }
  function jump(index: number) { locate(index); const block = document.getElementById(`${prefix}-p${index}`); block?.focus({ preventScroll: true }); block?.scrollIntoView({ block: 'start' }); }
  function render(tokens: Token[]) { return tokens.map((token, index) => token.match === undefined ? token.text : <mark key={index}
    id={`${prefix}-m${token.match}`} tabIndex={-1} className={active === token.match ? 'current-match' : undefined}>{token.text}</mark>); }
  return <div className="archive-document" ref={root} tabIndex={-1} aria-label="原文阅读工具与段落">
    <details className="archive-outline"><summary>原文定位 · 共 {blocks.length} 段</summary><p className="field-help">按原段顺序定位；没有标题的段落显示原句开头。</p>
      <nav aria-label="原文段落">{blocks.map((block, index) => {
        const url = new URL(window.location.href); url.searchParams.set('archive', archiveId); url.searchParams.set('report', mode);
        url.searchParams.set('paragraph', String(index)); url.searchParams.set('workspace', 'archive');
        return <a key={index} href={url.pathname + url.search + url.hash} aria-label={`定位原文第${index + 1}段`} aria-current={selected === index ? 'location' : undefined}
          onClick={event => { if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); jump(index); }}>
          <span>{index + 1}</span><span>{block.preview}</span></a>;
      })}</nav>
    </details>
    <form className="archive-find" role="search" aria-label="原文页内查找" onSubmit={event => { event.preventDefault(); find(1); }}>
      <label>查找原文<input type="search" value={query} maxLength={200} aria-label="查找原文" placeholder="查找这份原稿中的文字"
        onChange={event => { setQuery(event.target.value); setActive(-1); }} onKeyDown={event => {
          if (event.key === 'Enter' && event.shiftKey) { event.preventDefault(); find(-1); }
          if (event.key === 'Escape') { setQuery(''); setActive(-1); }
        }}/></label><div className="archive-find-actions"><button type="button" disabled={!matches.length} onClick={() => find(-1)}>上一处</button>
        <button type="submit" disabled={!matches.length}>下一处</button><button type="button" disabled={!query} onClick={() => { setQuery(''); setActive(-1); }}>清空查找</button></div>
      <span role="status">{!query ? '仅在这份原稿中查找' : !matches.length ? '原文中没有匹配文字' : active < 0 ? `共 ${matches.length} 处匹配` : `第 ${active + 1} / ${matches.length} 处匹配`}</span>
    </form>
    {paragraph !== null && selected === null && <p className="archive-position-missing" role="status">定位段落不存在或未绑定这份原稿，完整原文仍可阅读。<button onClick={top}>返回报告开头</button></p>}
    <div className="archive-prose">{blocks.map((block, index) => <section key={index} id={`${prefix}-p${index}`} tabIndex={-1} aria-label={`原文第${index + 1}段`}>
      {block.heading && <h3>{render(block.heading)}</h3>}{block.body.some(token => token.text) && <p>{render(block.body)}</p>}
    </section>)}</div><button className="archive-return-top" onClick={top}>返回报告开头</button>
  </div>;
}
