import {test} from 'node:test';
import assert from 'node:assert/strict';
import {rankStrength,toggleFundComparison} from '../../src/features/terminal/strengthSelection.ts';

test('strength ranks observed counts descending, known zero before missing, without mutating input',()=>{
 const rows:any[]=[{code:'missing',limit_up_count:null},{code:'zero',limit_up_count:0},{code:'second',limit_up_count:7},{code:'first',limit_up_count:11}];
 assert.deepEqual(rankStrength(rows).map(r=>r.code),['first','second','zero','missing']);
 assert.equal(rows[0].code,'missing');
});
test('checkbox selection adds, removes and remains empty after unchecking the last item',()=>{
 const a=toggleFundComparison([],'BK0001.DC');
 assert.deepEqual(toggleFundComparison(a,'BK0002.DC'),['BK0001.DC','BK0002.DC']);
 assert.deepEqual(toggleFundComparison(a,'BK0001.DC'),[]);
 assert.deepEqual(a,['BK0001.DC']);
});
test('ten curves are bounded and unchecking always works',()=>{
 const selected=Array.from({length:10},(_,i)=>`BK${i}`);
 assert.deepEqual(toggleFundComparison(selected,'outside'),selected);
 assert.equal(toggleFundComparison(selected,'BK0').length,9);
});
