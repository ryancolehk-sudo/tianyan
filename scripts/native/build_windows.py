"""Build a self-contained Windows x64 program and NSIS installer, without Docker.

Build tools run on the maintainer's Windows machine. End users install the EXE;
they do not install Python, Node, PostgreSQL, NSIS or Docker themselves.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def download(asset, cache):
    target = cache / asset['filename']
    if not target.exists():
        temporary = target.with_suffix(target.suffix + '.pending')
        request = urllib.request.Request(asset['url'], headers={'User-Agent': 'Tianyan-Windows-builder/0.5'})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        if digest(temporary) != asset['sha256']:
            raise ValueError('download_hash_mismatch:' + asset['filename'])
        temporary.replace(target)
    if digest(target) != asset['sha256']:
        raise ValueError('cached_hash_mismatch:' + asset['filename'])
    return target


def extract(archive, destination, *, prefix=None):
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            name = item.filename.replace('\\', '/')
            parts = name.split('/')
            if name.startswith('/') or '..' in parts or ':' in name or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError('unsafe_program_archive')
            if prefix is not None:
                if not name.startswith(prefix):
                    continue
                name = name[len(prefix):]
            if not name:
                continue
            target = destination / name
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as input_stream, target.open('wb') as output:
                    shutil.copyfileobj(input_stream, output)


def program_version():
    tree = ast.parse((ROOT / 'backend/app/version.py').read_text(encoding='utf-8'))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            values[node.targets[0].id] = node.value.value
    return values['APP_VERSION'], values['SCHEMA_REVISION']


def copy_tracked_tree(relative, destination):
    """Ship reviewed source files only; ignored local files are never inputs."""
    entries = subprocess.check_output(
        ['git', 'ls-files', '--stage', '-z', '--', relative], cwd=ROOT).split(b'\0')
    source = (ROOT / relative).resolve()
    if not source.is_relative_to(ROOT.resolve()):
        raise ValueError('unsafe_tracked_program_input')
    copied = 0
    for entry in entries:
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, _, stage = metadata.decode('ascii').split()
        path = ROOT / raw_path.decode('utf-8')
        if (mode not in {'100644', '100755'} or stage != '0'
                or path.is_symlink() or not path.resolve().is_relative_to(source)):
            raise ValueError('unsafe_tracked_program_input')
        target = destination / path.relative_to(ROOT / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    if not copied:
        raise ValueError('tracked_program_inputs_missing')


def check_frontend_release_inputs():
    """Vite copies public files even when Git ignores them; reject local inputs."""
    for ignored in (False, True):
        args = ['git', 'ls-files', '--others', '--exclude-standard', '-z']
        if ignored:
            args.append('--ignored')
        entries = subprocess.check_output([*args, '--', 'frontend'], cwd=ROOT).split(b'\0')
        for entry in entries:
            if not entry:
                continue
            parts = Path(entry.decode('utf-8')).parts
            # npm ci replaces dependencies, and the build empties dist. All
            # other untracked or ignored inputs could reach public JavaScript.
            if len(parts) > 2 and parts[1] in {'node_modules', 'dist'}:
                continue
            raise ValueError('unreviewed_frontend_input_not_allowed_in_release')


def prepare_frontend(frontend=None, *, development=False):
    if frontend is not None and not development:
        raise ValueError('external_frontend_requires_development')
    node = shutil.which('node')
    npm = shutil.which('npm.cmd' if sys.platform == 'win32' else 'npm')
    if not node or not npm:
        raise ValueError('node_24_and_npm_builder_required')
    node_version = subprocess.check_output([node, '--version'], cwd=ROOT, text=True).strip()
    if not re.fullmatch(r'v24\.\d+\.\d+', node_version):
        raise ValueError('node_24_builder_required')
    source = ROOT / 'frontend'
    # Vite embeds VITE_* values in public JavaScript. Release builds must not
    # inherit a developer's ignored dotenv files or shell configuration.
    if not development and (
        any(path.name != '.env.example' for path in source.glob('.env*'))
        or any(key.startswith('VITE_') for key in os.environ)
    ):
        raise ValueError('private_frontend_configuration_not_allowed_in_release')
    resolved_source = source.resolve()
    if not resolved_source.is_relative_to(ROOT.resolve()) or any(
        not (source / name).resolve().is_relative_to(resolved_source) for name in ('node_modules', 'dist')
    ):
        raise ValueError('frontend_build_directory_outside_source')
    if not development:
        check_frontend_release_inputs()
    # npm ci verifies package-lock integrity and installs the build tools even
    # when the maintainer's shell has NODE_ENV=production. Never reuse a local
    # node_modules tree or treat an ignored dist as a release input.
    subprocess.run([npm, 'ci', '--include=dev', '--no-audit', '--no-fund'], check=True, cwd=source)
    if frontend is None:
        subprocess.run([npm, 'run', 'build', '--', '--emptyOutDir'], check=True, cwd=source)
        frontend = source / 'dist'
        method = 'npm-ci-and-production-build'
    else:
        frontend = Path(frontend)
        method = 'development-external'
    if not (frontend / 'index.html').is_file():
        raise ValueError('production_frontend_dist_required')
    return frontend, {'method': method, 'node_version': node_version,
                      'package_lock_sha256': digest(source / 'package-lock.json')}


def build(args):
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise ValueError('windows_x64_builder_required')
    if sys.version_info[:2] != (3, 14):
        raise ValueError('python_314_builder_required')
    if args.frontend is not None and not args.development:
        raise ValueError('external_frontend_requires_development')
    version, schema = program_version()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('git_revision_required')
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT, text=True).strip())
    untracked_inputs = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '--',
        'backend/app', 'database', 'packaging/windows', 'scripts/native', 'frontend'], cwd=ROOT, text=True).strip()
    dirty = dirty or bool(untracked_inputs)
    if dirty and not args.development:
        raise ValueError('release_requires_clean_tracked_tree')
    subprocess.run([sys.executable, str(ROOT / 'scripts/check_security.py'),
                    '--staged', '--repo', str(ROOT)], check=True, cwd=ROOT)
    identity = version + '-' + revision[:12] + ('-development' if args.development else '')
    work = args.output / identity
    if work.exists():
        raise ValueError('build_output_exists_choose_new_directory')
    frontend, frontend_build = prepare_frontend(args.frontend, development=args.development)
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT / 'packaging/windows/assets.lock.json').read_text(encoding='utf-8'))
    assets = {key: download(lock[key], args.cache) for key in ('python', 'postgres', 'winsw', 'nsis', 'vcredist')}
    program = work / 'program'
    program.mkdir(parents=True)
    extract(assets['python'], program / 'runtime')
    (program / 'runtime' / 'python314._pth').write_text(
        'python314.zip\n.\nLib/site-packages\n../backend\nimport site\n', encoding='ascii')
    # Wheel content is generated from the same hash-locked runtime dependencies,
    # never copied from the developer's venv or from an installed user profile.
    site = program / 'runtime' / 'Lib' / 'site-packages'
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
        '--require-hashes', '--no-compile', '--target', str(site), '-r', str(ROOT / 'backend/requirements.lock'),
        '--cache-dir', str(args.cache / 'pip')], check=True, cwd=ROOT)
    copy_tracked_tree('backend/app', program / 'backend/app')
    shutil.copy2(ROOT / 'backend/alembic.ini', program / 'backend/alembic.ini')
    copy_tracked_tree('database', program / 'database')
    shutil.copytree(frontend, program / 'frontend')
    shutil.copy2(ROOT / 'docs/WINDOWS_INSTALL.md', program / 'WINDOWS_INSTALL.md')
    # Keep only the server, client and resource trees; never include an EDB
    # sample cluster, pgAdmin user profile or developer database.
    for part in ('bin', 'lib', 'share'):
        extract(assets['postgres'], program / 'postgresql' / part, prefix='pgsql/' + part + '/')
    with zipfile.ZipFile(assets['postgres']) as archive:
        for name in ('server_license.txt', 'commandlinetools_3rd_party_licenses.txt'):
            (program / 'postgresql' / name).write_bytes(archive.read('pgsql/' + name))
    shutil.copy2(assets['winsw'], program / 'TianyanService.exe')
    (program / 'redist').mkdir()
    shutil.copy2(assets['vcredist'], program / 'redist' / 'VC_redist.x64.exe')
    licenses = program / 'licenses'
    licenses.mkdir()
    shutil.copy2(ROOT / 'packaging/windows/assets.lock.json', licenses / 'assets.lock.json')
    shutil.copy2(ROOT / 'backend/requirements.lock', licenses / 'requirements.lock')
    shutil.copy2(ROOT / 'packaging/windows/THIRD_PARTY_NOTICES.md', licenses / 'THIRD_PARTY_NOTICES.md')
    shutil.copy2(ROOT / 'packaging/windows/WinSW-LICENSE.txt', licenses / 'WinSW-LICENSE.txt')
    frontend_lock = json.loads((ROOT / 'frontend/package-lock.json').read_text(encoding='utf-8'))
    for name, pinned in frontend_lock['packages'].items():
        if not name or pinned.get('dev'):
            continue
        package = ROOT / 'frontend' / name
        metadata = json.loads((package / 'package.json').read_text(encoding='utf-8'))
        if metadata['version'] != pinned['version']:
            raise ValueError('frontend_dependency_version_mismatch:' + name)
        notices = [path for path in package.iterdir() if path.is_file() and
            path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))]
        if not notices:
            raise ValueError('frontend_dependency_notice_missing:' + name)
        destination = licenses / 'frontend' / name.removeprefix('node_modules/')
        destination.mkdir(parents=True, exist_ok=True)
        for notice in notices:
            shutil.copy2(notice, destination / notice.name)
    (program / 'release.json').write_text(json.dumps({
        'format': 1, 'platform': 'windows-x64', 'version': version, 'schema_revision': schema,
        'build_revision': revision, 'development': args.development,
        'frontend_build': frontend_build,
        'built_at': datetime.now(UTC).isoformat(), 'python_version': lock['python']['version'],
        'postgres_version': lock['postgres']['version'], 'service_wrapper_version': lock['winsw']['version'],
    }, indent=2) + '\n', encoding='utf-8')
    # An offline smoke check proves this interpreter is self-contained. User and
    # system site-packages are disabled, and no developer PYTHONPATH is inherited.
    env = {key: value for key, value in os.environ.items() if not key.startswith(('PYTHON', 'JL_'))}
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([str(program / 'runtime/python.exe'), '-I', '-B', '-c',
        "import sys,site,ssl,psycopg,cryptography,uvicorn; from app.native.runtime import child_commands; "
        "assert sys.flags.no_user_site and sys.flags.dont_write_bytecode and site.ENABLE_USER_SITE is False; "
        "print('bundled-runtime-isolated-imports-ok')"],
        check=True, env=env, cwd=program)
    files = {path.relative_to(program).as_posix(): digest(path) for path in sorted(program.rglob('*')) if path.is_file()}
    (program / 'program-manifest.json').write_text(json.dumps({'format': 1, 'files': files}, sort_keys=True) + '\n', encoding='utf-8')
    if not args.development:
        final_revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        final_dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT, text=True).strip()
        if final_revision != revision or final_dirty:
            raise ValueError('source_changed_during_release_build')
    nsis = args.cache / 'nsis-3.11'
    if not (nsis / 'makensis.exe').exists():
        extract(assets['nsis'], args.cache)
    destination = work / f'Tianyan-Setup-{identity}-x64.exe'
    if not args.stage_only:
        subprocess.run([str(nsis / 'makensis.exe'), '/V2', '/INPUTCHARSET', 'UTF8', '/DPROGRAM=' + str(program),
            '/DOUTPUT=' + str(destination), '/DRELEASE_ID=' + identity,
            '/DVERSION=' + version, str(ROOT / 'packaging/windows/tianyan.nsi')], check=True)
    proof = {'format': 1, 'program': str(program), 'build_revision': revision,
        'version': version, 'development': args.development, 'program_files': len(files),
        'frontend_build': frontend_build,
        'program_manifest_sha256': digest(program / 'program-manifest.json'),
        'installer': str(destination) if destination.exists() else None,
        'installer_sha256': digest(destination) if destination.exists() else None,
        'installer_bytes': destination.stat().st_size if destination.exists() else None,
        'verified': ['pinned-input-hashes', 'bundled-python-imports'],
        'not_yet_verified': ['clean-Windows-install', 'SCM-registration', 'OS-reboot', 'live-trading-day']}
    (work / 'build-receipt.json').write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(proof))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frontend', type=Path, help='Development only: use an external web build; requires --development')
    parser.add_argument('--development', action='store_true')
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args()
    build(args)


if __name__ == '__main__':
    main()
