import {expect,test} from '@playwright/test';
import {fixture,openRotation} from './rotationTestFixtures';

for(const [width,height] of [[1920,1080],[1440,900],[1280,900]])test(`all thirty popularity rows fit without scrollbars ${width}x${height}`,async({page},info)=>{
 const state=await fixture(page);await page.setViewportSize({width,height});await openRotation(page);
 await expect(page.locator('.sr-pop-platform tbody tr')).toHaveCount(30);
 await expect(page.getByRole('heading',{name:'板块轮动',exact:true})).toHaveCount(0);
 const tools=page.locator('.dr-toolbar-actions'),research=page.getByRole('button',{name:'研究记录',exact:true});
 await expect(tools.getByRole('button',{name:'研究记录',exact:true})).toBeVisible();await expect(tools.getByLabel('板块轮动资料说明',{exact:true})).toBeVisible();
 const toolBox=await tools.boundingBox(),researchBox=await research.boundingBox();expect(researchBox!.y>=toolBox!.y&&researchBox!.y+researchBox!.height<=toolBox!.y+toolBox!.height+1).toBeTruthy();
 for(const region of await page.locator('.sr-pop-platform .sr-list-scroll,.sr-funds>.sr-list-scroll').all()){
  await expect.poll(()=>region.evaluate(el=>el.scrollHeight-el.clientHeight)).toBeLessThanOrEqual(1);
  const rects=await region.evaluate(el=>({bottom:el.getBoundingClientRect().bottom,last:el.querySelector('tbody tr:last-child')!.getBoundingClientRect().bottom}));expect(rects.last).toBeLessThanOrEqual(rects.bottom+1);
 }
 expect(await page.evaluate(()=>document.body.scrollWidth<=innerWidth+1&&document.body.scrollHeight<=innerHeight+1)).toBeTruthy();
 await page.screenshot({path:info.outputPath(`rotation-fit-${width}.png`)});expect(state.errors).toEqual([]);expect(state.writes).toEqual([]);
});

test('a genuinely short window keeps accessible overflow instead of hiding rows',async({page})=>{
 await fixture(page);await page.setViewportSize({width:1440,height:650});await openRotation(page);
 const list=page.locator('.sr-pop-platform .sr-list-scroll').first();await expect.poll(()=>list.evaluate(el=>el.scrollHeight>el.clientHeight)).toBeTruthy();
 expect(await list.evaluate(el=>getComputedStyle(el).overflowY)).toBe('auto');await list.evaluate(el=>{el.scrollTop=el.scrollHeight;});await expect(list.locator('tbody tr').last()).toBeInViewport();
});
