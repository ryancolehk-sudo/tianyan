"""Pinned single-host Linux deployment. Private state always lives outside Git.

Install: python3 scripts/cloud/deploy.py --state-dir /var/lib/tianyan install \
           --domain research.example.com --email ops@example.com --admin owner
Passwords are prompted privately, or supplied on stdin with --password-stdin.
"""
import argparse
import getpass
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
ROLES = ('worker', 'scheduler', 'realtime-worker', 'realtime-worker-secondary', 'recovery-worker')
SERVICES = ('api', *ROLES, 'ai-worker', 'read-cache-worker', 'maintenance-worker', 'web', 'edge')
IMAGE_KEYS = {'backend': 'TIANYAN_BACKEND_IMAGE', 'maintenance': 'TIANYAN_MAINTENANCE_IMAGE',
              'web': 'TIANYAN_WEB_IMAGE'}


class DeploymentError(RuntimeError):
    pass


def state_path(value):
    path = Path(value).expanduser().absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise DeploymentError('部署状态目录不能经过符号链接。')
    path = path.resolve()
    if path == ROOT or ROOT in path.parents:
        raise DeploymentError('私有配置和备份必须保存在代码仓库之外。')
    return path


def read_env(path):
    if path.is_symlink() or not path.is_file():
        raise DeploymentError('缺少安全的私有配置文件。')
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise DeploymentError('私有配置权限必须为0600。')
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value
    return values


def write_env(path, values, *, exclusive=True):
    for key, value in values.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or any(c in str(value) for c in '\r\n\x00$'):
            raise DeploymentError('配置包含不支持的字段或控制字符。')
    data = '\n'.join(f'{key}={value}' for key, value in values.items()) + '\n'
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != 'nt':
        path.chmod(0o600)


def validate_domain(value):
    try:
        domain = value.strip().rstrip('.').encode('idna').decode('ascii').lower()
    except (AttributeError, UnicodeError):
        raise DeploymentError('域名格式无效。') from None
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise DeploymentError('HTTPS部署需要可解析的域名。')
    labels = domain.split('.')
    if len(labels) < 2 or len(domain) > 253 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels
    ) or domain.endswith(('.localhost', '.local', '.invalid')):
        raise DeploymentError('域名格式无效。')
    return domain


def current_revision():
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT, text=True)
    if dirty.strip() or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise DeploymentError('部署需要已提交的固定版本；请先提交代码和锁文件。')
    return revision


def initialize(state, *, domain, email, project, revision):
    state = state_path(state)
    domain = validate_domain(domain)
    if not re.fullmatch(r'[A-Za-z0-9.!+_-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', email):
        raise DeploymentError('ACME联系邮箱格式无效。')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{2,45}', project):
        raise DeploymentError('实例名格式无效。')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise DeploymentError('需要完整提交版本。')
    if state.exists() and any(state.iterdir()):
        raise DeploymentError('部署目录非空；现有配置不会被覆盖。')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        state.chmod(0o700)
    for name in ('backups', 'updates', 'maintenance-status'):
        (state / name).mkdir(mode=0o700)
    values = {
        'COMPOSE_PROJECT_NAME': project, 'JL_APP_MODE': 'real',
        'JL_DEPLOYMENT_MODE': 'cloud', 'JL_PUBLIC_ORIGIN': 'https://' + domain,
        'JL_COOKIE_SECURE': 'true', 'JL_SESSION_COOKIE': project.replace('-', '_') + '_session',
        'JL_BUILD_REVISION': revision, 'JL_DATA_COLLECTION_ENABLED': 'true',
        'JL_READ_CACHE_ENABLED': 'true', 'JL_MODEL_ENABLED': 'false',
        'JL_MAINTENANCE_ENABLED': 'true', 'JL_UPDATE_INSTALLATION': 'source',
        'WEB_BIND': '127.0.0.1', 'WEB_PORT': '8088',
        'TIANYAN_DOMAIN': domain, 'TIANYAN_ACME_EMAIL': email,
        'TIANYAN_BACKEND_IMAGE': f'tianyan/backend:{revision}',
        'TIANYAN_MAINTENANCE_IMAGE': f'tianyan/maintenance:{revision}',
        'TIANYAN_WEB_IMAGE': f'tianyan/web:{revision}',
        'JL_BACKUP_HOST_DIRECTORY': (state / 'backups').as_posix(),
        'JL_UPDATE_HOST_DIRECTORY': (state / 'updates').as_posix(),
        'JL_MAINTENANCE_STATUS_DIRECTORY': (state / 'maintenance-status').as_posix(),
    }
    for key in ('DB_ADMIN_PASSWORD', 'DB_MIGRATE_PASSWORD', 'DB_APP_PASSWORD', 'DB_MAINTENANCE_PASSWORD'):
        values[key] = secrets.token_hex(32)
    write_env(state / 'cloud.env', values)
    return values


def compose_args(state, *args):
    state = state_path(state)
    values = read_env(state / 'cloud.env')
    return ['docker', 'compose', '--project-name', values['COMPOSE_PROJECT_NAME'],
            '--project-directory', str(ROOT), '--env-file', str(ROOT / 'infrastructure/images.lock.env'),
            '--env-file', str(ROOT / 'infrastructure/cloud/images.lock.env'),
            '--env-file', str(state / 'cloud.env'), '-f', str(ROOT / 'compose.yaml'),
            '-f', str(ROOT / 'infrastructure/cloud/compose.cloud.yaml'), *args]


def compose(state, *args, **kwargs):
    # Docker interpolation must not inherit another checkout's COMPOSE_* or
    # JL_* credentials. Only the selected state file defines this instance.
    locked_keys = {'PYTHON_IMAGE', 'NODE_IMAGE', 'NGINX_IMAGE', 'POSTGRES_IMAGE', 'PLAYWRIGHT_IMAGE', 'CADDY_IMAGE'}
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('COMPOSE_', 'JL_', 'DB_', 'TIANYAN_')) and key not in locked_keys}
    environment['COMPOSE_DISABLE_ENV_FILE'] = 'true'
    if not any(key in kwargs for key in ('stdout', 'stderr', 'capture_output')):
        kwargs['capture_output'] = True
    elif 'stdout' in kwargs and 'stderr' not in kwargs:
        kwargs['stderr'] = subprocess.PIPE
    kwargs.setdefault('text', True)
    if kwargs.get('text'):
        kwargs.setdefault('encoding', 'utf-8')
    result = subprocess.run(compose_args(state, *args), cwd=ROOT, env=environment, **kwargs)
    if result.returncode:
        # Compose diagnostics may contain interpolated values. Never echo them.
        raise DeploymentError('部署步骤失败；请在服务器本地检查对应服务状态，私有配置未输出。')
    return result


def preflight(state, *, online=True):
    state = state_path(state)
    values = read_env(state / 'cloud.env')
    if values.get('JL_PUBLIC_ORIGIN') != 'https://' + validate_domain(values.get('TIANYAN_DOMAIN', '')):
        raise DeploymentError('公开地址与HTTPS域名不一致。')
    if values.get('JL_COOKIE_SECURE') != 'true' or values.get('JL_APP_MODE') != 'real':
        raise DeploymentError('云端必须使用real实例及安全会话。')
    if shutil.disk_usage(state).free < 4 * 1024**3:
        raise DeploymentError('部署磁盘至少需要4GiB可用空间，行情历史另计。')
    model = json.loads(compose(state, 'config', '--format', 'json').stdout)
    for name, service in model['services'].items():
        if name != 'edge' and service.get('ports'):
            raise DeploymentError('云端只允许HTTPS入口发布端口。')
    if online:
        try:
            socket.getaddrinfo(values['TIANYAN_DOMAIN'], 443)
        except OSError:
            raise DeploymentError('域名尚不可解析，请先将域名指向此服务器。') from None
    return {'configuration': 'passed', 'dns': 'resolved' if online else 'not_checked',
            'revision': values['JL_BUILD_REVISION'], 'published_services': ['edge']}


def project_adapter(state):
    # Reuse the mature hash/credential-preserving backup and empty-target
    # restore protocol, with this deployment's Compose invocation only.
    spec = importlib.util.spec_from_file_location('tianyan_cloud_project', ROOT / 'scripts/project.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def selected_compose(*args, envfile=None, **kwargs):
        if envfile is not None and Path(envfile).resolve() != (state / 'cloud.env').resolve():
            raise DeploymentError('备份恢复不能切换到另一实例。')
        return compose(state, *args, **kwargs)
    module.compose = selected_compose
    return module


def build(state):
    values = read_env(state / 'cloud.env')
    if (state / 'release.json').exists():
        raise DeploymentError('已有构建回执；请保留原部署并在新发布目录构建升级。')
    if values['JL_BUILD_REVISION'] != current_revision():
        raise DeploymentError('检出版本与部署版本不一致。')
    pending = state / 'release.pending.json'
    if pending.is_file() and not pending.is_symlink():
        receipt = json.loads(pending.read_text(encoding='utf-8'))
        if receipt['revision'] != values['JL_BUILD_REVISION']:
            raise DeploymentError('中断构建回执与当前版本不一致。')
        for role, key in IMAGE_KEYS.items():
            values[key] = receipt['images'][role]
        verify_images(values, receipt)
        persist_release(state, values, receipt)
        return receipt
    compose(state, 'build', 'api', 'maintenance-worker', 'web')
    pinned = {}
    for role, key in IMAGE_KEYS.items():
        data = json.loads(subprocess.check_output(['docker', 'image', 'inspect', values[key]], text=True))[0]
        if data['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != values['JL_BUILD_REVISION']:
            raise DeploymentError('构建镜像的版本标签不一致。')
        pinned[role] = data['Id']
        values[key] = data['Id']
    receipt = {'revision': values['JL_BUILD_REVISION'], 'images': pinned}
    persist_release(state, values, receipt)
    return receipt


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def persist_release(state, values, receipt):
    # The journal precedes the environment replacement. A power loss between
    # these steps resumes from the same inspected immutable image IDs.
    pending = state / 'release.pending.json'
    atomic_json(pending, receipt)
    fd, temporary = tempfile.mkstemp(prefix='.cloud-', dir=state)
    os.close(fd)
    try:
        write_env(Path(temporary), values, exclusive=False)
        os.replace(temporary, state / 'cloud.env')
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    os.replace(pending, state / 'release.json')
    if os.name != 'nt':
        descriptor = os.open(state, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def verify_images(values, receipt):
    if receipt.get('revision') != values.get('JL_BUILD_REVISION'):
        raise DeploymentError('镜像回执版本不一致。')
    for role, key in IMAGE_KEYS.items():
        expected = receipt.get('images', {}).get(role, '')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', expected) or values.get(key) != expected:
            raise DeploymentError('程序镜像必须匹配固定构建回执，不能使用浮动标签。')
        actual = json.loads(subprocess.check_output(['docker', 'image', 'inspect', expected], text=True))[0]
        if actual['Id'] != expected or actual['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != receipt['revision']:
            raise DeploymentError('本机程序镜像与构建回执不一致。')


def run_admin(state, body):
    source = ROOT / 'scripts/cloud/admin.py'
    result = compose(state, 'run', '--rm', '--no-deps', '-T', '--volume',
        str(source) + ':/opt/tianyan/cloud_admin.py:ro', 'api', 'python', '/opt/tianyan/cloud_admin.py',
        input=json.dumps(body, ensure_ascii=True))
    return json.loads(result.stdout)


def start(state, *, bootstrap=None):
    state = state_path(state)
    preflight(state)
    if not (state / 'release.json').is_file():
        raise DeploymentError('未发现已核构建回执，请先执行install。')
    values = read_env(state / 'cloud.env')
    if current_revision() != values['JL_BUILD_REVISION']:
        raise DeploymentError('请检出该部署的固定提交后恢复启动。')
    verify_images(values, json.loads((state / 'release.json').read_text(encoding='utf-8')))
    compose(state, 'up', '-d', '--wait', '--no-build', 'db')
    operator = project_adapter(state)
    operator.prepare_maintenance_role(envfile=state / 'cloud.env')
    compose(state, 'run', '--rm', '--no-deps', '-T', 'migrate')
    operator.prepare_maintenance_role(envfile=state / 'cloud.env')
    compose(state, 'run', '--rm', '--no-deps', '-T', 'credential-init')
    if bootstrap:
        username, password = bootstrap
        compose(state, 'run', '--rm', '--no-deps', '-T', 'api', 'python', '-m', 'app.cli',
                'bootstrap', '--username', username, '--password-stdin', input=password + '\n')
    scope = run_admin(state, {'action': 'source-scope-check'})
    if not scope['shared_source_scope_ready']:
        raise DeploymentError('当前来源尚未确认云端共享范围；请保留关闭状态并在本机核对来源权利。')
    compose(state, 'up', '-d', '--wait', '--no-build', *SERVICES)
    wait_ready(state)
    return {'status': 'services_started', 'public_origin': read_env(state / 'cloud.env')['JL_PUBLIC_ORIGIN'],
            'note': '服务启动不等于行情完整，完整性见登录后的恢复状态。'}


def wait_ready(state, *, seconds=120):
    """Require actual database/schema/role readiness, not only a live API PID."""
    deadline = time.monotonic() + seconds
    code = ("import urllib.request; "
            "urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).read()")
    while time.monotonic() < deadline:
        try:
            compose(state, 'exec', '-T', 'api', 'python', '-c', code)
            return
        except DeploymentError:
            time.sleep(2)
    raise DeploymentError('服务尚未完成数据库、迁移和各角色就绪检查，请查看运行状态后重试start。')


def password(args):
    value = sys.stdin.readline().rstrip('\r\n') if args.password_stdin else getpass.getpass('设置密码: ')
    if not args.password_stdin and value != getpass.getpass('再次输入密码: '):
        raise DeploymentError('两次密码不一致。')
    if not 12 <= len(value) <= 1024 or '\x00' in value:
        raise DeploymentError('密码需要12至1024个字符。')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True, help='仓库外的私有部署目录')
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('install', help='初始化、构建固定版本并启动HTTPS实例')
    install.add_argument('--domain', required=True)
    install.add_argument('--email', required=True)
    install.add_argument('--project', default='tianyan-cloud')
    install.add_argument('--admin', required=True)
    install.add_argument('--password-stdin', action='store_true')
    configure = commands.add_parser('configure', help='为独立空恢复目标生成配置；不启动、不创建账号')
    configure.add_argument('--domain', required=True)
    configure.add_argument('--email', required=True)
    configure.add_argument('--project', required=True)
    resume = commands.add_parser('start', help='从已构建版本恢复服务；不重建镜像')
    resume.add_argument('--bootstrap-admin', help='仅在首次安装被中断且实例仍无账号时补建管理员')
    resume.add_argument('--password-stdin', action='store_true')
    commands.add_parser('build', help='首次安装中断后继续构建；已有构建回执时拒绝覆盖')
    check = commands.add_parser('check', help='检查配置、DNS和服务状态')
    check.add_argument('--offline', action='store_true')
    commands.add_parser('backup', help='导出带hash、凭据和独立主密钥的完整备份')
    restore = commands.add_parser('restore', help='只向空目标恢复，采集需之后显式start')
    restore.add_argument('folder', type=Path)
    user = commands.add_parser('create-user', help='服务器管理员创建独立使用账号')
    user.add_argument('--actor', required=True)
    user.add_argument('--username', required=True)
    user.add_argument('--role', choices=['researcher', 'viewer', 'admin'], default='researcher')
    user.add_argument('--password-stdin', action='store_true')
    args = parser.parse_args()
    try:
        if not sys.platform.startswith('linux'):
            raise DeploymentError('正式云端部署入口只运行在Linux；Windows使用原生安装包。')
        if shutil.which('docker') is None:
            raise DeploymentError('服务器需先安装Docker Engine及Compose插件。')
        state = state_path(args.state_dir)
        if args.command == 'install':
            secret = password(args)
            initialize(state, domain=args.domain, email=args.email, project=args.project, revision=current_revision())
            preflight(state)
            build(state)
            result = start(state, bootstrap=(args.admin, secret))
        elif args.command == 'configure':
            initialize(state, domain=args.domain, email=args.email, project=args.project, revision=current_revision())
            result = {'status': 'configured_not_started'}
        elif args.command == 'start':
            credentials = (args.bootstrap_admin, password(args)) if args.bootstrap_admin else None
            result = start(state, bootstrap=credentials)
        elif args.command == 'build':
            preflight(state)
            result = build(state)
        elif args.command == 'check':
            result = preflight(state, online=not args.offline)
            raw = compose(state, 'ps', '--all', '--format', 'json').stdout.strip()
            rows = json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines()]
            result['services'] = [{key: row.get(key) for key in ('Service', 'State', 'Health')} for row in rows]
        elif args.command == 'create-user':
            result = run_admin(state, {'action': 'create-user', 'actor': args.actor,
                'username': args.username, 'role': args.role, 'password': password(args)})
        else:
            operator = project_adapter(state)
            if args.command == 'backup':
                destination = state / 'exports' / ('backup-' + secrets.token_hex(8))
                operator.backup(envfile=state / 'cloud.env', destination=destination)
            else:
                operator.restore(args.folder, envfile=state / 'cloud.env')
            result = {'status': args.command + '_finished'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except DeploymentError as failure:
        parser.exit(1, str(failure) + '\n')
    except (OSError, ValueError, subprocess.SubprocessError):
        # No exception repr or subprocess diagnostics: both can carry secrets.
        parser.exit(1, '云端操作未完成；请检查私有目录权限、固定版本、域名和对应服务。未输出凭据。\n')


if __name__ == '__main__':
    main()
