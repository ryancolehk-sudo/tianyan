type StrengthRank = {rank: number; name: string; strength?: string | number | null};

// Shared by daily review and the live workbench; callers retain date/source ownership.
export function KplStrengthRanks({rows}: {rows: StrengthRank[]}) {
  const numeric = (v: StrengthRank['strength']) => v == null || String(v).trim() === '' || !Number.isFinite(Number(v)) ? null : Number(v);
  const largest = Math.max(1, ...rows.map(row => Math.abs(numeric(row.strength) ?? 0)));
  return <><div className="dir-list-columns"><span>排名 / 题材</span><span>强度值</span></div>
    <div className="dir-strength-list">{Array.from({length:10},(_,i)=>i+1).map(rank=>{
      const row=rows.find(row=>row.rank===rank), value=numeric(row?.strength);
      return <div className="dir-strength-row" key={rank}>
        <span className={`dir-rank${rank<=3?` dir-rank-${rank}`:''}`}>{rank}</span>
        <span className="dir-strength-name" title={row?.name}>{row?.name || '—'}</span>
        <span className="dir-strength-track" aria-hidden="true">{value!==null&&<i style={{width:`${Math.abs(value)/largest*100}%`}}/>}</span>
        <span className="dir-strength-value">{value===null?'—':value.toLocaleString('zh-CN',{maximumFractionDigits:2})}</span>
      </div>;
    })}</div></>;
}
