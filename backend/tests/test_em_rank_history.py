import json
from datetime import date,datetime

import pytest

from app.market import em_rank_history as history
from app.market.protocol import SourceFailure

DAY=date(2026,10,26)
PREVIOUS=date(2026,10,23)
CLOCK=datetime.fromisoformat('2026-10-26T18:00:00+08:00')


def body(rows):
    return json.dumps({'code':0,'data':rows}).encode()


def test_daily_history_dates_are_explicit_and_have_no_invented_minute():
    rows=history.parse(body([{'calcTime':'2026-10-23','rank':8},{'calcTime':'2026-10-26','rank':1}]),'SH600001',CLOCK)
    assert rows==[{'code':'600001','trade_date':'2026-10-23','rank':8,'source_row':0},
                  {'code':'600001','trade_date':'2026-10-26','rank':1,'source_row':1}]
    assert 'source_time' not in rows[1]


@pytest.mark.parametrize('rows',[
    [{'calcTime':'2026-10-27','rank':1}],
    [{'calcTime':'2026-10-26 15:00','rank':1}],
    [{'calcTime':'2026-10-26','rank':True}],
    [{'calcTime':'2026-10-26','rank':0}],
    [{'calcTime':'2026-10-26','rank':1}]*2,
    [{'calcTime':'2026-10-26','rank':1}]*401,
])
def test_changed_or_ambiguous_daily_history_is_rejected(rows):
    with pytest.raises(SourceFailure):history.parse(body(rows),'SZ000001',CLOCK)


def test_candidate_subset_is_never_misrepresented_as_full_top_ten():
    histories={str(600000+i):{'name':'合成'+str(i),'body_sha256':'a'*64,
        'rows':[{'trade_date':str(PREVIOUS),'rank':i+5,'source_row':0},
                {'trade_date':str(DAY),'rank':i,'source_row':1}]} for i in range(1,11)}
    result=history.top_ten(histories,DAY,PREVIOUS)
    assert result is not None and len(result)==10
    assert result[0]['rank_change'] is None
    assert result[0]['rank_change_previous_trading_day']=='5'
    assert result[0]['rank_comparison_day']=='2026-10-23'
    assert result[0]['source_history_row']=='1' and result[0]['source_day']==str(DAY)
    histories.pop('600005')
    assert history.top_ten(histories,DAY,PREVIOUS) is None
    histories['600099']={'name':'合成','body_sha256':'b'*64,
        'rows':[{'trade_date':str(DAY),'rank':11,'source_row':0}]}
    assert history.top_ten(histories,DAY,PREVIOUS) is None


def test_missing_comparison_date_remains_unknown():
    histories={str(600000+i):{'name':'合成','body_sha256':'a'*64,
        'rows':[{'trade_date':str(DAY),'rank':i,'source_row':0}]} for i in range(1,11)}
    result=history.top_ten(histories,DAY,PREVIOUS)
    assert all(row['rank_change_previous_trading_day'] is None for row in result)
