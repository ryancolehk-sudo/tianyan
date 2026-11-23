import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { test } from './isolated-browser.fixtures';
import type { NewsArticle, NewsSecurity } from '../../src/features/news-center/model';

const day = '2001-12-31', cutoff = day + 'T15:05:00+08:00';
const id = (n: number) => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
function article(n: number, title: string, subjects: NewsSecurity[] = []): NewsArticle {
  return { id:id(n), evidence_id:id(100+n), title, summary:null, category:'公司公告',
    source_name:'合成公告来源', publisher:'合成发行方', family:'cninfo_announcements',
    source_url:'https://www.cninfo.com.cn/new/disclosure/detail?announcementId=synthetic-'+n,
    published_at:null, published_day:day, received_at:cutoff, content_hash:'a'.repeat(64),
    is_read:true, is_saved:false, is_following:false, securities:subjects,
    security_status:subjects.length?'source_provided':'missing', related_sources:[] };
}
function subject(n: number, name: string | null, code: string): NewsSecurity {
  return { name, code, exchange:'SZSE', basis:name?'source_reported':'archived_announcement',
    evidence_id:id((name?100:200)+n), source_id:id(90), source_name:'合成公告来源',
    source_url:name?'https://www.cninfo.com.cn/new/disclosure/detail?announcementId=synthetic-'+n:'https://static.cninfo.com.cn/finalpage/'+day+'/synthetic-'+n+'.pdf',
    received_at:cutoff, content_hash:'a'.repeat(64), source_fields:name?['payload.securities[0].code','payload.securities[0].name']:['rows[id=synthetic-'+n+'].ts_code'] };
}
async function setup(page: Page) {
  const state = { articles:[article(1,'董事会决议公告',[subject(1,'合成证券甲','000001')]),
    article(2,'合成名称 600000 年度公告'),article(3,'关于年度审计机构的公告',[subject(3,null,'000002')])],
    topics:[] as URLSearchParams[], writes:[] as string[], errors:[] as string[] };
  state.articles[2].security_status='unverified';
  page.on('pageerror', error=>state.errors.push(error.message));
  await page.addLocatorHandler(page.getByRole('dialog',{name:'正在准备你的工作空间'}),()=>page.keyboard.press('Escape'));
  await page.route('**/api/v1/**',route=>{
    const request=route.request(),url=new URL(request.url());
    const ok=(data:unknown)=>route.fulfill({json:{data,meta:{app_mode:'demo',generated_at:cutoff,quality:{status:'partial',reasons:['合成资讯主体测试']}}}});
    if(request.method()!=='GET')state.writes.push(request.method()+' '+url.pathname);
    if(url.pathname==='/api/v1/session')return ok({user:{id:id(99),username:'synthetic',role:'viewer'},csrf_token:''});
    if(url.pathname==='/api/v1/market-session')return ok({server_time:cutoff,trade_day:day,phase:'closed',is_trading_day:true,automatic_live:false,current_day_active:false,next_transition_at:null,poll_seconds:60});
    if(url.pathname==='/api/v1/news/items')return ok({items:state.articles,focus:state.articles[0],total:3,next_cursor:null,latest_edition:null,sources:[],coverage_start:cutoff,ai_status:{state:'unconfigured',connection_id:null,model:null,reason:'合成禁用'}});
    if(url.pathname.startsWith('/api/v1/news/items/'))return ok(state.articles.find(row=>url.pathname.endsWith(row.id)));
    if(url.pathname==='/api/v1/news/topics'){
      state.topics.push(url.searchParams);
      return ok({topic_source:url.searchParams.get('topic_source'),topic_key:url.searchParams.get('topic_key'),keyword:url.searchParams.get('keyword'),trade_day:day,as_of_time:cutoff,events:[],total:0,next_cursor:null,members:[],coverage:[],gaps:['合成来源覆盖范围']});
    }
    if(url.pathname==='/api/v1/ai/settings')return ok({enabled:false,features:[]});
    return ok({items:[],next_cursor:null});
  });
  return state;
}

test('announcement focus, list and details use source subjects and retain explicit missing fields',async({page},info)=>{
  const state=await setup(page);
  await page.goto('/news');
  const focus=page.locator('.nc-focus'),list=page.locator('.nc-news-list');
  await expect(focus.locator('.nc-securities')).toHaveText('合成证券甲000001');
  const missing=list.locator('.nc-news-row').filter({hasText:'合成名称 600000 年度公告'});
  await expect(missing.locator('.nc-securities')).toHaveText('证券主体未提供');
  const partial=list.locator('.nc-news-row').filter({hasText:'关于年度审计机构的公告'});
  await expect(partial.locator('.nc-securities')).toContainText('简称未提供000002');
  await expect(partial.locator('.nc-security-pending')).toHaveText('主体待核');
  await focus.getByRole('button',{name:'董事会决议公告',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'资讯详情',exact:true});
  await expect(dialog.locator('.nc-securities')).toHaveText('合成证券甲000001');
  await dialog.locator('.nc-evidence>summary').click();
  await expect(dialog.locator('.nc-security-evidence')).toContainText('公告原始结构字段');
  await expect(dialog.locator('.nc-security-evidence')).toContainText(id(101));
  await expect(dialog.getByRole('link',{name:'核对主体来源'})).toHaveAttribute('href',state.articles[0].source_url);
  await dialog.getByRole('button',{name:'关闭资讯详情',exact:true}).click();
  await expect(dialog).toHaveCount(0);
  await partial.getByRole('button',{name:'关于年度审计机构的公告',exact:true}).click();
  await expect(dialog.locator('.nc-security-pending')).toBeVisible();
  await dialog.locator('.nc-evidence>summary').click();
  await expect(dialog.locator('.nc-security-evidence')).toContainText('同公告 ID 的已存来源档案');
  await expect(dialog.getByText('来源字段已留存，可用时点待核；不作为历史已确认关联。')).toBeVisible();
  await dialog.getByRole('button',{name:'关闭资讯详情',exact:true}).click();
  await expect(dialog).toHaveCount(0);
  for(const width of [1440,1024,390]){
    await page.setViewportSize({width,height:900});
    await expect(focus.locator('.nc-securities')).toBeVisible();
    await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath('announcement-subjects-'+width+'.png'),fullPage:true});
  }
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('legacy responses remain pending until revalidation explicitly provides a security status',async({page})=>{
  const state=await setup(page);
  delete (state.articles[0] as Partial<NewsArticle>).security_status;
  delete (state.articles[1] as Partial<NewsArticle>).security_status;
  delete (state.articles[1] as Partial<NewsArticle>).securities;
  await page.goto('/news');
  const focus=page.locator('.nc-focus');
  const missing=page.locator('.nc-news-row').filter({hasText:'合成名称 600000 年度公告'});
  await expect(focus.locator('.nc-security-pending')).toHaveText('主体待核');
  await expect(missing.locator('.nc-securities')).toHaveText('证券主体待核');
  await focus.getByRole('button',{name:'董事会决议公告',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'资讯详情',exact:true});
  await expect(dialog.locator('.nc-security-pending')).toHaveText('主体待核');
  await dialog.locator('.nc-evidence>summary').click();
  await expect(dialog.getByText('来源字段已留存，可用时点待核；不作为历史已确认关联。')).toBeVisible();
  await dialog.getByRole('button',{name:'关闭资讯详情',exact:true}).click();
  await expect(dialog).toHaveCount(0);
  state.articles[0].security_status='source_provided';
  state.articles[1].securities=[];
  state.articles[1].security_status='missing';
  await page.getByRole('button',{name:'刷新已存资讯',exact:true}).click();
  await expect(focus.locator('.nc-securities')).toHaveText('合成证券甲000001');
  await expect(focus.locator('.nc-security-pending')).toHaveCount(0);
  await expect(missing.locator('.nc-securities')).toHaveText('证券主体未提供');
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});

test('topic entry uses dark controls and keeps dates, cutoff and source identity in their proper places',async({page},info)=>{
  const state=await setup(page);
  await page.goto('/news?'+new URLSearchParams({view:'topics',trade_day:day,as_of_time:cutoff}));
  const topic=page.getByRole('region',{name:'题材研究',exact:true});
  await expect(topic).not.toContainText('keyword:待选择');
  const input=topic.getByRole('textbox',{name:'题材关键词',exact:true});
  await expect(input).toBeVisible();
  await expect(input).toHaveCSS('background-color','rgb(11, 20, 27)');
  await input.fill('  合成超长题材与原始资料核对  ');
  await topic.getByRole('button',{name:'查找已存线索',exact:true}).click();
  await expect(topic.getByRole('heading',{name:'合成超长题材与原始资料核对',exact:true})).toBeVisible();
  await expect(topic.locator('.topic-meta').first()).toContainText(day);
  await expect(topic.locator('.topic-meta').first()).toContainText('关键词检索');
  await expect(topic.getByText('keyword:合成超长题材与原始资料核对',{exact:true})).toBeHidden();
  expect(new URL(page.url()).searchParams.get('as_of_time')).toBe(cutoff);
  expect(state.topics.at(-1)?.get('keyword')).toBe('合成超长题材与原始资料核对');
  await topic.locator('.topic-identity>summary').click();
  await expect(topic.getByText('keyword:合成超长题材与原始资料核对',{exact:true})).toBeVisible();
  await topic.locator('.topic-identity>summary').click();
  for(const width of [1440,1024,390]){
    await page.setViewportSize({width,height:900});
    await expect(input).toBeVisible();
    await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath('topic-entry-'+width+'.png'),fullPage:true});
  }
  expect(state.writes).toEqual([]);expect(state.errors).toEqual([]);
});
