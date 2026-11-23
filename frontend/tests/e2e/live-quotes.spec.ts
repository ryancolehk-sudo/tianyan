import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const id='00000000-0000-4000-8000-000000000018';
const meta={request_id:id,app_mode:'demo',generated_at:'2020-01-03T02:00:05Z',as_of_time:null,quality:{status:'partial',reasons:['Synthetic UI fixtures only']}};

test('live source timestamps and date-only reference remain distinct in the actual UI',async({page},info)=>{
  let delayed=false;
  await page.route('**/api/v1/market-quotes*',route=>route.fulfill({json:{meta,data:{selection:'indices',query_time:meta.generated_at,historical:false,items:[{
    instrument:{id,exchange:'SSE',asset_type:'index',symbol:'000001',name:'合成指数'},state:delayed?'delayed_reference':'available',reason:null,
    freshness_limit_seconds:120,age_seconds:delayed?null:5,change:delayed?null:'0.3',change_percent:delayed?null:'2.5',
    quote:{price:'12.30',previous_close:delayed?null:'12',open:'12.123456789',high:'12.5',low:'12',price_unit:'point',volume_shares:null,amount_cny:null,
      instrument_name:'合成指数',quote_time:delayed?null:'2020-01-03T02:00:00Z',...(delayed?{quote_kind:'delayed_reference',trade_day:'2020-01-03'}:{quote_kind:'snapshot'})},
    provenance:{source_id:id,family:delayed?'ashare_public':'tickflow_live',source_name:delayed?'合成五分钟参考源':'合成实时快照源',source_version_id:id,adapter_version:'synthetic-ui-only',
      evidence_version_id:id,content_hash:'0'.repeat(64),received_at:meta.generated_at,published_at:null,available_at:delayed?null:meta.generated_at,historical_eligible:!delayed}
  }]}}}));
  await page.goto('/workbench?legacy=1');await signIn(page);
  const card=page.locator('.quote-card');await expect(card.locator('.quote-price strong')).toHaveText('12.30 点');
  await card.getByText('报价来源与数据详情',{exact:true}).click();
  await expect(card).toContainText('合成实时快照源');await expect(card).toContainText('2020/1/3 10:00:00');
  delayed=true;await card.getByRole('button',{name:'刷新报价',exact:true}).click();
  await expect(card).toContainText('盘中参考 · 约5分钟更新');
  await expect(card).toContainText('源时分秒未知');await expect(card).toContainText('不能作为实时快照或严格历史依据');
  await expect(card).toContainText('合成五分钟参考源');
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});await card.scrollIntoViewIfNeeded();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    const cells=await card.locator('.trading-quote-ticker dd').evaluateAll(nodes=>nodes.map(node=>({width:node.clientWidth,scroll:node.scrollWidth})));
    expect(cells.length).toBeGreaterThan(0);
    expect(cells.every(cell=>cell.scroll<=cell.width+1)).toBe(true);
    await page.screenshot({path:info.outputPath('free-quote-reference-'+width+'.png'),animations:'disabled'});
  }
});

test('data center lists the free realtime account prerequisite without exposing a credential field',async({page})=>{
  await page.goto('/data?view=sources');await signIn(page);
  const card=page.getByRole('article',{name:'TickFlow 免费实时快照',exact:true});
  await expect(card).toContainText('尚未配置本机Key，不会发起请求。');
  await expect(card.getByRole('button',{name:'采集一次',exact:true})).toBeDisabled();
  await card.getByRole('button',{name:'配置来源',exact:true}).click();
  await expect(card.getByLabel('采集标的',{exact:true})).toBeVisible();
  await expect(card.locator('input[type=password]')).toHaveCount(0);
  await expect(card).toContainText('页面不接收或显示');
});
