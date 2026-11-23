import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {dateContext} from '../../src/features/terminal/dateContext.ts';
const read=(path:string)=>readFileSync(new URL('../../'+path,import.meta.url),'utf8');
const base=(path:string)=>execFileSync('git',['show','5048468:'+path],{encoding:'utf8'});
test('approved workstation keeps market, merged ranking, research and screening workspaces',()=>{
 const text=read('src/features/terminal/Overview.tsx');
 const workspace=read('src/features/terminal/OverviewWorkspace.tsx');
 // ADR0078 merges the speed column into the gain table. The workspace owns
 // the ribbon and research/screen/AI panes after the approved layout split.
 assert.ok(text.includes('title="涨跌停家数走势"'));
 for(const component of ['BreadthChart','StockRanking','OverviewFunds','OverviewLadder','SectorTracking','OverviewBoards'])assert.ok(text.includes('<'+component),component);
 for(const component of ['Overview','MarketRibbon','ResearchWorkspace','ScreeningPanel','AIOverview','Coverage'])assert.ok(workspace.includes('<'+component),component);
 const terminal=read('src/features/terminal/Terminal.tsx');assert.match(terminal,/<OverviewWorkspace \{\.\.\.props\}/);
 assert.match(text,/<StockRanking[^>]*rows=\{data\.stocks\}/);
 const ranking=read('src/features/terminal/StockRanking.tsx');
 for(const field of ['speed_5m','pct_change','amount_cny','ths_themes'])assert.ok(ranking.includes("heading('"+field+"'"),field);
});
test('approved auction retains details, daily status, follow action and source clocks',()=>{
 const text=read('src/features/terminal/Auction.tsx');
 // ADR0086/0087 explicitly remove the screening, AI and watchlist panels.
 // The saved watchlist and detail follow action remain available.
 for(const key of ['所选股票跨日状态','auction_tags','one_word_count','header_total_cny','三个节点封单','实际竞价成交 · 跨日比较','volume_ratio','node_source','node_received_at'])assert.ok(text.includes(key),key);
 assert.match(text,/<Follow selected=\{selected\} watch=\{watch\}/);
 assert.match(text,/<AuctionReference user=\{props\.session\.data\.user\.id\}/);
});
test('all eight original review cards and original plan list remain in order',()=>{
 const text=read('src/features/daily-review/Overview.tsx');let last=-1;
 for(let i=1;i<=8;i++){const index=text.indexOf('number="'+String(i).padStart(2,'0')+'"');assert.ok(index>last);last=index;}
 assert.ok(text.includes('api.researchDocuments(\'plan\', previous)'));assert.ok(text.includes('<PriorPlans {...p}/>'));
});
function retained(a:any,b:any,path:string){
  // Pagination coverage changed this descriptive response default; its clock
  // and partial-coverage qualifications must remain explicit.
  if(path==='IntradayFunds/properties/scope/default'){
   assert.match(b,/单页或未齐页组不代表完整市场/);assert.match(b,/同源/);assert.match(b,/270至330秒/);return;
  }
  // A lower numeric minimum accepts every previously valid client value.
  if(path.endsWith('/minimum')&&typeof a==='number'){
   assert.ok(typeof b==='number'&&Number.isFinite(b)&&b<=a,path);return;
  }
  if(Array.isArray(a)){assert.ok(Array.isArray(b),path);if(path.endsWith('/enum')){for(const item of a)assert.ok(b.includes(item),path);}else if(path.endsWith('/parameters')){
   const key=(parameter:any)=>JSON.stringify([parameter.name,parameter.in]);
   assert.equal(new Set(b.map(key)).size,b.length,path+' duplicate parameters');
   for(const value of a){const existing=b.find(candidate=>key(candidate)===key(value));assert.ok(existing,path+'/'+key(value));retained(value,existing,path+'/'+key(value));}
  }else{assert.ok(b.length>=a.length,path);a.forEach((v,i)=>retained(v,b[i],path+'/'+i));}return;}
  if(a&&typeof a==='object'){for(const key of Object.keys(a)){assert.ok(b&&key in b,path+'/'+key);retained(a[key],b[key],path+'/'+key);}return;}
  assert.equal(b,a,path);
}
test('every original API path/method/parameter/response schema is retained',()=>{
 const previous=JSON.parse(base('contracts/openapi.p2.json'));const current=JSON.parse(readFileSync(new URL('../../../contracts/openapi.p2.json',import.meta.url),'utf8'));
 for(const [path,value] of Object.entries(previous.paths))retained(value,current.paths[path],path);
 for(const [name,value] of Object.entries(previous.components.schemas))retained(value,current.components.schemas[name],name);
});
test('additive query parameters and relaxed numeric minima preserve the old contract',()=>{
 const limit={name:'limit',in:'query',schema:{type:'integer'}};
 const offset={name:'offset',in:'query',schema:{type:'integer'}};
 retained([limit,offset],[offset,{name:'source_id',in:'query',schema:{type:'string'}},limit],'/get/parameters');
 retained({minimum:60},{minimum:1},'SourceConfiguration/interval');
});
test('parameter identity, type and response removal still fail contract retention',()=>{
 const parameter={name:'limit',in:'query',schema:{type:'integer'}};
 for(const current of [[],[{...parameter,name:'renamed'}],[{...parameter,in:'header'}],[{...parameter,schema:{type:'string'}}],[parameter,parameter]]){
  assert.throws(()=>retained([parameter],current,'/get/parameters'));
 }
 assert.throws(()=>retained({responses:{200:{schema:{type:'integer'}}}},{responses:{}},'/get'));
 assert.throws(()=>retained({minimum:60},{minimum:61},'SourceConfiguration/interval'));
 assert.throws(()=>retained({default:20},{default:30},'UnchangedDefault'));
});
test('dates distinguish selected history, stale automatic data and a wrong response without relabeling',()=>{
 assert.equal(dateContext('','2026-10-27','2026-11-02','').state,'lagging');
 assert.equal(dateContext('2026-10-27','2026-10-27','2026-11-02','').state,'history');
 assert.equal(dateContext('2026-11-02','2026-10-27','2026-11-02','').state,'mismatch');
 assert.equal(dateContext('','2026-11-02','2026-11-02','').state,'current');
});
