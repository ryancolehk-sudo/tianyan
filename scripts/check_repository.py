"""Offline repository checks. A pass is not runtime or deployment acceptance."""
import ast
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

from generate_client import generate

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.venv', 'node_modules', 'dist', 'backups', 'artifacts', '__pycache__',
            'output', 'private', 'credentials', 'secrets', 'logs', 'cache'}


def files(pattern):
    return [p for p in ROOT.rglob(pattern)
            if not EXCLUDED.intersection(p.relative_to(ROOT).parts)
            and not any(part.endswith('.private') or '.private.' in part
                        for part in p.relative_to(ROOT).parts)]


def main():
    checks = []
    python_files = files('*.py')
    for path in python_files:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    checks.append(f'Python syntax: {len(python_files)} files')
    json_files = files('*.json')
    for path in json_files:
        json.loads(path.read_text(encoding='utf-8'))
    checks.append(f'JSON syntax: {len(json_files)} files')
    spec = json.loads((ROOT / 'contracts/openapi.p2.json').read_text(encoding='utf-8'))
    refs = []
    def walk(value):
        if isinstance(value, dict):
            if '$ref' in value:
                target = value['$ref']
                assert target.startswith('#/'), f'Unexpected remote schema: {target}'
                node = spec
                for key in target[2:].split('/'):
                    node = node[key.replace('~1', '/').replace('~0', '~')]
                refs.append(target)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(spec)
    checks.append(f'Implemented contract references: {len(refs)} resolve')
    route_files = [ROOT / 'backend/app/main.py', ROOT / 'backend/app/evidence/api.py', ROOT / 'backend/app/research/api.py', ROOT / 'backend/app/observations/api.py', ROOT / 'backend/app/validations/api.py', ROOT / 'backend/app/review_archives/api.py', ROOT / 'backend/app/market/api.py', ROOT / 'backend/app/news_center/api.py', ROOT / 'backend/app/market/review_api.py', ROOT / 'backend/app/ai/api.py', ROOT / 'backend/app/integrations/api.py', ROOT / 'backend/app/chatgpt/api.py', ROOT / 'backend/app/chatgpt/model_api.py', ROOT / 'backend/app/system_settings/api.py', ROOT / 'backend/app/updates/api.py', ROOT / 'backend/app/screening/api.py']
    route_files.append(ROOT / 'backend/app/tomorrow/api.py')
    route_files.append(ROOT / 'backend/app/market/direction_api.py')
    route_files.append(ROOT / 'backend/app/market/recovery_api.py')
    route_files.append(ROOT / 'backend/app/market/market_radar.py')
    route_files.append(ROOT / 'backend/app/market/updates.py')
    route_files.append(ROOT / 'backend/app/integrations/ifind.py')
    route_files.append(ROOT / 'backend/app/market/kpl_strength_api.py')
    route_files.append(ROOT / 'backend/app/market/ths_funds_api.py')
    actual = {}
    functions = []
    for route_file in route_files:
        body = ast.parse(route_file.read_text(encoding='utf-8')).body
        prefixes = {}
        for node in body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == 'APIRouter':
                prefix = next((ast.literal_eval(k.value) for k in node.value.keywords if k.arg == 'prefix'), '')
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        prefixes[target.id] = prefix
        functions.extend((node, prefixes) for node in body)
    for function, prefixes in functions:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in function.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr in {'get','post','delete','patch','put'}:
                path = prefixes.get(decorator.func.value.id, '') + ast.literal_eval(decorator.args[0])
                operation_id = next(ast.literal_eval(k.value) for k in decorator.keywords if k.arg == 'operation_id')
                actual[(path, decorator.func.attr)] = operation_id
    expected = {(path, method): op['operationId'] for path, ops in spec['paths'].items() for method, op in ops.items()}
    assert actual == expected, 'Implemented route/method/operationId drift'
    checks.append(f'Implemented route/method/operationId: {len(actual)} match (static only)')
    assert (ROOT / 'frontend/src/generated/schema.ts').read_text(encoding='utf-8') == generate(), 'Generated client drift'
    checks.append('Generated TypeScript types: no drift')
    broken = []
    for p in files('*.md'):
        # Only relative file links; skip external URLs and anchor targets.
        for target in re.findall(r'\]\(([^)]+)\)', p.read_text(encoding='utf-8')):
            if ':' in target or target.startswith(('#','/')):
                continue
            target = unquote(target.split('#',1)[0].strip('<>'))
            if target and not (p.parent / target).exists():
                broken.append(f'{p.relative_to(ROOT)} -> {target}')
    assert not broken, '\n'.join(broken)
    checks.append('Maintained Markdown relative file links: resolve')
    print('\n'.join('PASS ' + check for check in checks))
    print('NOT RUN: dependency installation/build, PostgreSQL, browser, backup/restore, HTTPS.')
    return checks


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, KeyError, SyntaxError, ValueError) as e:
        print(f'FAIL {e}', file=sys.stderr)
        raise SystemExit(1) from None
