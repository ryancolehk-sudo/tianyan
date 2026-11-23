import { test } from 'node:test';
import assert from 'node:assert/strict';
import { nodeDelta, rankAuction } from '../../src/features/terminal/auctionComparison.ts';
import { clearWorkspaceState, stabilizeRanking } from '../../src/shared/workspaceState.ts';
import type { Stock } from '../../src/features/terminal/types.ts';

const stock=(code:string,amount:number|null):Stock=>({code,auction_amount_cny:amount,auction_pct:amount,
  node_amounts:{'09:15':0,'09:20':200,'09:25':null},node_source:'synthetic-source',node_received_at:'2001-12-31T09:25:00+08:00'} as Stock);
test('zero node is valid but percentage is undefined; missing never becomes zero',()=>{
  assert.deepEqual(nodeDelta(stock('A',0),'09:20'),{amount:200,pct:null,previous:'09:15'});
  assert.equal(nodeDelta(stock('A',0),'09:15'),null);
  assert.equal(nodeDelta(stock('A',0),'09:25'),null);
  assert.equal(nodeDelta({...stock('A',0),node_source:null},'09:20'),null);
});
test('delta uses only the supplied same-source daily row',()=>{
  const a=stock('A',0), b={...stock('A',0),node_source:'different',node_amounts:{'09:15':null,'09:20':900,'09:25':null}};
  assert.equal(nodeDelta(b,'09:20'),null);
  assert.equal(nodeDelta(a,'09:20')?.amount,200);
});
test('auction sorting keeps zero above missing and rolling does not borrow a node',()=>{
  const a=stock('A',null),b=stock('B',0),c=stock('C',300);
  assert.deepEqual(rankAuction([a,b,c],'amount','09:25').map(s=>s.code),['C','B','A']);
  assert.deepEqual(rankAuction([a,b],'current','09:20').map(s=>s.code),['A','B']);
});
test('refresh preserves reading order; newly observed rows append and vanished rows leave',()=>{
  assert.deepEqual(stabilizeRanking(['A','B','C'],['D','B','A']),['A','B','D']);
  assert.deepEqual(stabilizeRanking([],['B','A']),['B','A']);
});
test('logout clears all workspace owners while leaving unrelated storage intact',()=>{
  const saved={'ty-workspace-v1:user-a:scroll':'1','ty-workspace-v1:user-b:tab':'2','unrelated':'keep'};
  Object.defineProperty(saved,'removeItem',{value:(key:string)=>delete saved[key as keyof typeof saved],enumerable:false});
  Object.defineProperty(globalThis,'sessionStorage',{value:saved,configurable:true});
  clearWorkspaceState(); assert.deepEqual(Object.keys(saved),['unrelated']);
});
