import {expect,test} from '@playwright/test';
import {atlas,fixture,openRotation,initialDays,panel} from './rotationTestFixtures';
import {meta} from './reviewTestFixtures';

test('inline atlas links native strength, volume, leaders and ranking curves locally',async({page},info)=>{
 const state=await fixture(page),queries:URLSearchParams[]=[];
 await page.route('**/api/v1/direction-data/sector-atlas?**',route=>{const params=new URL(route.request().url()).searchParams;queries.push(params);return route.fulfill({json:{meta,data:atlas(params)}});});
 await openRotation(page);const board=page.locator('.sr-atlas');await expect(board.locator('.sr-matrix tbody tr')).toHaveCount(11);
 await expect(board.locator('.sr-leaders')).toContainText('龙五合成领涨戊');
 const before=queries.length;await board.locator('.sr-matrix button[data-code=K1]').first().click();
 const strength=board.getByRole('img',{name:'所选板块原始强度走势',exact:true});
 await expect(strength.locator('text').filter({hasText:/^0$/})).toHaveCount(1);
 expect((await strength.locator('.sr-strength-line').getAttribute('d'))?.match(/M/g)?.length).toBe(2);
 await expect(board.getByRole('img',{name:'所选板块成交额走势（亿元）'}).locator('text').filter({hasText:/^0亿$/})).toHaveCount(1);
 for(const code of ['K8','K9'])await board.locator(`.sr-matrix button[data-code=${code}]`).first().click();
 expect(queries).toHaveLength(before);expect(queries.every(p=>!p.has('selected_codes'))).toBeTruthy();
 const legend=board.locator('.sr-rank-legend button').filter({hasText:'商业航天'});await legend.click();await expect(legend).toHaveAttribute('aria-pressed','false');await legend.click();await expect(legend).toHaveAttribute('aria-pressed','true');
 await board.getByRole('button',{name:'近50日',exact:true}).click();await expect.poll(()=>queries.at(-1)?.get('lookback')).toBe('50');
 await board.getByRole('button',{name:'自定义',exact:true}).click();await board.getByRole('spinbutton',{name:'自定义交易日数'}).fill('12');await expect.poll(()=>queries.at(-1)?.get('lookback')).toBe('12');
 await board.getByRole('button',{name:'同花顺板块',exact:true}).click();await expect(board.locator('.sr-day-basis')).toHaveText('收盘数据');
 await expect(board.getByRole('img',{name:'所选板块原始强度走势',exact:true})).toHaveCount(0);await expect(board.locator('.sr-leaders')).toHaveCount(0);
 await expect(board.locator('.sr-matrix td button').first()).toContainText('%');await page.screenshot({path:info.outputPath('rotation-ths.png')});
 await board.getByRole('button',{name:'开盘啦板块',exact:true}).click();await expect(board.locator('.sr-leaders')).toBeVisible();expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('partial coverage stays unknown and only verified complete Top10 permits unranked baseline',async({page})=>{
 const state=await fixture(page);
 await page.route('**/api/v1/direction-data/sector-atlas?**',route=>{
  const days=initialDays.slice(0,3).map((d,i)=>({...panel(d,'合成覆盖检查',Array.from({length:10},(_,j)=>({code:'T'+(j+(i?1:0)),name:'概念'+(j+(i?1:0)),rank:j+1,pct_change:'1'}))),status:i===1?'partial':'ready',top_ten_complete:i!==1}));
  return route.fulfill({json:{meta,data:{days,limitations:['合成测试数据']}}});
 });
 await openRotation(page);await page.getByRole('button',{name:'同花顺板块',exact:true}).click();await page.locator('.sr-matrix button[data-code=T0]').first().click();
 const chart=page.getByRole('img',{name:'多板块前十排名轨迹'});
 await expect(chart.locator('title').filter({hasText:'2026-10-22 · 概念0'})).toHaveCount(0);
 await expect(chart.locator('title').filter({hasText:'2026-10-21 · 概念0 · 未进前十'})).toHaveCount(1);expect(state.writes).toEqual([]);
});

test('history end and single horizontal scroll leave prior funds anchored',async({page})=>{
 await fixture(page);const queries:URLSearchParams[]=[];
 await page.route('**/api/v1/direction-data/sector-atlas?**',route=>{const p=new URL(route.request().url()).searchParams;queries.push(p);return route.fulfill({json:{meta,data:atlas(p)}});});
 await openRotation(page);await expect(page.locator('.sr-funds-date')).toContainText('2026-10-22');
 await page.locator('input[aria-label="板块轮动截至日期"]').fill('2026-10-16');
 await expect.poll(()=>queries.at(-1)?.get('trade_day')).toBe('2026-10-16');await expect(page.locator('.sr-matrix thead th').nth(1)).toContainText('10-16');
 await expect(page.locator('.sr-funds-date')).toContainText('2026-10-22');
 await page.locator('.sr-atlas-scroll').evaluate(el=>{el.scrollLeft=300;});
 const matrix=await page.locator('.sr-matrix th').nth(1).boundingBox();const plot=await page.locator('.sr-strength-line').evaluate(el=>el.closest('svg')!.getBoundingClientRect().x);expect(Math.abs(matrix!.x-plot)).toBeLessThan(2);
 await page.getByRole('button',{name:'最近',exact:true}).click();await expect(page.locator('.sr-matrix thead th').nth(1)).toContainText('10-23');
});
