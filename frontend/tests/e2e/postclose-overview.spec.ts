import { expect, test } from '@playwright/test';
import { fixture } from './rotationTestFixtures';
import { meta, blank } from './reviewTestFixtures';

test('all same-day charts share pointer and keyboard selection and clear together',async({page})=>{
 const data=synthetic();await setup(page,data);await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');
 const plot=page.locator('.pcv-plot').first();await expect(page.locator('.pcv-chart')).toHaveCount(10);
 const bounds=await plot.boundingBox();await page.mouse.move(bounds!.x+bounds!.width*.45,bounds!.y+bounds!.height*.6);
 const day=await page.locator('.pcv-chart').first().getAttribute('data-focus-day');expect(day).toMatch(/^2026-10-/);
 await expect(page.locator(`.pcv-chart[data-focus-day="${day}"]`)).toHaveCount(10);
 await expect(page.locator('.pcv-tooltip>b')).toHaveText([day!]);
 await expect(page.locator('.pcv-linked-day')).toHaveText(Array(10).fill(day!.slice(5)));
 await plot.focus();await plot.press('ArrowLeft');const previous=await page.locator('.pcv-chart').first().getAttribute('data-focus-day');expect(previous).not.toBe(day);
 await expect(page.locator(`.pcv-chart[data-focus-day="${previous}"]`)).toHaveCount(10);
 await plot.press('Escape');await expect(page.locator('.pcv-chart[data-focus-day]')).toHaveCount(0);
 await page.mouse.move(10,10);await expect(page.locator('.pcv-tooltip')).toHaveCount(0);
});

for(const width of [1920,1280,390])test(`height entry has three smooth full-width curves and date interactions ${width}`,async({page})=>{
 const data=synthetic();await setup(page,data);await page.setViewportSize({width,height:900});
 await page.route('**/api/v1/review-data/heights?**',route=>route.fulfill({json:{meta,data:{...data,records:data.records.map((point,i)=>({...point,leaders:{height:[`合成领涨${i}`],second_height:[],gem_height:[]},values:{height:i===6?null:2+i%7,second_height:1+i%4,gem_height:i%3}}))}}}));
 await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');await page.getByRole('button',{name:'连板高度图',exact:true}).click();
 await expect(page.getByRole('region',{name:'连板高度图'})).toBeVisible();await expect(page.locator('[data-height-series]')).toHaveCount(3);
 const chartExtent=await page.locator('.rhh-plot').evaluate(element=>{const bounds=element.getBoundingClientRect(),svg=element.querySelector('svg')!.getBoundingClientRect();return{height:bounds.height,svgHeight:svg.height,svgBottom:svg.bottom,parentBottom:bounds.bottom,documentBottom:document.documentElement.scrollHeight,zero:[...element.querySelectorAll('svg text')].find(el=>el.textContent==='0')?.getBoundingClientRect().bottom};});
 expect(chartExtent.height).toBeGreaterThanOrEqual(300);expect(Math.abs(chartExtent.svgHeight-chartExtent.height)).toBeLessThanOrEqual(1);expect(chartExtent.svgBottom).toBeLessThanOrEqual(chartExtent.parentBottom+1);expect(chartExtent.zero).toBeLessThanOrEqual(chartExtent.documentBottom);
 expect(await page.locator('[data-height-series="height"] path').getAttribute('d')).toContain('C');
 expect((await page.locator('[data-height-series="height"] path').getAttribute('d'))!.match(/M/g)).toHaveLength(2);
 await page.getByLabel('高度图跳转日期').fill('2026-10-12');await expect(page.locator('.rhh-tooltip>b')).toHaveText('2026-10-12');
 await expect(page.locator('.rhh-tooltip')).toContainText('合成领涨11');
 await page.getByRole('button',{name:'创业板高度',exact:true}).click();await expect(page.locator('[data-height-series]')).toHaveCount(2);
 expect(await page.evaluate(()=>document.body.scrollWidth)).toBeLessThanOrEqual(width);
 await page.getByRole('button',{name:'图表',exact:true}).click();await expect(page.locator('.pcv-chart')).toHaveCount(10);
});

const keys = ['emotion','height','second_height','gem_height','up_amount','broken_amount','market_amount','advancing','declining','consecutive','limit_up','limit_down','first_promotion','relay_promotion','seal_rate','auction_up','auction_seal','auction_down','main_net','yesterday_up','yesterday_relay','broken','broken_rate'];
function synthetic() {
  const days = Array.from({length:20},(_,i)=>`2026-10-${String(i+1).padStart(2,'0')}`);
  const stocks = Array.from({length:20},(_,i)=>({ts_code:`6000${String(i).padStart(2,'0')}.SH`,name:`合成验证${i+1}`,rank:i+1,pct_change:i===1?'0':String(10-i),amount_cny:String((20-i)*1e8),main_net_cny:String((10-i)*1e8),actual_turnover_pct:'12.5',actual_float_cny:'1230000000',interval_pct:String(50-i),interval_amount_cny:'5000000000',interval_turnover_pct:'234',first_time:'09:30:01',last_time:'14:30:02',board_label:'2天2板',open_times:'2',themes:['合成题材甲','合成题材乙']}));
  const rankings=Object.fromEntries(['amount','main_net','limit_up','broken','5','10','20','30'].map(k=>[k,{rows:stocks.slice(0,/^\d/.test(k)?20:10),state:'ready',ranking_field:k==='main_net'?'main_net_cny':/^\d/.test(k)?'interval_pct':'amount_cny',valid_count:20,sample_count:20,reason:null}]));
  return {version:'postclose-v1',state:'partial',trade_day:days.at(-1),trade_days:days,sample_count:20,rankings,records:days.map((day,i)=>({trade_day:day,state:'partial',leaders:{height:['合成领涨甲']},values:Object.fromEntries(keys.map((k,j)=>[k,k==='auction_down'?null:k==='emotion'?[0,34,83,100][i%4]:k==='main_net'?String((i-10)*1e8):/amount|seal$/.test(k)?String((i+1)*1e8):String((i+j)%50)]))})),metric_definitions:keys.map(key=>({key,label:key,unit:'只'})),scope:'合成隔离验证资料，不是真实市场数据',interval_basis:'合成测试口径',coverage:[],schedule:{time:'16:30',late_refresh:'17:30'}};
}

const board = (page: import('@playwright/test').Page, kind: string) => page.locator(`.pcv-board[data-board="${kind}"]`);
const setup = async (page: import('@playwright/test').Page, data = synthetic()) => {
 const state=await fixture(page);await page.route('**/api/v1/review-data/overview?**',route=>route.fulfill({json:{meta,data}}));return state;
};
for (const [width,height] of [[2880,1547],[1920,1080],[1440,900],[1280,900],[390,844]]) test(`split charts and complete vertical rankings ${width}`,async({page})=>{
 const state=await setup(page);await page.setViewportSize({width,height});await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');
 await expect(page.locator('.pcv-chart')).toHaveCount(10);await expect(page.locator('.pcv-board')).toHaveCount(8);
 expect(await page.locator('.pcv-board-table tbody tr').count()).toBe(120);
 const geometry=await page.evaluate(()=>({body:document.body.scrollWidth,viewport:innerWidth,height:innerHeight,frame:document.querySelector('.pcv-workspace')!.getBoundingClientRect().toJSON(),left:document.querySelector('.pcv-chart-pane')!.getBoundingClientRect().toJSON(),right:document.querySelector('.pcv-rankings')!.getBoundingClientRect().toJSON(),overflow:[...document.querySelectorAll('.pcv-board-table,.pcv-rankings-scroll')].map(el=>el.scrollWidth-el.clientWidth)}));
 expect(geometry.body).toBeLessThanOrEqual(width);for(const overflow of geometry.overflow)expect(overflow).toBeLessThanOrEqual(1);
 if(width>=1100){expect(geometry.frame.bottom).toBeLessThanOrEqual(height+1);expect(geometry.left.right).toBeLessThan(geometry.right.left);expect(Math.abs(geometry.left.top-geometry.right.top)).toBeLessThanOrEqual(1);}
 const initialY=await page.locator('.pcv-chart').first().evaluate(el=>el.getBoundingClientRect().y);
 await page.getByRole('button',{name:'30日涨幅',exact:true}).click();expect(await page.locator('.pcv-rankings-scroll').evaluate(el=>el.scrollTop)).toBeGreaterThan(100);
 if(width>=1100)expect(await page.locator('.pcv-chart').first().evaluate(el=>el.getBoundingClientRect().y)).toBe(initialY);
 await expect(board(page,'30')).toBeInViewport();await board(page,'30').getByRole('button',{name:'30日涨幅榜按区间涨幅升序',exact:true}).click();await expect(board(page,'30').locator('tbody tr').first()).toContainText('合成验证20');await board(page,'30').getByRole('button',{name:'30日涨幅榜按区间涨幅降序',exact:true}).click();await expect(board(page,'30').locator('tbody tr').first()).toContainText('合成验证1');
 await page.getByRole('button',{name:'统计分析',exact:true}).click();await expect(page.locator('.pcv-metric')).toHaveCount(23);await expect(page.locator('.pcv-donut')).toHaveCount(2);await expect(page.locator('.pcv-board')).toHaveCount(8);
 expect(await page.evaluate(()=>document.body.scrollWidth)).toBeLessThanOrEqual(width);
 await page.locator('[data-metric="height"]').click();await expect(page.locator('.pcv-chart')).toHaveCount(1);await expect(page.locator('.pcv-chart-detailed')).toHaveCount(1);await page.getByRole('button',{name:'全部指标',exact:true}).click();await expect(page.locator('.pcv-metric')).toHaveCount(23);
 expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('unavailable ranks and unknown metrics keep the actual gap',async({page})=>{
 const data=synthetic();data.rankings['30'].rows=[];data.rankings['30'].state='unavailable';data.rankings['30'].reason='interval_sessions_incomplete' as never;for(const p of data.records)p.values.auction_down=null;
 await setup(page,data);await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');await expect(board(page,'30').locator('.pcv-empty')).toContainText('尚未齐全');await expect(board(page,'30').locator('tbody tr')).toHaveCount(0);
 await page.getByRole('button',{name:'统计分析',exact:true}).click();await expect(page.locator('[data-metric="auction_down"]')).toContainText('—');await expect(page.locator('[data-metric="auction_down"]')).toContainText('尚无已保存数据');await expect(page.locator('[data-metric="auction_down"] svg')).toHaveCount(0);
});

test('automatic latest date stays unpinned and malformed data retries in place',async({page})=>{
 const state=await fixture(page);let bad=true;
 await page.route('**/api/v1/review-data/overview?**',route=>{expect(new URL(route.request().url()).searchParams.has('trade_day')).toBeFalsy();return route.fulfill({json:{meta,data:bad?{}:synthetic()}});});
 await page.goto('/research/reviews?view=overview');await expect(page.getByRole('alert').filter({hasText:'响应不完整'})).toBeVisible();bad=false;await page.getByRole('button',{name:'重试',exact:true}).click();await expect(page.locator('.pcv-chart')).toHaveCount(10);await expect(page.getByLabel('复盘日期')).toHaveValue('2026-10-20');expect(new URL(page.url()).searchParams.has('trade_day')).toBeFalsy();expect(state.writes).toEqual([]);
});

test('direction, expanded leaders, full field details and selection are preserved',async({page})=>{
 const data=synthetic();data.records.at(-1)!.values.main_net='-52497000000';const row=data.rankings['30'].rows[0] as Record<string,unknown>;
 row.themes=['合成题材甲','合成题材乙','完整题材名称可由键盘展开'];row.interval_turnover_pct=null;row.field_reasons={interval_turnover_pct:'截至2026-10-20的30个交易日：仅收到20/30日自由流通换手率，缺失不补零。'};
 const state=await setup(page,data);await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');await expect(page.locator('.pcv-chart')).toHaveCount(10);
 const net=page.locator('.pcv-chart').filter({has:page.getByRole('button',{name:'放大主力流入',exact:true})});await expect(net.locator('.pcv-legend b')).toHaveCSS('color','rgb(67, 209, 172)');await net.locator('.pcv-plot').focus();await page.keyboard.press('ArrowRight');await expect(net.locator('.pcv-tooltip strong')).toHaveCSS('color','rgb(67, 209, 172)');await page.keyboard.press('Escape');
 await page.getByRole('button',{name:'放大连板高度',exact:true}).click();await expect(page.locator('.pcv-leader-label').first()).toContainText('合成领涨甲');await page.getByRole('button',{name:'全部图表',exact:true}).click();await expect(page.locator('.pcv-chart')).toHaveCount(10);
 await board(page,'30').locator('.pcv-theme-detail').first().focus();await expect(page.getByRole('tooltip')).toContainText('完整题材名称可由键盘展开');await page.keyboard.press('Escape');await expect(page.getByRole('tooltip')).toHaveCount(0);
 await board(page,'30').locator('tbody tr').first().getByText('—',{exact:true}).first().focus();await expect(page.getByRole('tooltip')).toContainText('20/30日');await page.keyboard.press('Escape');
 await board(page,'amount').getByRole('button',{name:'合成验证1 600000.SH',exact:true}).click();expect(new URL(page.url()).pathname).toBe('/research/reviews');expect(new URL(page.url()).searchParams.get('stock')).toBe('600000.SH');expect(state.errors).toEqual([]);
});

test('all zero counts are shown as zero, and a clock series retains gaps',async({page})=>{
 const data=synthetic();for(const p of data.records){p.values.limit_up='0';p.values.consecutive='0';p.values.advancing='0';p.values.declining='0';}data.records[10].values.emotion=null;
 await setup(page,data);await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');await page.getByRole('button',{name:'统计分析',exact:true}).click();await expect(page.locator('.pcv-donut svg text').first()).toHaveText('0');await expect(page.locator('.pcv-donut svg text').last()).toHaveText('0');
 const path=await page.locator('[data-metric="emotion"] svg path').getAttribute('d');expect(path?.match(/M/g)?.length).toBe(2);await expect(page.locator('[data-metric="limit_up"]')).toContainText('0只');
});

test('research records remain editable through their existing entry',async({page})=>{
 const state=await setup(page);await page.route('**/api/v1/review-data/notes?**',route=>route.fulfill({json:{meta,data:{trade_day:'2026-10-20',content:blank(),head_id:null,writable:true,revision:null}}}));await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');await expect(page.locator('.pcv-board')).toHaveCount(8);
 await page.getByRole('button',{name:'研究记录',exact:true}).click();await expect(page.getByRole('heading',{name:/市场环境与情绪/})).toBeVisible();await expect(page.getByRole('button',{name:/编辑判断/})).toBeEnabled();await page.getByRole('button',{name:/编辑判断/}).click();await expect(page.getByRole('dialog')).toBeVisible();await page.keyboard.press('Escape');await page.getByRole('button',{name:'收起记录',exact:true}).click();await expect(page.locator('.pcv-chart')).toHaveCount(10);await expect(page.locator('.pcv-board')).toHaveCount(8);expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

for(const width of [1920,1440])test(`board count sort stays available at ${width}`,async({page})=>{
 const data=synthetic();data.rankings.limit_up.rows.forEach((row,i)=>row.board_label=i===0?'10天10板':i===1?'2天2板':'3天3板');await setup(page,data);await page.setViewportSize({width,height:1080});await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');
 const up=board(page,'limit_up');await up.getByRole('button',{name:'涨停金额榜按板数降序',exact:true}).click();await expect(up.locator('tbody tr').first()).toContainText('合成验证1');await expect(up.getByRole('button',{name:'涨停金额榜按板数升序',exact:true})).toContainText('↓');await up.getByRole('button',{name:'涨停金额榜按板数升序',exact:true}).click();await expect(up.locator('tbody tr').first()).toContainText('合成验证2');await expect(up.getByRole('button',{name:'涨停金额榜按板数降序',exact:true})).toContainText('↑');
});
test('first-board ring opens the matching derived series and leaves invalid inputs as gaps',async({page})=>{
 const data=synthetic();data.records[7].values.consecutive=null;data.records[10].values.limit_up=null;data.records[12].values.consecutive='-1';data.records[14].values.limit_up='1';data.records[14].values.consecutive='2';await setup(page,data);await page.goto('/research/reviews?view=overview&trade_day=2026-10-20');await page.getByRole('button',{name:'统计分析',exact:true}).click();await page.getByRole('region',{name:'涨停分层',exact:true}).getByRole('button',{name:/首板/}).click();await expect(page.getByRole('heading',{name:'首板走势',exact:true})).toBeVisible();await expect(page.locator('.pcv-legend b')).toHaveText('1');const path=await page.locator('.pcv-plot svg path').getAttribute('d');expect(path?.match(/M/g)?.length).toBe(5);await page.getByRole('button',{name:'全部指标',exact:true}).click();await expect(page.locator('.pcv-metric')).toHaveCount(23);
});
