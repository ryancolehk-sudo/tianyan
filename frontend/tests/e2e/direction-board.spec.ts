import { expect,test } from '@playwright/test';
import {fixture,openRotation,researchDay} from './rotationTestFixtures';

for(const width of [1920,1440]) test(`rotation workspace fits the desktop viewport ${width}`,async({page},info)=>{
  const state=await fixture(page);await page.setViewportSize({width,height:1080});await openRotation(page);
  const board=page.getByRole('region',{name:'板块轮动工作区',exact:true});await expect(board).toBeVisible();
  await expect(board.locator('.sr-matrix td button')).toHaveCount(190);
  for(const name of ['同花顺人气榜','通达信人气榜','东方财富人气榜']) await expect(board.getByRole('article',{name,exact:true})).toBeVisible();
  await expect(board.getByRole('region',{name:'昨日资金净流入',exact:true})).toBeVisible();
  expect(await board.evaluate(el=>el.getBoundingClientRect().bottom<=innerHeight)).toBeTruthy();
  expect(await page.locator('body').evaluate(el=>el.scrollWidth<=innerWidth+2&&el.scrollHeight<=innerHeight+2)).toBeTruthy();
  const bottom=await board.locator('.sr-side').evaluate(el=>el.getBoundingClientRect().bottom);
  const left=await board.locator('.sr-atlas').evaluate(el=>el.getBoundingClientRect().bottom);expect(Math.abs(bottom-left)).toBeLessThan(2);
  await board.locator('.sr-pop-platform').first().getByRole('button',{name:'合成人气0',exact:true}).click();
  await expect(board.locator('.sr-pop-platform tr.is-selected')).toHaveCount(3);
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
  expect(await board.locator('.sr-quote-button').evaluateAll(els=>els.every(e=>e.scrollWidth<=e.clientWidth+1))).toBeTruthy();
  await page.screenshot({path:info.outputPath(`rotation-${width}.png`)});
});

test('default current popularity and prior funds retain independent verified dates',async({page})=>{
  const state=await fixture(page);state.currentDay='2026-10-27';await page.goto('/research/reviews?view=direction');
  await expect(page.locator('.sr-popularity>.sr-section-head')).toContainText('2026-10-27');
  await expect(page.locator('.sr-funds-date')).toContainText('2026-10-26');
  expect(state.queryDays.at(-1)?.get('automatic_current')).toBe('true');
  await page.getByRole('button',{name:'近10日',exact:true}).click();
  await expect(page.locator('.sr-matrix thead th')).toHaveCount(11);
  await expect(page.locator('.sr-funds-date')).toContainText('2026-10-26');expect(state.writes).toEqual([]);
});

test('explicit date and receipt cutoff never enable current rankings',async({page})=>{
  const state=await fixture(page);await openRotation(page,'&as_of_time=2026-10-23T08%3A30%3A00%2B08%3A00');
  await expect(page.locator('.sr-popularity')).toContainText('暂无资料');
  await expect(page.locator('.sr-pop-platform td>button')).toHaveCount(0);
  expect(state.queryDays.every(p=>!p.has('automatic_current')&&p.get('trade_day')===researchDay&&p.get('as_of_time')==='2026-10-23T08:30:00+08:00')).toBeTruthy();
});

test('zero, missing quote and native rank movement remain distinguishable in compact lists',async({page})=>{
  const state=await fixture(page);state.zeroChanges=true;await openRotation(page);
  const ths=page.getByRole('article',{name:'同花顺人气榜',exact:true});
  await expect(ths.locator('tbody tr').first()).toContainText('0.00%');
  await expect(ths.locator('tbody tr').nth(2).locator('.sr-quote-button')).toHaveText('—');
  await expect(ths.getByRole('button',{name:'合成人气0',exact:true})).toHaveAttribute('title',/名次变化 0/);
  await expect(page.getByRole('article',{name:'通达信人气榜',exact:true}).getByRole('button',{name:'合成人气0',exact:true})).toHaveAttribute('title',/名次变化未提供/);
  await expect(page.getByRole('article',{name:'东方财富人气榜',exact:true})).toContainText('较早记录');
  await ths.getByRole('button',{name:'合成人气0',exact:true}).focus();await page.keyboard.press('Enter');
  await expect(page.locator('.sr-pop-platform tr.is-selected')).toHaveCount(3);
  await ths.locator('summary').click();await expect(ths.locator('.sr-source-info>div')).toContainText('来源时间 2026-10-23 15:00:00');
});

test('funds classification changes only funds and a retired response cannot replace a newer choice',async({page})=>{
  const state=await fixture(page);await openRotation(page);await expect(page.locator('.sr-funds')).toContainText('合成概念0');
  await page.getByRole('button',{name:'同花顺板块',exact:true}).click();await expect(page.locator('.sr-atlas')).toHaveAttribute('data-source','ths');
  const hold=(()=>{let release!:()=>void;const promise=new Promise<void>(r=>release=r);return{promise,release};})();state.holdIndustry=hold;
  await page.getByRole('button',{name:'行业',exact:true}).click();await expect.poll(()=>state.queryDays.at(-1)?.get('sector_type')).toBe('industry');
  await page.getByRole('button',{name:'概念',exact:true}).click();hold.release();
  await expect(page.locator('.sr-funds')).toContainText('合成概念0');await expect(page.locator('.sr-atlas')).toHaveAttribute('data-source','ths');
  await expect(page.locator('.sr-popularity')).toContainText('合成人气0');expect(state.writes).toEqual([]);
});

test('same-scope failure retains saved rows and malformed rankings leave research records accessible',async({page})=>{
  const state=await fixture(page);await openRotation(page);await expect(page.locator('.sr-popularity')).toContainText('合成人气0');state.error=true;
  await page.getByRole('button',{name:'重读缓存',exact:true}).click();await expect(page.locator('.sr-error').filter({hasText:'合成榜单读取失败'})).toBeVisible();
  await expect(page.locator('.sr-popularity')).toContainText('合成人气0');
  state.error=false;state.mode='malformed';await page.goto('/research/reviews?view=direction&trade_day=2026-10-22');
  await expect(page.locator('.sr-error')).toContainText('榜单响应格式不完整');await page.getByRole('button',{name:'研究记录',exact:true}).click();
  await expect(page.getByRole('region',{name:'研究记录与核心跟踪',exact:true})).toBeVisible();expect(state.writes).toEqual([]);
});

for(const width of [1024,390])test(`narrow panes preserve every function without page overflow ${width}`,async({page},info)=>{
  const state=await fixture(page);await page.setViewportSize({width,height:900});await openRotation(page);await expect(page.locator('.sr-matrix td button')).toHaveCount(190);
  await expect(page.getByRole('navigation',{name:'板块轮动分区'})).toBeVisible();
  await page.screenshot({path:info.outputPath(`rotation-${width}.png`)});
  await page.getByRole('navigation',{name:'板块轮动分区'}).getByRole('button',{name:'人气',exact:true}).click();
  if(width<600){await page.getByRole('navigation',{name:'人气榜平台'}).getByRole('button',{name:'东方财富',exact:true}).click();await expect(page.getByRole('article',{name:'东方财富人气榜',exact:true})).toBeVisible();}
  await page.screenshot({path:info.outputPath(`popularity-${width}.png`)});
  await page.getByRole('navigation',{name:'板块轮动分区'}).getByRole('button',{name:'资金',exact:true}).click();await expect(page.locator('.sr-funds')).toBeVisible();
  expect(await page.locator('body').evaluate(el=>el.scrollWidth<=innerWidth+2)).toBeTruthy();expect(state.errors).toEqual([]);
});

test('research entry moves focus inside and Escape returns to the opener',async({page},info)=>{
 await fixture(page);await openRotation(page);const open=page.getByRole('button',{name:'研究记录',exact:true});await open.click();
 await expect(page.getByRole('button',{name:'收起记录',exact:true})).toBeFocused();
 await expect(page.locator('.sr-workspace')).toHaveAttribute('inert','');await page.screenshot({path:info.outputPath('research-records-focus.png')});
 await page.keyboard.press('Escape');await expect(open).toBeFocused();await expect(page.locator('.sr-research-drawer')).toHaveCount(0);
});
test('quote provenance is available by keyboard and missing quotes stay missing',async({page},info)=>{
 await fixture(page);await openRotation(page);const platform=page.getByRole('article',{name:'同花顺人气榜',exact:true});
 const quote=platform.getByRole('button',{name:'合成人气0涨幅及来源详情',exact:true});await quote.focus();await page.keyboard.press('Enter');
 const popover=page.getByRole('region',{name:'合成人气0行情详情',exact:true});await expect(popover).toBeVisible();await expect(popover).toContainText('行情时间 2026-10-23 15:00:00');await expect(popover).toContainText('收盘行情');await page.screenshot({path:info.outputPath('quote-provenance.png')});
 await page.keyboard.press('Escape');await expect(quote).toBeFocused();
 await platform.getByRole('button',{name:'合成人气2涨幅及来源详情',exact:true}).click();const missing=page.getByRole('region',{name:'合成人气2行情详情',exact:true});await expect(missing).toContainText('同日行情未提供');await expect(missing).toContainText('涨幅 —');
});
