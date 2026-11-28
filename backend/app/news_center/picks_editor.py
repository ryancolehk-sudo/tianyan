"""Local editorial workflow. Reading a published edition never starts this command."""
import argparse
from datetime import datetime
import json
import sys

from sqlalchemy.orm import Session

from app.auth import require_instance
from app.config import settings
from app.db import engine
from app.evidence.common import now
from app.market.protocol import SHANGHAI
from app.news_center.picks import FAMILIES, RUBRIC, Submission, candidates, publish


def main():
    parser = argparse.ArgumentParser(description='天衍财经精选')
    commands = parser.add_subparsers(dest='action', required=True)
    read = commands.add_parser('candidates', help='读取本次允许用于 AI 编辑的公开标题与短摘要')
    read.add_argument('--cutoff', type=datetime.fromisoformat)
    read.add_argument('--offset', default=0, type=int)
    read.add_argument('--limit', default=60, type=int)
    read.add_argument('--families', nargs='+', choices=FAMILIES, required=True, help='本次获准处理的来源')
    read.add_argument('--brief', action='store_true', help='仅列标题；按固定截点取原生摘要')
    read.add_argument('--ids', nargs='+', help='只读取指定的证据版本')
    commands.add_parser('publish', help='从标准输入读取经过模型审阅的 JSON 刊稿')
    args = parser.parse_args()
    if settings().deployment_mode != 'local':
        parser.error('此命令用于本机财经精选')
    with Session(engine()) as db, db.begin():
        require_instance(db)
        if args.action == 'publish':
            raw = sys.stdin.buffer.read(96 * 1024 + 1)
            if len(raw) > 96 * 1024:
                parser.error('本期稿件过大')
            edition = publish(db, Submission.model_validate_json(raw))
            result = dict(day=str(edition.day), cutoff=edition.cutoff.isoformat(), count=len(edition.items))
        else:
            clock = now(db)
            cutoff = args.cutoff or clock.replace(second=0, microsecond=0)
            if cutoff.tzinfo is None or cutoff > clock or not 1 <= args.limit <= 200 or args.offset < 0:
                parser.error('请提供带时区的已知截点及有效分页')
            pool = candidates(db, cutoff, families=args.families)
            levels = {'important': 2, 'watch': 1, 'regular': 0}
            selected = [(key, entry) for key, entry in pool.items() if not args.ids or str(key) in args.ids]
            selected.sort(key=lambda pair: (levels[pair[1]['priority'].importance],
                pair[1]['row'].published_at or pair[1]['row'].received_at, pair[0]), reverse=True)
            items = []
            for key, entry in selected[args.offset:args.offset+args.limit]:
                row = entry['row']
                item = dict(id=str(key), source=row.source_name, title=row.title,
                    published=row.published_at.astimezone(SHANGHAI).isoformat(timespec='minutes') if row.published_at else row.published_day,
                    copies=1 + len(entry['peers']))
                if not args.brief:
                    item['excerpt'] = entry['excerpt']
                items.append(item)
            result = dict(cutoff=cutoff.isoformat(), rubric=RUBRIC, total=len(selected), offset=args.offset, items=items)
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
