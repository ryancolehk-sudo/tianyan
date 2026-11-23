"""Receipt-bounded Eastmoney industry levels, never inferred from board names."""
import json
import re
from datetime import timedelta

from sqlalchemy import select

from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.market import read_facets
from app.market.models import DataFetchRun
from app.market.protocol import MAX_BODY, SHANGHAI, SourceFailure, fetch_https

URL = 'https://quote.eastmoney.com/center/api/sidemenu_new.json'
DATASET = 'market.eastmoney.industry_catalog'


def parse_catalog(body, received_at):
    try:
        if len(body) > MAX_BODY:
            raise SourceFailure('response_too_large')
        data = json.loads(body)
        entries = data['bklist']
        if not isinstance(entries, list) or not 1 <= len(entries) <= 5000:
            raise ValueError()
        rows, seen = [], set()
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError()
            if item.get('type') != 2:
                continue
            code, level = item.get('code'), item.get('flag')
            if (not isinstance(code, str) or not re.fullmatch(r'BK\d{4}', code)
                    or type(level) is not int or level not in {1, 2, 3} or code in seen):
                raise ValueError()
            seen.add(code)
            rows.append({'code': code, 'industry_level': str(level)})
        if not rows or not any(row['industry_level'] == '2' for row in rows):
            raise ValueError()
        return ReferencePayload(dataset=DATASET, title='东方财富行业层级目录',
            source_url=URL, attribution='东方财富公开行业目录；使用范围以当前核实配置为准',
            observed_day=received_at.astimezone(SHANGHAI).date(),
            scope='分类依据官方 type=2、flag=1/2/3。日期为本机采集目录日期，来源未提供目录生效时间；仅用于同日、接收后可见的分类，不追溯改写历史时点。',
            columns=[{'key': 'code', 'label': '板块代码', 'unit': None},
                     {'key': 'industry_level', 'label': '行业层级', 'unit': None}], rows=rows)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise SourceFailure('schema_changed') from None


def saved_catalog(db, source_id, day, cutoff):
    query = read_facets.with_facets(select(EvidenceVersion)).where(
        EvidenceVersion.source_id == source_id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
        read_facets.value('dataset') == DATASET,
        read_facets.observed_day() == day.isoformat(),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1)
    return db.scalar(query)


def catalog_due(db, source_id, clock):
    if saved_catalog(db, source_id, clock.astimezone(SHANGHAI).date(), clock):
        return False
    # A catalog attempt is the fourth request. Persisted runs limit failed
    # metadata probes to once an hour, even after a worker restart.
    return db.scalar(select(DataFetchRun.id).where(
        DataFetchRun.source_id == source_id, DataFetchRun.request_count == 4,
        DataFetchRun.capability != 'funds.minute_history',
        DataFetchRun.received_at >= clock - timedelta(hours=1),
        DataFetchRun.received_at <= clock).limit(1)) is None


def fetch_catalog():
    response = fetch_https(URL)
    parse_catalog(response.body, response.received_at)
    return response
