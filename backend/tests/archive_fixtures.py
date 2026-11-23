"""Self-created synthetic reports, never copied research, news, prices or opinions."""
import hashlib
import html
import json


def artifact(path, text):
    raw = text.encode("utf-8")
    return {"path": path, "text": text, "sha256": hashlib.sha256(raw).hexdigest(),
        "blob": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}


def archive_body(version=1, *, target="2002-01-02", summary=True):
    header = ("当前复盘数据截止2001-12-31 15:10:00。\n"
        f"以下内容针对目标交易日{target}进行复盘：\n"
        f"依据2001-12-31收盘资料｜第{version}版｜晚间复盘定稿2001-12-31 21:00:00")
    full = header + "\n\n市场判断\n这是合成归档原文，不是真实市场结论。\n\n重点方向\n合成研究方向，仅验证原文展示。\n\n开盘预案\n合成条件，不是当前预案。\n\n风险与边界\n没有真实行情，不判断收益。\n"
    card = {"schema": "2.0", "config": {}, "header": {"title": {"content": "合成总结"}}, "body": {"elements": [
        {"tag": "column_set", "columns": [{"tag": "column", "elements": [
            {"tag": "markdown", "content": "<font color='grey'>" + html.escape(header) + "</font>"},
            {"tag": "markdown", "content": "**市场判断**\n合成总结保留原句，不自动改写。"},
            {"tag": "markdown", "content": "**风险与边界**\n仅用于隔离验证，不是当前可执行预案。"}]}]},
    ]}}
    return {"source_repository": "example/research-archive", "source_commit": "a" * 40,
        "full": artifact(f"docs/synthetic-{target}-v{version}.md", full),
        "summary": artifact(f"docs/synthetic-{target}-v{version}.json", json.dumps(card, ensure_ascii=False)) if summary else None,
        "rights_basis": "Self-created synthetic report, no private research or actual market data"}
