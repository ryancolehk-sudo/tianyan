import { useEffect, useState } from 'react';
import type { PropsWithChildren } from 'react';
import { Card, Empty, EmptyTable, Segmented } from '../../shared/ui';
import { Icon } from '../../shared/icons';
import { ResearchToolbar } from './ResearchToolbar';
import { ChartEmpty, Metrics } from '../../shared/MarketPlaceholders';
import type { SessionResponse } from '../../generated/schema';
import { ResearchRecords } from './Records';
import { researchNames } from './recordTypes';
import { ArchiveDatePicker, ArchiveReader, updateReviewLocation, useReviewLocation } from './ArchiveReader';
import { updateLocation } from '../../shared/navigation';

function ViewCard({ title, label, options, children, className = '' }: PropsWithChildren<{ title: string; label: string; options: string[]; className?: string }>) {
  const [selected, setSelected] = useState(options[0]);
  return <Card title={title} className={className}><Segmented label={label} options={options} value={selected} onChange={setSelected}/>{children}</Card>;
}

function JudgementFields({ labels }: { labels: string[] }) {
  return <div className="judgement-fields">{labels.map(label => <div key={label}><span>{label}</span><strong>摘要尚未提供</strong><small>人工判断请查看当日记录</small></div>)}</div>;
}

function DailyReview({ create, writable }: { create: () => void; writable: boolean }) {
  const [perspective, setPerspective] = useState('我的判断');
  return <div className="research-layout daily-layout">
    <Card title="市场环境与情绪判断" className="review-environment"><div className="environment-layout"><div className="judgement-panel"><Segmented label="情绪判断来源" options={['我的判断', 'AI 参考']} value={perspective} onChange={setPerspective}/><JudgementFields labels={perspective === '我的判断' ? ['市场周期阶段', '今日情绪变化'] : ['AI 情绪参考', '证据与模型版本']}/><p className="feature-note">{perspective === '我的判断' ? '人工复盘可保存并保留每次修订，行情与 AI 保持未接入。' : 'AI 未接入，不生成默认分析。'}</p><button disabled={!writable || perspective !== '我的判断'} onClick={create}>记录我的判断</button></div><div className="emotion-context"><div className="subsection-heading"><h3>最近交易日情绪轨迹</h3><span className="badge muted">结构化轨迹尚未提供</span></div><div className="emotion-track"><span>阶段</span><div/><span>交易日</span></div><p className="empty-inline">已保存的复盘正文见当日记录，此处尚不汇总情绪轨迹。</p><h3>关键情绪指标</h3><Metrics labels={['昨日涨停溢价', '连板晋级率', '首板晋级率', '炸板率', '成交额']}/></div></div></Card>
    <ViewCard title="指数走势与市场成交" label="指数观察视图" options={['指数走势', '涨跌分布']} className="review-index"><ChartEmpty/><Metrics labels={['上涨家数', '下跌家数', '涨停', '跌停', '炸板', '成交额']}/></ViewCard>
    <ViewCard title="主要板块表现" label="板块排序" options={['涨幅榜', '成交额榜', '资金流向']} className="review-sectors"><EmptyTable columns={['排名', '板块名称', '涨幅', '成交额', '涨停数', '强度', '我的标记']} title="板块行情未接入">没有可用行情，当前不能排序或判断主线。</EmptyTable></ViewCard>
    <ViewCard title="涨跌效应分布" label="涨跌效应视图" options={['涨跌分布', '涨跌家数']} className="review-distribution"><ChartEmpty title="市场分布未知" note="无数据时不绘制合成柱状图。"/></ViewCard>
    <Card title="今日涨停梯队" className="review-ladder"><EmptyTable columns={['梯队', '数量', '代表个股']} title="梯队数据未接入">连板数与代表个股均未知。</EmptyTable></Card>
    <ViewCard title="核心题材与重点个股" label="研究题材分类" options={['主线题材', '次主线', '新方向', '轮动', '待观察']} className="review-core"><EmptyTable columns={['代码', '名称', '所属题材', '今日表现', '市场地位', '我的标记', '操作']} title="题材汇总尚未提供">人工复盘记录见下方列表；题材行情与结构化规则暂未接入。</EmptyTable></ViewCard>
    <Card title="今日关键事件与市场映射" className="review-events"><EmptyTable columns={['时间', '关键事件', '来源', '关联题材']} title="资讯源未接入">事件需保留原始发布时间、来源与市场证据。</EmptyTable></Card>
    <Card title="昨日预案验证" className="review-validation"><div className="reference-note"><Icon name="info"/> 按原预案目标日与精确情景核对</div><p>收盘后引用材料记录支持、反证或缺失原因；每次更正保留原文。</p><a className="text-link" href="/research/validations">进入次日验证 <Icon name="arrow"/></a></Card>
  </div>;
}

function Directions({ create, writable }: { create: () => void; writable: boolean }) {
  const [perspective, setPerspective] = useState('我的判断');
  const [cycle, setCycle] = useState('全部');
  return <div className="research-layout directions-layout">
    <Card title="市场主线结构" className="directions-structure"><Segmented label="方向判断来源" options={['我的判断', 'AI 参考']} value={perspective} onChange={setPerspective}/><div className="direction-judgement"><JudgementFields labels={['主线方向', '次主线方向', '新方向关注', '轮动题材']}/><div className="overall-judgement"><h3>{perspective === '我的判断' ? '我的整体判断' : 'AI 参考判断'}</h3><Empty title={perspective === '我的判断' ? '方向摘要尚未提供' : 'AI 未接入'}>已保存的方向正文与材料引用见当日记录，不自动推断你的判断。</Empty><button disabled={!writable || perspective !== '我的判断'} onClick={create}>记录我的判断</button></div></div></Card>
    <ViewCard title="题材表现与市场关注" label="题材表现维度" options={['题材表现', '涨幅', '成交额', '涨停数', '市场广度']} className="directions-themes"><EmptyTable columns={['题材名称', '涨幅', '涨停数', '成交额', '我的判断', '查看']} title="题材行情未接入">行情事实与个人标记分别记录，当前无可用数据。</EmptyTable></ViewCard>
    <Card title="题材生命周期与梯队结构" className="directions-cycle"><Segmented label="题材生命周期分类" options={['全部', '主线', '次主线', '新方向', '轮动']} value={cycle} onChange={setCycle}/><div className="cycle-layout"><div className="cycle-chart"><span className="cycle-y">阶段位置</span><Empty title="生命周期未知">生命周期汇总尚未提供，已保存的方向正文见当日记录。</Empty><span className="cycle-x">持续性：弱 → 强</span></div><EmptyTable columns={['梯队', '代表股', '涨幅', '地位']} title="梯队未接入"/></div></Card>
    <ViewCard title="核心个股跟踪" label="核心跟踪分类" options={['全部', '主线', '次主线', '新方向', '待观察']} className="directions-core"><EmptyTable columns={['代码', '名称', '题材', '现价', '涨幅', '地位', '备注']} title="核心跟踪汇总尚未提供">文字方向和材料引用可在下方记录保存；标的选择需已有基础资料。</EmptyTable><p className="feature-note">个人判断不作为市场原始事实。</p></ViewCard>
    <ViewCard title="重要事件与催化因素" label="催化分类" options={['全部', '已有关联', '待核查']} className="directions-events"><EmptyTable columns={['时间', '题材', '事件内容', '市场影响', '关联股']} title="事件来源未接入"/></ViewCard>
    <ViewCard title="板块成交与联动" label="板块联动视图" options={['成交额对照', '涨停扩散']} className="directions-volume"><ChartEmpty title="板块联动未知" note="成交额与资金净流入是不同指标，当前均未接入。"/></ViewCard>
    <ViewCard title="我的观察清单" label="观察清单分类" options={['持续跟踪', '新增关注', '风险提示']} className="directions-watch"><Empty title="观察清单尚未开放">观察清单分类尚未开放；方向正文与证据引用已可在当日记录保存。</Empty></ViewCard>
    <Card title="AI 参考结论" className="directions-ai"><span className="badge muted">AI 未接入</span><Empty title="暂无 AI 参考">模型、证据与授权未配置，不生成模板观点。</Empty></Card>
  </div>;
}

function Plans() {
  const [category, setCategory] = useState('全部');
  return <><div className="plan-context"><span>市场阶段 <b>—</b></span><span>研究主题 <b>请在预案中明确绑定</b></span><span>目标交易日 <b>以预案绑定日历为准</b></span><span className="badge pending">监控未启用</span></div><div className="research-layout plans-layout">
    <Card title="今日研究前提" className="plan-premise" action={<span className="secondary">来自复盘与方向版本</span>}><JudgementFields labels={['市场阶段', '主线判断', '核心观察', '风险倾向']}/><p className="feature-note">在下方预案记录中查看已绑定的复盘与方向版本。<a href="/research/reviews">查看每日复盘</a> · <a href="/research/directions">查看板块轮动</a></p></Card>
    <Card title="下一交易日关键锚点" className="plan-anchors" action={<button disabled><Icon name="plus"/> 添加锚点</button>}><EmptyTable columns={['关键问题', '关联对象', '验证方式', '重要性', '状态']} title="锚点汇总尚未提供">实际预案目标日按明确选择的日历核对，在下方记录中查看。</EmptyTable></Card>
    <Card title="观察对象与验证条件" className="plan-observations" action={<button disabled>添加观察对象</button>}><Segmented label="预案观察分类" options={['全部', '核心观察', '潜在机会', '低位观察']} value={category} onChange={setCategory}/><div className="plan-empty-columns">{['观察对象', '验证条件', '事实与引用'].map(name => <div key={name}><h3>{name}</h3><Empty title="行情汇总尚未接入">人工预案见下方记录列表，行情接入后再显示相关事实。</Empty></div>)}</div><p className="feature-note">实时监控未启用，不执行交易；人工条件与材料引用在下方预案记录中保存。</p></Card>
    <Card title="次日条件分支" className="plan-scenarios"><div className="scenario-labels">{['超预期', '符合预期', '不及预期', '环境恶化'].map(name => <span key={name}>{name}</span>)}</div><EmptyTable columns={['情景类型', '核心条件', '可能含义', '观察应对', '关联对象']} title="情景汇总尚未提供">已保存的情景与触发条件请查看当日预案记录；此处不生成默认交易建议。</EmptyTable></Card>
    <Card title="风险边界与研究失效条件" className="plan-risks"><EmptyTable columns={['失效条件', '触发证据', '复核动作', '风险范围']} title="风险汇总尚未提供">已保存的失效条件请查看当日预案记录；此处不判断风险是否触发。</EmptyTable></Card>
    <Card title="次日验证流程" className="plan-workflow"><div className="research-process">{[['竞价检查', '核对快照与量价'], ['开盘确认', '核对条件与证据'], ['盘中核验', '保留时点与变化'], ['收盘复盘', '回填支持或反证']].map(([name, detail], i) => <div key={name}><span>{i + 1}</span><strong>{name}</strong><small>{detail}</small></div>)}</div><div className="workflow-links"><a href="/auction">进入竞价观察 <Icon name="arrow"/></a><a href="/research/reviews">返回每日复盘 <Icon name="arrow"/></a><a href="/research/validations">进入次日验证 <Icon name="arrow"/></a><small>结论需引用原情景与材料，不代表成交或收益。</small></div></Card>
  </div></>;
}

export function Research({ path, session }: { path: string; session: SessionResponse }) {
  const [requestNew, setRequestNew] = useState(0);
  const writable = session.data.user.role !== 'viewer';
  useEffect(() => {
    if (window.location.hash === '#research-records') document.getElementById('research-records')?.focus();
  }, []);
  const kind = path.endsWith('reviews') ? 'review' : path.endsWith('directions') ? 'direction' : 'plan';
  const search = useReviewLocation();
  const query = new URLSearchParams(search);
  const manual = query.get('workspace') === 'manual' || query.get('workspace') !== 'archive' && (query.has('date') || query.has('document') || window.location.hash === '#research-records');
  const showManual = kind !== 'review' || manual;
  const create = () => { if (kind === 'review') updateReviewLocation({ workspace: 'manual' }); setRequestNew(value => value + 1); };
  const [date, setDate] = useState(() => {
    const selected = new URLSearchParams(window.location.search).get('date');
    if (selected && /^\d{4}-\d{2}-\d{2}$/.test(selected) && !Number.isNaN(Date.parse(selected)) && new Date(selected).toISOString().slice(0, 10) === selected) return selected;
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  });
  function changeDate(value: string) {
    if (!value) return;
    setDate(value); const url = new URL(window.location.href); url.searchParams.set('date', value); url.searchParams.delete('document'); url.searchParams.delete('revision'); updateLocation(url, { replace: true });
  }
  return <><ResearchToolbar path={path} day={date} dateLabel={showManual ? '研究日期' : undefined} onDayChange={changeDate}>{!showManual && <ArchiveDatePicker/>}<div className="research-actions"><button className="primary" disabled={!writable} onClick={create}>{'新建' + researchNames[kind]}</button><button disabled>导出</button><button disabled>分享</button></div></ResearchToolbar>{showManual && <div className="research-status-note"><Icon name="info"/><span>人工研究支持保存、固定版本发布与历史回查；行情与 AI 未接入。</span><span className="badge muted">人工研究</span><a className="text-link record-jump" href="#research-records">查看当日记录 <Icon name="arrow"/></a></div>}{kind === 'review' && <div className="review-workspace-switch"><Segmented label="每日复盘工作区" options={['归档阅读', '人工研究']} value={manual ? '人工研究' : '归档阅读'} onChange={name => updateReviewLocation({ workspace: name === '人工研究' ? 'manual' : 'archive' })}/></div>}{path.endsWith('reviews') ? manual ? <DailyReview create={create} writable={writable}/> : <ArchiveReader key={query.get('archive_date') ?? ''}/> : path.endsWith('directions') ? <Directions create={create} writable={writable}/> : <Plans/>}{showManual && <ResearchRecords key={kind + ':' + date} session={session} kind={kind} day={date} requestNew={requestNew}/>}</>;
}
