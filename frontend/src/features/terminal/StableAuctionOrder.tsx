import type {ReactNode} from 'react';
import type {Stock} from './types';
import {useStableRanking} from '../../shared/workspaceState';
export function StableAuctionOrder({rows,identity,reset,children}:{rows:Stock[];identity:string;reset:number;children:(rows:Stock[],changed:boolean,apply:()=>void)=>ReactNode}){
  const ranking=useStableRanking(rows,identity,reset);const map=new Map(rows.map(row=>[row.code,row]));
  return children(ranking.order.map(code=>map.get(code)!).filter(Boolean),ranking.changed,ranking.apply);
}
