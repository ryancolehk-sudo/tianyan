import { cohortMean, unclassifiedNote } from '../../shared/cohortFeedback';
import { records, text, safeUrl } from './model';
import type { ReportView } from './model';

export type Row = Record<string, unknown>;
function obj(v: unknown): Row { return v && typeof v === 'object' && !Array.isArray(v) ? v as Row : {}; }
function at(row: unknown, ...keys: string[]) { return keys.reduce<unknown>((v,k)=>obj(v)[k], row); }
function n(v: unknown) {
  const raw = typeof v === 'object' ? obj(v).value : v;
  return (typeof raw === 'number' || typeof raw === 'string' && raw.trim()) && Number.isFinite(Number(raw)) ? Number(raw) : null;
}
export function decimal(v: unknown, unit='') { const num=n(v); return num===null?'—':Number(num.toFixed(2))+unit; }
function dayOf(event: Row) {
  const stamp=Date.parse(String(event.published_at));
  return Number.isFinite(stamp) ? new Date(stamp+8*3600000).toISOString().slice(0,10) : String(event.published_day??'');
}
const topics = [
  ['财经','算力','算力|GPU|Token|资本开支','通信设备、半导体','算力投资先影响服务器与互联设备订单'],
  ['财经','PCB','PCB|印制电路板','元件','高端板的订单、交付和良率决定收入兑现'],
  ['财经','半导体','芯片|半导体|晶圆','半导体','芯片需求经库存、晶圆排产传导至制造环节'],
  ['财经','人工智能','人工智能|大模型|AI','软件开发、通信设备','训练投入与应用付费分别影响硬件和软件'],
  ['财经','工业原料','烧碱|甲醇|氯化铵|化工品','化学原料、化学制品','产品报价与原料成本的价差决定毛利方向'],
  ['财经','有色金属','有色金属|沪锌|沪银|沪铜','工业金属、贵金属','期货价格影响矿企售价与加工企业采购成本'],
  ['财经','能源价格','油价|燃油|液化气|原油','炼化及贸易、航运港口','燃料涨跌对生产方与使用方的成本影响相反'],
  ['财经','货币政策','利率决议|降准|降息|货币政策','银行、房地产开发','贷款定价与负债成本需分别核对'],
  ['财经','业绩','净利润|业绩预告|业绩快报','披露主体所属行业','观察利润变化有无经营现金流支持'],
  ['财经','并购重组','并购|重组|收购','交易双方所属行业','交割、对价及整合成本决定财务贡献'],
  ['财经','机器人','机器人|具身智能','自动化设备、通用设备','本体交付经零部件订单传导'],
  ['财经','光伏','光伏|硅料','光伏设备','硅料价格和组件出货决定链条利润分布'],
  ['社会','用电需求','用电|电力需求|耗电','电力、电网设备','用电增长可能带动利用小时与扩网投资'],
  ['社会','体育赛事','奥运|赛事|体育比赛','旅游及景区、体育','主办地客流与赛事订单是传导条件，海外赛事与A股关联较弱'],
  ['社会','消费','消费品|零售|消费者|消费需求','一般零售、食品加工','终端销量和折扣率共同决定零售利润'],
  ['社会','出行','出行|旅游|游客|客运','航空机场、旅游及景区','客流变化经客座率、票价传导至收入'],
  ['社会','医疗','就医|医保|医疗|药价','医疗服务、化学制药','支付标准与诊疗量影响医院、药企收入'],
  ['社会','住房','住房|房价|楼市','房地产开发、装修建材','销售回款领先于开工和建材需求'],
  ['社会','物价','物价|涨价|价格上涨|生活成本','一般零售、食品加工','原料涨价能否向终端转嫁决定利润'],
  ['社会','就业','就业|招聘|裁员','专业服务、一般零售','就业变化影响用工需求及居民消费能力'],
  ['社会','天气','寒潮|台风|暴雨|高温预警','电力、农业','天气通过用电负荷和农产品供给影响上市公司'],
  ['社会','交通','交通|道路|运输|港口','铁路公路、物流','运输量与运价影响收入，也改变下游交付成本'],
  ['社会','智能终端','手机|可穿戴|智能眼镜','消费电子','终端销量经备货节奏传导到零部件订单']
].map(([group,term,pattern,sectors,logic])=>({group,term,re:new RegExp(pattern,'i'),sectors,logic}));
const media = [
  {name:'财联社', domains:['cls.cn','caianpress.com'], providers:['每日复盘 · 财联社公开电报','财联社','财联社公开电报']},
  {name:'同花顺', domains:['10jqka.com.cn'], providers:['同花顺','同花顺财经']},
  {name:'东方财富', domains:['eastmoney.com'], providers:['东方财富','东方财富财经']}
];
function clean(v: unknown) { return String(v??'').replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim(); }
function newsResearch(report: ReportView) {
  const lookup = new Map(report.evidence_refs.map(r=>[r.id,r.source_name]));
  const days=at(report.dossier,'metrics','history_trade_days');
  const prior=Array.isArray(days)?days.map(String).filter(d=>d<report.source_trade_day).sort().pop():undefined;
  const seen=new Set<string>();
  const cutoff=Date.parse(report.as_of_time);
  const corpus=records(report.dossier.events).flatMap(e=>{
    if(e.kind!=='news') return [];
    if(!Number.isFinite(cutoff) || (e.published_at && !(Date.parse(String(e.published_at))<=cutoff)) || dayOf(e)>dayOf({published_at:report.as_of_time})) return [];
    const url=safeUrl(String(e.source_url??'')), host=url?new URL(url).hostname.toLowerCase():'';
    const refs=Array.isArray(e.evidence_refs)?e.evidence_refs.map(id=>lookup.get(String(id))):[];
    const source=media.find(m=>m.domains.some(d=>host===d||host.endsWith('.'+d))||(!host&&m.providers.some(s=>refs.includes(s))));
    const title=clean(e.title), day=dayOf(e), key=day+'|'+title.replace(/[\s【】]/g,'');
    const content=e.content_sufficient?clean(e.content):'';
    if(!source||!title||!content||seen.has(key)||!day) return [];
    seen.add(key);
    const body=title+' '+content, hits=topics.filter(t=>t.re.test(body));
    const macro=/降准|利率决议|货币政策|财政政策/.test(title)||/央行|国务院|证监会|财政部|发改委/.test(title)&&/印发|发布|出台|调整|决定|会议|实施|降息/.test(title);
    const catalyst=/订单|下修|上修|上调|下调|减产|投产|出台|获批|涨价|降价|价格涨幅|资本开支/.test(title);
    const domestic=/国内|中国|沪深|深市|沪市|A股/.test(title);
    const score=4*Number(macro)+3*Number(catalyst)+2*Number(domestic)+Math.min(2,hits.filter(t=>t.group==='财经').length);
    const summary=content.replace(/^【[^】]+】/,'').replace(/^财联社\d+月\d+日电[，、]?/,'').trim();
    return [{title,day,content,body,hits,url,source:source.name,time:String(e.published_at??day),score,summary,
      reason:[macro?'政策或利率传导':'',catalyst?'价格、订单或投资预期变化':'',domestic?'涉及境内市场':''].filter(Boolean).join('，')||'与产业链相关'}];
  });
  const today=corpus.filter(e=>e.day===report.source_trade_day&&e.content), before=corpus.filter(e=>e.day===prior&&e.content);
  const recognized = new Set(topics.map(t=>t.term));
  const extraTerms = records(report.dossier.sectors).map(s=>String(s.name??'')).filter(name=>
    name.length>=2 && name.length<=12 && !recognized.has(name) && !/昨日|涨停|昨日连板|融资融券|机构重仓/.test(name)
    && today.some(e=>e.title.includes(name)));
  const exactTopics = [...new Set(extraTerms)].map(term=>({group:'财经',term,re:new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i'),sectors:term,logic:'核对报道指向的成分股与次日板块反馈'}));
  const news=corpus.filter(e=>e.day>=report.source_trade_day&&e.score>=3&&e.content)
    .sort((a,b)=>b.score-a.score||b.time.localeCompare(a.time)||a.title.localeCompare(b.title)).slice(0,5);
  const words=(group:string)=>{
    const counts=[...topics,...exactTopics].filter(t=>t.group===group).map(t=>{
      const hits=today.filter(e=>t.re.test(e.body)), prev=before.filter(e=>t.re.test(e.body));
      const heat=Math.round(1000*hits.length/Math.max(1,today.length))/10;
      const change=before.length?Number((heat-Math.round(1000*prev.length/before.length)/10).toFixed(1)):null;
      // Read direction in the clause that mentions the term, never from the whole rolling bulletin.
      const clauses=hits.flatMap(e=>(e.title+'。'+e.content).split(/[：，。；！？\n]/).filter(c=>t.re.test(c)));
      const up=clauses.some(c=>/需求激增|订单排到|大幅上修|订单增长|获批|突破/.test(c)) || hits.some(e=>t.re.test(e.title)&&/订单排到/.test(e.title));
      const down=clauses.some(c=>/下修|削减|需求下滑|订单减少|出口限制/.test(c));
      const direction=up&&down?'利好与利空并存':up?'偏利好':down?'偏利空':'报道未指明方向';
      const story=hits.filter(e=>t.re.test(e.title)).sort((a,b)=>b.score-a.score||b.time.localeCompare(a.time))[0];
      return {heat,hits:hits.length,row:{报道数:hits.length,代表报道:story?.title,媒体:story?.source,链接:story?.url,热词:t.term,'热度分值（0-100）':heat,环比变化:change===null?'无上期对照':(change>0?'+':'')+change+' 分'+(change>0?' ↑':change<0?' ↓':''),
        '利好/利空':direction,'收益/受损板块':t.sectors+(direction==='报道未指明方向'?'（相关）':''),与股市关联:t.logic+'。涉及'+t.sectors}};
    });
    return counts.filter(x=>x.hits).sort((a,b)=>b.hits-a.hits||a.row.热词.localeCompare(b.row.热词,'zh')).slice(0,10).map((x,i)=>({排名:i+1,...x.row}));
  };
  return {news,financial:words('财经'),social:words('社会'),currentDocs:today.length,priorDocs:before.length,prior};
}

// Supplier pool industry labels are matched to SW 2021 level-two names.
// Only observed exact names and explicitly resolved supplier abbreviations participate.
const industryNames=new Set(('医疗服务 个护用品 化妆品 医疗美容 电力 燃气 航空机场 房地产开发 房地产服务 多元金融 国有大型银行 股份制银行 城商行 农商行 其他银行 证券 保险 一般零售 专业连锁 互联网电商 旅游零售 酒店餐饮 教育 体育 本地生活服务 旅游及景区 专业服务 农产品加工 渔业 种植业 养殖业 动物保健 汽车服务 摩托车及其他 汽车零部件 乘用车 商用车 计算机设备 黑色家电 白色家电 小家电 厨卫电器 照明设备 家电零部件 其他家电 食品加工 白酒 非白酒 饮料乳品 调味发酵品 休闲食品 服装家纺 饰品 文娱用品 家居用品 化学制药 生物制品 医药商业 中药 通信服务 林业 农业综合 饲料 化学纤维 化学原料 农化制品 化学制品 非金属材料 塑料 橡胶 纺织制造 造纸 医疗器械 半导体 其他电子 元件 光学光电子 电子化学品 消费电子 通信设备 水泥 玻璃玻纤 装修建材 房屋建设 装修装饰 工程咨询服务 基础建设 专业工程 电机 电网设备 光伏设备 风电设备 其他电源设备 电池 游戏 广告营销 影视院线 数字媒体 社交 出版 电视广播 煤炭开采 焦炭 油气开采 油服工程 炼化及贸易 冶钢原料 普钢 特钢 工业金属 贵金属 能源金属 小金属 金属新材料 通用设备 工程机械 自动化设备 轨交设备 专用设备 航天装备 航空装备 地面兵装 航海装备 军工电子 包装印刷 环境治理 环保设备 铁路公路 航运港口 物流 贸易 IT服务 软件开发 综合').split(' '));
const aliases: Record<string,string>={'炼化及贸':'炼化及贸易','金属新材':'金属新材料','计算机设':'计算机设备','农产品加':'农产品加工','其他电源':'其他电源设备','摩托车及':'摩托车及其他','电子化学':'电子化学品','家电零部':'家电零部件','光学光电':'光学光电子'};
function industry(raw:unknown) {const label=String(raw??'').replace(/Ⅱ$/,''); const key=aliases[label]??label;return industryNames.has(key)?key:null;}
function stockResearch(s:Row, day:string) {
  const fact=(key:string)=>n(at(s,'facts',key)), calc=obj(s.calculations), pool=obj(at(s,'facts','pool','value'));
  const close=fact('close'), vol=fact('vol'), amount=fact('amount'), ratio=n(calc.volume_to_prior5_mean);
  const avg=vol!==null && vol>0 && amount!==null && amount>0 ? amount*10/vol : null;
  const lo=n(at(calc,'support_resistance','support_candidate')), hi=n(at(calc,'support_resistance','resistance_candidate'));
  const pct=fact('pct_chg'), five=n(at(calc,'returns_pct','5')), flowRows=obj(s.fund_flows);
  const flows=(key:string)=>records(flowRows[key]).map(r=>obj(r.values)).find(v=>String(v.trade_date).replace(/-/g,'')===day.replace(/-/g,''));
  const ts=n(flows('tushare.moneyflow')?.net_mf_amount), dc=n(flows('tushare.moneyflow_dc')?.net_amount);
  const flow=ts??dc, open=n(pool.open_times), height=n(pool.limit_times), kind=String(pool.limit??'');
  const dailyBasic=records(at(s,'fundamentals','valuation')).map(v=>obj(v.values))
    .find(v=>String(v.trade_date).replace(/-/g,'')===day.replace(/-/g,''));
  const turn=n(pool.turnover_ratio) ?? n(dailyBasic?.turnover_rate);
  const businessLines=records(at(s,'fundamentals','business')).map(e=>obj(e.values)).filter(e=>e.bz_item&&e.end_date);
  const financialRows=obj(at(s,'fundamentals','financials'));
  const statement=records(financialRows['tushare.income']).map(e=>obj(e.values))[0];
  const disclosure=[businessLines[0]?'披露业务 '+text(businessLines[0].bz_item)+'（'+text(businessLines[0].end_date)+'）':'',
    statement&&n(statement.n_income_attr_p)!==null?'归母净利 '+decimal(Number(statement.n_income_attr_p)/1e8,'亿元')+'（'+text(statement.end_date)+'）':'',
    dailyBasic&&n(dailyBasic.pe_ttm)!==null?'PE(TTM) '+decimal(dailyBasic.pe_ttm)+'（'+text(dailyBasic.trade_date)+'）':''].filter(Boolean).join('，');
  const observed=n(at(calc,'support_resistance','observed_sessions'));
  const rangeGap=observed===null?'缺前20日高低价':observed<20?'前20日仅有 '+observed+' 日行情':'缺有效历史高低价';
  const position=close===null?'缺当日收盘价':hi===null||lo===null?rangeGap:close!==null&&hi!==null&&close>=hi?'收于前20日压力区上方':close!==null&&lo!==null&&close<lo?'收于前20日低点下方':'区间内观察';
  const relative=avg!==null&&avg>0&&close!==null?(close/avg-1)*100:null;
  const supply=turn!==null&&turn>=20?'换手超过两成，持仓换手较活跃，开板后须验证新增卖压。':relative!==null&&Math.abs(relative)<0.005?'收盘与当日成交均价基本持平。':relative!==null&&relative<0?'收盘低于当日成交重心，次日回到均价附近时观察解套卖压。':relative!==null&&relative>0?'收盘高于当日成交重心，回踩均价时观察这批成交能否得到承接。':'缺有效成交均价或收盘价。';
  const chip=avg===null?'当日成交均价缺失':('当日成交均价 '+decimal(avg,'元')+(close!==null?'，收盘较均价 '+decimal(100*(close/avg-1),'%'):''));
  const trend=kind==='U'?(open===null?'收盘封板，日榜未给开板次数':open===0?'封板未打开，次日先验溢价':'分歧回封，先验开板后的买盘'):kind==='D'?'跌停，先验卖盘能否被吸收':kind==='Z'?'触板未封，突破待重新确认':five!==null&&pct!==null?(pct>0&&five<0?'五日回落中的反弹':pct>0&&five>0?'短线延续上行':'价格承接待确认'):'历史窗口不足';
  const risk=[kind==='U'&&open!==null&&open>0?'盘中开板 '+open+' 次，次日再开时须看能否回封':'',flow!==null&&pct!==null&&pct>0&&flow<0?'价格上涨但资金净流出，留意冲高回落':'',kind==='D'?'卖出流动性受限':'',ratio!==null&&ratio<1?'成交量低于前五日均量，突破缺少量能确认':''].filter(Boolean).join('。')||'观察同板块跟随，个股冲高而同业回落时降低延续预期';
  return {code:text(s.code),name:text(s.name),disclosure,rawIndustry:String(pool.industry??''),industry:industry(pool.industry),close,pct,five,amount,ratio,avg,turn,lo,hi,flow,kind,open,height,trend,position,risk,
    firstTime:pool.first_time,lastTime:pool.last_time,verifiedUnsealed:calc.price_verified_limit_up===false,
    themes:Array.isArray(s.sectors)?s.sectors.map(String):[], chip:chip+'；'+(turn===null?'当日未披露换手率':'当日换手 '+decimal(turn,'%'))+'。'+supply,
    volume:'成交额 '+decimal(amount===null?null:amount/1e5,'亿元')+'；'+(ratio===null?'缺成交量五日对比':'成交量为此前五日均量的 '+decimal(ratio,' 倍'))+'；五日涨跌 '+decimal(five,'%')+'。'+(flow===null?'同日资金流缺失。':(ts!==null?'Tushare':'东财')+'净额 '+decimal(flow/1e4,'亿元')+'。'),
    take:close===null?'缺当日收盘价':hi===null?rangeGap:close>=hi?'已越过前20日高点 '+decimal(hi,'元')+'，若回落至该位下方且同业转弱，原突破理由减弱。':'前20日高点 '+decimal(hi,'元')+' 是上方供给观察位；接近时放量滞涨才构成止盈复核条件。',
    stop:kind==='U'&&avg!==null?'短线封板假设先看 '+decimal(avg,'元')+'（昨日成交均价）。打开涨停后跌穿且反抽无法收复、同业同步转弱，即复核退出。'+(lo===null?'':'较远的区间低点为 '+decimal(lo,'元')+'。'):
      close===null?'缺当日收盘价':lo===null?rangeGap:close<lo?'已跌破前20日低点 '+decimal(lo,'元')+'，先等重新站回，原支撑假设已经失效。':'前20日低点 '+decimal(lo,'元')+' 若被有效跌破且同业同步转弱，重新评估止损。'};
}
type Stock=ReturnType<typeof stockResearch>;
const business:[RegExp,string][]=[
  [/^电池$/,'核对动力与储能电芯的排产、单位售价和原料成本。出货上升时若售价降得更快，订单热度仍难转化为利润。'],
  [/^汽车服务$/,'经销商靠新车价差、售后及二手车周转取得毛利。销量回升还要与库存天数、终端折扣一起验证。'],
  [/^通信设备$/,'运营商与数据中心采购影响订单，设备交付、验收节奏决定收入；海外需求还要核对产品实际供货关系。'],
  [/^元件$/,'PCB交付与被动元件备货的节奏不同，按公司产品核对订单能见度、产能利用率与售价。'],
  [/^游戏$/,'新游上线与存量产品流水决定付费规模，渠道分成和买量费用决定利润；板块走强后优先核对产品流水。'],
  [/^电视广播$/,'广告投放决定传统媒体收入，版权与内容采购形成成本；收视增长能否变现需由广告单价与订单验证。'],
  [/零售|消费|食品|饮料|小家电|厨卫|白色家电|服装|休闲食品/,'终端销量先影响收入，折扣、原材料及渠道费用决定毛利能否跟上。'],
  [/游戏|传媒|影视|出版|数字媒体|电视广播|广告/,'内容上线或投放先产生流量，再经付费转化、分账比例与获客成本反映在利润。'],
  [/设备|元件|电池|半导体|电子|电机/,'下游资本开支先形成订单，排产、交付和验收决定收入确认；扩产时还要核对价格与毛利。'],
  [/金属|化学原料|化学制品|炼化|煤炭|燃气/,'产品售价与投入成本的价差决定吨利；库存重估可以先于经营现金流变化。'],
  [/房地产|水泥|玻璃|工程|建材/,'项目开工带动用量，施工进度决定结算；回款慢会削弱利润向现金流的转化。'],
  [/公路|物流|航运|航空/,'运量与运价共同影响收入，燃料与运力成本决定经营弹性。'],
  [/通信服务|IT服务|软件/,'新增客户与合同额先反映需求，续约、交付及回款决定实际利润。']
];
function grouped(stocks:Stock[]) {
  const groups=new Map<string,Stock[]>();
  for(const s of stocks) if(s.industry) { const list=groups.get(s.industry)??[];list.push(s);groups.set(s.industry,list); }
  return [...groups].map(([name,members])=>{
    const sealed=members.filter(s=>s.kind==='U'), broken=members.filter(s=>s.kind==='Z').length, down=members.filter(s=>s.kind==='D').length;
    const first=sealed.filter(s=>s.height===1).length, repeat=sealed.filter(s=>s.height!==null&&s.height>1).length;
    const ordered=[...members].sort((a,b)=>Number(b.kind==='U')-Number(a.kind==='U')||(b.height??0)-(a.height??0)||(b.amount??0)-(a.amount??0)||a.code.localeCompare(b.code));
    return {name,members:ordered,up:sealed.length,broken,down,first,repeat,
      reason:sealed.length+' 家封住（首板 '+first+'、连板 '+repeat+'），另有 '+broken+' 家炸板、'+down+' 家跌停。',
      logic:business.find(([re])=>re.test(name))?.[1]??'需以成分公司披露的主营收入、订单和回款核实板块需求。',
      continuity:down||broken?'内部已有负反馈。先看'+(broken?'炸板股能否修复':'跌停股能否打开')+'，再看封住的同业能否留下溢价；核心独强时维持局部判断。':
        first===0?'封板集中在连板股，未出现首板补充。次日看核心分歧后的承接和新的低位首板，只有高位晋级仍属局部强势。':
        repeat===0&&first>1?'多个首板同时出现，尚无连板确认。次日先看能否晋级二板，再看同业是否继续出现新首板。':
        sealed.length>1?'首板与连板并存。次日看首板溢价及新晋级家数，梯队收缩则降低延续预期。':'当前仅单点封板。次日出现同业跟随才支持扩散，独强回落则取消假设。'};
  }).filter(g=>g.up>0).sort((a,b)=>b.up-a.up||b.repeat-a.repeat||a.down-b.down||a.name.localeCompare(b.name,'zh'));
}
function sectorResearch(sectors:Row[],stocks:Stock[],pool:Row,day:string) {
  const fullDown=n(pool.down)!==null&&stocks.filter(s=>s.kind==='D').length===n(pool.down);
  return sectors.flatMap(s=>{
    const funds=records(s.funds).filter(row=>row.ts_code===s.code && String(row.trade_date).replace(/-/g,'')===day.replace(/-/g,''));
    const directory=n(at(s,'facts','pct_change')), change=directory??(funds.length===1?n(funds[0].pct_change):null);
    const breadth=n(at(s,'calculations','member_up_share_pct')), week=n(at(s,'calculations','returns_pct','5'));
    const flow=funds.length===1?n(funds[0].net_amount):null, flowShare=funds.length===1?n(funds[0].net_amount_rate):null;
    if(change===null) return [];
    const cap=(x:number)=>Math.max(-1,Math.min(1,x));
    const signals=[{weight:4, value:change/5}, {weight:3, value:breadth===null?null:(breadth-50)/50},
      {weight:2, value:flowShare===null?null:flowShare/10}, {weight:1, value:week===null?null:week/10}];
    const weight=signals.reduce((sum,sig)=>sum+(sig.value===null?0:sig.weight),0);
    const signed=signals.reduce((sum,sig)=>sum+(sig.value===null?0:sig.weight*cap(sig.value)),0)/weight;
    const positive=signed>=0, up=n(at(s,'calculations','member_limit_up_n'));
    const members=stocks.filter(st=>st.themes.includes(text(s.code)));
    const memberCount=n(at(s,'calculations','members'));
    const down=fullDown&&memberCount!==null&&memberCount>0?members.filter(st=>st.kind==='D').length:null;
    const rising=n(at(s,'facts','up_num')), falling=n(at(s,'facts','down_num'));
    const breadthText=breadth!==null?'已覆盖成分 '+decimal(breadth,'%')+' 上涨':
      rising!==null && falling!==null?'板块日榜 '+rising+' 涨、'+falling+' 跌':'缺成分涨跌家数';
    const limits=members.filter(m=>m.kind===(positive?'U':'D'));
    const breadthNote=breadth===0?'有行情的成分股全部未涨':breadth===100?'有行情的成分股全部上涨':breadthText;
    const evidence=[directory===null?'涨幅取自同日同代码资金榜':'',breadthNote,week!==null?'近五日累计 '+decimal(week,'%'):'',
      flow && change && (flow>0)!==(change>0)?(flow>0?'股价走低仍有资金净流入':'板块上涨但资金净流出'):''].filter(Boolean);
    let follow=positive?'看次日上涨家数能否超过下跌家数。':'看下跌家数能否连续收窄。';
    if (limits.length) follow=positive?`看${limits[0].name}开板后能否回封，同板块能否出现新涨停。`:`先看${limits[0].name}能否打开跌停，再看同业是否止跌。`;
    else if (flow && change && (flow>0)!==(change>0)) follow=flow>0?'有资金承接，次日收复今日跌幅才支持企稳。':'涨势与资金背离，次日观察净流出能否收窄。';
    else if (week!==null && change*week<0) follow=change>0?'五日仍跌，次日若回吐今日涨幅，反弹延续性偏弱。':'五日仍涨，观察回落是否扩散至板块内多数股票。';
    else if (breadth!==null) follow=positive?`观察次日上涨占比能否维持在今日 ${decimal(breadth,'%')} 附近。`:`上涨占比仅 ${decimal(breadth,'%')}，先看次日能否恢复到半数。`;
    return [{score:Math.abs(signed)*10,positive,week,change,breadth,breadthText,flow,name:text(s.name),members,row:{板块:text(s.name),
      '利好/利空':signed===0?'中性':positive?'盘面利好':'盘面利空',影响强度:decimal(Math.abs(signed)*10)+'/10',
      原因分析:evidence.join('。')+'。', 后续逻辑:follow,
      资金流向:flow===null?'无同日板块净额':decimal(flow/1e8,'亿元'),
      涨跌幅:decimal(change,'%'),'涨停/跌停':memberCount===null||memberCount===0?'缺当日成分明细':decimal(up)+' / '+decimal(down)}}];
  }).sort((a,b)=>b.score-a.score||a.name.localeCompare(b.name,'zh'));
}
export function research(report: ReportView) {
  const pool=obj(at(report.dossier,'market','limits','supplier_reported'));
  const stocks=records(report.dossier.stocks).map(s=>stockResearch(s,report.source_trade_day));
  const groups=grouped(stocks), sectors=sectorResearch(records(report.dossier.sectors),stocks,pool,report.source_trade_day);
  const relay=obj(at(report.dossier,'market','relay'));
  const relayRows=[['first_board','昨日首板'],['consecutive','昨日连板'],['broken','昨日炸板'],['failed','昨日断板']].filter(([key])=>obj(relay[key]).N!==undefined).map(([key,name])=>{
    const v=obj(relay[key]);
    function meanCell(session:'open'|'close') {
      const {value,note}=cohortMean(v,session);
      return value===null ? note : decimal(value,'%')+(note?'（'+note+'）':'');
    }
    function countCell(side:'up'|'down') {
      const count=n(v[side+'_limit_n']), valid=n(v[side+'_limit_valid_n']);
      if (valid===0 && n(v.N)!==0) return '缺限制价或报价';
      return count===null ? '—' : valid!==null && valid!==n(v.N) ? count+'（可判定 '+valid+' 只）' : count;
    }
    return {昨日样本:name,样本数:v.N,开盘均值:meanCell('open'),收盘均值:meanCell('close'),收盘中位:decimal(v.close_median_pct,'%'),
      今日涨停:countCell('up'),今日跌停:countCell('down')};
  });
  const first=obj(relay.first_board), failed=obj(relay.failed);
  const {value:mean,note}=cohortMean(first,'close'), median=n(first.close_median_pct);
  const relayText=mean!==null&&median!==null?'昨日首板'+(note?'（'+note+'）':'')+'平均 '+decimal(mean,'%')+'，中位数 '+decimal(median,'%')+'。'+(mean>0&&median<0?'少数强股拉高平均值，多数首板的隔日收盘反馈仍弱。':median>0?'典型首板样本仍有溢价，下一步核对晋级和开板情况。':'典型首板样本缺少隔日溢价。'):'';
  const knownDown=n(failed.down_limit_n), validDown=n(failed.down_limit_valid_n);
  const previousDay=String(relay.previous_trade_day || '上一交易日'), priorDay=String(relay.prior_trade_day || '前两个交易日');
  const failedText=knownDown!==null && n(failed.N)!==null && Number(failed.N)>0 && validDown!==0?
    priorDay+' 涨停榜中，有 '+failed.N+' 只在 '+previousDay+' 有成交、收盘未能续板。跟踪这组股票至 '+report.source_trade_day+'：'+(validDown!==null&&validDown!==n(failed.N)?'其中可核验 '+validDown+' 只，':'')+knownDown+' 只收盘价等于当日跌停价。':'';
  return {...newsResearch(report),stocks,groups,sectors,relayRows,relayText,failedText,classificationNote:unclassifiedNote(obj(relay.failed_classification))};
}
