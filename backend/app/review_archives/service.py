"""Import exact saved outputs. No remote reads, summaries, publication or model calls."""
import html
import json
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.auth import APIError
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.service import audit
from app.review_archives import schemas as s
from app.review_archives.models import ReviewArchive

DAY = r"\d{4}-\d{2}-\d{2}"
TIME = DAY + r" \d{2}:\d{2}:\d{2}"
HEADER = re.compile(r"^当前复盘数据截止(?P<cutoff>" + TIME + r")。\r?\n"
    r"以下内容针对目标交易日(?P<target>" + DAY + r")进行复盘：\r?\n"
    r"依据(?P<based>" + DAY + r")收盘资料｜第(?P<version>[1-9]\d{0,5})版｜"
    r"(?P<stage>[^\r\n]{1,80}?)定稿(?P<generated>" + TIME + r")(?=\r?\n|$)")


def parse_header(text):
    match = HEADER.match(text)
    if not match:
        raise APIError(422, "archive_header_invalid", "原稿缺少可识别的依据日、适用日、版号和时点，不按文件名猜测。")
    try:
        result = {"based_on_trade_day": date.fromisoformat(match["based"]), "target_trade_day": date.fromisoformat(match["target"]),
            "source_version": int(match["version"]), "source_stage": match["stage"],
            "evidence_cutoff_at": datetime.fromisoformat(match["cutoff"]).replace(tzinfo=ZoneInfo("Asia/Shanghai")),
            "source_generated_at": datetime.fromisoformat(match["generated"]).replace(tzinfo=ZoneInfo("Asia/Shanghai"))}
    except ValueError:
        raise APIError(422, "archive_header_invalid", "原稿中的日期或时点格式无效。") from None
    if result["target_trade_day"] <= result["based_on_trade_day"] or result["evidence_cutoff_at"] > result["source_generated_at"]:
        raise APIError(422, "archive_time_invalid", "原稿适用日或截止与定稿时点不一致。")
    return result, match[0].replace("\r\n", "\n")


def summary_text(raw, expected_header):
    try:
        card = json.loads(raw)
        if card.get("schema") != "2.0" or not isinstance(card["body"]["elements"], list):
            raise ValueError()
        contents = []
        def visit(node, depth=0):
            if not isinstance(node, dict) or depth > 12 or len(contents) > 100:
                raise ValueError()
            tag = node.get("tag")
            if tag == "markdown" and isinstance(node.get("content"), str):
                # Legacy cards escape text and wrap metadata in a font element.
                # Remove only this presentation wrapper; remaining text stays literal.
                contents.append(html.unescape(re.sub(r"</?font\b[^>]*>", "", node["content"])).strip())
            elif tag in {"column_set", "column"}:
                children = node.get("columns" if tag == "column_set" else "elements")
                if not isinstance(children, list):
                    raise ValueError()
                for child in children:
                    visit(child, depth + 1)
            else:
                raise ValueError()
        for node in card["body"]["elements"]:
            visit(node)
        if not contents or contents[0].replace("\r\n", "\n") != expected_header:
            raise APIError(422, "archive_summary_mismatch", "总结卡片与完整稿的日期、版本或时点不一致，不能混用。")
        if len(contents) < 2 or not any(contents[1:]):
            raise ValueError()
        return "\n\n".join(contents[1:])
    except APIError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise APIError(422, "archive_summary_invalid", "总结卡片格式尚不支持；请保留原文件，不能用自动摘要替代。") from None


def summary(row):
    return s.ArchiveSummary(id=row.id, based_on_trade_day=row.based_on_trade_day, target_trade_day=row.target_trade_day,
        source_version=row.source_version, source_stage=row.source_stage, evidence_cutoff_at=row.evidence_cutoff_at.astimezone(UTC),
        source_generated_at=row.source_generated_at.astimezone(UTC), imported_at=row.imported_at.astimezone(UTC), has_summary=row.summary_text is not None,
        source_repository=row.source_repository, source_commit=row.source_commit, content_hash=row.content_hash)


def details(row):
    body = row.payload
    return s.ReviewArchive(**summary(row).model_dump(), full_text=body["full"]["text"], summary_text=row.summary_text,
        full_source=s.file_info(row.source_repository, row.source_commit, body["full"]),
        summary_source=s.file_info(row.source_repository, row.source_commit, body["summary"]) if body["summary"] else None,
        rights_basis=body["rights_basis"])


def import_archive(db, owner, body, request_id):
    metadata, header = parse_header(body.full.text)
    imported = now(db)
    if metadata["source_generated_at"] > imported:
        raise APIError(422, "archive_future", "原稿定稿时间晚于本机接收时间，不能作为已有归档导入。")
    extracted = summary_text(body.summary.text, header) if body.summary else None
    identity = content_hash({"repository": body.source_repository, "commit": body.source_commit, "path": body.full.path})
    digest = content_hash(body)
    lock(db, f"review-archive:{owner.id}:{identity}")
    duplicate = db.scalar(select(ReviewArchive).where(ReviewArchive.owner_id == owner.id, ReviewArchive.source_identity == identity))
    if duplicate:
        if duplicate.content_hash != digest:
            raise APIError(409, "archive_source_conflict", "同一来源文件已导入且内容不同，原归档不会被覆盖。")
        return details(duplicate), "partial", ["相同归档已存在，未重复导入；归档内容尚未由本系统核验"]
    row = ReviewArchive(owner_id=owner.id, source_identity=identity, source_repository=body.source_repository,
        source_commit=body.source_commit, **metadata, imported_at=imported, payload=normalized(body), summary_text=extracted, content_hash=digest)
    db.add(row); db.flush()
    audit(db, owner.id, "review_archive.import", request_id, "review_archive", row.id)
    return details(row), "partial", ["旧项目研究归档，仅供回查，不是当前正式预案或已核验市场事实"]
