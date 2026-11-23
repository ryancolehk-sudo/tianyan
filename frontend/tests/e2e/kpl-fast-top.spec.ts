import {test,expect} from '@playwright/test';
import {terminalFixture} from './terminalTestFixtures';

test('independent top ten updates overview without replacing full-board coverage',async({page})=>{
  const day=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());
  const at=new Date(day+'T13:05:00+08:00');
  await page.clock.install({time:at});
  const rows=Array.from({length:24},(_,i)=>({code:String(801000+i),name:'合成快榜'+i,strength:String(1000-i),limit_up_count:null}));
  let indices=[5,6,7,8,9,10,11,12,13,14], stamp=at.toISOString(), enabled=true;
  let fullCalls=0,topCalls=0;
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/v1/**',async route=>{
    const u=new URL(route.request().url());
    const ok=(data:unknown)=>route.fulfill({json:{data,meta:{app_mode:'real',quality:{status:'partial',reasons:[]}}}});
    if(u.pathname==='/api/v1/session')return ok({user:{id:'fast-top-synthetic',username:'synthetic',role:'admin'},csrf_token:'synthetic'});
    if(u.pathname==='/api/v1/workbench-view')return ok({...terminalFixture(u.searchParams),trade_day:day,available_days:[day]});
    if(u.pathname==='/api/v1/kpl-strength'){
      if(u.searchParams.get('top_only')==='true'){
        topCalls++;
        const selected=indices.map((i,rank)=>({...rows[i],strength:String(2000-rank)}));
        return ok({state:enabled?'available':'disabled',coverage:'first_page',received_at:stamp,
          rows:enabled?selected:[],history:enabled?[{received_at:stamp,values:Object.fromEntries(selected.map(row=>[row.code,row.strength]))}]:[],truncated:false});
      }
      fullCalls++;
      return ok({state:'available',coverage:'complete_pages',total_count:24,received_at:day+'T13:04:00+08:00',history_truncated:false,rows,
        history:[{received_at:day+'T13:04:00+08:00',values:Object.fromEntries(rows.map(row=>[row.code,row.strength]))}]});
    }
    if(u.pathname==='/api/v1/ai/settings')return ok({enabled:false,features:[]});
    return ok({items:[],rows:[],history:[],points:[],next_cursor:null});
  });
  await page.goto('/workbench?view=overview&trade_day='+day);
  const plotted=()=>page.locator('[data-kpl-code]').evaluateAll(nodes=>nodes.map(node=>node.getAttribute('data-kpl-code')));
  const codes=(ids:number[])=>ids.map(i=>String(801000+i));
  await expect.poll(plotted).toEqual(codes([5,6,7,8,9]));
  await expect(page.locator('.ty-kpl-top-ten')).toContainText('前十接收');
  await expect(page.getByLabel('开盘啦强度降序排行').locator('label')).toHaveCount(24);
  await expect(page.getByLabel('开盘啦强度降序排行')).toContainText('合成快榜0（1,000）');
  const before=fullCalls;
  indices=[20,5,6,7,8,9,10,11,12,13];stamp=new Date(at.getTime()+5000).toISOString();
  await page.clock.fastForward(5_001);
  await expect.poll(plotted).toEqual(codes([20,5,6,7,8]));
  expect(topCalls).toBeGreaterThan(1);expect(fullCalls).toBe(before);
  enabled=false;
  await page.clock.fastForward(5_001);
  await expect.poll(plotted).toEqual(codes([0,1,2,3,4]));
  await expect(page.getByLabel('开盘啦强度降序排行').locator('label')).toHaveCount(24);
  expect(errors).toEqual([]);
});
