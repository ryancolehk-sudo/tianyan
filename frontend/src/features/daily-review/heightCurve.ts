/** Monotone cubic interpolation: a smooth curve never invents a higher board. */
export function curvePath(points: Array<{ x: number; y: number } | null>): string {
  const runs: Array<Array<{x:number;y:number}>> = []; let run: Array<{x:number;y:number}> = [];
  for (const point of points) { if (point) run.push(point); else if (run.length) { runs.push(run); run=[]; } }
  if(run.length) runs.push(run);
  return runs.map(values => {
    if(values.length===1)return `M${values[0].x},${values[0].y}`;
    const slopes=values.slice(1).map((value,index)=>(value.y-values[index].y)/(value.x-values[index].x));
    const tangents=values.map((_,index)=>index===0?slopes[0]:index===values.length-1?slopes.at(-1)!:slopes[index-1]*slopes[index]<=0?0:2/(1/slopes[index-1]+1/slopes[index]));
    return `M${values[0].x},${values[0].y}`+values.slice(1).map((value,index)=>{const previous=values[index],dx=(value.x-previous.x)/3;return ` C${previous.x+dx},${previous.y+tangents[index]*dx} ${value.x-dx},${value.y-tangents[index+1]*dx} ${value.x},${value.y}`;}).join('');
  }).join(' ');
}
