import { useEffect, useId, useState, useLayoutEffect, useRef } from 'react';
import type { KeyboardEvent, PointerEvent, PropsWithChildren, RefObject } from 'react';
import './chartInspection.css';

export type ChartSample = {
  key: string;
  label: string;
  x: number;
  rows: { label: string; value: string; color: string; y: number | null }[];
};

/** The cursor selects an actual observation, never a value interpolated along a path. */
export function useChartInspection(samples: ChartSample[], scope: string, snapDistance?: number) {
  const id = useId();
  const [selection, setSelection] = useState<{ scope: string; key: string; pinned: boolean; keyboard: boolean } | null>(null);
  const active = selection?.scope === scope ? samples.find(sample => sample.key === selection.key) ?? null : null;
  const current = active ? selection : null;
  useEffect(() => {
    // A refreshed series or changed subject must release a removed pin as well
    // as hide it, otherwise an invisible stale pin would block pointer reading.
    if (selection && !active) setSelection(null);
  }, [active, selection]);
  const clear = () => setSelection(null);
  function nearest(event: PointerEvent<SVGSVGElement>) {
    const svg = event.currentTarget;
    const matrix = svg.getScreenCTM();
    if (!matrix) return null;
    const position = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    let best: ChartSample | null = null, dx = Infinity, dy = Infinity;
    for (const sample of samples) {
      const nextX = Math.abs(sample.x - position.x);
      const values = sample.rows.flatMap(row => row.y == null ? [] : [Math.abs(row.y - position.y)]);
      const nextY = values.length ? Math.min(...values) : Infinity;
      // Distinct real clocks can share the compressed lunch coordinate. Keep them
      // separate and use vertical distance for pointer selection; arrows visit both.
      if (nextX < dx - .01 || Math.abs(nextX - dx) <= .01 && nextY < dy) {
        best = sample; dx = nextX; dy = nextY;
      }
    }
    return snapDistance != null && dx > snapDistance ? null : best;
  }
  function choose(sample: ChartSample, pinned: boolean, keyboard: boolean) {
    setSelection(previous => previous?.scope === scope && previous.key === sample.key && previous.pinned === pinned && previous.keyboard === keyboard
      ? previous : { scope, key: sample.key, pinned, keyboard });
  }
  function keyDown(event: KeyboardEvent<SVGSVGElement>) {
    if (event.key === 'Escape') { event.preventDefault(); clear(); return; }
    if (!samples.length) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault(); choose(active ?? samples[samples.length - 1], !current?.pinned, true); return;
    }
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = active ? samples.findIndex(sample => sample.key === active.key) : -1;
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? samples.length - 1
      : event.key === 'ArrowRight' ? Math.min(samples.length - 1, index + 1)
      : index < 0 ? samples.length - 1 : Math.max(0, index - 1);
    choose(samples[next], current?.pinned ?? false, true);
  }
  return {
    active, pinned: !!current?.pinned, keyboard: !!current?.keyboard, clear,
    helpId: id + '-help', readoutId: id + '-readout',
    events: {
      tabIndex: samples.length ? 0 : undefined,
      'aria-describedby': id + '-help',
      onPointerMove: (event: PointerEvent<SVGSVGElement>) => {
        if (event.pointerType === 'touch' || current?.pinned) return;
        const sample = nearest(event);
        if (sample) choose(sample, false, false); else clear();
      },
      onPointerUp: (event: PointerEvent<SVGSVGElement>) => {
        const sample = nearest(event);
        if (!sample) { clear(); return; }
        if (current?.pinned && active?.key === sample.key) clear();
        else choose(sample, true, false);
      },
      onPointerLeave: () => { if (!current?.pinned && !current?.keyboard) clear(); },
      onFocus: () => { if (!active && samples.length) choose(samples[samples.length - 1], false, true); },
      onBlur: () => { if (!current?.pinned) clear(); },
      onKeyDown: keyDown,
    },
  };
}

type Inspection = ReturnType<typeof useChartInspection>;

/** Match drawing units to pixels in the combined workspace so axis text remains readable. */
export function useChartDimensions(enabled: boolean, minimumHeight = 100) {
  const ref = useRef<SVGSVGElement>(null);
  const [size,setSize] = useState({width:720,height:300});
  useLayoutEffect(() => {
    if (!enabled || !ref.current) return;
    const element=ref.current;
    const measure=()=>{
      const rect=element.getBoundingClientRect();
      if(rect.width>100 && rect.height>minimumHeight) setSize(previous => Math.abs(previous.width-rect.width)<.5 && Math.abs(previous.height-rect.height)<.5 ? previous : {width:rect.width,height:rect.height});
    };
    measure(); const observer=new ResizeObserver(measure); observer.observe(element);
    return ()=>observer.disconnect();
  },[enabled, minimumHeight]);
  return {ref,...(enabled ? size : {width:720,height:300})};
}

export function ChartReference({ inspection, top, bottom, left, right }: { inspection: Inspection; top: number; bottom: number; left: number; right: number }) {
  const sample = inspection.active;
  if (!sample) return null;
  const primary = sample.rows.find(row => row.y != null);
  return <g className="ty-chart-reference" pointerEvents="none" aria-hidden="true">
    <line x1={sample.x} x2={sample.x} y1={top} y2={bottom}/>
    {primary?.y != null && <line className="ty-chart-reference-horizontal" x1={left} x2={right} y1={primary.y} y2={primary.y}/>}
    {sample.rows.map(row => row.y != null && <circle key={row.label} cx={sample.x} cy={row.y} r="4" stroke={row.color}/>)}
  </g>;
}

export function ChartReadout({ inspection, width, subject }: { inspection: Inspection; width: number; subject?: string }) {
  const { active, pinned, keyboard } = inspection;
  return <>
    <span id={inspection.helpId} className="sr-only">左右方向键逐点查看真实采样，Home 和 End 跳到首末点。轻触或按回车固定读数，Escape 收起。缺测时段不补值。</span>
    {active && <div id={inspection.readoutId} className="ty-chart-readout" data-side={active.x > width * .55 ? 'left' : 'right'} data-pinned={pinned || undefined} role="status" aria-live={keyboard ? 'polite' : 'off'}>
      <div className="ty-chart-readout-heading"><strong>{active.label}</strong><span>{pinned ? '已固定' : '实际采样'}</span></div>
      {subject && <div className="ty-chart-readout-subject">{subject}</div>}
      <dl>{active.rows.map(row => <div key={row.label}><dt><i style={{ backgroundColor: row.color }}/>{row.label}</dt><dd style={{ color: row.color }}>{row.value}</dd></div>)}</dl>
      <small>{pinned ? '再次轻触释放 · ← → 逐点查看' : '轻触固定 · ← → 逐点查看'}</small>
    </div>}
  </>;
}

/** Keep pointer state inside this surface so the complete series are not rebuilt
 * on every cursor movement. React reuses the unchanged chart children. */
export function InteractiveChart({ samples, scope, snapDistance, width, height, svgRef, label, subject, children }: PropsWithChildren<{
  samples: ChartSample[]; scope: string; snapDistance?: number; width: number; height: number;
  svgRef?: RefObject<SVGSVGElement | null>; label: string; subject?: string;
}>) {
  const inspection = useChartInspection(samples, scope, snapDistance);
  return <><svg ref={svgRef} viewBox={`0 0 ${width} ${height}`} role="img" {...inspection.events} aria-label={label}>
    {children}
    <ChartReference inspection={inspection} top={22} bottom={height - 40} left={48} right={width - 28}/>
  </svg><ChartReadout inspection={inspection} width={width} subject={subject}/></>;
}
