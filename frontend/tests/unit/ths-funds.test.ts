import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withThsFunds, projectThsFunds } from '../../src/features/terminal/thsFundsData.ts';

const day = '2001-12-31', received = day+'T13:05:00+08:00';
const base:any = {trade_day:day, selected_members:[{code:'EM'}], funds_history:[{value:999}],
  sectors:[{code:'EM'}], sector_strength:[{code:'EM',limit_up_count:9}], stocks:[{code:'STOCK'}],
  indices:[{code:'INDEX'}], capabilities:[{key:'members',status:'available'}]};
const data:any = {sampled_day:day,status:'available',received_at:received,complete:true,missing_pages:[],rejected_rows:0,
  rows:[{code:'881001',name:'synthetic',net_cny:'0',pct_change:null,company_count:'12',received_at:received},
        {code:'881002',name:'missing',net_cny:null,pct_change:'NaN',company_count:null,received_at:received}],
  history:[{received_at:received,points:[{code:'881001',net_cny:'0',received_at:received}]}]};
test('THS adapter preserves prices but never borrows EM funds, members or timestamps',()=>{
 const value=withThsFunds(base,{industry:{data,error:''}},'THS:industry:881001',day);
 assert.equal(value.stocks,base.stocks);assert.equal(value.indices,base.indices);
 assert.deepEqual(value.selected_members,[]);assert.deepEqual(value.funds_history,[]);
 assert.equal(value.sectors[0].main_net_cny,null);
 assert.equal(value.intraday_funds?.sectors[0].main_net_cny,0);
 assert.equal(value.intraday_funds?.sectors[1].main_net_cny,null);
 assert.equal(value.intraday_funds?.sectors[1].pct_change,null);
 assert.equal(value.intraday_funds?.latest_source_time,null);
 assert.equal(value.intraday_funds?.time_basis,'received_at');
 assert.equal(value.sector_strength?.[0].limit_up_count,null);
 assert.equal(value.capabilities[0].status,'missing');
 assert.equal(base.selected_members.length,1);
});
test('missing dates and disabled results cannot borrow another day or the prior supplier',()=>{
 const value=withThsFunds(base,{industry:{data,error:''}},'', '2001-12-28');
 assert.deepEqual(value.intraday_funds?.sectors,[]);assert.deepEqual(value.intraday_funds?.points,[]);
 assert.deepEqual(value.funds_history,[]);
});
test('categories keep independent identities, partial coverage and receipt clocks',()=>{
 const value=withThsFunds(base,{industry:{data,error:''},concept:{data:{...data,complete:false,missing_pages:[2],rejected_rows:1},error:''}},'',day);
 assert.equal(value.intraday_funds?.sectors.length,4);
 assert.equal(new Set(value.intraday_funds?.sectors.map(row=>row.code)).size,4);
 assert.equal(value.intraday_funds?.coverage?.[1].expected_count,null);
 assert.match(value.intraday_funds?.category_notes?.concept??'',/缺第 2 页/);
 assert.equal(value.intraday_funds?.points[0].time,received);
 assert.equal(value.intraday_funds?.sectors[0].source_time,null);
});

test('new quotes reuse the unchanged funds projection and a new context cannot reuse it',()=>{
 const reads={industry:{data,error:''}};
 const prepared=projectThsFunds(reads,day);
 const first=withThsFunds(base,reads,'',day,prepared);
 const nextBase={...base,stocks:[{code:'STOCK',price:22}]};
 const next=withThsFunds(nextBase,reads,'',day,prepared);
 assert.equal(first.intraday_funds?.points,next.intraday_funds?.points);
 assert.equal(next.stocks,nextBase.stocks);
 const other=withThsFunds(nextBase,{},'',day,prepared);
 assert.deepEqual(other.intraday_funds?.points,[]);
 const past=withThsFunds(nextBase,reads,'','2001-12-28',prepared);
 assert.deepEqual(past.intraday_funds?.points,[]);
});

test('disabled rows cannot survive and duplicate coverage remains explicit',()=>{
 const disabled=withThsFunds(base,{industry:{data:{...data,status:'disabled'},error:''}},'',day);
 assert.deepEqual(disabled.intraday_funds?.sectors,[]);
 const value=withThsFunds(base,{industry:{data:{...data,complete:false,cross_page_duplicates:2,history_truncated:true},error:''}},'',day);
 assert.match(value.intraday_funds?.category_notes?.industry??'',/跨页重复/);
 assert.match(value.intraday_funds?.category_notes?.industry??'',/超出/);
 assert.equal(value.intraday_funds?.coverage?.[0].complete,false);
});
