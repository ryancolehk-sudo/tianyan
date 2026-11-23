import {test,expect} from '@playwright/test';
import {terminalFixture,terminalDay} from './terminalTestFixtures';

const stamp=terminalDay+'T13:05:00+08:00';
function snapshot(category:string, day=terminalDay){
 const concept=category==='concept';
 const rows=Array.from({length:12},(_,i)=>({code:String((concept?300000:881000)+i),name:(concept?'合成概念':'合成行业')+i,
  net_cny:i===11?null:String((10-i)*100000000),pct_change:'1',company_count:'12',received_at:stamp}));
 return {sampled_day:day,status:'available',source_time:null,received_at:stamp,complete:!concept,
  missing_pages:concept?[7]:[],rejected_rows:concept?4:0,last_error:concept?'incomplete_page':null,rows,
  history:['13:00','13:05','13:20'].map((t,j)=>({received_at:terminalDay+'T'+t+':00+08:00',points:rows.map(r=>({code:r.code,net_cny:r.net_cny==null?null:String(Number(r.net_cny)+j*1000000),received_at:terminalDay+'T'+t+':00+08:00'}))}))};
}
async function setup(page:any, state:{status?:number}={}){
 const requests:string[]=[];
 await page.route('**/api/v1/**',async(route:any)=>{
  const u=new URL(route.request().url());requests.push(u.pathname+u.search);
  const ok=(data:unknown)=>route.fulfill({json:{data,meta:{request_id:'synthetic',app_mode:'real',generated_at:stamp,as_of_time:null,quality:{status:'partial',reasons:['synthetic only']}}}});
  if(u.pathname==='/api/v1/session')return ok({user:{id:'ths-fixture',username:'synthetic',role:'admin'},csrf_token:'synthetic'});
  if(u.pathname==='/api/v1/workbench-view')return ok(terminalFixture(u.searchParams));
  if(u.pathname==='/api/v1/ths-funds'){
   if(state.status)return route.fulfill({status:state.status,json:{}});
   const day=u.searchParams.get('sampled_day')||terminalDay;
   const data=snapshot(u.searchParams.get('category')||'industry',day);
   if(day!==terminalDay||u.searchParams.has('as_of_time')){data.rows=[];data.history=[];data.status='missing';}
   return ok(data);
  }
  if(u.pathname==='/api/v1/ai/settings')return ok({enabled:false,features:[]});
  return ok({items:[],next_cursor:null});
 });
 return requests;
}

test('original funds layout uses THS, retains comparison and keeps missing fields honest',async({page})=>{
 const requests=await setup(page);const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('/workbench?view=funds&trade_day='+terminalDay);
 await page.getByRole('button',{name:'资金净额',exact:true}).click();
 await expect(page.getByRole('group',{name:'资金数据来源'})).toHaveCount(0);
 await expect(page.getByRole('heading',{name:'板块资金脉络',exact:true})).toBeVisible();
 await expect(page.getByRole('heading',{name:'板块强度',exact:true})).toBeVisible();
 await expect(page.getByRole('heading',{name:'盘中同步排名',exact:true})).toBeVisible();
 await expect(page.getByRole('heading',{name:'资金与表现对照',exact:true})).toBeVisible();
 await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(10);
 await expect(page.locator('.ty-funds-chart g[data-sector-code]')).toHaveCount(10);
 await expect(page.locator('.ty-members-panel tbody tr')).toHaveCount(0);
 await expect(page.getByText('来源未提供行情日期和时刻',{exact:false})).toBeVisible();
 const line=await page.locator('.ty-funds-chart g[data-sector-code]').first().locator('path').getAttribute('d');
 expect(line?.match(/M/g)).toHaveLength(2); // 15-minute missing interval remains a gap.
 expect(line).toContain('L');
 await page.getByLabel('筛选强度板块').fill('合成行业11');
 await expect(page.locator('.ty-strength-row')).toHaveCount(1);
 await page.getByRole('checkbox',{name:'对比合成行业11资金',exact:true}).check();
 await expect(page.locator('.ty-funds-chart g[data-sector-code]')).toHaveCount(0); // null is not zero
 await expect(page.locator('.ty-fund-legend')).toContainText('缺采样');
 await page.getByLabel('筛选强度板块').fill('');
 await page.getByRole('button',{name:'恢复资金前十',exact:true}).click();
 await expect(page.locator('.ty-funds-chart g[data-sector-code]')).toHaveCount(11); // selected outside top ten
 await page.locator('.ty-funds-ranking').getByRole('button',{name:'合成行业1',exact:true}).click();
 await expect(page.locator('.ty-members-panel h2')).toContainText('合成行业1');
 await expect(page.locator('.ty-funds-chart g[data-selected=true]')).toHaveAttribute('data-sector-code','THS:industry:881001');
 await page.getByRole('button',{name:'概念板块',exact:true}).click();
 await expect(page.getByText('缺第 7 页',{exact:false})).toBeVisible();
 await expect(page.locator('.ty-funds-ranking tbody')).toContainText('合成概念0');
 await expect(page).toHaveURL(/fund_sector=THS%3Aconcept%3A/);
 await page.reload();
 await expect(page.getByRole('button',{name:'概念板块',exact:true})).toHaveAttribute('aria-pressed','true');
 await page.getByRole('button',{name:'盘后 · 近10日',exact:true}).click();
 await expect(page.getByText('等待盘后资金历史',{exact:true})).toBeVisible();
 await expect(page.locator('.ty-funds-compare tbody tr')).toHaveCount(0);
 await page.getByRole('button',{name:'盘中 · 当日',exact:true}).click();
 await page.screenshot({path:'test-results/ths-original-layout-1440.png',fullPage:true});
 await page.setViewportSize({width:800,height:900});
 await page.screenshot({path:'test-results/ths-original-layout-800.png',fullPage:true});
 expect(requests.some(r=>r.includes('fund_compare=THS'))).toBe(false);
 expect(errors).toEqual([]);
});

test('refresh failure retains samples, permission denial clears them and cutoff stays isolated',async({page})=>{
 await page.clock.install({time:new Date(stamp)});
 const state:{status?:number}={};const requests=await setup(page,state);
 await page.goto('/workbench?view=funds&trade_day='+terminalDay);
 await page.getByRole('button',{name:'资金净额',exact:true}).click();
 await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(10);
 state.status=503;
 await page.clock.runFor(31_000);
 await expect(page.getByText('同花顺资金读取失败，保留本范围上次记录',{exact:false})).toBeVisible();
 await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(10);
 state.status=403;
 await page.clock.runFor(31_000);
 await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(0);
 state.status=undefined;
 await page.goto('/workbench?view=funds&trade_day=2001-12-28&as_of_time=2001-12-28T10%3A00%3A00%2B08%3A00');
 await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(0);
 await expect.poll(()=>requests.some(r=>r.includes('/ths-funds?')&&r.includes('sampled_day=2001-12-28')&&r.includes('as_of_time='))).toBe(true);
});

test('slow concept reads do not block industry and a late result cannot undo permission denial',async({page})=>{
 await page.clock.install({time:new Date(stamp)});
 await setup(page);
 const calls={industry:0,concept:0};let denied=false,conceptDone=false;
 let release=()=>{};const pending=new Promise<void>(resolve=>{release=resolve;});
 await page.route('**/api/v1/ths-funds?**',async route=>{
  const category=new URL(route.request().url()).searchParams.get('category')==='concept'?'concept':'industry';
  calls[category]++;
  if(category==='concept'){await pending;conceptDone=true;}
  if(category==='industry'&&denied)return route.fulfill({status:403,json:{}});
  return route.fulfill({json:{data:snapshot(category),meta:{app_mode:'real',quality:{status:'partial',reasons:['synthetic']}}}});
 });
 try{
  await page.goto('/workbench?view=funds&trade_day='+terminalDay);
  await page.getByRole('button',{name:'资金净额',exact:true}).click();
  await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(10);
  await expect.poll(()=>calls.concept).toBe(calls.industry);
  const initial={...calls};
  await page.clock.fastForward(15_001);
  await expect.poll(()=>calls.industry).toBe(initial.industry+1);expect(calls.concept).toBe(initial.concept);
  denied=true;
  await page.clock.fastForward(15_001);
  await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(0);
  release();await expect.poll(()=>conceptDone).toBe(true);
  await page.getByRole('button',{name:'概念板块',exact:true}).click();
  await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(0);
  await expect(page.getByText('合成概念0',{exact:true})).toHaveCount(0);
  const before={...calls};await page.clock.fastForward(60_001);
  expect(calls).toEqual(before);
 }finally{release();}
});

for(const cutoff of [false,true])test(`past fund context stops periodic reads (${cutoff})`,async({page})=>{
 await page.clock.install({time:new Date(stamp)});
 const requests=await setup(page);
 await page.goto('/workbench?view=funds&trade_day=2001-12-28'+(cutoff?'&as_of_time=2001-12-28T10%3A00%3A00%2B08%3A00':''));
 await expect.poll(()=>requests.filter(path=>path.startsWith('/api/v1/ths-funds?')).length).toBeGreaterThanOrEqual(2);
 await page.waitForLoadState('networkidle');
 const before=requests.filter(path=>path.startsWith('/api/v1/ths-funds?')).length;
 await page.clock.fastForward(90_001);
 expect(requests.filter(path=>path.startsWith('/api/v1/ths-funds?')).length).toBe(before);
});
