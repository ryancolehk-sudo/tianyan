"""Reading priority for the industry news feed, derived from the currently visible revision."""
from decimal import Decimal
from itertools import chain
import re
from typing import NamedTuple
from unicodedata import normalize


class Priority(NamedTuple):
    importance: str
    event_type: str | None
    reason: str | None


ROUTINE = Priority('regular', None, None)
SOURCE_FOCUS = Priority('watch', None, '发布方标红')
# Hong Kong feeds retain their original text. Fold only characters used by the rules.
TRADITIONAL = str.maketrans(dict((
    '訂订 單单 購购 獲获 標标 簽签 銷销 額额 億亿 萬万 幣币 產产 業业 應应 斷断 暫暂 復复 啟启 動动 擴扩 '
    '實实 現现 貨货 礦矿 運运 減减 進进 頒颁 佈布 發发 補补 貼贴 辦办 劃划 綱纲 見见 臺台 電电 藥药 證证 '
    '預预 計计 畫划 擬拟 傳传 聞闻 認认 謠谣 終终 選选 關关 閉闭 組组 機机 構构 儲储 鋰锂 鋼钢 鐵铁 '
    '稅税 執执 項项 國国 銀银 審审 議议 試试 約约 輸输 據据 導导 體体 術术 絕绝 緊紧 張张 確确 詢询 '
    '會会 線线 與与 興兴 資资 鏈链 達达 淨净 報报 場场 風风 險险 給给 對对 質质 無无 競竞 書书 開开 '
    '營营 違违 規规 劑剂 稱称 種种 準准 顧顾 彙汇 療疗 過过 錄录 總总 處处 長长 獎奖'
).split()))

ORDER = re.compile(r'订单|合同|中标|中选|采购协议|供货协议|供应协议|长协|采购意向|意向协议|框架协议|项目定点|大单')
SIGNED = re.compile(r'签[署订约]|订立|签下|中标|中选|获得|获取|接获|斩获|获.{0,10}订单|拿下|承接|收到.{0,12}通知书')
CANCELLED = re.compile(r'取消|解除|终止|撤销|撤回')
MATERIAL = re.compile(r'重大.{0,8}(?:订单|合同|项目|中标)|(?:订单|合同|中标).{0,5}(?:创.{0,3}纪录|历史最大)|大额订单|大单|最大单笔')
AMOUNT = re.compile(r'(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>万亿|亿|万)\s*(?:元人民币|人民币|元)')
WRITTEN_YI = re.compile(r'(?<![点零\.\d])(?:[一二两三四五六七八九十百千万]+|数|上|超(?:过)?|逾)亿元(?:人民币)?')
OTHER_MONEY = re.compile(r'营收|营业收入|净利|市值|总资产|总投资|预算|上限|不超过|最高|不足|不到|未达|少于|低于|近|港币|港元|美元|日元|韩元|台币|欧元')
SUPPLY_UP = re.compile(r'复产|复供|恢复(?:.{0,6}生产|供[货应]|出口)|上调产量|提高产量')
SUPPLY_DOWN = re.compile(r'停产|减产|限产|停供|断供|(?:暂停|停止)(?:供[货应]|出口)|生产中断|'
                         r'供应中断|供应短缺|供给中断|产能受损|下调产量|削减产量')
SUPPLY_WATCH = re.compile(r'扩产|扩建产能|供应趋紧|供应紧张|供需缺口|供不应求|产能利用率|排产|供货周期|交货周期|供应瓶颈')
PRODUCTION = re.compile(r'(?:正式|已实现|实现|已启动|开始|投入|进入|达成|成功).{0,7}(?:量产|投产|批量交付|批量生产)|'
                        r'投产运营|投产达效|建成投产|量产下线|量产出货|首批.{0,10}(?:完成交付|正式交付)')
PRODUCT = re.compile(r'量产|投产|批量交付|首批.{0,10}交付|临床试验|新药|药品注册|获批上市|新品发布|产品验证|首台套')
AUTHORITY = re.compile(r'国务院|发改委|发展改革委|工信部|工业和信息化部|财政部|商务部|能源局|药监局|交通运输部|科技部|'
                       r'生态环境部|住建部|人民政府|[省市区]政府|海关总署|欧盟委员会|欧盟|政府|两部门|多部门|三部门|四部门')
POLICY = re.compile(r'产业政策|产业规划|行业准入|实施方案|行动方案|行动计划|补贴|出口管制|进口关税|出口配额|以旧换新|行业标准|指导意见')
INDUSTRY = re.compile(r'制造|产业|工业|行业|企业|产品|能源|电力|汽车|芯片|半导体|算力|电池|机器人|钢铁|稀土|'
                      r'医药|药品|光伏|风电|储能|设备|消费品|农业|材料|出口管制|关税|配额|以旧换新')
ENACTED = re.compile(r'印发|发布|颁布|出台|施行|实施|生效|公布|正式启动|下达')
DRUG = re.compile(r'药品|新药|疫苗|适应[症证]|注射液|胶囊|片剂')
DRUG_APPROVED = re.compile(r'获批上市|批准上市|获批新适应[症证]|取得.{0,5}药品注册证书')
UNCERTAIN = re.compile(r'意向|框架|备忘录|候选|预中标|征求意见|征询意见|草案|拟|有望|预计|预期|或将|可能|计划于|'
                       r'计划.{0,5}(?:扩产|停产|投产|量产)|传闻|传言|传出|网传|据传|考虑|能否|是否|如果|假如|假设|若|'
                       r'有待|尚待|尚需|待.{0,5}批准|为目标|力争|将于|有消息称|消息人士|[?？]')
DENIAL = re.compile(r'否认|辟谣|不属实|不实消息|消息不实|传闻不实|并非|并未|尚未|未获批|未在.{0,10}(?:获批|注册|批准)|未经批准|没有.{0,8}(?:订单|合同|停产|减产|断供)|'
                    r'(?:未|不)[曾会涉及]*(?:中标|签订|签署|获得|停产|减产|断供|投产|量产)|不存在.{0,10}(?:订单|停产|断供)')
COMMENTARY = re.compile(r'分析师|研报|研究报告|专家解读|投资者提问|情景分析|演练|回顾|复盘|如何看|怎么看|供应前景|供应展望|市场观望')
ATTACHMENT = re.compile(r'法律意见书|核查意见|会议资料|备查文件|合同范本|合同管理制度|采购管理制度')
EVENT_MENTION = re.compile('|'.join(pattern.pattern for pattern in (
    ORDER, SUPPLY_UP, SUPPLY_DOWN, PRODUCTION, DRUG_APPROVED, POLICY, SUPPLY_WATCH, PRODUCT)))


def folded(text):
    return re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', normalize('NFKC', text or '').translate(TRADITIONAL))


def large_order(text, family):
    if MATERIAL.search(text):
        return True
    for match in chain(AMOUNT.finditer(text), WRITTEN_YI.finditer(text)):
        context = text[max(0, match.start()-18):match.start()]
        if OTHER_MONEY.search(context):
            continue
        if family in {'rthk_finance', 'hk_gov_finance'} and '人民币' not in context + match.group():
            continue
        if match.re is WRITTEN_YI:
            return True
        units = {'亿': 100_000_000, '万': 10_000, '万亿': 10**12}
        if Decimal(match['value']) * units[match['unit']] >= 100_000_000:
            return True
    return False


def event_priority(text, family):
    """Classify one statement. Specific outcomes take precedence over general coverage."""
    order = ORDER.search(text)
    if order and (SIGNED.search(text) or CANCELLED.search(text)) and large_order(text, family):
        if CANCELLED.search(text):
            return Priority('important', 'order', '重大订单取消或终止')
        return Priority('important', 'order', '重大项目中标' if re.search(r'中标|中选', text) else '重大订单已签或已获')
    if SUPPLY_UP.search(text):
        return Priority('important', 'supply', '复产、增产或恢复供应')
    if SUPPLY_DOWN.search(text):
        return Priority('important', 'supply', '停减产或供应收紧')
    if PRODUCTION.search(text):
        return Priority('important', 'catalyst', '投产量产或批量交付')
    if DRUG.search(text) and DRUG_APPROVED.search(text):
        return Priority('important', 'catalyst', '产品获批上市')
    policy = POLICY.search(text) and INDUSTRY.search(text)
    if policy and AUTHORITY.search(text) and ENACTED.search(text):
        return Priority('important', 'catalyst', '产业政策已发布或实施')
    if order and (SIGNED.search(text) or re.search(r'订单|候选|框架协议|采购意向|意向协议', text)):
        return Priority('watch', 'order', '订单进展')
    if policy:
        return Priority('watch', 'catalyst', '产业政策动向')
    if SUPPLY_WATCH.search(text):
        return Priority('watch', 'supply', '产能与供需动向')
    if PRODUCT.search(text):
        return Priority('watch', 'catalyst', '产品与产线进展')
    return ROUTINE


def qualification(text):
    if DENIAL.search(text):
        return '澄清或否认'
    if re.search(r'意向|框架|备忘录', text):
        return '意向或框架协议'
    if re.search(r'候选|预中标', text):
        return '尚处中标候选阶段'
    if re.search(r'草案|征求意见|征询意见', text):
        return '草案或征求意见'
    if re.search(r'(?:累计|历年).{0,90}(?:获批上市|批准上市|中标|签订合同|建成投产)', text):
        return '累计数据或阶段回顾'
    if UNCERTAIN.search(text):
        return '预期或待确认'
    if COMMENTARY.search(text):
        return '解读或回顾'
    return None


def assess(text, family):
    # A forecast for next year's revenue does not undo an already signed order.
    # A denial anywhere in the same headline still qualifies the whole report.
    qualifier = qualification(text)
    if qualifier and ',' in text:
        first, tail = text.split(',', 1)
        if (not qualification(first) and re.match(r'\s*(?:预计|预期)', tail)
                and re.search(r'交付|交货|年产|产值|营收|收入|利润|贡献', tail)
                and not re.search(r'否认|传闻|未|意向|草案|候选|待批|尚需|须经', tail)):
            confirmed = event_priority(first, family)
            if confirmed.importance == 'important':
                return confirmed, None
    return event_priority(text, family), qualifier


def reading_priority(title, lead, family, marked):
    title = folded(title)
    fallback = SOURCE_FOCUS if marked else ROUTINE
    if ATTACHMENT.search(title):
        return fallback
    lead = folded(lead)
    if not EVENT_MENTION.search(title) and not EVENT_MENTION.search(lead):
        return fallback
    headline, headline_qualifier = assess(title, family)
    if headline.event_type and headline_qualifier:
        return Priority('watch', headline.event_type, headline_qualifier)
    if headline.importance == 'important':
        return headline
    best = headline if headline.event_type else fallback
    # Adjacent, unrelated headlines must not lend one another an amount or an action.
    for statement in re.split(r'[。；;！？!?\n]', lead):
        found, qualifier = assess(statement, family)
        if not found.event_type:
            continue
        qualifier = headline_qualifier or qualifier
        if found.importance == 'important' and not qualifier:
            return found
        if best.event_type is None:
            best = Priority('watch', found.event_type, qualifier or found.reason)
    return best
