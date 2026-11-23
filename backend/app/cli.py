import argparse
import getpass
import json
import re
import sys

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import hasher, require_instance
from app.db import engine
from app.models import AuditEvent, Job, User


def import_review_archive(username, value):
    from app.review_archives.schemas import ArchiveInput
    from app.review_archives.service import import_archive
    body = ArchiveInput.model_validate(value)
    with Session(engine()) as db, db.begin():
        require_instance(db)
        user = db.scalar(select(User).where(User.username == username, User.enabled.is_(True)))
        if not user or user.role != "admin":
            raise ValueError("本机运维导入只允许指定已启用的管理员。")
        imported = import_archive(db, user, body, None)[0]
        return {"id": str(imported.id), "target_trade_day": str(imported.target_trade_day),
            "source_version": imported.source_version, "has_summary": imported.has_summary}


def bootstrap(username: str, password: str):
    if not re.fullmatch(r"[a-z0-9_.-]{1,80}", username):
        raise ValueError("账号限小写英文字母、数字、点、横线、下划线，最多80字符。")
    if not 12 <= len(password) <= 1024:
        raise ValueError("密码需要12至1024个字符。")
    with Session(engine()) as db, db.begin():
        require_instance(db)
        db.execute(text("SELECT pg_advisory_xact_lock(73401901)"))
        if db.scalar(select(func.count()).select_from(User)):
            raise ValueError("实例已初始化，拒绝覆盖现有账号。")
        user = User(username=username, password_hash=hasher.hash(password), role="admin")
        db.add(user)
        db.flush()
        db.add(AuditEvent(actor_id=user.id, action="instance.bootstrap"))


def configure_free_data(username):
    """Use the audited source service; preserve existing configuration, no fetch."""
    from urllib.parse import urlsplit
    from app.config import settings
    from app.market.models import SourceSettings
    from app.market.schemas import SourceConfiguration
    from app.market.service import configure_source, source_id
    if urlsplit(settings().public_origin).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('免费预设仅用于本机个人研究实例，远程部署需另核使用范围。')
    bases = {
        'cninfo': '本机个人非商业研究；巨潮法律声明允许非商业浏览和下载。仅保存公开代码、简称和目录身份，不出售或再分发。依据：https://www.cninfo.com.cn/new/snapshot/companyDetailCn?code=002270；2026-10-20核对。',
        'baostock_daily': '本机个人研究使用官方公开匿名API；发布者明确Free china stock market data与金融分析用途。仅保存盘后未复权数据和日历，不承诺实时或商业再分发。依据：https://pypi.org/project/baostock/0.9.4/；2026-10-20核对；客户端元数据BSD，完整许可制品缺口另记REUSE，不扩大为公开分发授权。',
    }
    result = []
    with Session(engine()) as db, db.begin():
        require_instance(db)
        owner = db.scalar(select(User).where(User.username == username, User.enabled.is_(True)))
        if owner is None or owner.role != 'admin':
            raise ValueError('只能指定本机已启用的管理员，原账号不作修改。')
        for family, basis in bases.items():
            if db.get(SourceSettings, source_id(family)):
                result.append({'family': family, 'result': 'existing_configuration_preserved'})
                continue
            body = SourceConfiguration(family=family, symbols=[], interval_seconds=86400 if family == 'cninfo' else 21600,
                rights_basis=basis, entitlement_status='verified', enabled=True,
                rights={'display': 'allowed', 'cache': 'allowed', 'export': 'denied', 'redistribute': 'denied'})
            configure_source(db, owner, body)
            result.append({'family': family, 'result': 'configured_without_fetch'})
    return result


def configure_public_data(username):
    """Local operator invokes the same audited configuration service, no secrets."""
    from app.market.models import SourceSettings
    from app.market.registry import SOURCES
    from app.market.schemas import SourceConfiguration
    from app.market.service import configure_source, source_id
    result=configure_free_data(username)  # Reuses the loopback and admin checks.
    bases={
        'baostock_history':'BaoStock官方免费匿名API，本机个人研究；近180自然日日线/复权/估值，保留供应商口径。https://pypi.org/project/baostock/0.9.4/；2026-10-20核验，非实时，不公开分发。',
        'baostock_fundamental':'BaoStock官方免费匿名API，本机个人研究；最近四期财务指标、证券资料和上年分红，并非完整原始三表。https://pypi.org/project/baostock/0.9.4/；2026-10-20核验，不公开分发。',
        'baostock_classification':'BaoStock官方免费匿名API，本机个人研究；行业分类及三指数成分，仅当前来源版本。https://pypi.org/project/baostock/0.9.4/；2026-10-20核验，不公开分发。',
        'ecb_public':'欧洲央行允许免费使用其直接发布信息，准确引用并注明出处；仅每日参考汇率。https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html；2026-10-20核验。',
        'worldbank_public':'World Bank WDI公开API及CC-BY4.0说明；明确署名和原始指标，年度数据仅供本机参考。https://datacatalog.worldbank.org/public-licenses；2026-10-20核验。',
        'ecb_news':'欧洲央行原生RSS和官方允许免费使用信息的说明；保存标题/原文链接/源摘要/发布时间，准确署名，不采第三方文章。https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html；2026-10-20核验。',
        'fed_public':'美联储官方RSS；其免责声明说明网站原生资料除另行声明外属公共领域。仅保存官方条目，不采第三方或图片。https://www.federalreserve.gov/disclaimer.htm；2026-10-20核验。',
        'cninfo_announcements':'巨潮法律声明允许非商业浏览下载；仅本机个人研究的最近七日公开公告首页标题、出处链接和时间，不下载PDF、不再分发。https://www.cninfo.com.cn/new/snapshot/companyDetailCn?code=002270；2026-10-20核验。',
    }
    with Session(engine()) as db,db.begin():
        owner=db.scalar(select(User).where(User.username==username,User.enabled.is_(True)))
        if owner is None or owner.role!='admin':raise ValueError('Existing local admin required.')
        for family,basis in bases.items():
            if db.get(SourceSettings,source_id(family)):
                result.append({'family':family,'result':'existing_configuration_preserved'});continue
            symbols=([{'exchange':'SSE','asset_type':'index','symbol':'000001'},{'exchange':'SZSE','asset_type':'stock','symbol':'000001'},
                {'exchange':'SSE','asset_type':'stock','symbol':'600519'}] if family=='baostock_history' else
                [{'exchange':'SZSE','asset_type':'stock','symbol':'000001'}] if family=='baostock_fundamental' else [])
            body=SourceConfiguration(family=family,symbols=symbols,interval_seconds=SOURCES[family].minimum_interval,
                rights_basis=basis,entitlement_status='verified',enabled=True,
                rights={'display':'allowed','cache':'allowed','export':'denied','redistribute':'denied'})
            configure_source(db,owner,body);result.append({'family':family,'result':'configured_without_fetch'})
    return result


def configure_quote_data(username):
    from urllib.parse import urlsplit
    from app.config import settings
    from app.market.models import SourceSettings
    from app.market.registry import INDEXES, SOURCES
    from app.market.schemas import SourceConfiguration
    from app.market.service import configure_source, source_id
    if settings().app_mode!='real' or urlsplit(settings().public_origin).hostname not in {'127.0.0.1','localhost','::1'}:
        raise ValueError('Quote presets are limited to local personal instances.')
    result=[]
    with Session(engine()) as db,db.begin():
        require_instance(db)
        owner=db.scalar(select(User).where(User.username==username,User.enabled.is_(True)))
        if owner is None or owner.role!='admin':raise ValueError('Existing local admin required.')
        for family,basis in {
            'tickflow_live':'本人注册的TickFlow官方免费档，本机个人研究；10次/分钟、每次最多5标的，仅快照；凭据本机配置，实际权益和时延由真实返回另验。https://tickflow.org/pricing/；2026-10-20核验，不公开再分发。',
            'ashare_public':'AShareAPI官方公开匿名接口及服务条款允许自己的应用/研究分析使用；本机最多5标的、300秒间隔，约5分钟更新/源仅日期，不称实时。https://ashareapi.com/terms/；2026-10-20核验，不原样转售或公开建库。',
        }.items():
            if db.get(SourceSettings,source_id(family)):
                result.append({'family':family,'result':'existing_configuration_preserved'});continue
            symbols=[{'exchange':exchange,'symbol':symbol,'asset_type':'index'} for exchange,symbol,_ in INDEXES]
            symbols.append({'exchange':'SZSE','symbol':'000001','asset_type':'stock'})
            body=SourceConfiguration(family=family,symbols=symbols,interval_seconds=SOURCES[family].minimum_interval,
                rights_basis=basis,entitlement_status='verified',enabled=True,
                rights={'display':'allowed','cache':'allowed','export':'denied','redistribute':'denied'})
            configure_source(db,owner,body)
            result.append({'family':family,'result':'configured_without_fetch'})
    return result


def configure_breadth_data(username):
    from urllib.parse import urlsplit
    from app.config import settings
    from app.market.models import SourceSettings
    from app.market.schemas import SourceConfiguration
    from app.market.service import configure_source, source_id
    if settings().app_mode != 'real' or urlsplit(settings().public_origin).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('Breadth presets are limited to local personal instances.')
    with Session(engine()) as db, db.begin():
        require_instance(db)
        owner = db.scalar(select(User).where(User.username == username, User.enabled.is_(True)))
        if owner is None or owner.role != 'admin':
            raise ValueError('Existing local admin required.')
        if db.get(SourceSettings, source_id('ashare_breadth')):
            return [{'family': 'ashare_breadth', 'result': 'existing_configuration_preserved'}]
        body = SourceConfiguration(family='ashare_breadth', symbols=[], interval_seconds=300, enabled=True, entitlement_status='verified',
            rights_basis='AShareAPI官方免费changedist及条款允许本机个人应用/研究分析；2026-10-20低频实测，源日期/完整分母未知，不称实时全市场。https://ashareapi.com/terms/；每300秒采集，共享35秒匿名请求预算，不公开再分发。',
            rights={'display': 'allowed', 'cache': 'allowed', 'export': 'denied', 'redistribute': 'denied'})
        configure_source(db, owner, body)
    return [{'family': 'ashare_breadth', 'result': 'configured_without_fetch'}]


def main():
    parser = argparse.ArgumentParser(description="天衍系统运维入口；不提供默认密码。")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("bootstrap", help="仅在空实例创建管理员")
    init.add_argument("--username", required=True)
    init.add_argument("--password-stdin", action="store_true", help="自动化通过标准输入传入，不写命令参数")
    check = sub.add_parser("enqueue-check", help="提交内部健康检查任务")
    check.add_argument("--username", required=True)
    archive = sub.add_parser("import-review-archive", help="从标准输入导入私有复盘归档JSON；不联网、不发布预案")
    archive.add_argument("--username", required=True)
    sources = sub.add_parser('configure-free-data', help='为本机个人研究配置巨潮目录和BaoStock盘后数据；保留现有配置，不采集')
    sources.add_argument('--username', required=True)
    public_sources=sub.add_parser('configure-public-data',help='为本机个人研究配置已核验的免费历史、财务、分类、官方资讯与参考数据；保留原配置')
    public_sources.add_argument('--username',required=True)
    quotes=sub.add_parser('configure-quote-data',help='配置本机免费实时候选与五分钟参考；Key不在参数中传递，保留原配置')
    quotes.add_argument('--username',required=True)
    breadth=sub.add_parser('configure-breadth-data',help='配置免费供应商广度参考；未知日期保留，保留原来源配置')
    breadth.add_argument('--username',required=True)
    args = parser.parse_args()
    if args.command == "bootstrap":
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("设置管理员密码: ")
        if not args.password_stdin and password != getpass.getpass("再次输入密码: "):
            parser.error("两次密码不一致。")
        try:
            bootstrap(args.username, password)
        except ValueError as e:
            parser.error(str(e))
        print("管理员已创建；密码不会显示或记录。")
    elif args.command in {'configure-free-data','configure-public-data','configure-quote-data','configure-breadth-data'}:
        try:
            handler={'configure-free-data':configure_free_data,'configure-public-data':configure_public_data,'configure-quote-data':configure_quote_data,'configure-breadth-data':configure_breadth_data}[args.command]
            print(json.dumps(handler(args.username), ensure_ascii=False))
        except (ValueError, SQLAlchemyError):
            parser.exit(1, '免费来源配置未完成：请核对本机实例、管理员及数据库状态；未显示敏感内容。\n')
    elif args.command == "import-review-archive":
        try:
            from app.auth import APIError
            from pydantic import ValidationError
            raw = sys.stdin.buffer.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("导入包过大。")
            result = import_review_archive(args.username, json.loads(raw))
        except (ValueError, ValidationError, APIError, SQLAlchemyError):
            parser.exit(1, "归档导入未完成：请核对原稿头、同版卡片、文件hash、来源冲突与管理员权限；原文未输出。\n")
        print(json.dumps(result))
    else:
        with Session(engine()) as db, db.begin():
            require_instance(db)
            user = db.scalar(select(User).where(User.username == args.username, User.enabled.is_(True)))
            if not user or user.role != "admin":
                parser.error("仅允许为已启用的管理员创建内部检查任务。")
            job = Job(owner_id=user.id, kind="system.check")
            db.add(job)
            db.flush()
            print(job.id)


if __name__ == "__main__":
    main()
