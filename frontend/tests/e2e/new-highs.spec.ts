import {expect,test,type Page} from '@playwright/test';
import {fixture} from './rotationTestFixtures';
import {meta} from './reviewTestFixtures';

function sample(){
 const trade_days=['2026-10-16','2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23'];
 const rows=Array.from({length:34},(_,i)=>({ts_code:`${600001+i}.SH`,name:`合成新高${i+1}`,themes:'合成题材 / 合成第二题材',group_name:i<15?'合成医药':i<29?'合成银行':'合成其他',is_new:i%3===0,membership_day:'2026-10-23',quote_day:'2026-10-23',price:String(10+i),pct_chg:String(12-i/2),amount:String((i+1)*1e8),main_net:String((15-i)*1e7),actual_float:String((i+1)*1e9),actual_turnover:String(i+1)}));
 const groups=[{name:'合成医药',count:15,new_count:5,members:['合成医药']},{name:'合成银行',count:14,new_count:5,members:['合成银行']},{name:'合成其他',count:5,new_count:2,members:['合成其他']}];
 return{version:'new-highs-v1',state:'complete',trade_day:'2026-10-23',start_day:'2026-10-23',trade_days,rows,groups,total:34,new_count:12,loaded_count:34,missing_days:[] as string[],coverage:[],records:trade_days.map((trade_day,i)=>({trade_day,total:20+i*3,new_count:i+4,group_counts:{'合成医药':{count:10+i,new_count:2},'合成银行':{count:8+i,new_count:2},'合成其他':{count:2,new_count:i}}}))};
}
async function setup(page:Page){
 const domNestingErrors:string[]=[];
 page.on('console',message=>{if(/validateDOMNesting|In HTML,|cannot contain a nested|cannot be a (?:child|descendant)|hydration error/i.test(message.text()))domNestingErrors.push(message.text());});
 const state=await fixture(page);const data=sample();let malformed=false;
 await page.route('**/api/v1/review-data/new-highs?**',route=>{const q=new URL(route.request().url()).searchParams;return route.fulfill({json:{meta,data:malformed?{}:{...data,trade_day:q.get('trade_day')||data.trade_day,start_day:q.get('start_day')||data.trade_day}}});});
 return{state,data,domNestingErrors,setMalformed:(value:boolean)=>malformed=value};
}
async function expectEmptySemantics(page:Page,domNestingErrors:string[]){
 const status=page.locator('.nh-list-empty');await expect(status).toBeVisible();await expect(status).toHaveAttribute('role','status');
 // The state contains paragraphs and actions, so no paragraph may contain it.
 // This remains meaningful in production, where React omits nesting warnings.
 expect(await status.evaluate(node=>node.closest('p')!==null)).toBe(false);
 expect(await status.locator('p').evaluateAll(nodes=>nodes.some(node=>node.querySelector('p,div,section,article,ul,ol,table')))).toBe(false);
 expect(domNestingErrors).toEqual([]);
}
for(const [width,height] of [[2880,1547],[1920,1080],[1440,900],[1280,900],[390,844]])test(`hundred-day highs complete reference layout and no horizontal scrolling ${width}`,async({page})=>{
 const {state}=await setup(page);await page.setViewportSize({width,height});await page.goto('/research/reviews?view=new-highs&trade_day=2026-10-23');
 await expect(page.locator('.nh-donut')).toBeVisible();await expect(page.locator('.nh-trend .pcv-chart')).toBeVisible();await expect(page.locator('.nh-group')).toHaveCount(3);await expect(page.locator('.nh-table tbody tr')).toHaveCount(34);
 expect(await page.evaluate(()=>document.body.scrollWidth)).toBeLessThanOrEqual(width);
 for(const overflow of await page.locator('.nh-table-scroll,.nh-results-scroll').evaluateAll(elements=>elements.map(el=>el.scrollWidth-el.clientWidth)))expect(overflow).toBeLessThanOrEqual(1);
 const geometry=await page.evaluate(()=>{
  const chart=document.querySelector('.nh-charts')!.getBoundingClientRect(),svg=document.querySelector('.nh-trend svg')!.getBoundingClientRect();
  const groups=[...document.querySelectorAll('.nh-group')].map(el=>el.getBoundingClientRect());
  const row=document.querySelector('.nh-table tbody tr')!,cells=[...row.querySelectorAll('td')].map(el=>el.getBoundingClientRect()),name=row.querySelector('strong')!.getBoundingClientRect();
  return{chartBottom:chart.bottom,svgBottom:svg.bottom,dataSize:getComputedStyle(row.querySelector('td:nth-child(2)')!).fontSize,headSize:getComputedStyle(document.querySelector('.nh-table th')!).fontSize,overlappingGroups:groups.some((a,i)=>groups.some((b,j)=>j>i&&Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)),nameWidth:name.width,rowWidth:row.getBoundingClientRect().width,cells:cells.map(el=>({width:el.width,left:el.left,right:el.right}))};
 });
 expect(geometry.svgBottom).toBeLessThanOrEqual(geometry.chartBottom+1);expect(geometry.overlappingGroups).toBe(false);expect(geometry.dataSize).toBe('13px');expect(geometry.headSize).toBe('12px');
 if(width<600){expect(geometry.nameWidth).toBeGreaterThan(150);expect(geometry.cells[0].width).toBeGreaterThan(geometry.rowWidth*.9);for(const cell of geometry.cells.slice(1,7))expect(cell.width).toBeGreaterThan(110);}
 await page.getByRole('button',{name:'按个股',exact:true}).click();await expect(page.locator('.nh-table tbody tr')).toHaveCount(34);await expect(page.locator('.nh-table thead')).toContainText('实际流通');
 if(width<600){await page.getByLabel('新高排序字段').selectOption('main_net');}else{await page.locator('.nh-table').getByRole('button',{name:'主力净额按降序',exact:true}).click();}await expect(page.locator('.nh-table tbody tr').first()).toContainText('合成新高1');if(width<600){await page.getByRole('button',{name:'新高切换为升序',exact:true}).click();}else{await page.getByRole('button',{name:'主力净额按升序',exact:true}).click();}await expect(page.locator('.nh-table tbody tr').first()).toContainText('合成新高34');
 await page.locator('.nh-table tbody tr').first().locator('td').nth(3).click();expect(new URL(page.url()).searchParams.get('stock')).toBe('600034.SH');expect(new URL(page.url()).searchParams.get('view')).toBe('new-highs');
 await page.getByRole('button',{name:'合成医药 15',exact:true}).click();await expect(page.locator('.nh-table tbody tr')).toHaveCount(15);await expect(page.locator('.nh-trend h3')).toContainText('合成医药');await page.getByRole('button',{name:'全部板块',exact:true}).click();
 await page.getByRole('button',{name:'多日',exact:true}).click();await expect(page.getByLabel('新高开始日期')).toHaveValue('2026-10-16');await expect(page.locator('.nh-summary')).toContainText('期间新增');await page.getByRole('button',{name:'单日',exact:true}).click();await expect(page.getByLabel('新高日期')).toBeVisible();
 expect(state.errors).toEqual([]);expect(state.writes).toEqual([]);
});

test('distribution labels every board without overlap or clipping and keeps selection linked',async({page})=>{
 const {data}=await setup(page);data.groups=Array.from({length:24},(_,i)=>({name:`合成板块${i+1}`,count:3,new_count:1,members:[`合成板块${i+1}`]}));data.total=72;const template=data.rows[0];data.rows=data.groups.flatMap((g,i)=>Array.from({length:3},(_,j)=>({...template,ts_code:`${620001+i*3+j}.SH`,group_name:g.name})));data.loaded_count=72;data.new_count=24;
 await page.setViewportSize({width:1280,height:900});await page.goto('/research/reviews?view=new-highs&trade_day=2026-10-23');await expect(page.locator('.nh-tags button')).toHaveCount(25);await expect(page.locator('.nh-donut path[role=button]')).toHaveCount(24);
 await expect(page.locator('.nh-donut-label text')).toHaveCount(24);
 const layout=await page.locator('.nh-donut').evaluate(svg=>{
  const box=svg.getBoundingClientRect(),labels=[...svg.querySelectorAll('.nh-donut-label text')].map(e=>e.getBoundingClientRect());
  return{outside:labels.filter(r=>r.left<box.left-.5||r.right>box.right+.5||r.top<box.top-.5||r.bottom>box.bottom+.5).length,overlap:labels.some((a,i)=>labels.some((b,j)=>j>i&&Math.min(a.right,b.right)>Math.max(a.left,b.left)&&Math.min(a.bottom,b.bottom)>Math.max(a.top,b.top)+.5)),font:getComputedStyle(svg.querySelector('.nh-donut-label text')!).fontSize};
 });
 expect(layout).toEqual({outside:0,overlap:false,font:'12px'});
 await expect(page.locator('.nh-tags').getByRole('button',{name:'合成板块24 3',exact:true})).toBeVisible();
 await page.locator('.nh-donut-label text').filter({hasText:/^合成板块24 3$/}).click();await expect(page.locator('.nh-table tbody tr')).toHaveCount(3);await expect(page.locator('.nh-trend h3')).toContainText('合成板块24');
});

test('all boards share one list and the final stock is reachable without nested scrolling',async({page})=>{
 const {data}=await setup(page),template=data.rows[0];
 data.groups=Array.from({length:24},(_,i)=>({name:`合成板块${i+1}`,count:i===0?23:i===23?45:3,new_count:0,members:[`合成板块${i+1}`]}));
 data.rows=[];
 for(const group of data.groups)for(let i=0;i<group.count;i++){const id=data.rows.length+1;data.rows.push({...template,ts_code:`${610000+id}.SH`,name:`合成个股${id}`,group_name:group.name,is_new:false});}
 data.total=data.loaded_count=data.rows.length;data.new_count=0;
 await page.setViewportSize({width:1280,height:720});await page.goto('/research/reviews?view=new-highs&trade_day=2026-10-23');
 await expect(page.locator('.nh-group')).toHaveCount(24);await expect(page.locator('.nh-table tbody tr')).toHaveCount(data.total);
 const pane=page.getByRole('region',{name:'新高股票完整名单',exact:true});
 const root=await page.locator('.nh-root').boundingBox();expect(root!.y+root!.height).toBeLessThanOrEqual(720);
 expect(await page.locator('.nh-group .nh-table-scroll').evaluateAll(elements=>elements.filter(e=>e.scrollHeight>e.clientHeight+1).length)).toBe(0);
 await page.locator('.nh-table tbody tr').first().hover();await page.mouse.wheel(0,800);
 await expect.poll(()=>pane.evaluate(e=>e.scrollTop)).toBeGreaterThan(0);
 await pane.focus();await page.keyboard.press('End');await expect(page.locator('.nh-group').last().locator('tbody tr').last()).toBeInViewport();
 await page.locator('.nh-tags').getByRole('button',{name:'合成板块24 45',exact:true}).click();await expect(page.locator('.nh-group')).toHaveCount(1);await expect.poll(()=>pane.evaluate(e=>e.scrollTop)).toBe(0);
 await page.getByRole('button',{name:'全部板块',exact:true}).click();await expect(page.locator('.nh-group')).toHaveCount(24);await expect.poll(()=>pane.evaluate(e=>e.scrollTop)).toBe(0);
});

test('filters, missing fields, keyboard selection, cutoff and malformed retry',async({page})=>{
 const {data,setMalformed}=await setup(page);data.rows[0].main_net=null as unknown as string;
 await page.goto('/research/reviews?view=new-highs&trade_day=2026-10-23&as_of_time=2026-10-26T20%3A00%3A00%2B08%3A00');
 await page.getByRole('button',{name:'按个股',exact:true}).click();await page.getByRole('button',{name:'选择合成新高1',exact:true}).focus();await page.keyboard.press('Enter');expect(new URL(page.url()).searchParams.get('stock')).toBe('600001.SH');
 await page.locator('.nh-filter summary').click();await page.getByRole('checkbox',{name:'只看新增',exact:true}).check();await expect(page.locator('.nh-table tbody tr')).toHaveCount(12);await page.getByRole('button',{name:'重置筛选',exact:true}).click();await expect(page.locator('.nh-table tbody tr')).toHaveCount(34);
 setMalformed(true);await page.getByRole('button',{name:'重读缓存',exact:true}).click();await expect(page.getByRole('alert').filter({hasText:'百日新高响应不完整'})).toBeVisible();setMalformed(false);await page.getByRole('button',{name:'重试',exact:true}).click();await expect(page.locator('.nh-donut')).toBeVisible();
 data.rows=[];data.groups=[];data.total=null as unknown as number;data.new_count=null as unknown as number;data.state='unavailable';data.missing_days=['2026-10-23'] as never;await page.getByRole('button',{name:'重读缓存',exact:true}).click();await expect(page.locator('.nh-missing')).toContainText('完整名单尚未入库');await expect(page.locator('.nh-summary')).toContainText('—');
});

test('missing list stays unknown while the independent trend and source date remain visible',async({page},info)=>{
 const {state,data,domNestingErrors}=await setup(page);const history=structuredClone(data.records);data.state='unavailable';data.rows=[];data.groups=[];data.loaded_count=0;data.total=null as unknown as number;data.new_count=null as unknown as number;data.missing_days=['2026-10-23'];
 await page.goto('/research/reviews?view=new-highs&trade_day=2026-10-23&as_of_time=2026-10-26T20%3A00%3A00%2B08%3A00');
 await expect(page.locator('.nh-root')).toHaveAttribute('data-list-state','incomplete');await expect(page.getByLabel('新高名单计数',{exact:true})).toHaveText('完整名单未就绪 · 数量待核');
 await expect(page.locator('.nh-summary')).toContainText('—');await expect(page.locator('.nh-summary')).not.toContainText('0');await expect(page.locator('.nh-total')).toHaveText('—');
 await expect(page.getByText('完整名单尚未就绪',{exact:true})).toBeVisible();await expect(page.getByText('没有符合筛选条件的股票',{exact:true})).toHaveCount(0);await expectEmptySemantics(page,domNestingErrors);
 await expect(page.locator('.nh-trend .pcv-chart')).toBeVisible();expect(data.records).toEqual(history);await expect(page.getByLabel('新高日期',{exact:true})).toHaveValue('2026-10-23');
 await page.getByRole('button',{name:'按个股',exact:true}).click();await expect(page.getByText('完整名单尚未就绪',{exact:true})).toBeVisible();await expectEmptySemantics(page,domNestingErrors);
 await page.screenshot({path:info.outputPath('synthetic-new-highs-missing-list.png'),fullPage:true});expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('a complete zero list is distinct from filter misses and resetting keeps the complete source count',async({page},info)=>{
 const {state,data,domNestingErrors}=await setup(page);await page.goto('/research/reviews?view=new-highs&trade_day=2026-10-23');
 await page.locator('.nh-filter summary').click();await page.getByRole('spinbutton',{name:'最低价格',exact:true}).fill('9999');
 await expect(page.locator('.nh-root')).toHaveAttribute('data-list-state','no-match');await expect(page.getByLabel('新高名单计数',{exact:true})).toHaveText('筛选后 0 只 / 全部 34 只');
 await expect(page.locator('.nh-summary')).toContainText('34');await expect(page.getByText('没有符合筛选条件的股票',{exact:true})).toBeVisible();await expect(page.locator('.nh-missing')).toHaveCount(0);await expectEmptySemantics(page,domNestingErrors);
 await page.getByRole('button',{name:'清除筛选',exact:true}).click();await expect(page.getByLabel('新高名单计数',{exact:true})).toHaveText('共 34 只');await expect(page.locator('.nh-table tbody tr')).toHaveCount(34);
 data.rows=[];data.groups=[];data.total=0;data.new_count=0;data.loaded_count=0;await page.getByRole('button',{name:'重读缓存',exact:true}).click();
 await expect(page.locator('.nh-root')).toHaveAttribute('data-list-state','empty');await expect(page.getByText('完整名单已核对，本范围没有新高股票',{exact:true})).toBeVisible();
 await expect(page.getByLabel('新高名单计数',{exact:true})).toHaveText('共 0 只');await expect(page.locator('.nh-total')).toHaveText('0');await expect(page.locator('.nh-missing')).toHaveCount(0);await expectEmptySemantics(page,domNestingErrors);
 await page.screenshot({path:info.outputPath('synthetic-new-highs-complete-zero.png'),fullPage:true});expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('partial multi-day lists disclose loaded scope without turning a subset filter miss into a final zero',async({page})=>{
 const {state,data}=await setup(page);data.state='partial';data.missing_days=['2026-10-22'];data.rows=data.rows.slice(0,3);data.loaded_count=3;
 await page.setViewportSize({width:390,height:844});await page.goto('/research/reviews?view=new-highs&trade_day=2026-10-23&nh_start=2026-10-16');
 await expect(page.getByLabel('新高名单计数',{exact:true})).toHaveText('已载入 3 只 · 完整数量待核');await expect(page.locator('.nh-summary')).toContainText('—');
 await page.locator('.nh-filter summary').click();await page.getByRole('spinbutton',{name:'最低价格',exact:true}).fill('9999');
 await expect(page.getByLabel('新高名单计数',{exact:true})).toHaveText('已载入 3 只 · 完整数量待核');await expect(page.getByText('已载入范围中暂无匹配，完整名单的最终数量仍待核对。',{exact:true})).toBeVisible();
 await page.getByRole('button',{name:'清除筛选',exact:true}).click();await expect(page.locator('.nh-table tbody tr')).toHaveCount(3);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
 expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});
