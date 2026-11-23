import {test,expect} from '@playwright/test';
import {terminalFixture,terminalDay,sectorA} from './terminalTestFixtures';

test('overview combines independent strength, funds and market views without cross-selecting suppliers',async({page})=>{
  const errors:string[]=[];const reads:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));
  const stamp=terminalDay+'T13:05:00+08:00';
  await page.route('**/api/v1/**',async route=>{
    const url=new URL(route.request().url());reads.push(url.pathname+url.search);
    const ok=(data:unknown)=>route.fulfill({json:{data,meta:{app_mode:'real',request_id:'synthetic',generated_at:stamp,quality:{status:'partial',reasons:[]}}}});
    if(url.pathname==='/api/v1/session')return ok({user:{id:'overview-fixture',username:'synthetic',role:'admin'},csrf_token:'synthetic'});
    if(url.pathname==='/api/v1/workbench-view'){
      const data=terminalFixture(url.searchParams);
      data.detail_revision='overview-clarity-fixture';
      data.stocks=Array.from({length:24},(_,i)=>({...data.stocks[0],code:'9000'+String(i).padStart(2,'0')+'.SH',name:'测试股票'+i,source_time:stamp,pct_change:10-i*.1,speed_5m:2-i*.1,theme:'融资融券',ths_themes:['半导体','人工智能'],previous_height:1+i%4}));
      data.stocks[0].price_basis='daily_close';data.stocks[0].source_time=null;
      data.stocks[1]={...data.stocks[1],price_basis:'snapshot',source_time:null,source_snapshot_time:stamp};
      data.stocks[2]={...data.stocks[2],price_basis:'snapshot',source_time:null,source_snapshot_time:'2026-10-01T13:05:00+08:00'};
      data.intraday_ladder={trade_day:terminalDay,as_of_time:stamp,phase:'continuous',sample_count:24,sealed_count:24,stale_count:0,
        rows:data.stocks.map(stock=>({code:stock.code,name:stock.name,target_height:stock.previous_height,status:'sealed',freshness:'current',pct_change:stock.pct_change,source_time:stamp})),scope:'合成浏览器测试'};
      return ok(data);
    }
    if(url.pathname==='/api/v1/workbench-details'){
      const data=terminalFixture(url.searchParams);
      return ok({...data,revision:'overview-clarity-fixture',seals:{},sector_code:url.searchParams.get('sector'),members:data.selected_members,members_trade_day:terminalDay,
        tracking_members:Array.from({length:12},(_,i)=>({code:'8000'+String(i).padStart(2,'0')+'.SZ',name:'追踪股票'+i,pct_change:i,speed_5m:12-i,source_time:stamp}))});
    }
    if(url.pathname==='/api/v1/sector-membership')return ok({trade_day:terminalDay,sector_code:url.searchParams.get('sector'),
      state:'available',members_trade_day:terminalDay,member_count:12,truncated:false,
      members:Array.from({length:12},(_,i)=>({code:'9000'+String(i).padStart(2,'0')+'.SH',name:'测试股票'+i}))});
    if(url.pathname==='/api/v1/kpl-emotion')return ok({state:'enabled',today:{trade_day:terminalDay,latest:{received_at:stamp,value:'67'},points:['09:15','09:16','09:17','09:25','13:05'].map((t,i)=>({received_at:terminalDay+'T'+t+':00+08:00',value:String(40+i*5)}))},yesterday:{trade_day:'2026-10-28',latest:null,points:[{received_at:'2026-10-28T09:16:00+08:00',value:'55'}]}});
    if(url.pathname==='/api/v1/kpl-strength')return ok({trade_day:terminalDay,state:'available',received_at:stamp,coverage:'complete',total_count:12,
      rows:Array.from({length:13},(_,i)=>({code:String(800000+i),name:i===0?'共同名称':'强度题材'+i,strength:i===12?null:String(2000-i*300),limit_up_count:i===0?20:i===1?0:null})),
      history:['13:00','13:05'].map(t=>({received_at:terminalDay+'T'+t+':00+08:00',values:Object.fromEntries(Array.from({length:13},(_,i)=>[String(800000+i),i===12?null:String(2000-i*300)]))}))});
    if(url.pathname==='/api/v1/ths-funds'){
      const concept=url.searchParams.get('category')==='concept';
      const rows=Array.from({length:12},(_,i)=>({code:String((concept?300000:881000)+i),name:i===0?'共同名称':(concept?'概念':'行业')+i,net_cny:i===11?null:String((8-i)*100000000),pct_change:'1',company_count:'10',received_at:stamp}));
      return ok({sampled_day:terminalDay,status:concept?'available':'stale',received_at:stamp,complete:!concept,missing_pages:concept?[7]:[],rejected_rows:0,last_error:null,rows,
        history:['13:00','13:05','13:20'].map(t=>({received_at:terminalDay+'T'+t+':00+08:00',points:rows.map(row=>({code:row.code,net_cny:row.net_cny,received_at:terminalDay+'T'+t+':00+08:00'}))}))});
    }
    if(url.pathname==='/api/v1/ai/settings')return ok({enabled:false,features:[]});
    return ok({items:[],next_cursor:null});
  });
  await page.goto('/workbench?view=overview&trade_day='+terminalDay+'&sector='+sectorA);
  const overview=page.getByLabel('综合盯盘',{exact:true});
  await expect(page.getByRole('navigation',{name:'本页功能直达'})).toHaveCount(0);
  for(const name of ['市场全景','强度排名','资金流向','涨跌停家数走势','个股涨幅榜','连板梯队'])await expect(overview.getByRole('heading',{name,exact:true})).toBeVisible();
  await expect(page.locator('[data-kpl-code]')).toHaveCount(5);
  await expect(page.locator('[data-emotion-series]')).toHaveCount(2);
  await expect(page.locator('.ty-emotion-band')).toHaveCount(4);
  expect((await page.locator('[data-emotion-series=今日情绪] path').getAttribute('d'))?.match(/M/g)?.length).toBe(3);
  await expect(page.locator('.ty-distribution-row')).toHaveCount(11);
  await expect(page.locator('.ty-distribution-count')).toHaveText('含快照 · 23 只有效');
  const topTen=page.getByRole('region',{name:'开盘啦题材强度前10',exact:true});
  await expect(topTen.locator('.dir-strength-row')).toHaveCount(10);
  await expect(topTen.locator('.dir-strength-row').first()).toContainText('共同名称');
  await expect(topTen.locator('.dir-strength-value').first()).toHaveText('2,000');
  await expect(topTen.locator('.dir-strength-value').last()).toHaveText('-700');
  expect(await page.locator('[data-kpl-code]').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('data-kpl-code')))).toEqual([0,1,2,3,4].map(i=>String(800000+i)));
  await expect(page.getByRole('checkbox',{name:'自动选择共同名称强度',exact:true})).toBeChecked();
  await expect(page.getByRole('checkbox',{name:'自动选择共同名称强度',exact:true})).toBeDisabled();
  await expect(page.getByRole('checkbox',{name:'自动选择强度题材5强度',exact:true})).not.toBeChecked();
  await expect(page.getByRole('checkbox',{name:'自动选择强度题材5强度',exact:true})).toBeDisabled();
  await expect(page.locator('.ty-sector-tracking tbody tr')).toHaveCount(12);
  await expect(page.locator('.ty-overview-funds [data-sector-code],.ty-overview-funds svg[role=img]')).toHaveCount(0);
  await expect(page.getByLabel('开盘啦强度降序排行')).toContainText('20涨停');
  await expect(page.getByLabel('开盘啦强度降序排行')).toContainText('0涨停');
  await expect(page.getByLabel('开盘啦强度降序排行')).toContainText('—涨停');
  const sourceDetail=page.locator('.ty-overview-funds .ty-overview-source details');
  await expect(sourceDetail).not.toHaveAttribute('open','');
  const funds=page.getByLabel('同花顺资金排名');
  await funds.getByRole('button',{name:'行业8',exact:true}).click();
  expect(new URL(page.url()).searchParams.get('sector')).toBe(sectorA);
  expect(new URL(page.url()).searchParams.get('fund_sector')).toBe('THS:industry:881008');
  await expect(page.locator('[data-kpl-code]')).toHaveCount(5);
  await expect(funds.getByRole('button',{name:'行业8',exact:true})).toHaveAttribute('aria-pressed','true');
  await funds.getByRole('button',{name:'净额',exact:true}).click();
  await expect(funds.locator('tbody tr').first()).toContainText('行业10');
  await expect(funds.locator('tbody tr').first()).toContainText('-2.0');
  await expect(funds.locator('tbody tr').last()).toContainText('行业11');
  await expect(funds.getByRole('columnheader',{name:'净额',exact:true})).toHaveAttribute('aria-sort','ascending');
  await page.getByLabel('搜索资金板块').fill('行业11');
  await expect(funds.locator('tbody tr')).toHaveCount(1);
  await expect(funds.locator('tbody tr td').nth(2)).toHaveText('—');
  await page.getByLabel('搜索资金板块').fill('行业8');
  await expect(funds.locator('tbody tr td').nth(2)).toHaveText('0.0');
  await page.getByLabel('搜索资金板块').fill('');
  await page.getByRole('button',{name:'概念',exact:true}).click();
  await expect(funds.locator('tbody')).toContainText('概念1');
  await expect(page.locator('.ty-overview-warning')).toContainText('缺第 7 页');
  await page.getByRole('button',{name:'行业',exact:true}).click();
  await expect(funds.locator('tbody tr').first()).toContainText('行业10');
  await page.reload();
  await expect(funds.locator('tbody tr').first()).toContainText('行业10');
  await funds.getByRole('button',{name:'净额',exact:true}).click();
  await expect(funds.locator('tbody tr').first()).toContainText('共同名称');
  await expect(funds.locator('tbody tr').last()).toContainText('行业11');
  for(const width of [1920,1440,1366,1024,390]){
    await page.setViewportSize({width,height:900});
    await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    const panels=await page.locator('.ty-overview-market-grid>.ty-panel,.ty-overview-stock-row>.ty-panel').evaluateAll(nodes=>nodes.filter(node=>node.getBoundingClientRect().height>0).map(node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};}));
    if(width>=1366){expect(panels[0].x).toBeLessThan(panels[1].x);expect(panels[0].h).toBeGreaterThan(panels[1].h*2);expect(panels[1].x).toBe(panels[2].x);expect(panels[2].y).toBeGreaterThan(panels[1].y);}
    const svg=page.getByRole('img',{name:'开盘啦板块强度接收采样曲线',exact:true});
    const scale=await svg.evaluate(node=>{const s=node as SVGSVGElement;return s.getBoundingClientRect().width/s.viewBox.baseVal.width;});
    expect(scale).toBeGreaterThan(.95);
    if(width>=1024){
      const chartBounds=await svg.boundingBox(),rankBounds=await topTen.boundingBox();
      expect(chartBounds!.x+chartBounds!.width).toBeLessThanOrEqual(rankBounds!.x+1);
      expect(rankBounds!.width).toBeGreaterThanOrEqual(155);
    }
    for(let i=0;i<panels.length;i++)for(let j=i+1;j<panels.length;j++){
      const a=panels[i],b=panels[j];expect(a.x+a.w<=b.x+1||b.x+b.w<=a.x+1||a.y+a.h<=b.y+1||b.y+b.h<=a.y+1).toBe(true);
    }
    if(width>=1366){
      expect(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight)).toBe(true);
      const ladderBounds=await page.locator('.ty-mini-ladder').boundingBox();
      const trackingBounds=await page.locator('.ty-sector-tracking').boundingBox();
      expect(trackingBounds!.x+trackingBounds!.width).toBeLessThanOrEqual(ladderBounds!.x);
      expect(trackingBounds!.y+trackingBounds!.height).toBeCloseTo(ladderBounds!.y+ladderBounds!.height,0);
      expect(ladderBounds!.height).toBeGreaterThan(trackingBounds!.height);
      await expect(page.locator('.ty-mini-ladder-stocks button')).toHaveCount(24);
      await expect(page.getByRole('heading',{name:'个股涨速榜',exact:true})).toHaveCount(0);
      expect((await page.locator('.ty-overview-strength-list').boundingBox())!.width).toBeLessThanOrEqual(280);
      const selectors=['.ty-market-ribbon','.ty-overview-strength','.ty-breadth-panel','.ty-overview-strength-list','.ty-overview-funds','.ty-overview-stock-row>.ty-rank-panel:first-child','.ty-overview-market-grid>.ty-mini-ladder','.ty-overview-stock-row>.ty-sector-tracking'];
      for(const selector of selectors){
        const bounds=await page.locator(selector).boundingBox();
        expect(bounds?.y,selector).toBeGreaterThanOrEqual(0);
        expect(bounds!.y+bounds!.height,selector+' must fit in the first screen').toBeLessThanOrEqual(900);
      }
      const rowHeight=await page.locator('.ty-rank-table tbody tr').first().evaluate(n=>n.getBoundingClientRect().height);
      expect(rowHeight).toBeLessThanOrEqual(28);
      const themes=page.locator('.ty-rank-table tbody tr:first-child .ty-theme-text');
      const themeWidths=await themes.evaluateAll(nodes=>nodes.map(node=>{
        const style=getComputedStyle(node),parent=node.parentElement!,parentStyle=getComputedStyle(parent);
        const probe=document.createElement('div');
        probe.style.cssText='position:absolute;visibility:hidden;box-sizing:content-box;width:20ch;height:0;padding:0;border:0;';
        // Computed font shorthand may be empty. Measure the actual text font,
        // rather than inheriting the probe's unrelated body typography.
        probe.style.fontFamily=style.fontFamily;probe.style.fontSize=style.fontSize;probe.style.fontWeight=style.fontWeight;
        probe.style.fontStyle=style.fontStyle;probe.style.fontStretch=style.fontStretch;
        probe.style.fontVariantNumeric=style.fontVariantNumeric;probe.style.fontFeatureSettings=style.fontFeatureSettings;
        document.body.append(probe);
        const twentyCh=probe.getBoundingClientRect().width;probe.remove();
        const cellInnerWidth=parent.clientWidth-parseFloat(parentStyle.paddingLeft)-parseFloat(parentStyle.paddingRight);
        return {width:node.getBoundingClientRect().width,limit:Math.min(cellInnerWidth,twentyCh)};
      }));
      expect(themeWidths).toHaveLength(2);
      for(const {width:themeWidth,limit} of themeWidths)expect(themeWidth).toBeLessThanOrEqual(limit+1);
      for(const theme of await themes.all()){
        const fullText=await theme.innerText();
        await expect(theme).toHaveAccessibleName('查看完整内容：'+fullText);
        await theme.focus();
        await expect(page.getByRole('tooltip')).toHaveText(fullText);
        await page.keyboard.press('Escape');
        await expect(page.getByRole('tooltip')).toHaveCount(0);
        await theme.blur();
      }
      for(const rank of await page.locator('.ty-overview-stock-row>.ty-rank-panel').all()){
        const list=await rank.locator('.ty-table-scroll').boundingBox();
        const last=await rank.locator('tbody tr').nth(7).boundingBox();
        expect(last!.y+last!.height).toBeLessThanOrEqual(list!.y+list!.height+1);
      }
      const trackingList=await page.locator('.ty-sector-tracking .ty-table-scroll').boundingBox();
      const sixth=await page.locator('.ty-sector-tracking tbody tr').nth(5).boundingBox();
      expect(sixth!.y+sixth!.height).toBeLessThanOrEqual(trackingList!.y+trackingList!.height+1);
    }
    await page.screenshot({path:`test-results/overview-unified-${width}.png`,fullPage:true});
  }
  await page.setViewportSize({width:1562,height:633});
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight)).toBe(true);
  await expect.poll(()=>page.locator('.ty-market-emotion .ty-chart-grid>g').evaluateAll(nodes=>{
    const ticks=nodes.map(node=>node.querySelector('text')!.getBoundingClientRect()).sort((a,b)=>a.top-b.top);
    return ticks.every((tick,i)=>!i||tick.top>=ticks[i-1].bottom);
  })).toBe(true);
  await page.setViewportSize({width:1366,height:768});
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight)).toBe(true);
  const workspaces=page.getByRole('navigation',{name:'综合盯盘工作区'});
  await page.getByRole('link',{name:'板块研究',exact:true}).click();
  await expect(page.getByRole('region',{name:'板块与个股工作区',exact:true})).toBeVisible();
  await expect(page.locator('.rw-desktop-detail')).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight)).toBe(true);
  await workspaces.getByRole('button',{name:'盘面',exact:true}).click();
  await expect(funds.locator('tbody tr').first()).toContainText('共同名称');
  await page.getByRole('table',{name:'个股涨幅榜',exact:true}).getByRole('button',{name:'测试股票0',exact:true}).click();
  await expect(workspaces.getByRole('button',{name:'盘面',exact:true})).toHaveAttribute('aria-pressed','true');
  await expect(page.locator('.ty-selected')).toContainText('测试股票0');
  await expect(page.getByRole('region',{name:'盘面工作区',exact:true})).toBeVisible();
  await page.getByRole('link',{name:'板块研究',exact:true}).click();
  await expect(workspaces.getByRole('button',{name:'板块与个股',exact:true})).toHaveAttribute('aria-pressed','true');
  await expect(page.locator('.rw-detail-head')).toContainText('测试股票0');
  for(const width of [1024,390]){
    await page.setViewportSize({width,height:900});
    await page.locator('.rw-mobile-nav').getByRole('button',{name:'详情',exact:true}).click();
    await expect(page.locator('.rw-desktop-detail')).toBeVisible();
    await expect(page.locator('.rw-detail-head')).toContainText('测试股票0');
    expect(new URL(page.url()).searchParams.get('stock')).toBe('900000.SH');
    expect(new URL(page.url()).searchParams.get('trade_day')).toBe(terminalDay);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  }
  await page.setViewportSize({width:1366,height:768});
  await page.getByRole('button',{name:'证据',exact:true}).click();
  await expect(page.locator('.rw-detail')).toContainText('来源：');
  await workspaces.getByRole('button',{name:'数据覆盖',exact:true}).click();
  await page.locator('.ty-coverage>summary').click();
  await expect(page.locator('.ty-coverage')).toHaveAttribute('open','');
  await workspaces.getByRole('button',{name:'AI 观察',exact:true}).click();
  await expect(page.getByRole('region',{name:'AI 观察工作区',exact:true})).toBeVisible();
  await workspaces.getByRole('button',{name:'盘面',exact:true}).click();
  await expect(page.locator('[data-kpl-code]')).toHaveCount(5);
  expect(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight)).toBe(true);
  expect(reads.some(r=>r.includes('fund_compare=THS'))).toBe(false);
  expect(reads.some(r=>r.includes('/ths-funds?')&&r.includes('sampled_day='+terminalDay))).toBe(true);
  expect(errors).toEqual([]);
});
