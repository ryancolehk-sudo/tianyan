import './progressCircle.css';

type Props = {
  'aria-label': string;
  size?: 'sm' | 'md' | 'lg';
} & ({ isIndeterminate: true; value?: never; maxValue?: never } |
     { isIndeterminate?: false; value: number; maxValue?: number });

export function ProgressCircle(props: Props) {
  const size = { sm: 16, md: 24, lg: 32 }[props.size ?? 'md'];
  const max = props.maxValue ?? 100;
  const value = props.isIndeterminate ? undefined : Math.min(max, Math.max(0, props.value));
  const length = value === undefined ? 27 : max > 0 ? value / max * 100 : 0;
  return <svg className="progress-circle" width={size} height={size} viewBox="0 0 32 32"
    data-spinning={props.isIndeterminate || undefined} role="progressbar" aria-label={props['aria-label']}
    aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} focusable="false">
    <circle className="progress-circle-track" cx="16" cy="16" r="12"/>
    {length > 0 && <circle className="progress-circle-arc" cx="16" cy="16" r="12"
      pathLength="100" strokeDasharray={length + ' ' + (100 - length)} transform="rotate(-90 16 16)"/>}
  </svg>;
}
