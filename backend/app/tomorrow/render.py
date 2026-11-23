"""Render all chapters from one frozen dossier; model text never supplies table values."""
from decimal import Decimal, InvalidOperation

TITLES = (
    ('market', '大盘概览'), ('news', '核心新闻'), ('hotwords', '财经与社会热词'),
    ('sectors', '板块分析与仓位'), ('stocks', '重点板块与标的'),
    ('limits', '涨停池全景'), ('earnings', '热门概念与业绩'), ('risks', '风险与后续日程'),
)


def value(row, *keys):
    for key in keys:
        current = row
        for part in key.split('.'):
            current = current.get(part) if isinstance(current, dict) else None
        if current is not None:
            return current.get('value') if isinstance(current, dict) and 'value' in current else current
    return None


def text(item):
    if item is None:
        return '—'
    if isinstance(item, bool):
        return '是' if item else '否'
    if isinstance(item, list):
        return '；'.join(text(v) for v in item[:12]) or '—'
    if isinstance(item, dict):
        return '；'.join(f'{k}：{text(v)}' for k, v in item.items() if v is not None) or '—'
    return str(item)


def table(rows, columns):
    return [{label: value(row, *keys) for label, keys in columns} for row in rows]


def earnings_row(stock):
    fundamentals = stock.get('fundamentals') or {}
    valuation = (fundamentals.get('valuation') or [{}])[0].get('values') or {}
    financials = fundamentals.get('financials') or {}
    known = []
    labels = {'basic_eps': '每股收益（元）', 'eps': '每股收益（元）', 'roe': 'ROE（%）',
              'n_income_attr_p': '归母净利润（元）', 'total_revenue': '营业总收入（元）',
              'n_cashflow_act': '经营现金净额（元）'}
    for dataset, periods in financials.items():
        if not periods:
            continue
        latest = periods[0].get('values') or {}
        selected = {label: latest[key] for key, label in labels.items() if latest.get(key) is not None}
        if selected:
            known.append(dict(数据集=dataset, 报告期=latest.get('end_date'), 已公布字段=selected))
    business = [row.get('values') or {} for row in fundamentals.get('business') or []]
    return {'标的': stock.get('name') or stock.get('code'), '估值日期': valuation.get('trade_date'),
            'PE（TTM）': valuation.get('pe_ttm'), 'PB': valuation.get('pb'),
            '已披露财务': known or None, '业务证据': business or None,
            '资料限制': stock.get('limitations') or []}


def visible_content(report):
    """The renderer's shared content projection, excluding audit badges and storage metadata."""
    return dict(summary=report['summary'], sections=[{key: section.get(key) for key in
        ('key', 'title', 'state', 'summary', 'paragraphs', 'columns', 'rows', 'limitations', 'evidence_refs')}
        for section in report['sections']], scenarios=report['dossier'].get('scenarios') or [],
        core_directions=[{key: row.get(key) for key in ('code', 'name', 'judgment', 'judgment_authorship')}
                         for row in report['dossier'].get('core_directions') or []],
        risks=report['dossier'].get('risks') or [], limitations=report.get('limitations') or [])


def chapter(key, title, rows, limitations, paragraphs=None, summary=''):
    return dict(key=key, title=title, state='partial' if limitations else 'complete' if rows else 'unavailable',
                summary=summary, paragraphs=paragraphs or [],
                columns=[dict(key=key, label=key) for key in (rows[0] if rows else {})],
                rows=[{k: text(v) for k, v in row.items()} for row in rows],
                evidence_refs=[], limitations=list(dict.fromkeys(limitations)))


def portfolio(bundle, profile):
    policy = profile.get('risk_policy') or {}
    holdings = profile.get('holdings') or []
    quotes = {value(s, 'code', 'ts_code'): value(s, 'close', 'facts.close', 'price') for s in bundle.get('stocks', [])}
    result = dict(state='unavailable', cash=profile.get('cash'), holdings=holdings,
                  allocations=[], constraints=policy, limitations=[])
    if not policy.get('approved'):
        result['limitations'].append('仓位与价位规则尚未确认，具体数值留空。')
        return result
    if profile.get('cash') is None:
        result['limitations'].append('缺少现金数据，不能计算实际组合仓位。')
        return result
    try:
        cash = Decimal(str(profile['cash']))
        positions = [(h, Decimal(str(quotes.get(h['code'])))) for h in holdings]
        total = cash + sum(price * h['quantity'] for h, price in positions)
        if total <= 0 or any(not price.is_finite() or price <= 0 for _, price in positions):
            raise ValueError('invalid portfolio quotes')
    except (InvalidOperation, ValueError, TypeError):
        result['limitations'].append('部分持仓缺少本次截止前的有效价格，组合仓位不可判断。')
        return result
    weights = {h['code']: price * h['quantity'] / total for h, price in positions}
    invested = sum(weights.values())
    result.update(state='complete', total_value=str(total), cash_weight=str(cash / total),
                  invested_weight=str(invested), allocations=[dict(code=code, weight=str(w)) for code, w in weights.items()])
    if policy.get('max_total_weight') is not None and invested > Decimal(str(policy['max_total_weight'])):
        result['limitations'].append('当前总仓位超过已确认上限。')
    stock_cap = policy.get('max_stock_weight')
    for code, weight in weights.items():
        if stock_cap is not None and weight > Decimal(str(stock_cap)):
            result['limitations'].append(f'{code} 超过已确认单股仓位上限。')
    # Each holding enters each concept once; overlapping concepts are never summed as total exposure.
    exposure = {}
    for h, _ in positions:
        for sector in set(h.get('sector_codes') or []):
            exposure[sector] = exposure.get(sector, Decimal(0)) + weights[h['code']]
    result['sector_exposure'] = {key: str(v) for key, v in exposure.items()}
    for sector, weight in exposure.items():
        if policy.get('max_sector_weight') is not None and weight > Decimal(str(policy['max_sector_weight'])):
            result['limitations'].append(f'{sector} 超过已确认板块仓位上限。')
    if any(not h.get('sector_codes') for h, _ in positions):
        result['limitations'].append('部分持仓未标记板块，板块集中度检查不完整。')
    result['state'] = 'partial' if result['limitations'] else 'complete'
    return result


def build_report(bundle, profile, *, judgments=None, ai_results=None, previous=None):
    # Exact row/field lineage remains once in the immutable run bundle. A report
    # keeps the visible research projection and reference IDs rather than a
    # second full copy of every member/history dependency in every revision.
    def presentation(value):
        if isinstance(value, list):
            return [presentation(item) for item in value]
        if isinstance(value, dict):
            return {key: presentation(item) for key, item in value.items() if key not in
                    {'evidence', 'row_reference', 'source_row_ranges', 'cached_references', 'relative_to_sectors',
                     'net_amount_series', 'lhb_seats', 'member_codes', 'macro_references', 'historical_plans'}}
        return value
    dossier = presentation(bundle)
    stocks, sectors, events = (dossier.get(k) or [] for k in ('stocks', 'sectors', 'events'))
    market = dossier.get('market') or {}
    common = list(dossier.get('limitations') or [])
    dossier['portfolio'] = portfolio(bundle, profile)
    dossier['core_directions'] = []
    dossier['sector_observations'] = sectors[:10]
    dossier['watchlist'] = stocks
    dossier['risks'] = list(dict.fromkeys(common + dossier['portfolio']['limitations']))
    dossier['scenarios'] = dossier.get('scenarios') or []
    manual = judgments or []
    dossier['manual_judgments'] = manual
    # A single confirmed judgment feeds the summary cards and every chapter.
    for judgment in manual:
        kind, identity = judgment['object_type'], judgment['object_id']
        objects = {'stock': stocks, 'sector': sectors, 'event': events, 'market': [market]}.get(kind, [])
        for item in objects:
            if kind == 'market' or identity in {str(item.get(k)) for k in ('code', 'ts_code', 'id')}:
                item['judgment'], item['judgment_authorship'] = judgment['judgment'], 'manual'
                item['support_refs'], item['refuting_refs'] = judgment.get('support_refs', []), judgment.get('refuting_refs', [])
    notes = (dossier.get('manual_notes') or {}).get('content') or {}
    manual_directions = {row.get('sector_code') for row in notes.get('directions') or []}
    for sector in sectors:
        if sector.get('code') in manual_directions and len(dossier['core_directions']) < 3:
            sector.setdefault('judgment', '来自人工复盘的方向，等待本次条件核验。')
            sector['judgment_authorship'] = 'manual'
            dossier['core_directions'].append(sector)
    paragraphs = {key: [] for key, _ in TITLES}
    audit_result = None
    for result in ai_results or []:
        role = result.get('role')
        if role == 'review_write':
            audit_result = result.get('result')
            continue
        keys = {'market_events': ('market', 'news'), 'sectors_stocks': ('sectors', 'stocks', 'earnings'),
                'conditional_plan': ('stocks', 'risks')}.get(role, ())
        content = result.get('result') or {}
        for item in content.get('items') or []:
            chosen_keys = (item['section_key'],) if item.get('section_key') else keys
            for key in chosen_keys:
                paragraphs[key].append(dict(title=item['title'], text=item['text'], evidence_refs=item['evidence_refs'], authorship='ai'))
            kind, identity = item.get('object_type'), item.get('object_id')
            candidates = {'stock': stocks, 'sector': sectors, 'event': events, 'market': [market]}.get(kind, [])
            for obj in candidates:
                if kind != 'market' and identity not in {str(obj.get(k)) for k in ('id', 'code', 'ts_code')}:
                    continue
                obj.setdefault('ai_proposals', []).append(dict(text=item['text'], evidence_refs=item['evidence_refs']))
                if obj.get('judgment_authorship') != 'manual':
                    obj['judgment'], obj['judgment_authorship'] = item['text'], 'ai'
                if kind == 'sector' and item.get('direction_slot') in {'primary', 'secondary'} and len(dossier['core_directions']) < 3:
                    if obj not in dossier['core_directions']:
                        dossier['core_directions'].append(obj)
                if kind == 'stock' and item.get('candidate_role'):
                    obj['candidate_role'] = item['candidate_role']
                if item.get('next_checks'):
                    dossier['scenarios'] = [s for s in dossier['scenarios'] if s.get('object_id') != identity]
                    checks = item['next_checks']
                    dossier['scenarios'].append(dict(id=f'{identity}:research', object_type=kind,
                        object_id=identity, label=item['title'], target_trade_day=bundle.get('target_trade_day'),
                        status='pending', next_checks=checks, trigger=checks[0]['support'],
                        confirmation='由目标时段实际观察证据确认；缺少记录不视为满足。',
                        invalidation=checks[0]['invalidation'], observation_window=checks[0]['window'],
                        risk_veto=['交易状态不适用', '重大风险未排除', '必要数据不足'],
                        required_data=[check['missing_data'] for check in checks],
                        verification_method='manual', judgment_authorship='ai', evidence_refs=item['evidence_refs']))
    for judgment in manual:
        kind = judgment.get('object_type')
        key = {'market': 'market', 'sector': 'sectors', 'stock': 'stocks', 'event': 'news'}.get(kind)
        if key:
            paragraphs[key].append(dict(title='人工判断 · ' + judgment['object_id'], text=judgment['judgment'],
                                        evidence_refs=judgment.get('support_refs', []), authorship='manual'))
            for paragraph in paragraphs[key]:
                if paragraph['authorship'] == 'ai':
                    paragraph['review_required'] = True
    indices = market.get('indices') or []
    if isinstance(indices, dict):
        indices = [dict(code=k, **v) for k, v in indices.items() if isinstance(v, dict)]
    rows = {
        'market': table(indices, [('指数', ('name', 'code', 'ts_code')), ('收盘', ('close',)), ('涨跌幅（%）', ('pct_chg', 'pct_change')), ('成交额（千元）', ('amount',))]),
        'news': [dict(row, 内容节选=str(row['内容节选'])[:600] + ('（原文另存于依据）' if len(str(row['内容节选'])) > 600 else ''))
                 for row in table(events[:40], [('事件', ('title', 'name')), ('时间', ('published_at', 'published_day', 'source_time')), ('内容节选', ('summary', 'text', 'content')), ('证据状态', ('state', 'status', 'kind'))])],
        'hotwords': table((dossier.get('hotwords') or {}).get('financial', []) if isinstance(dossier.get('hotwords'), dict) else dossier.get('hotwords') or [], [('词语', ('term', 'word', 'name')), ('出现次数', ('document_count', 'count', 'mentions')), ('统计范围', ('scope',))]),
        'sectors': [dict(row, **{'资金净额（元）': (sector.get('funds') or [{}])[0].get('net_amount') if len(sector.get('funds') or []) == 1 else None})
                    for sector, row in zip(sectors[:20], table(sectors[:20], [('板块', ('name', 'code', 'ts_code')), ('当日涨跌幅（%）', ('facts.pct_change', 'pct_change', 'pct_chg')), ('5日表现（%）', ('calculations.returns_pct.5', 'return_5d')), ('20日表现（%）', ('calculations.returns_pct.20', 'return_20d')), ('涨停数', ('calculations.member_limit_up_n', 'limit_up_n'))]), strict=True)],
        'stocks': table(stocks[:30], [('标的', ('name', 'code', 'ts_code')), ('代码', ('code', 'ts_code')), ('收盘（元）', ('facts.close', 'close')), ('涨跌幅（%）', ('facts.pct_chg', 'pct_chg')), ('观察原因', ('candidate_reasons', 'reasons')), ('共享判断', ('judgment',)), ('参考支撑', ('calculations.support_resistance.support_candidate', 'support')), ('参考压力', ('calculations.support_resistance.resistance_candidate', 'resistance'))]),
        'limits': table([s for s in stocks if value(s, 'facts.pool.value.limit', 'limit', 'limit_status') in ('U', 'D', 'Z')], [('标的', ('name', 'code')), ('状态', ('facts.pool.value.limit', 'limit')), ('连板数', ('facts.pool.value.limit_times', 'limit_times')), ('首次封板', ('facts.pool.value.first_time', 'first_time')), ('开板次数', ('facts.pool.value.open_times', 'open_times'))]),
        'earnings': [earnings_row(stock) for stock in stocks[:30]],
        'risks': [{'风险与缺口': item} for item in dossier['risks']] + [dict(风险与缺口='后续事件：' + text(item)) for item in dossier.get('future_events') or []],
    }
    gaps = {key: [] for key, _ in TITLES}
    gaps['market'] = [] if bundle.get('ready') else ['核心行情资料不齐，不能形成完整市场判断。']
    gaps['news'] = ['仅依据本次已保存正文或摘要；来源覆盖不代表全网。']
    gaps['hotwords'] = ['财经词频只代表已缓存语料；社会热词数据未接通，不生成全网热度分。']
    gaps['sectors'] = ['概念范围与成员覆盖分别记录；仓位需完整持仓、现金及确认规则。'] + dossier['portfolio']['limitations']
    gaps['stocks'] = ['支撑压力为历史计算参考，未经确认不转为买入或止损指令。']
    gaps['limits'] = ['供应商非ST涨跌停/炸板池；梯队由同范围连板字段派生。']
    gaps['earnings'] = ['缺少公司业务或财务证据时，不判断业绩兑现和估值高低。']
    gaps['risks'] = ['事件日历只显示有日期和出处的已存记录。']
    gaps['news'].append(f'本章列示前{min(40, len(events))}个已存事件，完整事件对象保留在统一底稿。')
    gaps['sectors'].append(f'本章列示涨幅分项前{min(20, len(sectors))}个概念，完整{len(sectors)}个概念保留在底稿；此排序不等于推荐。')
    for key in ('stocks', 'earnings'):
        gaps[key].append(f'本章列示前{min(30, len(stocks))}只候选，完整{len(stocks)}只召回候选保留在底稿。')
    sections = []
    for key, title in TITLES:
        section = chapter(key, title, rows[key], gaps[key])
        section['analysis'] = paragraphs[key]
        section['paragraphs'] = [f"{p['title']}：{p['text']}" for p in paragraphs[key]]
        sections.append(section)
    breadth, turnover = market.get('breadth') or {}, market.get('turnover') or {}
    sections[0]['summary'] = (f"实际样本上涨{text(breadth.get('up'))}只、下跌{text(breadth.get('down'))}只、平盘{text(breadth.get('flat'))}只；"
        f"上涨占比{text(breadth.get('up_share_pct'))}%；成交额{text(turnover.get('amount_yuan'))}元，较前一交易日{text(turnover.get('change_pct'))}%。"
        '样本范围与缺失项见数据覆盖；上涨占比不表示账户盈利概率。')
    refs = dossier.get('evidence_refs') or []
    allowed = {str(ref['id']) for ref in refs}
    chapter_objects = {'market': [market], 'news': events[:40], 'hotwords': (dossier.get('hotwords') or {}).get('financial', []),
                       'sectors': sectors[:20], 'stocks': stocks[:30], 'limits': stocks, 'earnings': stocks[:30], 'risks': []}
    for section in sections:
        citations = {str(ref) for item in section.get('analysis') or [] for ref in item['evidence_refs']}
        citations |= {str(ref) for item in chapter_objects[section['key']] for ref in item.get('evidence_refs') or []}
        section['evidence_refs'] = sorted(citations & allowed)
    if previous:
        old_refs = {ref['id'] for ref in previous.get('evidence_refs') or []}
        dossier['overnight_change'] = dict(base_report_id=previous.get('id'),
            new_evidence_refs=[ref['id'] for ref in refs if ref['id'] not in old_refs],
            note='保留晚间原稿；本稿按新的接收截止追加隔夜变化。')
    summary = next((r['result']['summary'] for r in ai_results or [] if r['role'] == 'market_events'),
                   '已根据冻结资料生成研究草稿，请核对数据缺口、确认条件与失效条件。')
    return dict(format='tianyan.tomorrow.report.v1', source_trade_day=bundle['source_trade_day'],
                target_trade_day=bundle.get('target_trade_day'), as_of_time=bundle['as_of_time'],
                summary=summary,
                sections=sections, dossier=dossier, judgments=manual, evidence_refs=refs,
                limitations=list(dict.fromkeys(common + dossier['portfolio']['limitations'])),
                ai_completed_roles=[r['role'] for r in ai_results or []], ai_audit=audit_result)
