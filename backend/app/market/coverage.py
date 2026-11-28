"""One truthful implementation ledger for the 31 catalogued capabilities."""
from sqlalchemy import cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.orm import aliased

from app.evidence.models import EvidenceVersion
from app.market.read_facets import value, with_facets
from app.market.schemas import DataCapability, DataCoverage
from app.market.news_feeds import NEWS_FEEDS

# IDs are the catalogue identities, not provider display-name matching.
SCOPE = [
 ('D01','标的与代码搜索',('cninfo',),(), '目录已导入；上市退市历史和别名尚不完整。'),
 ('D02','交易日历与开闭市',('baostock_daily','szse_public'),(), '月度开闭市日期；交易时段和临时休市另核。'),
 ('D03','四指数与标的报价',('tickflow_live','ashare_public','tencent_web','baostock_daily'),(), '免费实时快照需本人本机Key及实际返回；五分钟盘中参考与盘后报价分别标记，不按配置数声称实时或全市场。'),
 ('D04','全市场截面与排序',(),(), '现有沪深盘后报价；全市场同刻截面、北交所和排序读取仍待实现。'),
 ('D05','分时与分钟 / 周月图',tuple('sina_minute_'+period for period in ('1m','5m','15m','30m','60m')),(), '本人研究新浪原生分钟，按已配置股票、周期和真实来源时标读取；可用性按实际记录，未知单位与尾柱最终性明确显示。日K不替代分钟，缺口不补造，全市场及交易时段持续稳定性另验。'),
 ('D06','历史日线与估值',('baostock_history',),('bars.daily.',), '近180自然日、已配置标的；含三种复权口径与原始估值字段。'),
 ('D07','复权因子与公司行动',('baostock_history',),('corporate.factors',), '供应商复权因子样本；不视为过去已知的公司行动全历史。'),
 ('D08','交易状态与制度',('baostock_history',),('bars.daily.unadjusted',), '历史日线中的ST及交易状态；盘中状态和完整生效规则尚缺。'),
 ('D09','板块分类与指数成分',('baostock_classification','ashare_sector_heat'),('classification.industry','index.','market.ashare.sector_heat'), '行业/三指数当前成分及供应商热门板块关注排名；混合命名空间、时间原文分别保留。热门排名不是涨幅/资金榜，行业/概念/地域行情与历史归属仍缺。'),
 ('D10','个股与板块资金流',('mairui_funds',),('market.mairui.industry_flow','market.mairui.stock_flow'), '麦蕊本人证书的行业/个股盘后资金参考，保留供应商口径、代码、日期与原更新时间；非实时或直接识别主力，概念资金及历史仍缺。财务现金流不是交易资金流。'),
 ('D11','涨跌停池与连板梯队',('mairui_pools',),('market.mairui.limit_up','market.mairui.limit_down','market.mairui.broken_limit'), '麦蕊涨停/跌停/炸板原股池。请求日期依据已核沪深日历，原响应无独立日期/更新时间；封板时间与连板字段原样，非本地规则、完整盘中轨迹或长期历史。'),
 ('D12','市场广度与晋级统计',('ashare_breadth',),('market.ashare.',), '供应商沪深涨跌家数/区间及首页涨跌停数量采集曲线；横轴为本机接收时间。源日期和完整分母未知，不称实时全市场或严格历史回放，晋级/溢价/炸板与成交额仍缺。'),
 ('D13','09:15 / 09:20竞价观察',('fuyao_auction','eastmoney_auction','tencent_auction','duanxianxia_auction','duanxianxia_auction_history'),('fuyao.auction_snapshot','market.auction.summary','market.duanxianxia.auction.summary'), '现有同花顺API原生竞价未匹配量；以当日限制价识别双侧，保留实际请求/返回/有效范围。09:15/09:20取节点后90秒内最早有效接收；响应时间不是交易所时刻，错过不补造。短线侠旧记录保留，采集已由管理员另行暂停。'),
 ('D14','09:25撮合结果与历史',('eastmoney_auction','tencent_auction','duanxianxia_auction','duanxianxia_auction_history'),('market.auction.summary','market.duanxianxia.auction.summary'), '09:25 仅表示来源时点命中该检查点，不是最终成交明细或完整竞价历史；逐笔/撤单/委托队列仍缺。短线侠历史表只提供逐日封单金额，不是撮合明细，也不补成连续历史。'),
 ('D15','逐笔、撤单与委托队列',(),(), '缺具备对应L2字段的行情权益或已核验免费来源。'),
 ('D16','快讯与基本资讯',('ecb_news','fed_public','hk_gov_finance', *NEWS_FEEDS),(), '香港与全球财经/科技/国际新闻RSS及央行发布；按实际配置及调用展示，保留原题/出处/时间精度，香港政府来源所有合法发布日期均只保留日期。同一媒体多个频道不算多家信源；同花顺、财联社及东方财富滚动快讯按采集窗口展示。'),
 ('D17','公司公告与风险披露',('cninfo_announcements',),(), '最近七日公开公告首页，保留标题/出处/时间；不下载全文，不代表全历史。'),
 ('D18','研报目录与评级',(),(), '目录与评级接口、具体自动化使用范围尚未核验；不复制付费研报。'),
 ('D19','财务三表与估值',('baostock_fundamental',),('financial.',), '最近四报告期的利润/资产负债/现金流/成长指标；并非完整原始三表。'),
 ('D20','股东与公司事件',('baostock_fundamental',),('corporate.dividends',), '已配置标的上一年度分红；股东、解禁、增减持、回购和调研仍待接入。'),
 ('D21','龙虎榜 / 大宗 / 两融',(),(), '逐表来源、金额和发布时间仍待验证，尚无可用本机数据。'),
 ('D22','北向与互联互通统计',(),(), '按现行披露制度核对；网页可达未完成统计解析，不生成已停止披露的实时净流入。'),
 ('D23','ETF行情与净值',(),(), 'ETF报价候选可达；实际字段、净值/份额/IOPV和使用范围未完成验收。'),
 ('D24','可转债行情与条款',(),(), '报价与条款版本、转股价/赎回字段待验，尚无已接通来源。'),
 ('D25','宏观 / 政策 / 行业',('worldbank_public',),('macro.',), '中国年度宏观；月度统计、央行及部委国内发布仍待接入。'),
 ('D26','利率与参考汇率',('worldbank_public',),('macro.',), '年度贷款/实际利率参考；不是LPR、SHIBOR或中债曲线。'),
 ('D27','海外与扩展市场',('ecb_public',),('fx.',), '欧洲央行每日汇率参考；海外股票、期货期权仍待接入。'),
 ('D28','互动问答与热度',(),(), '互动平台及排行的接口、时间和使用范围尚待核验。'),
 ('D29','自选 / 关注 / 研究',(),(), '按账号保存有序自选、批量读取本机报价；研究保存与旧稿阅读已有，资讯关注另核。不向供应商发送研究。'),
 ('D30','事件映射 / 规则 / AI',(),(), '不是单一数据接口；缺完整规则和模型/材料外发条件，保持不可判断。'),
 ('D31','来源健康与调用成本',(),(), '本机来源配置、调用、覆盖和错误可查；没有费用账单仍显示未知。'),
]


TUSHARE_SCOPE = {
 'D04': (('tencent_market_rt','tushare_market_rt'), ('tencent.rt_market','tushare.rt_market'), '查看已保存的批量行情截面，覆盖股票数和接收时点按来源批次核验。'),
 'D28': (('direction_ths_hot','direction_tdx_hot','direction_em_hot','direction_kpl_strength'),
     ('market.direction.',), '方向页三平台人气前10与开盘啦题材强度前10按来源独立保存；实际源日期与采集快照归档日分别标记，未知行情时点不补造。历史人气只查已有归档，不把当前榜补成过去排名。互动问答仍待接入。'),
 'D08': (('tushare_stk_limit',), ('tushare.stk_limit',), '已购Tushare所选股票近10日价格限制资料；不等于已涨停/停牌状态，完整生效规则仍缺。'),
 'D20': (('tushare_dividend',), ('tushare.dividend',), '已购Tushare所选股票有界分红资料优先；预案与实施进度分别保留，其他股东/事件仍待接入。'),
 'D01': (('tushare_stock_basic',), (), '已购Tushare目录优先；当前正常上市代码与简称，历史别名/退市仍缺。'),
 'D02': (('tushare_calendar_sse','tushare_calendar_szse'), (), '已购Tushare沪深月历优先；北交所预检空表，不代填日历。'),
 'D03': (('tushare_market_rt','tushare_rt_indices','tushare_rt_k','tushare_daily','tushare_index_daily'), (), '已购Tushare沪深京批量股票快照及四指数优先；按每行行情时间、实际覆盖与日期核验，不把停牌旧价当今日实时。免费源补缺。'),
 'D12': (('direction_kpl_emotion','tushare_market_rt','tushare_market_limits'), ('market.direction.kpl_emotion','review.emotion_history','tushare.rt_market','tushare.market_limits','review.limits'), '行情总览另存开盘啦原生每日历史情绪及实际补采时间，不回填旧盘中样本。开盘啦情绪独立60秒采样；是否接通以实际记录为准，来源仅提供日期，盘中曲线按接收时间保存；启用前不回填。Tushare批量行情与当日限制价精确比较当前封板；保留ST/不同涨跌幅股票，旧日/无正限制价不计，显示可比与未判定范围。接入前免费曲线保留原来源，不拼接或回填。'),
 'D06': (('tushare_daily','tushare_daily_basic'), ('tushare.daily','tushare.daily_basic'), '已购Tushare单标的近180自然日日线/估值优先；不复权日K可看，免费三种复权历史保留。'),
 'D07': (('tushare_adj_factor',), ('tushare.adj_factor',), '已购Tushare单标的复权因子优先；不按当前因子伪造过去已知信息。'),
 'D09': (('tushare_ths_index','tushare_ths_member','tushare_ths_daily','direction_kpl_strength','tushare_index_basic_sse','tushare_index_basic_szse','eastmoney_boards'), ('tushare.ths_index','tushare.ths_member','tushare.ths_daily','market.direction.kpl_atlas_v1','tushare.index_basic','market.eastmoney.'), '同花顺概念目录、当前成分分页快照与板块日行情通过已购兼容通道独立登记；日频不当实时。开盘啦完整题材分页另存版本，原始强度及成交额与旧前十区分；领涨保留来源龙一至龙五原始标记。实际启用、历史覆盖及失败以采集记录为准，不以适配器存在当已接通。'),
 'D10': (('tushare_moneyflow','eastmoney_funds','tushare_moneyflow_dc','tushare_moneyflow_mkt_dc'), ('tushare.moneyflow','market.eastmoney.','review.stock_funds','review.market_funds'), '行情总览按已核交易日16:30自动归档DC个股/沪深大盘资金，原表万元/元分开；接通与覆盖以真实采集回执为准。已购Tushare所选股盘后资金保留；东方财富个股/行业/概念主力净额适配独立登记，每表一页最多200条。按实际源日与保存批次读取，未核权利/未采集不显示已接通。净额口径不同不拼表。'),
 'D11': (('tushare_limit_list_d','tushare_limit_list_ths'), ('tushare.limit_list_d','tushare.limit_list_ths','review.pools','review.pool_details'), '已购Tushare涨跌停/炸板原榜优先，工作台分U/Z/D及原连板字段展示；日期来自返回，不是逐笔实时轨迹。复盘补充THS类型/原因/标签及每日指标；实际接通以归档成功为准。'),
 'D14': (('tushare_stk_auction','fuyao_auction'), ('tushare.stk_auction','fuyao.auction_snapshot'), '已购Tushare撮合成交与同花顺竞价未匹配量独立保存。原生final仅补最终竞价封单，保留真实迟收时间，不补造09:15/09:20或声明交易所09:25精确时刻。'),
 'D19': (('tushare_income','tushare_balancesheet','tushare_cashflow','tushare_fina_indicator'), ('tushare.income','tushare.balancesheet','tushare.cashflow','tushare.fina_indicator'), '已购Tushare最近4条财务返回优先，含三表部分字段、报告期与公告日；不是完整原始三表或全部报告期。'),
 'D21': (('tushare_top_list','tushare_top_inst','tushare_block_trade','tushare_margin_detail'), ('tushare.top_list','tushare.top_inst','tushare.block_trade','tushare.margin_detail'), '已购Tushare龙虎榜/买卖席位、大宗与两融优先；席位按股票、交易日、原因和买卖类型区分，权益与实际成功逐连接核验，不把登记当接通。新解析单位明确，旧未知不补造。'),
 'D25': (('tushare_cn_cpi',), ('tushare.cn_cpi',), '已购Tushare最近12个月CPI原表优先；其他宏观仍由已接免费源补缺。'),
 'D26': (('tushare_shibor',), ('tushare.shibor',), '已购Tushare近10日SHIBOR优先；原期限/数值保留，免费年度利率仅作补充。'),
}


def proof(db, ids, kind, prefixes=(), rate=False):
    """Find an actual current revision in this capability's own sources, across all dates."""
    if not ids:
        return None
    e = EvidenceVersion
    newer = aliased(e)
    query = select(e.id).where(e.source_id.in_(ids), e.kind == kind, e.visibility == 'shared',
        e.owner_id.is_(None), e.received_at <= func.now(),
        ~exists(select(newer.id).where(newer.source_id == e.source_id, newer.source_key == e.source_key,
            newer.kind == e.kind, newer.visibility == 'shared', newer.owner_id.is_(None),
            newer.version_number > e.version_number, newer.received_at <= func.now())))
    if prefixes:
        dataset = value('dataset')
        query = with_facets(query).where(or_(*(dataset.startswith(prefix, autoescape=True) for prefix in prefixes)),
            func.jsonb_array_length(e.payload['rows']) > 0)
        if rate:
            query = query.where(or_(dataset == 'tushare.shibor', func.jsonb_path_exists(e.payload, cast(
                '$.rows[*] ? (@.indicator like_regex "^FR[.]INR[.]" && @.value != null)', JSONPATH))))
    return db.scalar(query.limit(1))


def coverage(db):
    from app.market.service import permission_reason, source_states
    from app.market.models import InstrumentOrigin
    from app.models import Provider
    sources = {source.family: source for source in source_states(db)}
    permitted = {provider.family: provider.id for provider in db.scalars(select(Provider))
                 if provider.family in sources and not permission_reason(provider)}
    result = []
    for ident, name, families, prefixes, detail in SCOPE:
        if ident in TUSHARE_SCOPE:
            additional, datasets, detail = TUSHARE_SCOPE[ident]
            families, prefixes = (*additional, *families), (*datasets, *prefixes)
        ids = [permitted[f] for f in families if f in permitted]
        sample = None
        kind = {'D02':'calendar','D03':'quote','D05':'bar','D06':'bar','D16':'news','D17':'news'}.get(ident)
        if ident == 'D04':
            # Both market adapters persist all large snapshots under a fixed key.
            # Seek the current revision before reading its several thousand rows.
            e = EvidenceVersion
            for family, dataset in zip(families, prefixes):
                if family not in permitted:
                    continue
                current = select(e.id).where(e.source_id == permitted[family], e.owner_id.is_(None),
                    e.source_key == 'reference:' + dataset, e.kind == 'reference', e.visibility == 'shared',
                    e.received_at <= func.now()).order_by(e.version_number.desc()).limit(1).correlate(None)
                sample = db.scalar(select(e.id).where(e.id == current.scalar_subquery(),
                    e.payload['dataset'].astext == dataset, func.jsonb_array_length(e.payload['rows']) > 0))
                if sample is not None:
                    break
        elif kind:
            sample = proof(db, ids, kind)
        elif prefixes:
            sample = proof(db, ids, 'reference', prefixes, rate=ident == 'D26')
        actual = sample is not None or ident in {'D29', 'D31'}
        if ident == 'D01' and ids:
            actual = db.scalar(select(InstrumentOrigin.id).where(InstrumentOrigin.source_id.in_(ids),
                InstrumentOrigin.origin_kind == 'catalog', InstrumentOrigin.last_observed_at <= func.now()).limit(1)) is not None
        state = 'partial' if actual else 'blocked' if ident in {'D15', 'D30'} else 'pending'
        result.append(DataCapability(id=ident, name=name, state=state, detail=detail,
            families=list(dict.fromkeys(families)), dataset_ids=[sample] if sample else []))
    return DataCoverage(items=result)
