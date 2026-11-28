import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { closingBrief } from './SessionBrief';
import { displayTime, records, text } from './model';
import type { ReportView } from './model';
import { research, decimal } from './reportResearch';
import { amount, detailTables, money, newsPoints, stockDetail } from './reportDetails';
import './paperReport.css';

type Row = Record<string, unknown>;
function num(v: unknown) {
  if ((typeof v !== 'string' && typeof v !== 'number') || String(v).trim() === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function fmt(v: unknown, unit = '', factor = 1) {
  const n = num(v);
  return n === null ? '—' : (n * factor).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + unit;
}
function cnDate(day: string) {
  const [y,m,d] = day.split('-');
  return y + '年' + Number(m) + '月' + Number(d) + '日';
}
function cell(key: string, value: unknown) {
  if (key === '代码') return text(value);
  if (key.endsWith('封板') && /^\d{5,6}$/.test(String(value))) {
    const t = String(value).padStart(6,'0');
    return t.slice(0,2) + ':' + t.slice(2,4);
  }
  return num(value) === null ? text(value) : fmt(value);
}
const FullBody = createContext(false);
function Table({ rows, columns, cap, empty = '本次没有符合条件的记录。' }: { rows: Row[]; columns: string[]; cap?: number; empty?: string }) {
  if (useContext(FullBody)) cap = undefined;
  const [all,setAll] = useState(false);
  const shown = cap && !all ? rows.slice(0,cap) : rows;
  return <><div className="pr-scroll"><table><thead><tr>{columns.map(c => <th key={c}>{c}</th>)}</tr></thead><tbody>{shown.map((r,i) => <tr key={i}>{columns.map(c => {
    const n = num(r[c]);
    const signed = /涨跌幅|日表现|净额|净流入/.test(c) && n !== null && n !== 0;
    return <td key={c} className={signed ? n > 0 ? 'pr-gain' : 'pr-loss' : undefined}>{cell(c,r[c])}</td>;
  })}</tr>)}{!rows.length&&<tr><td colSpan={columns.length} className="pr-missing">{empty}</td></tr>}</tbody></table></div>{cap && rows.length > cap && <button type="button" onClick={() => setAll(!all)}>{all ? '收起明细' : '展开全部 ' + rows.length + ' 条'}</button>}</>;
}
function Note({ name, children }: { name: string; children: ReactNode }) { return <p><strong className="pr-label">{name}：</strong>{children}</p>; }

const chapters = [
  ['market','大盘概览'], ['news','核心新闻'], ['hotwords','财经与社会热词'], ['sectors','市场板块'],
  ['stocks','重点板块与个股'], ['limits','涨停池'], ['concepts','封板行业'], ['risks','风险预警'],
] as const;

function ChapterNav() {
  const [current, setCurrent] = useState<string>(chapters[0][0]);
  useEffect(() => {
    const sections = chapters.map(([key]) => document.getElementById('plan-' + key));
    const initial = sections.find(section => section && '#' + section.id === location.hash);
    initial?.scrollIntoView({block:'start'});
    let frame = 0;
    function update() {
      frame = 0;
      let index = 0;
      for (let i = 1; i < sections.length; i++) {
        if ((sections[i]?.getBoundingClientRect().top ?? Infinity) > 80) break;
        index = i;
      }
      if (window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) index = sections.length - 1;
      setCurrent(chapters[index][0]);
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(update); }
    update();
    window.addEventListener('scroll', schedule, {passive:true, capture:true});
    window.addEventListener('resize', schedule);
    return () => {
      window.removeEventListener('scroll', schedule, true);
      window.removeEventListener('resize', schedule);
      cancelAnimationFrame(frame);
    };
  }, []);
  return <nav className="pr-chapters" aria-label="报告章节">
    <strong>报告目录</strong>
    <ol>{chapters.map(([key,label]) => <li key={key}><a href={'#plan-' + key} aria-current={current===key?'location':undefined}>{label}</a></li>)}</ol>
    <select aria-label="跳至报告章节" value={current} onChange={e => {
      const id = 'plan-' + e.target.value;
      if (location.hash === '#' + id) document.getElementById(id)?.scrollIntoView({block:'start'});
      else location.hash = id;
      setCurrent(e.target.value);
    }}>{chapters.map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select>
  </nav>;
}

export function PaperReport({report, navigation, modelPicker, fullBody=false}: {report: ReportView; navigation: ReactNode; modelPicker: ReactNode; fullBody?: boolean}) {
  const brief = closingBrief(report);
  const study = useMemo(()=>research(report),[report]);
  const get = (key: string) => report.sections.find(s => s.key === key)?.rows ?? [];
  const indices = get('market');
  const risks = get('risks');
  const market = report.dossier.market as {breadth?: Row; turnover?: Row; limits?: {supplier_reported?: Row; price_verified?: Row}} | undefined;
  const breadth = market?.breadth ?? {}, pool = market?.limits?.supplier_reported ?? {};
  const verified = market?.limits?.price_verified;
  const portfolio = (report.dossier.portfolio ?? {}) as Row;
  const sealed = study.stocks.filter(s => s.kind === 'U');
  const [direction,setDirection]=useState(fullBody?'全部':'重点');
  const leaders=study.sectors.filter(s=>s.positive && s.change>0).slice(0,3);
  const laggards=study.sectors.filter(s=>!s.positive && s.change<0).slice(0,3);
  const featured=new Set([...leaders,...laggards]);
  const sectorRows=study.sectors.map((s,i)=>({s,rank:i+1})).filter(({s})=>direction==='重点'?(featured.size?featured.has(s):true):direction==='全部'||s.row['利好/利空']===direction).map(({s,rank})=>{
    const example=s.members.filter(m=>m.flow!==null).sort((a,b)=>Math.abs(b.flow!)-Math.abs(a.flow!))[0];
    return {排名:rank,...s.row,资金流向:s.row.资金流向+(example?'；'+example.name+' '+money(example):'')};
  });
  const details = useMemo(()=>detailTables(study,pool,breadth),[study,pool,breadth]);
  const triggerFor = (industry:string) => study.news.find(e=>e.hits.some(t=>t.group==='财经' && t.sectors.split('、').includes(industry)));
  const constraints=(portfolio.constraints??{}) as Row;
  const capFor=(key:string)=>constraints.approved===true&&num(constraints[key])!==null?'已确认上限 '+fmt(constraints[key],'%',100):'未设定';
  const positionRows=study.groups.slice(0,5).map(g=>{
    const leader=g.members[0];
    const priorHigh = leader.hi !== null && leader.close !== null && leader.hi > leader.close;
    return {板块:g.name,入场节奏:leader.name+' · '+stockDetail(leader).plan,上方观察位:priorHigh?decimal(leader.hi,'元')+'（前高）':leader.hi!==null && leader.close!==null?'收盘已到前高，观察能否站稳':'—',下方观察位:leader.avg!==null && leader.close!==null && leader.avg<=leader.close?'成交均价 '+decimal(leader.avg,'元'):'—',风险提示:g.down?g.down+' 家跌停':g.broken?g.broken+' 家炸板':g.first===0?'没有新增首板':'观察首板次日能否连板'};
  });
  const portfolioNote=Array.isArray(portfolio.limitations)?portfolio.limitations.map(text).join(' '):'';
  const calendar=risks.filter(r=>r['类别']==='已公告日程').map(r=>{
    const subject=String(r['对象']??'');
    const st=study.stocks.find(s=>s.code===subject);
    return {时间:r['日期'],事件:subject+' · '+text(r['观察事项']),影响板块:st?.industry??'未提供行业',重要性:st?'优先核验 · 涉及观察标的':'按公告日期跟踪'};
  });
  const nextChecks=(brief?.checks??[]).map(c=>({日期:report.target_trade_day??'下一交易日',观察对象:c.title,依据:c.why,验证条件:c.verify,放弃条件:c.reject}));
  const indexLevels=indices.filter(r=>Number(r['已覆盖交易日'])===20&&num(r['收盘'])!==null&&num(r['近20日收盘最低'])===num(r['收盘'])).map(r=>({关键位:text(r['指数'])+' · '+fmt(r['收盘'],'点'),当前状态:'收在近20日最低收盘位',破位影响:'再创新低时，指数企稳假设减弱',应对措施:'与下跌家数、成交额交叉验证，观察反抽能否收回该位。'}));
  const comments = (key: string) => report.sections.find(s=>s.key === key)?.analysis?.map((a,i)=><Note key={i} name={a.title}>{a.text}</Note>);
  return <FullBody.Provider value={fullBody}><div className="pr-ground"><div className="pr-layout">{!fullBody && <ChapterNav/>}<article className="pr-paper" data-report-id={report.id} aria-label="明日预案报告正文">
    <div className="pr-heading">
      <h1>{report.target_trade_day ? cnDate(report.target_trade_day) + ' A股交易预案' : 'A股交易预案 · 缺下一交易日历'}</h1>
      {navigation}
    </div>
    {modelPicker}
    <aside className="pr-lead"><Note name="复盘日期">{cnDate(report.source_trade_day)}收盘　｜　资料截止 {displayTime(report.as_of_time)}（北京时间）</Note>
      <Note name="核心判断">{brief?.title ?? report.summary}</Note>
      {brief?.strongest && <Note name="重点方向">{text(brief.strongest['板块'])}上涨 {fmt(brief.strongest['当日涨跌幅（%）'],'%')}，已覆盖成分中 {fmt(brief.strongest['上涨成分占比（%）'],'%')} 上涨。</Note>}
      {brief?.narrow && <Note name="分歧提示">{text(brief.narrow['板块'])}上涨 {fmt(brief.narrow['当日涨跌幅（%）'],'%')}，但只有 {fmt(brief.narrow['上涨成分占比（%）'],'%')} 的已覆盖成分上涨。</Note>}
          {(study.relayText||study.failedText)&&<Note name="机会与亏损集中处">{study.relayText} {study.failedText}</Note>}
    </aside>
    <section id="plan-market" tabIndex={-1}><h2>一、大盘概览</h2>
      <Table rows={indices.map(r=>({指数:r['指数'],收盘:r['收盘'],'涨跌幅（%）':r['涨跌幅（%）'],'成交额（亿）':fmt(r['成交额（千元）'],'',0.00001)}))} columns={['指数','收盘','涨跌幅（%）','成交额（亿）']}/>
      <Note name="涨跌家数">{fmt(breadth.up)} 涨 / {fmt(breadth.down)} 跌 / {fmt(breadth.flat)} 平　｜　上涨占比 {fmt(breadth.up_share_pct,'%')}</Note>
      <Note name="沪深供应商收盘榜">{report.source_trade_day}　涨停 {fmt(pool.up)} 家 / 跌停 {fmt(pool.down)} 家 / 炸板 {fmt(pool.broken)} 家（当日触及涨停、收盘未封住）。</Note>
      {verified && <Note name="沪深收盘价核验">可比 {fmt(verified.valid_n)} 只：涨停 {fmt(verified.up)} 家 / 跌停 {fmt(verified.down)} 家。上方断板样本的跌停数按收盘价核验，不按供应商榜单计数。</Note>}
      <Note name="沪深成交额">{fmt(market?.turnover?.amount_yuan,' 亿元',0.00000001)}</Note>
      {brief?.checks[0] && <Note name="大盘判断">{brief.checks[0].why} {brief.checks[0].verify}</Note>}{comments('market')}
      <h3>隔日接力反馈</h3><Table rows={study.relayRows} columns={['昨日样本','样本数','开盘均值','收盘均值','收盘中位','今日涨停','今日跌停']}/>
      {study.classificationNote && <small>{study.classificationNote}。</small>}
      <h3>1.1　指数近20日位置</h3><Table rows={indices} columns={['指数','收盘','近20日收盘最低','近20日收盘最高','已覆盖交易日']}/>
    </section>
    <section id="plan-news" tabIndex={-1}><h2>二、今日财经核心热点新闻摘要</h2>
      {study.news.map((e,i)=><div className="pr-story" key={e.title}><h3>{i+1}. {e.title} <span className="pr-priority">{e.score>=7?'优先关注':e.score>=5?'重点跟踪':'跟踪'}</span></h3>
        <ul className="pr-facts">{newsPoints(e.summary,e.title).map(line=><li key={line}>{line}</li>)}</ul>
        <Note name="主题关联（规则推断）">{e.hits.filter(t=>t.group==='财经').slice(0,2).map(t=>t.sectors+' · '+t.logic).join('。')||e.reason}。</Note>
        <small>{e.url?<a href={e.url} target="_blank" rel="noreferrer">{e.source}原文</a>:e.source}　｜　{e.time.length>10?displayTime(e.time):e.day}</small></div>)}
      {!study.news.length&&<p>截至本稿时点，三个指定来源中没有符合重要性条件的已存正文。</p>}
      <small>财联社、同花顺、东方财富。优先考虑政策传导、境内影响、订单和价格变化，最多五条。盘后消息供次日验证。</small>
    </section>
    <section id="plan-hotwords" tabIndex={-1}><h2>三、财经高频热词与社会关联热词分析</h2><h3>3.1　财经高频热词 TOP10</h3>{study.financial[0] && <div className="pr-word-lead"><b>{study.financial[0].热词}</b> 出现在 {study.financial[0].报道数} 篇报道中。{study.financial[0].代表报道 && <span>{study.financial[0].媒体}报道《{study.financial[0].代表报道}》</span>}</div>}<Table rows={study.financial} columns={['排名','热词','热度分值（0-100）','环比变化','利好/利空','收益/受损板块']}/>
      <h3>3.2　社会关联热词 TOP10</h3>{study.social[0] && <div className="pr-word-lead"><b>{study.social[0].热词}</b> 出现在 {study.social[0].报道数} 篇报道中。{study.social[0].代表报道 && <span>{study.social[0].媒体}报道《{study.social[0].代表报道}》</span>}</div>}<Table rows={study.social} columns={['排名','热词','热度分值（0-100）','环比变化','与股市关联']}/>
      <details className="pr-method"><summary>热度怎么算</summary><small>按财联社、同花顺、东方财富留存正文计分。每个主题每篇只计一次，报道占比每增加 1%，热度增加 1 分，保留一位小数。{report.source_trade_day} 共 {study.currentDocs} 篇，{study.prior?'前一交易日 '+study.prior+' 共 '+study.priorDocs+' 篇。':'本稿缺前一交易日语料。'}环比为两日分差。社会议题反映这份财经语料的关注度。</small></details></section>
    <section id="plan-sectors" tabIndex={-1}><h2>四、市场板块分析</h2><h3>4.1　板块影响强度量化（0–10分）</h3>{(leaders.length>0 || laggards.length>0) && <aside>{leaders.length>0 && <Note name="走强">{leaders.map(s=>`${s.name} +${fmt(s.change,'%')}`).join('、')}</Note>}{laggards.length>0 && <Note name="承压">{laggards.map(s=>`${s.name} ${fmt(s.change,'%')}`).join('、')}</Note>}</aside>}<div className="pr-directions" role="group" aria-label="板块影响方向">{['重点','盘面利好','盘面利空','全部'].map(d=><button type="button" key={d} aria-pressed={direction===d} onClick={()=>setDirection(d)}>{d}</button>)}</div><Table empty={study.sectors.length?'没有符合当前方向的板块。':'本稿缺当日板块涨跌幅。'} rows={sectorRows} columns={['排名','板块','利好/利空','影响强度','原因分析','后续逻辑','资金流向','涨跌幅','涨停/跌停']} cap={10}/>
      <details className="pr-method"><summary>评分依据</summary><small>覆盖供应商的行业、概念和行情分组；涨跌幅缺失时，仅使用同日同代码且唯一的资金榜记录补充。盘面影响按方向与强度分别呈现。日涨跌、成分广度、净流入占比、五日涨跌的权重为 4∶3∶2∶1；分别以 ±5%、上涨面偏离一半 ±50 个百分点、±10%、±10% 为满幅，截断后取加权均值的绝对值乘 10。缺项只使用其余指标计分。缺成分明细时列出日榜涨跌家数，该家数不参与广度计分。涨跌停按已存同日成分与供应商池交叉核对。</small></details>
      <h3>4.2　涨停样本的行业代表股观察</h3><Table rows={positionRows} columns={['板块','入场节奏','上方观察位','下方观察位','风险提示']}/>{constraints.approved===true&&num(constraints.max_sector_weight)!==null&&<Note name="单板块仓位上限">{fmt(constraints.max_sector_weight,'%',100)}</Note>}{num(portfolio.invested_weight)!==null&&<Note name="当前仓位">{fmt(portfolio.invested_weight,'%',100)}</Note>}{comments('sectors')}</section>
    <section id="plan-stocks" tabIndex={-1}><h2>五、重点板块深度分析与观察标的</h2>
      <small>按二级行业内封板家数、连板家数排序，优先展开前五个方向。每股价位取前 20 日未复权高低点，适用前需核对除权。止盈止损栏列出复核条件。</small>
      {study.groups.slice(0,5).map((g,i)=><div className="pr-story" key={g.name}><h3>5.{i+1}　{g.name}</h3>
        <Note name="需求与盈利">{g.logic}</Note><Note name="板块强弱">{g.reason} {details.groupFlow(g)}。</Note><Note name="后续验证">{g.continuity}</Note>
        {triggerFor(g.name)&&<Note name="相关报道（仅主题匹配）">{triggerFor(g.name)!.title}。观察消息能否得到本组个股的成交与同业跟随验证。</Note>}
        {g.members.filter(s=>s.kind==='U').map((s,j)=><div className="pr-stock" key={s.code}><h4>{j+1}. {s.name}（{s.code}） ｜ 收盘 {decimal(s.close,'元')} ｜ {decimal(s.pct,'%')} ｜ 成交 {amount(s)} ｜ 资金净额 {money(s)}</h4>
          <Note name="标的地位">{decimal(s.height)} 板，{s.trend}。{g.up>1?'同业对照 '+g.members.filter(p=>p.kind==='U'&&p.code!==s.code).slice(0,2).map(p=>p.name).join('、')+'；核对分歧后谁先恢复。':'本组仅此一只封住，带动范围待验证。'}</Note>
          {s.disclosure&&<Note name="公司披露">{s.disclosure}</Note>}<Note name="筹码与换手">{s.chip}</Note><Note name="量价与资金">{s.volume} {s.position}。</Note>
          <Note name="量价阶段">{stockDetail(s).phase}，{stockDetail(s).volume}。</Note><Note name="操作预案">{stockDetail(s).plan}</Note><Note name="止盈观察">{s.take}</Note><Note name="止损观察">{s.stop}</Note><Note name="个股风险">{s.risk}。</Note></div>)}</div>)}
      {!study.groups.length&&<p>本稿缺少可匹配二级行业的封板记录。</p>}
      <h3>5.6　标的量价关系分析</h3><Table rows={details.volumeRows} columns={['股票','价格','主力资金','量价阶段','关键支撑','关键压力','量能健康度','成交均价','当日换手','资金判断']} cap={12}/>
      <small>本稿没有历史持仓成本分布，个股列出可实测的成交均价与换手率。关键位为此前20日未复权低点和高点；量能以此前五日均量为基准。威科夫吸筹或派发需完整价格过程验证，本表仅列可观察的量价阶段。</small>
      <h3>5.7　趋势预测与操作建议</h3><Table rows={details.forecasts} columns={['股票','趋势预测','操作建议','预警信号']} cap={8}/>{comments('stocks')}</section>
    <section id="plan-limits" tabIndex={-1}><h2>六、涨停池全景分析（{cnDate(report.source_trade_day)} · {fmt(pool.up)}只涨停）</h2>
      <aside><Note name="筛选顺序">先按行业封板家数和梯队比较扩散，再看个股开板次数、成交额和资金分歧。涨停身份采用供应商当日披露口径。</Note></aside>
      <h3>6.1　按板块分类统计</h3><Table rows={details.counts} columns={['板块','涨停数','占比','代表标的','资金方向']} cap={10}/>
      <h3>6.2　前排板块封板明细</h3>{study.groups.slice(0,3).map(g=><div key={g.name}><h4>{g.name}（{g.up}只）</h4><Table rows={g.members.filter(s=>s.kind==='U').map(details.stockRow)} columns={['标的','代码','涨幅','成交额','核心逻辑','质量评级']}/></div>)}
      <h3>6.3　连板梯队总览</h3><Note name="梯队结构">首板 {fmt(pool.first_board)} 家，二板 {fmt(pool.second_board)} 家，三板及以上 {fmt(pool.third_plus)} 家。炸板率 {fmt(pool.broken_rate_pct,'%')}。</Note><Table rows={details.ladder} columns={['连板高度','标的','代码','板块','涨幅','成交额','封板质量','性质判断']} cap={12}/>
      <h3>6.4　涨停池精选观察</h3><aside>从封板家数居前的二级行业各取一只，最多三只。行业内优先开板少、成交额大的标的，两项相同时再看连板高度。</aside>
      {details.leaders.map(({stock:s,group:g},i)=><div className="pr-stock" key={s.code}><h4>{i+1}. {s.name}（{s.code}） ｜ {g.name} ｜ 涨幅 {decimal(s.pct,'%')} ｜ 成交 {amount(s)}</h4>
        <Note name="入选依据">{stockDetail(s).seal}，{stockDetail(s).funds}。行业内 {g.up} 家封住，{g.broken} 家炸板、{g.down} 家跌停。</Note>
        <Note name="量价与筹码">{stockDetail(s).phase}，{stockDetail(s).volume}。{s.chip}</Note><Note name="次日预案">{stockDetail(s).plan} {s.take}</Note><Note name="失效与风险">{s.stop} {s.risk}。</Note></div>)}
      {!details.leaders.length&&<p>封板过程或成交资料不足，暂无入选标的。</p>}
      <h3>6.5　未涨停但走势强势标的</h3><Table rows={details.strong} columns={['标的','代码','板块','涨幅','成交额','状态','后续策略']} cap={8}/>
      <h3>6.6　下一波板块观察</h3><Table rows={details.following} columns={['板块','观察逻辑','观察标的','量价分析']}/>
      <h3>6.7　近期板块趋势分析</h3><Table empty="缺少可核验的板块五日涨跌幅，不判断趋势。" rows={details.trends} columns={['板块','近5日趋势','当前阶段','风险提示']} cap={10}/></section>
    <section id="plan-concepts" tabIndex={-1}><h2>七、涨停股的二级行业分布</h2><h3>7.1　按二级行业归类的热点 TOP10</h3>
      <small>使用当日涨跌停池披露的个股行业，与<a href="https://wxweb.swsresearch.com/swsreport/2021_08/328340.pdf" target="_blank" rel="noreferrer">申万二级行业名称</a>匹配。热度衡量封板集聚度，排序按封板家数、连板家数、较少跌停依次比较。每只股票只归入一个行业，未匹配者跳过。覆盖 {study.stocks.filter(s=>s.industry&&s.kind==='U').length} / {sealed.length} 只已存封板股。</small>
      <Table rows={study.groups.slice(0,10).map((g,i)=>({排名:i+1,二级板块:g.name,热度:decimal(g.up/sealed.length*100,'%'),今日表现:g.reason,高热度原因:g.members.filter(s=>s.kind==='U').slice(0,2).map(s=>s.name+' '+decimal(s.height)+'板').join('、')+'。'+details.groupFlow(g),产业逻辑:g.logic+(triggerFor(g.name)?' 相关报道：'+triggerFor(g.name)!.title:''),持续性:g.continuity}))} columns={['排名','二级板块','热度','今日表现','高热度原因','产业逻辑','持续性']}/><small>热度列为该行业占当日已存封板股的比例。</small>
      <h3>7.2　分行业观察标的</h3>{study.groups.slice(0,5).map(g=><div className="pr-concept" key={g.name}><h4>{g.name}</h4><Table rows={g.members.filter(s=>s.kind==='U').map(s=>({标的:s.name,代码:s.code,逻辑:s.trend+'，'+stockDetail(s).funds+'。'+stockDetail(s).plan,业绩分析:s.disclosure||'尚无可核验的公司财务披露；板块订单与个股盈利需分别验证。'}))} columns={['标的','代码','逻辑','业绩分析']}/></div>)}</section>
    <section id="plan-risks" tabIndex={-1}><h2>八、全市场风险预警</h2><h3>8.1　风险等级 · {details.riskLevel}{details.riskScore===null?'':'（'+details.riskScore+'/10）'}</h3>
      <p>{brief?.title??'缺当日市场涨跌统计'}。上涨面 {fmt(breadth.up_share_pct,'%')}，涨停 {fmt(pool.up)} 家、跌停 {fmt(pool.down)} 家，炸板率 {fmt(pool.broken_rate_pct,'%')}。</p><small>取未上涨占比、跌停占涨跌停总数比例和炸板率的均值，换算至十分并取整。0–3 为较低、4–6 为中等、7–10 为较高；分数描述收盘压力，不是次日下跌概率。</small>
      <h3>8.2　追高预警</h3><Table rows={details.chase} columns={['预警标的','当前状态','预警原因','建议']} cap={8}/>
      <h3>8.3　破位预警</h3><Table rows={[...indexLevels,...details.levels]} columns={['关键位','当前状态','破位影响','应对措施']} cap={8}/>
      <h3>8.4　流动性风险</h3><ul className="pr-facts"><li>沪深成交额 {fmt(market?.turnover?.amount_yuan,'亿元',1e-8)}，较前交易日 {fmt(market?.turnover?.change_pct,'%')}。</li><li>沪深供应商榜单：触板未封 {fmt(pool.broken)} 家，跌停 {fmt(pool.down)} 家。观察次日跌停股能否恢复成交，以及反弹时成交额是否跟上。</li></ul>
      <h3>8.5　仓位与现金约束</h3><Table rows={[{配置范围:'当前组合',仓位:fmt(portfolio.invested_weight,'%',100),配置方向:'按已录入持仓估算',现金比例:fmt(portfolio.cash_weight,'%',100)},{配置范围:'组合风险约束',仓位:capFor('max_total_weight'),配置方向:'单股 '+capFor('max_stock_weight')+'；单板块 '+capFor('max_sector_weight'),现金比例:'依实际配置'}]} columns={['配置范围','仓位','配置方向','现金比例']}/>{records(portfolio.allocations).length>0&&<Table rows={records(portfolio.allocations).map(r=>({持仓:text(r.code),组合占比:fmt(r.weight,'%',100)}))} columns={['持仓','组合占比']}/>}<small>{portfolioNote}</small>
      <Table rows={[{风险偏好:'进取型',观察顺序:'先看连板核心与同业首板能否共同留住溢价。',降低风险的条件:'高标反复开板、同业跌停扩大。'},{风险偏好:'稳健型',观察顺序:'先等板块上涨面和资金流同时改善，再比较容量股承接。',降低风险的条件:'指数反弹但上涨家数未改善。'},{风险偏好:'保守型',观察顺序:'先核对跌停家数、指数前低及成交恢复，再评估参与条件。',降低风险的条件:'指数续创新低且跌停卖盘延续。'}]} columns={['风险偏好','观察顺序','降低风险的条件']}/>
      <h3>8.6　后续关注要点</h3><Table empty="本稿收录的公告中没有列明后续日期的事件。" rows={calendar} columns={['时间','事件','影响板块','重要性']} cap={6}/>
      {nextChecks.length>0&&<><h4>下一交易日盘面验证</h4><Table rows={nextChecks} columns={['日期','观察对象','依据','验证条件','放弃条件']}/></>}{comments('risks')}</section>
    <aside className="pr-end"><Note name="报告生成时间">{displayTime(report.created_at)}</Note><Note name="数据来源">{[...new Set(report.evidence_refs.map(r=>r.source_name).filter(Boolean))].join(' / ') || '来源未列示'}。完整出处见研究底稿。</Note></aside>
  </article></div></div></FullBody.Provider>;
}
