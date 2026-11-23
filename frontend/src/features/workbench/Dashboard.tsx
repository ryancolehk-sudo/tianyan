import { useEffect, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, PointerEvent, ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../../shared/icons';
import './dashboard.css';

type Widget = { id: string; title: string; content: ReactNode };
type Tile = { id: string; x: number; y: number; w: number; h: number };
type Props = { owner: string; widgets: Widget[]; onRemove: (id: string) => void; toolbarTarget: HTMLElement | null };
type Action = { id: string; mode: 'move' | 'resize'; px: number; py: number; scroll: number; step: number; rowStep: number; start: Tile[] };
const storageKey = (owner: string) => `tianyan.workbench.geometry.v1:${owner}`;
const clamp = (n: number, min: number, max: number) => Math.max(min, Math.min(max, Math.round(Number.isFinite(n) ? n : min)));
const minWidth = (id: string) => id === 'quote' ? 8 : id === 'pools' || id === 'top-list' ? 5 : 3;
const minHeight = (id: string) => id === 'quote' ? 22 : 6;
const initial: Record<string, [number, number, number, number]> = {
  quote: [0, 0, 8, 22], funds: [8, 0, 4, 22],
  pools: [0, 22, 6, 11], 'top-list': [6, 22, 6, 11],
  sectors: [0, 33, 6, 11], news: [6, 33, 6, 11], research: [0, 44, 12, 9],
  rules: [0, 53, 6, 9], signals: [6, 53, 6, 9],
};
const intersects = (a: Tile, b: Tile) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;

// Keep the tile being manipulated in place and move collisions below it.
// Remove entirely empty rows, including gaps left by hidden widgets.
function settle(tiles: Tile[], changed?: string): Tile[] {
  const placed: Tile[] = [];
  for (const tile of [...tiles].sort((a, b) => a.id === changed ? -1 : b.id === changed ? 1 : a.y - b.y || a.x - b.x)) {
    const next = { ...tile };
    let hit: Tile | undefined;
    while ((hit = placed.find(other => intersects(next, other)))) next.y = hit.y + hit.h;
    placed.push(next);
  }
  const rows = [...new Set(placed.flatMap(tile => Array.from({ length: tile.h }, (_, i) => tile.y + i)))].sort((a, b) => a - b);
  const rowIndex = new Map(rows.map((row, i) => [row, i]));
  return tiles.map(tile => { const next = placed.find(other => other.id === tile.id)!; return { ...next, y: rowIndex.get(next.y) ?? 0 }; });
}

function defaults(widgets: Widget[]): Tile[] {
  let extra = 0;
  return settle(widgets.map(widget => {
    const [x, y, w, h] = initial[widget.id] ?? [(extra % 2) * 6, 49 + Math.floor(extra++ / 2) * 12, 6, 12];
    return { id: widget.id, x, y, w, h };
  }));
}

function validTile(value: unknown): value is Tile {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const tile = value as Record<string, unknown>;
  return Object.keys(tile).sort().join(',') === 'h,id,w,x,y'
    && typeof tile.id === 'string' && /^[a-z][a-z0-9-]{0,63}$/.test(tile.id)
    && ['x', 'y', 'w', 'h'].every(key => typeof tile[key] === 'number' && Number.isInteger(tile[key]))
    && Number(tile.x) >= 0 && Number(tile.y) >= 0 && Number(tile.y) <= 4096
    && Number(tile.w) >= 3 && Number(tile.w) <= 12 && Number(tile.x) + Number(tile.w) <= 12
    && Number(tile.h) >= 4 && Number(tile.h) <= 40;
}

function read(owner: string): { tiles: Tile[]; migrated: boolean } {
  try {
    const raw = localStorage.getItem(storageKey(owner));
    if (raw) {
      if (raw.length > 20000) return { tiles: [], migrated: false };
      const parsed: unknown = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { tiles: [], migrated: false };
      const value = parsed as Record<string, unknown>;
      if (Object.keys(value).sort().join(',') !== 'tiles,version' || value.version !== 1 || !Array.isArray(value.tiles)
        || value.tiles.length > 64 || !value.tiles.every(validTile) || new Set(value.tiles.map(tile => tile.id)).size !== value.tiles.length) return { tiles: [], migrated: false };
      const oldDefault: Record<string, number[]> = { quote: [0,0,8,18], news: [8,0,4,9], sectors: [8,9,4,9], funds: [0,18,6,10], 'top-list': [6,18,6,10], pools: [0,28,12,12], rules: [0,40,4,9], signals: [4,40,4,9], research: [8,40,4,9] };
      const prior = value.tiles as Tile[];
      const untouched = prior.length > 0 && prior.every(tile => oldDefault[tile.id]?.every((n, i) => n === [tile.x,tile.y,tile.w,tile.h][i]));
      return { tiles: untouched ? [] : prior.map(tile => ({ ...tile, w: Math.max(tile.w, minWidth(tile.id)), x: Math.min(tile.x, 12 - Math.max(tile.w, minWidth(tile.id))), h: Math.max(tile.h, minHeight(tile.id)) })), migrated: false };
    }
    const user = owner.split(':').at(-1);
    const legacy = localStorage.getItem(`tianyan:workbench-layout:v1:${user}`);
    if (!legacy || legacy.length > 20000) return { tiles: [], migrated: false };
    const previous = JSON.parse(legacy);
    if (previous?.version !== 1 || !Array.isArray(previous.tiles) || previous.tiles.length > 64) return { tiles: [], migrated: false };
    const aliases: Record<string, string> = { rule: 'rules', heat: 'sectors', watch: 'research' };
    const migrated = previous.tiles.flatMap((item: Record<string, unknown>) => {
      if (!item || typeof item !== 'object' || typeof item.id !== 'string' || ['indices', 'breadth', 'timeline'].includes(item.id)) return [];
      const tile = { id: aliases[item.id] ?? item.id, x: item.x, y: item.y, w: item.w, h: item.h };
      return validTile(tile) ? [tile] : [];
    }) as Tile[];
    return { tiles: migrated.filter((tile, index) => migrated.findIndex(other => other.id === tile.id) === index).map(tile => ({ ...tile, w: Math.max(tile.w, minWidth(tile.id)), x: Math.min(tile.x, 12 - Math.max(tile.w, minWidth(tile.id))), h: Math.max(tile.h, minHeight(tile.id)) })), migrated: true };
  } catch { return { tiles: [], migrated: false }; }
}

function merge(remembered: Tile[], current: Tile[]) {
  const ids = new Set(current.map(tile => tile.id));
  return [...remembered.filter(tile => !ids.has(tile.id)), ...current].slice(-64);
}

export function Dashboard(props: Props) { return <DashboardCanvas key={props.owner} {...props}/>; }

function DashboardCanvas({ owner, widgets, onRemove, toolbarTarget }: Props) {
  const [loaded] = useState(() => read(owner));
  const [saved, setSaved] = useState(loaded.tiles);
  const [geometry, setGeometry] = useState(loaded.tiles);
  const [editing, setEditing] = useState(false);
  const [wide, setWide] = useState(false);
  const [message, setMessage] = useState('');
  const [selected, setSelected] = useState('');
  const grid = useRef<HTMLDivElement>(null);
  const action = useRef<Action | null>(null);
  const pending = useRef<Tile[] | null>(null);
  const frame = useRef<number | null>(null);
  const base = defaults(widgets);
  const tiles = settle(base.map(tile => geometry.find(item => item.id === tile.id) ?? tile));
  const tilesRef = useRef(tiles);
  tilesRef.current = tiles;
  function replace(next: Tile[]) { setGeometry(current => merge(current, next)); }

  useEffect(() => {
    const observer = new ResizeObserver(entries => setWide(entries[0].contentRect.width >= 900));
    if (grid.current) observer.observe(grid.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (loaded.migrated) {
      try { localStorage.setItem(storageKey(owner), JSON.stringify({ version: 1, tiles: loaded.tiles })); }
      catch { setMessage('旧布局已恢复，本次使用；浏览器未能保存。'); }
    }
  }, [loaded, owner]);
  useEffect(() => {
    function escape(event: globalThis.KeyboardEvent) {
      if (event.key === 'Escape' && action.current) {
        const start = action.current.start;
        action.current = null;
        pending.current = null;
        if (frame.current !== null) cancelAnimationFrame(frame.current);
        frame.current = null;
        setGeometry(current => merge(current, start));
      }
    }
    window.addEventListener('keydown', escape);
    return () => { window.removeEventListener('keydown', escape); if (frame.current !== null) cancelAnimationFrame(frame.current); };
  }, []);

  function begin(event: PointerEvent<HTMLButtonElement>, id: string, mode: Action['mode']) {
    if (!wide || event.button !== 0) return;
    event.preventDefault();
    setSelected(id);
    event.currentTarget.setPointerCapture(event.pointerId);
    const style = getComputedStyle(grid.current!);
    const gap = Number.parseFloat(style.columnGap) || 0;
    const rowStep = (Number.parseFloat(style.gridAutoRows) || 28) + (Number.parseFloat(style.rowGap) || 0);
    action.current = { id, mode, px: event.clientX, py: event.clientY, scroll: window.scrollY, step: (grid.current!.clientWidth + gap) / 12, rowStep, start: tilesRef.current };
  }
  function move(event: PointerEvent<HTMLButtonElement>) {
    const active = action.current;
    if (!active) return;
    const tile = active.start.find(item => item.id === active.id)!;
    const dx = Math.round((event.clientX - active.px) / active.step);
    const dy = Math.round((event.clientY - active.py + window.scrollY - active.scroll) / active.rowStep);
    const patch = active.mode === 'move'
      ? { x: clamp(tile.x + dx, 0, 12 - tile.w), y: clamp(tile.y + dy, 0, 1000) }
      : { w: clamp(tile.w + dx, minWidth(tile.id), 12 - tile.x), h: clamp(tile.h + dy, minHeight(tile.id), 40) };
    pending.current = settle(active.start.map(item => item.id === tile.id ? { ...item, ...patch } : item), tile.id);
    if (frame.current === null) frame.current = requestAnimationFrame(() => {
      frame.current = null;
      if (pending.current) { replace(pending.current); pending.current = null; }
    });
  }
  function finish(cancel = false) {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    if (cancel && action.current) replace(action.current.start);
    else if (pending.current) replace(pending.current);
    pending.current = null;
    action.current = null;
  }
  function keyboard(event: KeyboardEvent<HTMLButtonElement>, tile: Tile, resize = false) {
    if (!wide || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    setSelected(tile.id);
    const dx = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0;
    const dy = event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0;
    const patch = resize || event.shiftKey
      ? { w: clamp(tile.w + dx, minWidth(tile.id), 12 - tile.x), h: clamp(tile.h + dy, minHeight(tile.id), 40) }
      : { x: clamp(tile.x + dx, 0, 12 - tile.w), y: clamp(tile.y + dy, 0, 1000) };
    replace(settle(tiles.map(item => item.id === tile.id ? { ...item, ...patch } : item), tile.id));
  }
  function save() {
    const current = pending.current ?? tiles;
    finish();
    const next = merge(geometry, current);
    try {
      localStorage.setItem(storageKey(owner), JSON.stringify({ version: 1, tiles: next }));
      setGeometry(next); setSaved(next); setEditing(false); setMessage('布局已保存');
    } catch { setMessage('浏览器未能保存布局，请允许本地存储后重试。'); }
  }
  const toolbar = <div className="dashboard-toolbar" aria-label="工作台布局操作">
    {editing ? <>
      <button type="button" onClick={() => { finish(); setGeometry(defaults(widgets)); setMessage('已恢复默认布局，保存后生效'); }}>恢复默认</button>
      <button type="button" onClick={() => { finish(true); setGeometry(saved); setEditing(false); setMessage('已取消布局调整'); }}>取消</button>
      <button type="button" className="primary" onClick={save}>保存布局</button>
    </> : <button type="button" className="dashboard-edit-button" onClick={() => { setEditing(true); setMessage(''); }}>编辑布局</button>}
  </div>;
  return <section className={`dashboard${editing ? ' dashboard-editing' : ''}`} aria-label="自定义工作台">
    {toolbarTarget ? createPortal(toolbar, toolbarTarget) : toolbar}
    {(editing || message) && <p className="dashboard-status" role="status">{message || (wide ? '拖动标题移动 · 右下角调整大小 · 方向键移动，Shift＋方向键缩放。位置和尺寸需保存；组件开关即时保存。' : '窄屏下可移除组件；在宽屏下调整位置与大小。组件开关即时保存。')}</p>}
    <div ref={grid} className={`dashboard-grid${wide ? ' dashboard-wide' : ''}`} aria-label="工作台组件画布">
      {[...tiles].sort((a, b) => a.y - b.y || a.x - b.x).map(tile => {
        const widget = widgets.find(item => item.id === tile.id)!;
        return <section key={tile.id} data-widget-id={tile.id} aria-label={`${widget.title}组件`} className={`dashboard-tile${editing && selected === tile.id ? ' is-selected' : ''}`} style={wide ? { gridColumn: `${tile.x + 1} / span ${tile.w}`, gridRow: `${tile.y + 1} / span ${tile.h}` } as CSSProperties : undefined}>
          {editing && <div className="dashboard-tile-tools">
            <button type="button" className="dashboard-drag" aria-label={`移动${widget.title}`} onPointerDown={event => begin(event, tile.id, 'move')} onPointerMove={move} onPointerUp={() => finish()} onPointerCancel={() => finish(true)} onLostPointerCapture={() => finish()} onKeyDown={event => keyboard(event, tile)} onClick={() => setSelected(tile.id)}><Icon name="drag"/> {widget.title}</button>
            <button type="button" className="dashboard-remove" aria-label={`移除${widget.title}`} onClick={() => { finish(); onRemove(tile.id); setMessage(`已移除${widget.title}，可从“管理组件”重新添加`); }}><Icon name="close"/></button>
          </div>}
          <div className="dashboard-tile-content" tabIndex={0}>{widget.content}</div>
          {editing && wide && <button type="button" className="dashboard-resize" aria-label={`调整${widget.title}大小`} onPointerDown={event => begin(event, tile.id, 'resize')} onPointerMove={move} onPointerUp={() => finish()} onPointerCancel={() => finish(true)} onLostPointerCapture={() => finish()} onKeyDown={event => keyboard(event, tile, true)}><Icon name="resize"/></button>}
        </section>;
      })}
    </div>
    {!widgets.length && <div className="dashboard-empty"><strong>选择你要关注的市场信息</strong><p>展开顶部“管理组件”，添加工作台组件。</p></div>}
  </section>;
}
