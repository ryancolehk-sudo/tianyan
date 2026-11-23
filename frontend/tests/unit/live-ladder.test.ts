import {test} from 'node:test';
import assert from 'node:assert/strict';
import {ladderColumns} from '../../src/features/daily-review/liveLadder.ts';
const day='2026-11-03';
const history=[{trade_day:'2026-11-02',state:'complete',stocks:[{ts_code:'600001.SH',board_height:2}]},{trade_day:day,state:'missing',stocks:[]}];
const live:any={trade_day:day,phase:'trading',source_time:day+'T10:00:00+08:00',rows:[{code:'600001.SH',name:'合成',target_height:3,previous_height:2,status:'sealed',freshness:'current',source_time:day+'T10:00:00+08:00'}]};
test('only current day is overlaid and history source objects remain unchanged',()=>{
 const result=ladderColumns(history,live,day,'');assert.equal(result[0],history[0]);
 assert.equal(result[1].basis,'intraday_ladder');assert.equal(history[1].state,'missing');
 assert.equal(result[1].stocks?.[0].live_status,'sealed');
});
test('next quote changes a seal into an open without preserving a stale success',()=>{
 const next={...live,rows:[{...live.rows[0],status:'opened'}]};
 assert.equal(ladderColumns(history,next,day,'')[1].stocks?.[0].live_status,'opened');
});
test('historical links, another day and old backend responses cannot borrow live data',()=>{
 assert.equal(ladderColumns(history,live,day,day+'T09:40:00+08:00'),history);
 assert.equal(ladderColumns(history,live,'2026-11-02',''),history);
 assert.equal(ladderColumns(history,undefined,day,''),history);
});
test('after close a received daily result replaces the explicitly intraday last observation',()=>{
 const closed=[...history.slice(0,1),{trade_day:day,state:'complete',stocks:[{ts_code:'600001.SH',board_height:3}]}];
 assert.equal(ladderColumns(closed,{...live,phase:'closed'},day,''),closed);
 assert.equal(ladderColumns(history,{...live,phase:'closed'},day,'')[1].basis,'intraday_ladder');
});
