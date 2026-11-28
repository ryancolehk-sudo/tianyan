from uuid import UUID
from datetime import date
from typing import Literal
from app.market.news_feeds import NEWS_FAMILIES

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import AwareDatetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import csrf, principal, require_role
from app.db import db_session
from app.evidence.api import business_key, paged
from app.evidence.common import write_once
from app.http import envelope
from app.market.models import DataFetchRun
from app.market.schemas import DailyHistoryRequest, CollectionJobReceipt, FetchRun, RuntimeSource, SourceConfiguration, SourceIndex, QuoteBatch
from app.market.read_models import quote_batch
from app.market.datasets import bars, breadth_history, datasets, news
from app.market.schemas import AuctionView, BarSeries, BreadthSeries, DatasetList, NewsList, TradingDatasetList, WatchlistView, WatchlistUpdate
from app.market.trading_datasets import trading_datasets
from app.market.watchlist import watchlist, update_watchlist
from app.market.auction_read import auction_view
from app.market.auction_reference import AuctionReferenceView, reference_view
from app.market.auction_support import AuctionSupportView, support_view
from app.market.schemas import DataCoverage
from app.market.schemas import WorkbenchView
from app.market.workbench_v2 import workbench_view as legacy_workbench_view
from app.market.coverage import coverage
from app.market.terminal_schemas import TerminalView, TerminalDetails, SectorMembership
from app.market.service import configure_source, enqueue_collection, source_index, source_states
from app.market.session import MarketSession, market_session
from app.schemas import DTO, Envelope, Error, Page
from app.market.quote_focus import QuoteFocusRequest, QuoteFocusReceipt, record_interest
from app.market.kpl_focus import KplFocusRequest, KplFocusReceipt, record_interest as record_kpl_interest

router = APIRouter()
ERRORS = {401:{'model':Error},403:{'model':Error},404:{'model':Error},409:{'model':Error},422:{'model':Error},429:{'model':Error},503:{'model':Error}}


@router.put('/api/v1/quote-focus', response_model=Envelope[QuoteFocusReceipt], responses=ERRORS,
            operation_id='updateQuoteFocus')
def quote_focus(body: QuoteFocusRequest, request: Request, identity=Depends(csrf), db: Session=Depends(db_session)):
    result = record_interest(db, identity[0], body)
    db.commit()
    return envelope(request, result, 'complete')


@router.put('/api/v1/kpl-focus', response_model=Envelope[KplFocusReceipt], responses=ERRORS,
            operation_id='updateKplFocus')
def kpl_focus(body: KplFocusRequest, request: Request, identity=Depends(csrf), db: Session=Depends(db_session)):
    result = record_kpl_interest(db, identity[0], body)
    db.commit()
    return envelope(request, result, 'complete')

from app.market.ladder_observation import LadderObservation, observation_view, export_xlsx


@router.get('/api/v1/ladder-observation', response_model=Envelope[LadderObservation], responses=ERRORS,
            operation_id='getLadderObservation')
def ladder_observation(request: Request, trade_day: date | None=None,
                       lookback: int=Query(default=6, ge=1, le=20),
                       as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                       db: Session=Depends(db_session)):
    value = observation_view(db, identity[0].id, trade_day, lookback, as_of_time)
    return envelope(request, value, 'complete' if value.state == 'complete' else 'partial' if value.days else 'empty',
                    as_of_time=as_of_time)


@router.get('/api/v1/ladder-observation.xlsx', response_class=Response,
            responses={**ERRORS,200:{'content':{'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':{'schema':{'type':'string','format':'binary'}}}}},
            operation_id='exportLadderObservation')
def ladder_observation_export(trade_day: date | None=None,
                              lookback: int=Query(default=6, ge=1, le=20),
                              as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                              db: Session=Depends(db_session)):
    value = observation_view(db, identity[0].id, trade_day, lookback, as_of_time)
    return Response(export_xlsx(value), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="ladder-observation.xlsx"', 'Cache-Control':'private, no-store'})


@router.get('/api/v1/auction-support', response_model=Envelope[AuctionSupportView], responses=ERRORS,
            operation_id='getAuctionSupport')
def auction_support(request: Request, trade_day: date | None=None, as_of_time: AwareDatetime | None=None,
                    identity=Depends(principal), db: Session=Depends(db_session)):
    result = support_view(db, identity[0].id, trade_day=trade_day, as_of_time=as_of_time)
    return envelope(request, result, 'partial' if result.rows else 'empty',
                    ['仅已保存的原日竞价成交、行情及昨日日榜；不把封单当撮合成交。'], as_of_time=as_of_time)


@router.get('/api/v1/auction-reference', response_model=Envelope[AuctionReferenceView], responses=ERRORS,
            operation_id='getAuctionReference')
def auction_reference(request: Request, trade_day: date | None=None, as_of_time: AwareDatetime | None=None,
                      identity=Depends(principal), db: Session=Depends(db_session)):
    result = reference_view(db, trade_day=trade_day, as_of_time=as_of_time)
    return envelope(request, result, 'partial' if result.report else 'empty',
                    ['本机已存阶段封单观察，阶段净变化不能区分撤单与成交消耗。'], as_of_time=as_of_time)


@router.get('/api/v1/market-session', response_model=Envelope[MarketSession], responses=ERRORS, operation_id='getMarketSession')
def shared_market_session(request: Request, identity=Depends(principal), db: Session=Depends(db_session)):
    result = market_session(db)
    return envelope(request, result, 'partial' if result.is_trading_day is not None else 'empty',
        ['交易时段和数据新鲜度独立判断；依据本机已核实沪深日历，缺失时不猜测休市或开市。'])


@router.get('/api/v1/workbench-v2', response_model=Envelope[WorkbenchView], responses=ERRORS, operation_id='getWorkbenchV2')
def legacy_fixed_workbench(request: Request, view: Literal['overview','auction','ladder','funds']='overview',
                    trade_day: date | None=None, instrument_id: UUID | None=None,
                    sector_code: str | None=Query(default=None, pattern=r'^BK\d{4}(?:\.DC)?$'),
                    as_of_time: AwareDatetime | None=None,
                    identity=Depends(principal), db: Session=Depends(db_session)):
    result = legacy_workbench_view(db, identity[0].id, view=view, trade_day=trade_day,
        instrument_id=instrument_id, sector_code=sector_code, as_of_time=as_of_time)
    present = bool(result.indices or result.stocks or result.sectors or result.auction_days)
    return envelope(request, result, 'partial' if present else 'empty',
        ['仅读取Tushare有权缓存；日频资金、竞价成交与盘口封单分开。日期回看可含迟到资料；指定截止时点排除其后接收的版本。'], as_of_time=as_of_time)


class SourceRefresh(DTO):
    day: date | None = None


@router.get('/api/v1/data-sources', response_model=Envelope[SourceIndex], responses=ERRORS, operation_id='listDataSources')
def list_sources(request: Request, identity=Depends(principal), db: Session=Depends(db_session)):
    result=source_index(db)
    return envelope(request, result, 'partial' if result.items else 'empty',
                    ['来源配置、实际采集、数据权利分别核对，不代表供应商全部能力已接通'])


@router.post('/api/v1/data-sources', response_model=Envelope[RuntimeSource], status_code=201, responses=ERRORS, operation_id='configureDataSource')
def configure(body: SourceConfiguration, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def save(db):
        provider=configure_source(db,identity[0],body,request.state.request_id)
        state=next(row for row in source_states(db) if row.provider.id==provider.id)
        return state,'partial',['配置已保存；本操作未发起外部采集，不能据此判定连接通过']
    return write_once(request, identity[0], key, body, save)


@router.post('/api/v1/data-sources/{source_id}/refresh', response_model=Envelope[CollectionJobReceipt], status_code=202, responses=ERRORS, operation_id='refreshDataSource')
def refresh(source_id: UUID, body: SourceRefresh, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def queue(db):
        from app.config import settings
        from app.auth import APIError
        if not settings().data_collection_enabled:
            raise APIError(422,'collection_disabled','本机自动采集尚未启用，未发起外部请求。')
        job=enqueue_collection(db,source_id,request_id=request.state.request_id,day=body.day)
        return CollectionJobReceipt(job_id=job.id,source_id=source_id,state=job.state,requested_day=body.day,acquisition_mode='backfill' if body.day else 'observed'),'partial',['采集任务已排队，实际结果另见调用记录']
    return write_once(request, identity[0], key, body, queue, status=202)


@router.post('/api/v1/market-history', response_model=Envelope[CollectionJobReceipt], status_code=202, responses=ERRORS, operation_id='collectMarketHistory')
def collect_history(body: DailyHistoryRequest, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def queue(db):
        from app.market.daily_history import enqueue_history
        job=enqueue_history(db,identity[0],body,request.state.request_id)
        ctx=job.payload['daily_history']
        return CollectionJobReceipt(job_id=job.id,source_id=UUID(job.payload['source_id']),state=job.state,
            instrument_id=UUID(ctx['instrument_id']),requested_start_day=ctx['start_day'],requested_end_day=ctx['end_day']), 'partial', ['所选标的历史任务已排队；单次最多6000行，实际覆盖见结果，不代表完整上市历史。']
    return write_once(request,identity[0],key,body,queue,status=202)


@router.get('/api/v1/data-fetch-runs', response_model=Envelope[Page[FetchRun]], responses=ERRORS, operation_id='listDataFetchRuns')
def list_runs(request: Request, source_id: UUID | None=None, job_id: UUID | None=None, cursor: str | None=Query(default=None,max_length=512),
              limit: int=Query(default=50,ge=1,le=100), identity=Depends(principal), db: Session=Depends(db_session)):
    stmt=select(DataFetchRun)
    if source_id:
        stmt=stmt.where(DataFetchRun.source_id==source_id)
    if job_id:
        stmt=stmt.where(DataFetchRun.job_id==job_id)
    rows,next_cursor=paged(db,stmt,DataFetchRun,identity[0].id,{'resource':'data_fetch_runs','source_id':source_id,'job_id':job_id},'received_at',cursor,limit)
    from app.market.minute_backfill import fetch_run_dto
    return envelope(request,{'items':[fetch_run_dto(db,row) for row in rows],'next_cursor':next_cursor},'complete' if rows else 'empty')


@router.get('/api/v1/market-quotes', response_model=Envelope[QuoteBatch], responses=ERRORS, operation_id='getMarketQuotes')
def cached_quotes(request: Request, instrument_id: UUID | None=None, as_of_time: AwareDatetime | None=None,
                  instrument_ids: str | None=Query(default=None, max_length=3700),
                  identity=Depends(principal), db: Session=Depends(db_session)):
    ids = None
    if instrument_ids is not None:
        from app.auth import APIError
        try:
            values = instrument_ids.split(',')
            ids = [UUID(value) for value in values]
            if instrument_id is not None or not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
                raise ValueError()
        except (ValueError, AttributeError):
            raise APIError(422, 'invalid_instrument_batch', '批量报价须传1至100个不重复的标的UUID，且不能同时传单个标的。') from None
    result=quote_batch(db,instrument_id=instrument_id,instrument_ids=ids,as_of_time=as_of_time)
    states={row.state for row in result.items}
    status='stale' if states=={'stale'} else 'partial' if states & {'available','partial','stale'} else 'empty' if states=={'empty'} else 'unavailable'
    return envelope(request,result,status,['只读取本机已保存报价；数据时点与首次接收分开，数量和范围不代表全市场或实时'],as_of_time=as_of_time)


@router.get('/api/v1/watchlist',response_model=Envelope[WatchlistView],responses=ERRORS,operation_id='getWatchlist')
def get_watchlist(request: Request,identity=Depends(principal),db: Session=Depends(db_session)):
    result=watchlist(db,identity[0].id)
    return envelope(request,result,'complete' if result.items else 'empty')


@router.post('/api/v1/watchlist',response_model=Envelope[WatchlistView],responses=ERRORS,operation_id='replaceWatchlist')
def replace_watchlist(body: WatchlistUpdate,request: Request,identity=Depends(csrf),key=Depends(business_key)):
    def save(db):
        result=update_watchlist(db,identity[0],body,request.state.request_id)
        return result,'complete' if result.items else 'empty',[]
    return write_once(request,identity[0],key,body,save,status=200)


@router.get('/api/v1/trading-datasets',response_model=Envelope[TradingDatasetList],responses=ERRORS,operation_id='listTradingDatasets')
def list_trading_datasets(request: Request,instrument_id: UUID | None=None,
    dataset: str | None=Query(default=None,pattern=r'^[a-z][a-z0-9_.]{1,79}$'),
    family: str | None=Query(default=None,pattern=r'^[a-z][a-z0-9_]{1,79}$'),day: date | None=None,
    source_id: UUID | None=None,as_of_time: AwareDatetime | None=None,
    identity=Depends(principal),db: Session=Depends(db_session)):
    result=trading_datasets(db,instrument_id,dataset,family,day,source_id,as_of_time)
    return envelope(request,result,'partial' if result.items else 'empty',
        ['按实际源数据日选择本机已存版本；原表可能含多日记录，按原交易日期读取。不补历史、不把接收日当交易日。',
         '无交易日资料通过view_rows按公告日或已接收批次回看，保持reference；不是严格当时接收重建。payload、id和hash仍指原始证据，原表不经筛改。'],as_of_time=as_of_time)


@router.get('/api/v1/reference-datasets',response_model=Envelope[DatasetList],responses=ERRORS,operation_id='listReferenceDatasets')
def reference_datasets(request: Request,instrument_id: UUID | None=None,dataset: str | None=Query(default=None,pattern=r'^[a-z][a-z0-9_.]{1,79}$'),
                       family: str | None=Query(default=None,pattern=r'^[a-z][a-z0-9_]{1,79}$'),day: date | None=None,
                       identity=Depends(principal),db: Session=Depends(db_session)):
    result=datasets(db,instrument_id,dataset,family,day)
    return envelope(request,result,'partial' if result.items else 'empty',['仅列本机有权来源的最新资料版本；样本和未知值不代表全市场完整覆盖。'])


@router.get('/api/v1/market-breadth-history',response_model=Envelope[BreadthSeries],responses=ERRORS,operation_id='getMarketBreadthHistory')
def market_breadth_history(request: Request, day: date | None=None, latest_only: bool=False,
    source: Literal['auto','tushare_market_rt','tencent_market_rt','ashare_breadth']='auto', identity=Depends(principal), db: Session=Depends(db_session)):
    result=breadth_history(db,day,latest_only=latest_only,source=source)
    return envelope(request,result,'partial' if result.items or result.latest else 'empty',
        ['按本机接收时间排列；Tushare系列按当时已接收的当日限制价计算，显示覆盖与源时段；免费备用系列源时点未知。不同来源不拼成一条曲线，不用于严格历史回放。'])


@router.get('/api/v1/market-news',response_model=Envelope[NewsList],responses=ERRORS,operation_id='listMarketNews')
def market_news(request: Request,family: Literal[*NEWS_FAMILIES] | None=None,
                identity=Depends(principal),db: Session=Depends(db_session)):
    result=news(db,family=family)
    return envelope(request,result,'partial' if result.items else 'empty',['仅显示已采集条目；发布时间、接收时间分开，原文链接不代表核查结论。'])


@router.get('/api/v1/market-bars',response_model=Envelope[BarSeries],responses=ERRORS,operation_id='getMarketBars')
def market_bars(request: Request,instrument_id: UUID,adjustment: Literal['unadjusted','forward','backward']='unadjusted',
                interval: Literal['1m','5m','15m','30m','60m','1d']='1d', day: date | None=None,
                source_id: UUID | None=None, include_live: bool=False, before: date | None=None,
                start_day: date | None=None, end_day: date | None=None, limit: int=Query(default=400,ge=1,le=2000),
                identity=Depends(principal),db: Session=Depends(db_session)):
    result=bars(db,instrument_id,adjustment,interval=interval,day=day,source_id=source_id,include_live=include_live,before=before,start_day=start_day,end_day=end_day,limit=limit)
    status='stale' if result.state=='stale' else 'partial' if result.items or result.current_daily else 'empty'
    return envelope(request,result,status,['只读本机有权缓存；来源时间、接收时间、区间结束及供应商最终确认分别显示。分钟不补缺口、不混源；实时日K为来源原生当日OHLC投影，不生成分钟。'])


@router.get('/api/v1/market-auction',response_model=Envelope[AuctionView],responses=ERRORS,operation_id='getMarketAuction')
def market_auction(request: Request,trade_day: date | None=None,family: Literal['eastmoney_auction','tencent_auction'] | None=None,
                   identity=Depends(principal),db: Session=Depends(db_session)):
    result=auction_view(db,trade_day=trade_day,family=family)
    status='partial' if any(item.state=='available' for item in result.items) else 'empty'
    return envelope(request,result,status,
        ['只读取本机已存竞价批次；来源时点未命中检查点±60秒时不标检查点，接收时间不冒充行情时点；汇总与明细按 batch_hash 配对，缺失不补造。'])


@router.get('/api/v1/data-coverage',response_model=Envelope[DataCoverage],responses=ERRORS,operation_id='getDataCoverage')
def data_coverage(request: Request,identity=Depends(principal),db: Session=Depends(db_session)):
    return envelope(request,coverage(db),'partial',['31类能力逐项核对；部分可用不表示整类字段或所有供应商已接通。'])


@router.get('/api/v1/workbench-view',response_model=Envelope[TerminalView],responses={**ERRORS,304:{'description':'Authorized cached representation is unchanged.'}},operation_id='getWorkbenchView')
def fixed_workbench(request: Request,view: Literal['overview','auction','ladder','funds']='overview',
    day: date | None=None,stock: str | None=Query(default=None,pattern=r'^\d{6}\.(SH|SZ|BJ)$'),
    sector: str | None=Query(default=None,pattern=r'^BK\d{4,8}\.DC$'),instrument_id: UUID | None=None,
    as_of_time: AwareDatetime | None=None,identity=Depends(principal),db: Session=Depends(db_session),
    fund_compare: str | None=Query(default=None,pattern=r'^BK\d{4,8}\.DC(?:,BK\d{4,8}\.DC){0,9}$')):
    from app.read_cache import cached_endpoint
    return cached_endpoint(request,db,identity[0],'workbench',dict(view=view,day=day,stock=stock,
        sector=sector,as_of_time=as_of_time,instrument_id=instrument_id,**({'fund_compare':fund_compare} if fund_compare else {})))


@router.get('/api/v1/workbench-details',response_model=Envelope[TerminalDetails],responses={**ERRORS,304:{'description':'Authorized cached representation is unchanged.'}},operation_id='getWorkbenchDetails')
def fixed_workbench_details(request: Request,day: date,
    codes: str=Query(default='',pattern=r'^(?:\d{6}\.(?:SH|SZ|BJ)(?:,\d{6}\.(?:SH|SZ|BJ)){0,23})?$'),
    sector: str | None=Query(default=None,pattern=r'^BK\d{4,8}\.DC$'),
    as_of_time: AwareDatetime | None=None,observation_cutoff: AwareDatetime | None=None,
    identity=Depends(principal),db: Session=Depends(db_session)):
    selected=list(dict.fromkeys(codes.split(','))) if codes else []
    from app.read_cache import cached_endpoint
    return cached_endpoint(request,db,identity[0],'details',dict(view='overview',day=day,
        sector=sector,as_of_time=as_of_time,observation_cutoff=observation_cutoff,detail_codes=selected))


@router.get('/api/v1/sector-membership', response_model=Envelope[SectorMembership], responses=ERRORS,
            operation_id='getSectorMembership')
def sector_membership(request: Request, day: date,
    sector: str=Query(pattern=r'^BK\d{4,8}\.DC$'), as_of_time: AwareDatetime | None=None,
    observation_cutoff: AwareDatetime | None=None, identity=Depends(principal), db: Session=Depends(db_session)):
    from app.market.sector_membership import read_membership
    result = read_membership(db, day, sector, as_of_time=as_of_time, observation_cutoff=observation_cutoff)
    return envelope(request, result, 'partial' if result.members else 'empty',
                    ['板块成分独立日期；报价由当前工作台提供。'], as_of_time=as_of_time)

@router.get('/api/v1/review-data/anomaly', responses=ERRORS, operation_id='getAnomalyMonitor')
def anomaly_monitor(request: Request, trade_day: date | None = None, as_of_time: AwareDatetime | None = None,
                    identity=Depends(principal)):
    from app.market.anomaly_archive import read_archive
    result = read_archive(trade_day, as_of_time)
    return envelope(request, result, 'complete' if result else 'empty', as_of_time=as_of_time)
