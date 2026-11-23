"""Read both persisted DC member scopes without rewriting source configuration."""
import re

from app.market.protocol import SourceFailure


def member_sector_codes(config):
    modern = config.get('sector_codes', [])
    legacy = config.get('board_codes', [])
    if not isinstance(modern, list) or not isinstance(legacy, list):
        raise SourceFailure('sector_codes_required')
    if legacy and (len(legacy) != 1 or any(not isinstance(code, str) or not re.fullmatch(r'BK\d{4}', code) for code in legacy)):
        raise SourceFailure('board_required')
    converted = [code + '.DC' for code in legacy]
    if modern and converted and modern != converted:
        raise SourceFailure('conflicting_sector_scope')
    result = modern or converted
    if not 1 <= len(result) <= 10 or any(not isinstance(code, str) or not re.fullmatch(r'BK\d{4,8}\.DC', code) for code in result):
        raise SourceFailure('sector_codes_required')
    if len(set(result)) != len(result):
        raise SourceFailure('duplicate_sector_code')
    return list(result)
