import {test,expect} from '@playwright/test';
import {terminalFixture,terminalDay} from './terminalTestFixtures';

test('whole market sorts both directions, filters real THS concepts and retains missing speeds',async({page})=>{
  await page.setViewportSize({width:1920,height:900});
  let nextBatch = false;
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/v1/**',async route=>{
    const url=new URL(route.request().url());
    const ok=(data:unknown)=>route.fulfill({json:{data,meta:{app_mode:'real',quality:{status:'partial',reasons:[]}}}});
    if(url.pathname==='/api/v1/session')return ok({user:{id:'ranking-fixture',username:'synthetic',role:'admin'},csrf_token:'synthetic'});
    if(url.pathname==='/api/v1/workbench-view'){
      const data=terminalFixture(url.searchParams);
      data.stocks=Array.from({length:5001},(_,i)=>({...data.stocks[0],code:String(100000+i)+'.'+(i%2?'SZ':'SH'),name:`股票${i}`,
        pct_change:i===5000?null:nextBatch&&i===4998?30:(i-2500)/100,speed_5m:i===5000?null:(i-2500)/1000,amount_cny:i===5000?null:i*1e8,
        theme:'融资融券',ths_themes:i===4200?['芯片','机器人']:['合成题材'],previous_height:null}));
      return ok(data);
    }
    if(url.pathname==='/api/v1/ai/settings')return ok({enabled:false,features:[]});
    return ok({items:[],rows:[],history:[],points:[],next_cursor:null});
  });
  await page.goto('/workbench?view=overview&trade_day='+terminalDay);
  const table=page.getByRole('table',{name:'个股涨幅榜',exact:true});
  await expect(table.locator('tbody tr')).toHaveCount(50);
  await expect(page.getByLabel('5001只股票，第1页，共101页').first()).toBeVisible();
  await expect(table.locator('tbody tr').first()).toContainText('股票4999');
  await table.getByRole('button',{name:'涨幅',exact:true}).click();
  await expect(table.locator('th').nth(3)).toHaveAttribute('aria-sort','ascending');
  await expect(table.locator('tbody tr').first()).toContainText('股票0');
  await page.getByRole('button',{name:'个股涨幅榜下一页',exact:true}).click();
  await expect(table.locator('tbody tr').first()).toContainText('股票50');
  await page.getByLabel('个股涨幅榜搜索', {exact:true}).fill('机器人');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expect(table.locator('tbody')).toContainText('股票4200');
  await expect(table.locator('tbody')).not.toContainText('融资融券');
  await page.getByLabel('个股涨幅榜搜索',{exact:true}).fill('');
  await page.getByLabel('个股涨幅榜范围',{exact:true}).selectOption('SZ');
  await expect(table.locator('tbody tr').first()).toContainText('股票1');
  await table.getByRole('button',{name:'成交额',exact:true}).click();
  await expect(table.locator('tbody tr').first()).toContainText('股票4999');
  await table.getByRole('button',{name:'成交额',exact:true}).click();
  await expect(table.locator('tbody tr').first()).toContainText('股票1');
  await expect(page.getByRole('heading',{name:'个股涨速榜',exact:true})).toHaveCount(0);
  await page.getByLabel('个股涨幅榜范围',{exact:true}).selectOption('all');
  await table.getByRole('button',{name:'5分涨速',exact:true}).click();
  await expect(table.getByRole('columnheader',{name:'5分涨速',exact:true})).toHaveAttribute('aria-sort','descending');
  await expect(table.locator('tbody tr').first()).toContainText('股票4999');
  await table.getByRole('button',{name:'5分涨速',exact:true}).click();
  await expect(table.getByRole('columnheader',{name:'5分涨速',exact:true})).toHaveAttribute('aria-sort','ascending');
  await expect(table.locator('tbody tr').first()).toContainText('股票0');
  await page.getByLabel('个股涨幅榜搜索',{exact:true}).fill('105000');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expect(table.locator('tbody tr td').nth(2)).toHaveText('—');
  await page.reload();
  await expect(page.getByLabel('个股涨幅榜搜索',{exact:true})).toHaveValue('105000');
  await expect(table.getByRole('columnheader',{name:'5分涨速',exact:true})).toHaveAttribute('aria-sort','ascending');
  await page.goto('/workbench?view=overview');
  await page.getByLabel('个股涨幅榜搜索',{exact:true}).fill('');
  const gainSort = table.getByRole('button',{name:'涨幅',exact:true});
  await gainSort.click();
  await expect(table.locator('tbody tr').first()).toContainText('股票4999');
  nextBatch = true;
  await page.evaluate(()=>window.dispatchEvent(new Event('online')));
  await expect(table.locator('tbody tr').first()).toContainText('股票4998');
  await expect(table.locator('th').nth(3)).toHaveAttribute('aria-sort','descending');
  expect(errors).toEqual([]);
});
