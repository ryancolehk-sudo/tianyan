import { test, expect } from '@playwright/test';
import { terminalFixture } from './terminalTestFixtures';

for (const replay of [false,true]) test(`focus interests respect live versus replay (${replay})`,async({page})=>{
  const today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());
  const interests:Array<{panel:string;codes:string[];trade_day:string}>=[];
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/v1/**',async route=>{
    const url=new URL(route.request().url());
    const ok=(data:unknown)=>route.fulfill({json:{data,meta:{app_mode:'real',quality:{status:'partial',reasons:[]}}}});
    if(url.pathname==='/api/v1/session')return ok({user:{id:'focus-synthetic-owner',username:'synthetic',role:'admin'},csrf_token:'synthetic-csrf'});
    if(url.pathname==='/api/v1/workbench-view'){
      const data=terminalFixture(url.searchParams);
      data.trade_day=today;data.available_days=[today];data.selected_stock_code='000001.SZ';
      data.stocks=Array.from({length:65},(_,index)=>({...data.stocks[0],code:String(index+1).padStart(6,'0')+'.SZ',name:`合成关注${index+1}`,pct_change:65-index}));
      data.capabilities.push({key:'focused_quotes',label:'关注报价',status:'available',reason:null,source_family:'tencent_web',last_received_at:null});
      return ok(data);
    }
    if(url.pathname==='/api/v1/quote-focus'){
      expect(route.request().method()).toBe('PUT');
      expect(route.request().headers()['x-csrf-token']).toBe('synthetic-csrf');
      interests.push(route.request().postDataJSON());
      return ok({active:true,accepted_count:interests.at(-1)!.codes.length,expires_at:new Date(Date.now()+90_000).toISOString()});
    }
    if(url.pathname==='/api/v1/ai/settings')return ok({enabled:false,features:[]});
    return ok({items:[],rows:[],history:[],points:[],next_cursor:null});
  });
  await page.goto('/workbench?view=overview&trade_day='+today+(replay?'&as_of_time='+today+'T10:00:00%2B08:00':''));
  const table=page.getByRole('table',{name:'个股涨幅榜',exact:true});
  await table.scrollIntoViewIfNeeded();await expect(table.locator('tbody tr')).toHaveCount(50);
  if(replay){
    await page.waitForTimeout(500);
    expect(interests).toEqual([]);
  } else {
    await expect.poll(()=>interests.some(item=>item.panel==='selected'&&item.codes[0]==='000001.SZ')).toBe(true);
    await expect.poll(()=>interests.some(item=>item.panel==='ranking'&&item.codes.length===50)).toBe(true);
    await page.getByRole('button',{name:'个股涨幅榜下一页',exact:true}).click();
    await expect.poll(()=>interests.some(item=>item.panel==='ranking'&&item.codes[0]==='000051.SZ')).toBe(true);
    expect(interests.every(item=>item.codes.length<=50&&item.trade_day===today)).toBe(true);
  }
  expect(errors).toEqual([]);
});
