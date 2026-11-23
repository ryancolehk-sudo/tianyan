// Local preview only. Demo fixtures and read-only existing data are kept separate.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname,sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import {terminalFixture,stockA,stockB,sectorA,sectorB} from './e2e/terminalTestFixtures.ts';
import {day,blank,days,theme,ref} from './e2e/reviewTestFixtures.ts';
const stored=process.argv.includes('--stored'),port=Number(process.argv.find(value=>value.startsWith('--port='))?.slice(7)||(stored?18098:18099));
const projectRoot=resolve(fileURLToPath(new URL('../../',import.meta.url)));
const distArg=process.argv.find(value=>value.startsWith('--dist='))?.slice(7);
const root=resolve(projectRoot,distArg||'frontend/dist');
const controlPath=resolve(projectRoot,process.argv.find(value=>value.startsWith('--control='))?.slice(10)||'output/additive-preview.private/control.json');
const meta={request_id:'00000000-0000-4000-8000-000000000077',app_mode:'demo',generated_at:'2001-12-31T18:00:00+08:00',as_of_time:null,quality:{status:'partial',reasons:['隔离合成演示，不是真实行情']}};
let reads=0;
const server=http.createServer(async(req,res)=>{try{
 const url=new URL(req.url,'http://127.0.0.1:'+port),p=url.pathname,q=url.searchParams;
 const ok=(data,status=200)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({meta,data}));};
 const fail=(message,status=503)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({error:{code:'preview_unavailable',message},meta}));};
 if(p.startsWith('/api/')){
  if(req.method!=='GET')return fail('本机预览只读，保存操作请使用正式系统。',405);
  if(stored){
   if(p==='/api/v1/news/topics'||p==='/api/v1/review-data/analysis'&&q.get('kind')==='comparison')return fail('新增查询尚未发布到正式服务；可在合成演示页查看完整交互。',501);
   const headers={accept:'application/json'};if(req.headers.cookie)headers.cookie=req.headers.cookie;
   const upstream=await fetch('http://127.0.0.1:8088'+url.pathname+url.search,{headers,redirect:'error',signal:AbortSignal.timeout(45000)});
   res.writeHead(upstream.status,{'Content-Type':upstream.headers.get('content-type')||'application/json','Cache-Control':'no-store'});return res.end(Buffer.from(await upstream.arrayBuffer()));
  }
  let control={};try{control=JSON.parse(await readFile(controlPath,'utf8'));}catch{}
  if(control.authExpired)return fail('合成会话已过期，请重新登录。',401);
  if(p==='/api/v1/session')return ok({user:{id:'additive-synthetic',username:'增量预览·合成数据',role:'admin'},csrf_token:'synthetic-only'});
  if(p==='/api/v1/ai/settings')return ok({enabled:false,features:[]});
  if(p==='/api/v1/market-session')return ok({server_time:'2001-12-31T15:05:00+08:00',trade_day:'2001-12-31',phase:'closed',is_trading_day:true,automatic_live:false,current_day_active:true,next_transition_at:null,poll_seconds:60});
  if(p==='/api/v1/workbench-view'||p==='/api/v1/workbench-details'){
   if(control.failWorkbench)return fail('合成慢网测试：资料读取失败，原页面保留。');
   const data=terminalFixture(q);data.detail_revision='additive-test-r1';data.detail_codes=[stockA,stockB];
   if(control.oldDay)data.trade_day='2001-12-27';
   for(let i=0;i<78;i++)data.stocks.push({...data.stocks[0],code:`98${String(i).padStart(4,'0')}.SZ`,name:'合成样本'+i,pct_change:(control.reverseRank?78-i:i)/10});
   data.breadth.scope='隔离合成样本（80只），不是真实市场';
   if(control.liveLadder && q.get('view')==='ladder' && !q.get('as_of_time')) {
    const stamp=data.trade_day+'T10:00:00+08:00';
    const row=(code,name,previous,status,freshness='current')=>({code,name,previous_height:previous,target_height:previous===null?null:previous+1,status,freshness,source_time:stamp,source_name:'合成盘口',pct_change:10});
    data.intraday_ladder={trade_day:data.trade_day,previous_trade_day:'2001-12-28',as_of_time:stamp,phase:'trading',source_time:stamp,sample_count:80,sealed_count:control.liveOpened?0:1,stale_count:1,scope:'隔离合成盘中样本，不是真实行情；昨日板数缺失不推定首板。',rows:[row('600001.SH','合成封板',6,control.liveOpened?'opened':'sealed'),row('600002.SH','合成开板',1,'opened'),row('600003.SH','合成待晋级',2,'waiting'),row('600004.SH','合成板数缺失',null,'touched'),row('600005.SH','合成过期',3,'sealed','stale')]};
   }
   data.selected_members=q.get('sector')===sectorB?[data.stocks[1]]:data.stocks;
   data.auction_days.forEach(entry=>{entry.node_source='合成封单来源';entry.node_received_at=entry.day+'T09:26:00+08:00';entry.one_word_count=2;entry.header_total_cny=300000000;entry.items.forEach((stock,i)=>{stock.node_amounts={'09:15':0,'09:20':200000000,'09:25':i?null:300000000};stock.node_source=entry.node_source;stock.node_received_at=entry.node_received_at;stock.auction_tags=['保留原标签'];});});
   if(p==='/api/v1/workbench-view'){reads++;await new Promise(r=>setTimeout(r,control.delay||0));return ok(data);}
   if(control.failSector===q.get('sector'))return fail('合成板块读取失败，请重试。');
   await new Promise(r=>setTimeout(r,control.sectorDelay||(q.get('sector')===sectorA?700:100)));
   const codes=(q.get('codes')||'').split(',');return ok({trade_day:data.trade_day,revision:'additive-test-r1',stocks:data.stocks.filter(s=>codes.includes(s.code)),auction_days:data.auction_days,events:[],seals:{},sector_code:q.get('sector'),members:data.selected_members,tracking_members:data.selected_members,tracking_member_count:data.selected_members.length,tracking_members_truncated:false,members_trade_day:data.trade_day,funds_history:data.funds_history});
  }
  if(p==='/api/v1/news/topics'){
   if(control.failTopic)return fail('合成局部故障：题材证据暂不可用。');
   return ok({topic_source:q.get('topic_source'),topic_key:q.get('topic_key'),keyword:q.get('keyword'),trade_day:q.get('trade_day'),as_of_time:q.get('trade_day')+'T15:00:00+08:00',total:1,next_cursor:null,members:[],coverage:[],gaps:['合成覆盖缺口：未保存历史成员'],events:[{id:'synthetic',evidence_id:'synthetic-1',title:'合成事件：'+q.get('keyword'),summary:'仅用于测试切换与依据展示，不是真实资讯。',basis:'keyword_clue',basis_note:'关键词命中不等同催化原因',source_id:'synthetic',source_name:'合成来源',source_url:null,published_at:q.get('trade_day')+'T09:00:00+08:00',received_at:q.get('trade_day')+'T09:01:00+08:00',content_hash:'synthetic'}]});
  }
  if(p==='/api/v1/review-data/day')return ok(day(q.get('trade_day')||days[1]));
  if(p==='/api/v1/review-data/series')return ok({shared_date:days[1],state:'partial',trade_days:days,records:days.map(d=>({trade_day:d,state:'partial',metrics:day(d).metrics,indices:day(d).indices,themes:{records:[theme.provider_record]},coverage:[],evidence_refs:[]}))});
  if(p==='/api/v1/review-data/notes')return ok({trade_day:q.get('trade_day')||days[1],state:'empty',content:blank(),head_id:null,writable:false,is_current:true,revision:null});
  if(p==='/api/v1/review-data/analysis'&&q.get('kind')==='ladder'&&control.liveLadder)return ok({kind:'ladder',state:'partial',records:[{trade_day:'2001-12-28',state:'complete',stocks:[{ts_code:'600001.SH',name:'合成历史六板',board_height:6}]},{trade_day:'2001-12-31',state:'missing',stocks:[]}],coverage:[],scope_note:'隔离合成历史对照'});
  if(p==='/api/v1/review-data/analysis'&&q.get('kind')==='comparison')return ok({kind:'comparison',state:'partial',trade_days:days,comparison:{previous_trade_day:days[0],trade_day:days[1],comparable:true,promoted_n:1,failed_n:0,unknown_n:1,records:[{code:'600001.SH',name:'合成晋级',previous_height:1,current_height:2,state:'promoted',reason:'合成同源相邻日',previous_evidence:ref,current_evidence:ref},{code:'600002.SH',name:'合成缺失',previous_height:2,current_height:null,state:'unknown',reason:'未观测，不能认定掉队'}],events:[{code:'600001.SH',name:'合成打开记录',state:'U',open_times:2,first_seal:'93000',last_seal:'140005',evidence:ref}]},coverage:[],scope_note:'合成数据：缺失不认定掉队，未记录的过程不补画。'});
  if(p==='/api/v1/research-documents')return ok({items:[{id:'synthetic-plan',head_revision_title:'合成已发布预案',head_revision_number:2,head_revision_id:'synthetic-v2',source_trade_day:days[0],target_trade_day:days[1]}],next_cursor:null});
  if(p.endsWith('/synthetic-plan/revisions'))return ok({items:[{id:'synthetic-v1',title:'合成预案原件',revision_number:1,publication_status:'published'}],next_cursor:null});
  if(p.includes('synthetic-v1')&&!p.includes('/validations'))return ok({id:'synthetic-v1',revision_number:1,published_at:'2026-10-22T18:00:00+08:00',scenarios:[{id:'synthetic-scenario',label:'合成情景',trigger_text:'观察条件成立后由本人确认',invalidation_text:'证据不足时不判断',evidence_refs:[ref]}]});
  if(p==='/api/v1/validations')return ok({items:[{id:'synthetic-check1',scenario_id:'synthetic-scenario',outcome:'supported',superseded_by_id:'synthetic-check2'},{id:'synthetic-check2',scenario_id:'synthetic-scenario',outcome:'insufficient',superseded_by_id:null}],next_cursor:null});
  return ok({items:[],records:[],next_cursor:null});
 }
 let file=resolve(root,decodeURIComponent(p).slice(1));if(!file.startsWith(root+sep)&&file!==root){res.writeHead(403);return res.end();}
 if(!extname(file))file=resolve(root,'index.html');
 let body=await readFile(file);
 if(extname(file)==='.html')body=Buffer.from(body.toString().replace('<body>','<body><div style="background:#302613;color:#ffe0a0;padding:8px 16px;font:14px sans-serif;position:relative;z-index:10000">'+(stored?'本机只读预览 · 原8088未改动 · 新增聚合查询未发布':'交互演示 · 全部为合成数据 · 原8088未改动')+' <a style="color:#fff" href="http://127.0.0.1:'+(stored?'18099':'18098')+'/workbench">切换'+(stored?'合成交互演示':'已存数据只读预览')+'</a></div>'));
 res.writeHead(200,{'Content-Type':{'.html':'text/html; charset=utf-8','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png'}[extname(file)]||'application/octet-stream','Cache-Control':'no-store'});res.end(body);
}catch{res.writeHead(503,{'Content-Type':'text/plain; charset=utf-8'});res.end('本机预览读取失败，正式服务未改动。');}});
server.listen(port,'127.0.0.1',()=>console.log(`Local ${stored?'read-only stored data':'synthetic'} preview: http://127.0.0.1:${port}`));
