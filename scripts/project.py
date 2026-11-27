"""Cross-platform operator entry point. Requires Python 3.10+ and Docker Compose v2."""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ["backend/requirements.in", "backend/requirements-dev.in", "frontend/package.json", "dependencies/image-inputs.json"]
LOCKS = ["backend/requirements.lock", "backend/requirements-dev.lock", "frontend/package-lock.json", "infrastructure/images.lock.env"]
MANIFEST = ROOT / "dependencies/lock-manifest.json"


def run(args, **kw):
    if kw.get("text"):
        kw.setdefault("encoding", "utf-8")
    return subprocess.run(args, cwd=ROOT, check=True, **kw)


def checksum(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def sanitize_browser_diagnostics(folder, password):
    # Playwright's failure snapshot can include a password input's value.
    # Only disposable fixture credentials are ever passed to this function.
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".json", ".txt", ".log"}:
            content = path.read_text(encoding="utf-8")
            content = content.replace(password, "[redacted-fixture-password]")
            content = re.sub(r'(?m)(- textbox "密码":).*$', r'\1 [redacted-fixture-password]', content)
            if content != path.read_text(encoding="utf-8"):
                path.write_text(content, encoding="utf-8")


def config(path=None):
    path = path or ROOT / ".env"
    if not path.is_file():
        raise SystemExit("尚未初始化：先运行 python scripts/project.py init")
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#") and "=" in line)


def compose(*args, envfile=None, extra_files=(), **kw):
    instance_env = envfile or ROOT / ".env"
    # Explicit project selection also protects disposable tests from ambient shell overrides.
    cmd = ["docker", "compose", "--project-name", config(instance_env)["COMPOSE_PROJECT_NAME"],
           "--project-directory", str(ROOT), "--env-file", str(instance_env)]
    if (ROOT / "infrastructure/images.lock.env").exists():
        cmd += ["--env-file", str(ROOT / "infrastructure/images.lock.env")]
    cmd += ["-f", "compose.yaml", "-f", "infrastructure/compose.test.yaml"]
    for path in extra_files:
        cmd += ["-f", str(path)]
    inherited = kw.pop("env", None)
    source = os.environ if inherited is None else inherited
    pinned_images = {"PYTHON_IMAGE", "NODE_IMAGE", "NGINX_IMAGE", "POSTGRES_IMAGE", "PLAYWRIGHT_IMAGE", "CADDY_IMAGE"}
    environment = {key: value for key, value in source.items()
                   if not key.upper().startswith(("JL_", "DB_", "COMPOSE_", "WEB_", "TIANYAN_", "VITE_"))
                   and key.upper() not in pinned_images}
    if inherited is not None and "JL_E2E_PASSWORD" in inherited and "browser" in args:
        environment["JL_E2E_PASSWORD"] = inherited["JL_E2E_PASSWORD"]
    environment["COMPOSE_DISABLE_ENV_FILE"] = "true"
    return run(cmd + list(args), env=environment, **kw)


def require_locks():
    if not MANIFEST.exists() or any(not (ROOT / p).exists() for p in LOCKS):
        raise SystemExit("依赖尚未冻结。请在有 Docker 和网络的新电脑先运行：python scripts/project.py lock")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for p in INPUTS + LOCKS:
        if checksum(ROOT / p) != manifest["sha256"].get(p):
            raise SystemExit(f"依赖输入或锁文件已改变：{p}。审阅后运行 lock --refresh，再提交所有锁文件。")


def initialize(path, mode, project, port):
    if path.exists():
        raise SystemExit("配置文件已存在，不会覆盖。不同实例请使用不同检出目录。")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,45}", project):
        raise SystemExit("实例名须由小写字母、数字、横线或下划线组成。")
    values = {"COMPOSE_PROJECT_NAME": project, "JL_APP_MODE": mode,
        "JL_SESSION_COOKIE": project + "_session",
        "JL_PUBLIC_ORIGIN": f"http://127.0.0.1:{port}", "JL_COOKIE_SECURE": "false",
        "WEB_BIND": "127.0.0.1", "WEB_PORT": str(port),
        "DB_ADMIN_PASSWORD": secrets.token_hex(24), "DB_MIGRATE_PASSWORD": secrets.token_hex(24),
        "DB_APP_PASSWORD": secrets.token_hex(24), "DB_MAINTENANCE_PASSWORD": secrets.token_hex(24),
        "JL_BUILD_REVISION": "development"}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")


def ensure_maintenance_configuration(envfile=None):
    """Upgrade old private env files without replacing existing credentials."""
    path = envfile or ROOT / '.env'
    values = config(path)
    if not values.get('DB_MAINTENANCE_PASSWORD'):
        with path.open('a', encoding='utf-8', newline='\n') as output:
            output.write('\nDB_MAINTENANCE_PASSWORD=' + secrets.token_hex(24) + '\n')
    return config(path)


def prepare_maintenance_role(*, envfile=None):
    values = ensure_maintenance_configuration(envfile)
    password = values['DB_MAINTENANCE_PASSWORD']
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', password):
        raise SystemExit('维护账号密码格式无效；请在本机私有配置中使用至少32位字母数字或下划线横线。')
    # Feed the credential over stdin; never put passwords in shell arguments or
    # SQL diagnostics. This also handles existing PostgreSQL volumes whose init
    # entrypoint never runs again after an application upgrade.
    sql = ("\\set maintenance_password '" + password + "'\n" + """
SELECT format('CREATE ROLE jlpm_maintenance LOGIN PASSWORD %L', :'maintenance_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='jlpm_maintenance') \\gexec
ALTER ROLE jlpm_maintenance LOGIN PASSWORD :'maintenance_password';
GRANT CONNECT ON DATABASE jiulong TO jlpm_maintenance;
GRANT USAGE ON SCHEMA public TO jlpm_maintenance;
GRANT pg_read_all_data TO jlpm_maintenance;
DO $$ DECLARE resource text; BEGIN
FOREACH resource IN ARRAY ARRAY['maintenance_jobs','backup_records','system_settings_state','system_update_state'] LOOP
IF to_regclass('public.' || resource) IS NOT NULL THEN
EXECUTE format('GRANT INSERT, UPDATE, DELETE ON TABLE public.%I TO jlpm_maintenance', resource);
END IF; END LOOP;
IF to_regclass('public.audit_events') IS NOT NULL THEN
GRANT INSERT, UPDATE ON TABLE public.audit_events TO jlpm_maintenance;
END IF; END $$;
""")
    compose('exec', '-T', 'db', 'psql', '-U', 'postgres', '-d', 'jiulong', '-v', 'ON_ERROR_STOP=1',
            input=sql, capture_output=True, text=True, envfile=envfile)


def lock(refresh=False):
    if MANIFEST.exists() and not refresh:
        require_locks()
        print("现有依赖锁有效；更新需要显式使用 --refresh。")
        return
    images = json.loads((ROOT / "dependencies/image-inputs.json").read_text(encoding="utf-8"))
    playwright = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))["devDependencies"]["@playwright/test"]
    if images["PLAYWRIGHT_IMAGE"] != f"mcr.microsoft.com/playwright:v{playwright}-noble":
        raise SystemExit("Playwright 工具版本必须与官方浏览器镜像版本一致。")
    frozen = {}
    for name, tag in images.items():
        run(["docker", "pull", tag])
        digest = run(["docker", "image", "inspect", tag, "--format", "{{index .RepoDigests 0}}"], capture_output=True, text=True).stdout.strip()
        if "@sha256:" not in digest:
            raise SystemExit(f"无法固定镜像摘要：{name}")
        frozen[name] = digest
    # Resolve with the same immutable images recorded in the manifest, avoiding tag drift.
    uid = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    mount = ["--mount", f"type=bind,source={ROOT},target=/workspace", "-w", "/workspace"]
    run(["docker", "run", "--rm", *uid, *mount, "-e", "NPM_CONFIG_CACHE=/tmp/npm-cache", frozen["NODE_IMAGE"],
         "npm", "--prefix", "frontend", "install", "--package-lock-only", "--ignore-scripts"])
    command = "python -m pip install --no-cache-dir --target /tmp/tools uv==0.9.2 && /tmp/tools/bin/uv pip compile --cache-dir /tmp/uv --python-version 3.14 --universal --generate-hashes backend/requirements.in -o backend/requirements.lock && /tmp/tools/bin/uv pip compile --cache-dir /tmp/uv --python-version 3.14 --universal --generate-hashes -c backend/requirements.lock backend/requirements-dev.in -o backend/requirements-dev.lock"
    run(["docker", "run", "--rm", *uid, *mount, frozen["PYTHON_IMAGE"], "sh", "-ec", command])
    (ROOT / "infrastructure/images.lock.env").write_text("\n".join(f"{k}={v}" for k, v in frozen.items()) + "\n", encoding="utf-8")
    MANIFEST.parent.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
        "image_inputs": images, "resolver_images": {k: frozen[k] for k in ["PYTHON_IMAGE", "NODE_IMAGE"]},
        "uv_version": "0.9.2", "sha256": {p: checksum(ROOT / p) for p in INPUTS + LOCKS}, "verification": "dependency resolution only; run test next"}, indent=2) + "\n", encoding="utf-8")
    print("依赖锁已生成。请运行 test，审阅依赖许可/安全结果，再将锁文件提交到分支。")


def wait_ready(url, seconds=90):
    # A Windows/WSL auto-proxy must never receive requests to our loopback service.
    local = urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            with opener.open(url + "/health/ready", timeout=4) as r:
                if r.status == 200:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise SystemExit("基础服务未就绪。请运行 status，并检查本实例日志。")


def test_network(value):
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except ValueError:
        raise argparse.ArgumentTypeError("测试网段必须是规范的 IPv4 CIDR。") from None
    private = (ipaddress.IPv4Network("10.0.0.0/8"), ipaddress.IPv4Network("172.16.0.0/12"),
               ipaddress.IPv4Network("192.168.0.0/16"))
    if not 16 <= network.prefixlen <= 28 or not any(network.subnet_of(item) for item in private):
        raise argparse.ArgumentTypeError("测试网段须在 RFC1918 私有地址内，掩码范围 /16 到 /28。")
    return str(network)


def browser_specs(names=None):
    available = {path.name: path for path in (ROOT / 'frontend/tests/e2e').glob('*.spec.ts')
                 if path.name != 'workbench-v2.spec.ts'}
    if not available:
        raise SystemExit('页面用例目录缺失或为空，不能把零项检查记为通过。')
    if names is not None and (not names or any(name not in available for name in names)):
        raise SystemExit('页面用例须为当前已挂载界面的现有.spec.ts文件名，不能传入路径或归档界面。')
    return [available[name] for name in sorted(set(names) if names is not None else available)]


TEST_BIND_OWNERSHIP_CODE = """
import os, sys
uid, gid = map(int, sys.argv[1:3])
for root in sys.argv[3:]:
    if not os.path.exists(root):
        continue
    assert os.path.isdir(root) and not os.path.islink(root)
    for directory, children, files in os.walk(root, followlinks=False):
        for name in children + files:
            os.chown(os.path.join(directory, name), uid, gid, follow_symlinks=False)
        os.chown(directory, uid, gid, follow_symlinks=False)
"""


def restore_test_bind_ownership(temporary, project_name, *, envfile, compose_command):
    # Linux application containers deliberately make their private bind folders
    # 0700. Return only this disposable test's two folders to the invoking uid;
    # do not relax modes or let root cleanup touch a normal installation.
    if not hasattr(os, 'getuid') or os.getuid() == 0:
        return
    root = Path(temporary).resolve()
    values = config(envfile)
    if (root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith('jlpm-check-')
            or Path(envfile).resolve() != root / '.env'
            or not re.fullmatch(r'jlpm-check-[a-f0-9]{10}', project_name)
            or values.get('COMPOSE_PROJECT_NAME') != project_name or values.get('JL_APP_MODE') != 'demo'):
        raise SystemExit('拒绝修改本轮独立测试以外的目录权限。')
    bindings = {'JL_BACKUP_HOST_DIRECTORY': ('backups', '/workspace/backups'),
                'JL_UPDATE_HOST_DIRECTORY': ('updates', '/workspace/updates')}
    for key, (child, _) in bindings.items():
        path = root / child
        if Path(values.get(key, '')) != path or path.is_symlink() or path.resolve().parent != root:
            raise SystemExit('测试维护目录必须是本轮临时目录中的原绑定。')
    if not any((root / child).exists() for child, _ in bindings.values()):
        return
    rendered = json.loads(compose_command('config', '--format', 'json', envfile=envfile,
                                         capture_output=True, text=True).stdout)
    if rendered.get('name') != project_name:
        raise SystemExit('测试实例身份不匹配，拒绝修改目录权限。')
    mounts = rendered['services']['credential-init']['volumes']
    for child, target in bindings.values():
        matches = [mount for mount in mounts if mount.get('target') == target]
        if (len(matches) != 1 or matches[0].get('type') != 'bind'
                or Path(matches[0]['source']).resolve() != root / child or matches[0].get('read_only')):
            raise SystemExit('实际容器绑定不是本轮临时目录，拒绝修改目录权限。')
    compose_command('run', '--rm', '--no-deps', '--user', '0:0', '-T', '--entrypoint', 'python',
                    'credential-init', '-c', TEST_BIND_OWNERSHIP_CODE, str(os.getuid()), str(os.getgid()),
                    *[target for _, target in bindings.values()], envfile=envfile,
                    capture_output=True, text=True)


def test(subnet=None, *, suite='all', specs=None):
    if suite not in {'all', 'backend', 'browser'} or suite == 'backend' and specs is not None:
        raise SystemExit('请选择all、backend或browser；backend阶段不能指定页面文件。')
    selected_specs = browser_specs(specs)
    if subnet is not None:
        subnet = test_network(subnet)
    require_locks()
    with tempfile.TemporaryDirectory(prefix="jlpm-check-") as tmp:
        envfile = Path(tmp) / ".env"
        extra_files = ()
        if subnet is not None:
            override = Path(tmp) / "compose.network.yaml"
            override.write_text("networks:\n  default:\n    ipam:\n      config:\n        - subnet: " + subnet + "\n", encoding="utf-8")
            extra_files = (override,)

        def compose_test(*args, **kw):
            return compose(*args, extra_files=extra_files, **kw)
        project = "jlpm-check-" + secrets.token_hex(5)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        initialize(envfile, "demo", project, port)
        # Test HTTP originates inside the dedicated Compose network.
        with envfile.open("a", encoding="utf-8") as f:
            f.write("JL_PUBLIC_ORIGIN=http://127.0.0.1:8080\n")
            f.write('JL_BACKUP_HOST_DIRECTORY=' + (Path(tmp) / 'backups').as_posix() + '\n')
            f.write('JL_UPDATE_HOST_DIRECTORY=' + (Path(tmp) / 'updates').as_posix() + '\n')
            f.write('JL_MAINTENANCE_STATUS_DIRECTORY=' + (Path(tmp) / 'maintenance-status').as_posix() + '\n')
        built = False
        try:
            compose_test("--profile", "test", "build", envfile=envfile)
            built = True
            compose_test("up", "-d", "db", envfile=envfile)
            compose_test("run", "--rm", "migrate", envfile=envfile)
            if suite != 'browser':
                compose_test("run", "--rm", "--no-deps", "test", envfile=envfile)
            else:
                print('NOT RUN: backend phase (explicit --suite browser).', flush=True)
            if suite == 'backend':
                return
            browser_user = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
            compose_test("up", "-d", "api", "worker", "scheduler", "realtime-worker", "realtime-worker-secondary", "recovery-worker", "ai-worker", "read-cache-worker", "maintenance-worker", "web", envfile=envfile)
            wait_ready(f"http://127.0.0.1:{port}")
            password = secrets.token_urlsafe(24)
            compose_test("exec", "-T", "api", "python", "-m", "app.cli", "bootstrap", "--username", "e2e", "--password-stdin",
                    envfile=envfile, input=password + "\n", text=True)
            # Disposable browser accounts only; no management API or production backdoor.
            fixture_users = "import sys; from sqlalchemy.orm import Session; from app.db import engine; from app.models import User; from app.auth import hasher; password=sys.stdin.readline().rstrip(); db=Session(engine()); db.add_all([User(username='e2e-viewer',password_hash=hasher.hash(password),role='viewer'),User(username='e2e-other',password_hash=hasher.hash(password),role='researcher')]); db.commit(); db.close()"
            compose_test("exec", "-T", "api", "python", "-c", fixture_users, envfile=envfile, input=password + "\n", text=True)
            assert re.fullmatch(r"jlpm-check-[a-f0-9]{10}", project) and config(envfile)["JL_APP_MODE"] == "demo"
            compose_test("exec", "-T", "-e", "JL_TEST_PROJECT=" + project, "api", "python", "-c",
                    (ROOT / "backend/tests/auction_fixtures.py").read_text(encoding="utf-8"),
                    envfile=envfile, input="e2e\n", capture_output=True, text=True)
            env = dict(os.environ, JL_E2E_PASSWORD=password)
            screenshots = ROOT / "artifacts/browser" / project
            screenshots.mkdir(parents=True, exist_ok=True)
            compose_test("run", "--rm", "--no-deps", *browser_user, "-e", "HOME=/tmp", "browser",
                         "node", "--test", "tests/fund-chart-order.test.mjs", envfile=envfile)
            # Each file remains below the real IP login limit. Clear only this
            # random disposable project's throttle between files, never the
            # normal instance or the application's authentication rules.
            failed_specs = []
            for spec in selected_specs:
                assert re.fullmatch(r"jlpm-check-[a-f0-9]{10}", project)
                assert config(envfile)["COMPOSE_PROJECT_NAME"] == project and config(envfile)["JL_APP_MODE"] == "demo"
                if spec.name == 'zz-data-sources.spec.ts':
                    # This suite is last because it adds explicit synthetic
                    # source rows; earlier UI regression retains its empty baseline.
                    compose_test('stop','worker',envfile=envfile)
                    try:
                        compose_test('exec','-T','-e','JL_TEST_PROJECT='+project,'api','python','-c',
                            (ROOT/'scripts/fixtures/seed_market_sources.py').read_text(encoding='utf-8'),envfile=envfile,
                            input='e2e\n',capture_output=True,text=True)
                    finally:
                        compose_test('up','-d','worker',envfile=envfile)
                elif spec.name == 'zzz-regressions.spec.ts':
                    # Catalog display-scope regression runs after the quote-source
                    # suite and changes only this disposable project's config.
                    compose_test('stop','worker',envfile=envfile)
                    try:
                        compose_test('exec','-T','-e','JL_TEST_PROJECT='+project,'api','python','-c',
                            (ROOT/'scripts/fixtures/seed_catalog_source.py').read_text(encoding='utf-8'),envfile=envfile,
                            input='e2e\n',capture_output=True,text=True)
                    finally:
                        compose_test('up','-d','worker',envfile=envfile)
                compose_test("exec", "-T", "db", "psql", "-U", "postgres", "-d", "jiulong", "-v", "ON_ERROR_STOP=1",
                        "-c", "TRUNCATE login_throttles;", envfile=envfile, capture_output=True)
                print("Browser file:", spec.name, flush=True)
                try:
                    result = compose_test("run", "--rm", "--no-deps", *browser_user, "-e", "HOME=/tmp", "-e", "JL_E2E_PASSWORD", "-v",
                                     f"{screenshots}:/workspace/frontend/test-results", "browser", "npx", "playwright", "test",
                                     "tests/e2e/" + spec.name, "--output", "test-results/" + spec.stem, envfile=envfile, env=env, capture_output=True, text=True)
                except subprocess.CalledProcessError as error:
                    result = error
                finally:
                    sanitize_browser_diagnostics(screenshots, password)
                print((result.stdout or "").replace(password, "[redacted-fixture-password]"))
                print((result.stderr or "").replace(password, "[redacted-fixture-password]"), file=sys.stderr)
                if result.returncode:
                    failed_specs.append(spec.name)
            if failed_specs:
                raise SystemExit('Browser files failed: ' + ', '.join(failed_specs) + '; fixture credentials redacted.')
        finally:
            # Only the randomly named, newly created test project is destroyed.
            try:
                if built:
                    if hasattr(os, 'getuid') and os.getuid() != 0:
                        # Stop this test's writers before handing ownership back.
                        compose_test('stop', envfile=envfile)
                    restore_test_bind_ownership(tmp, project, envfile=envfile, compose_command=compose_test)
            finally:
                compose_test("down", "--volumes", "--remove-orphans", envfile=envfile)


def backup(*, envfile=None, destination=None):
    require_locks()
    ensure_maintenance_configuration(envfile)
    result = compose('run', '--rm', '--no-deps', '-T', 'maintenance-worker',
                     'python', '-m', 'app.maintenance.cli', 'backup',
                     capture_output=True, text=True, envfile=envfile)
    try:
        metadata = json.loads(result.stdout)
        identity = metadata['id']
        key = metadata['location_key']
        assert re.fullmatch(r'backup-[a-f0-9-]{36}', key)
    except (ValueError, KeyError, AssertionError):
        raise SystemExit('维护进程未返回有效备份结果；请在系统设置检查任务状态。') from None
    default = ROOT / 'backups' / key
    folder = Path(destination) if destination is not None else default
    if destination is None and default.is_dir() and (default / 'manifest.json').is_file():
        # The standard deployment uses a fixed host bind, so UI and CLI share
        # the same verified files without producing a second copy.
        if checksum(default / 'manifest.json') != metadata['sha256']:
            raise SystemExit('备份清单校验失败。')
    else:
        if folder.exists():
            raise SystemExit('备份目标已经存在，不会覆盖。')
        folder.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='tianyan-backup-', dir=folder.parent) as temporary:
            stream_file = Path(temporary) / 'export.tar'
            with stream_file.open('wb') as output:
                compose('run', '--rm', '--no-deps', '-T', 'maintenance-worker',
                        'python', '-m', 'app.maintenance.cli', 'export', identity,
                        stdout=output, envfile=envfile)
            with tarfile.open(stream_file) as archive:
                for member in archive:
                    parts = Path(member.name).parts
                    if (not parts or parts[0] not in {'backup', 'backup-master-key'}
                            or '..' in parts or member.name.startswith('/')
                            or not (member.isfile() or member.isdir())):
                        raise SystemExit('维护进程导出内容无效，未写入备份目录。')
                archive.extractall(temporary, filter='data')
            if checksum(Path(temporary) / 'backup' / 'manifest.json') != metadata['sha256']:
                raise SystemExit('备份导出校验失败。')
            master_target = folder.with_name(folder.name + '-master-key')
            if master_target.exists():
                raise SystemExit('独立主密钥目标已经存在，不会覆盖。')
            shutil.move(str(Path(temporary) / 'backup-master-key'), str(master_target))
            shutil.move(str(Path(temporary) / 'backup'), str(folder))
    print('备份已保存：' + str(folder))
    print('文件校验通过；尚未进行实际恢复演练。备份不含 .env，请私下保管配套独立主密钥。')
    return folder


def restore(folder, *, envfile=None):
    require_locks()
    folder = Path(folder).resolve()
    data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if data["app_mode"] != config(envfile)["JL_APP_MODE"]:
        raise SystemExit("备份与实例模式不一致，拒绝恢复。")
    names = ['database.dump', 'artifacts.tar'] + (['credentials.tar'] if data.get('format') == 2 else [])
    for name in names:
        if checksum(folder / name) != data["sha256"][name]:
            raise SystemExit("备份校验失败。")
    master_file = folder.with_name(folder.name + '-master-key') / 'ai_master_key'
    if data.get('format') == 2 and (not master_file.is_file() or checksum(master_file) != data['ai_master_sha256']):
        raise SystemExit('缺少匹配的独立主密钥备份；请将对应的-master-key目录放在备份目录旁。')
    compose("up", "-d", "--wait", "db", envfile=envfile)
    r = compose("exec", "-T", "db", "psql", "-U", "postgres", "-d", "jiulong", "-tAc",
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'", capture_output=True, text=True, envfile=envfile)
    if int(r.stdout.strip()) != 0:
        raise SystemExit("目标库不是空库。请在新检出目录初始化独立实例再恢复；不会覆盖现有数据。")
    if data.get('format') == 2:
        # Validate both volumes before touching the database, so a failed restore
        # remains retryable. The final writer repeats these checks against races.
        code = ("from pathlib import Path; "
                "assert not any(Path('/master').iterdir()), 'Target master volume must be empty'; "
                "assert not any(Path('/credentials').iterdir()), 'Target credential volume must be empty'")
        compose('run', '--rm', '--no-deps', '-T', 'credential-init', 'python', '-c', code, envfile=envfile)
    with (folder / "database.dump").open("rb") as f:
        compose("exec", "-T", "db", "pg_restore", "-U", "postgres", "-d", "jiulong", "--role=jlpm_migrate", "--no-owner", "--single-transaction", stdin=f, envfile=envfile)
    # Target initialization grants broad defaults when tables are recreated.
    # Reapply the migrations' explicit append-only revokes for both old bundles
    # without ACL entries and new bundles whose GRANTs do not subtract defaults.
    immutable = ['source_versions', 'evidence_versions', 'source_health', 'calendar_versions',
                 'trading_sessions', 'snapshot_items', 'document_revisions', 'revision_links',
                 'publication_events', 'plan_scenarios', 'auction_observations', 'data_fetch_runs',
                 'review_archives', 'validations', 'news_editions', 'news_edition_items',
                 'screen_rule_versions', 'screen_runs', 'screen_events',
                 'tomorrow_snapshots', 'tomorrow_reports', 'tomorrow_confirmations', 'tomorrow_validations']
    sql = ("DO $$ DECLARE resource text; BEGIN FOREACH resource IN ARRAY ARRAY[" +
           ','.join("'" + table + "'" for table in immutable) + "] LOOP "
           "IF to_regclass('public.' || resource) IS NOT NULL THEN "
           "EXECUTE format('REVOKE UPDATE, DELETE ON TABLE public.%I FROM jlpm_app', resource); "
           "END IF; END LOOP; "
           "FOREACH resource IN ARRAY ARRAY['instrument_origins','screen_rules','tomorrow_settings','tomorrow_runs'] LOOP "
           "IF to_regclass('public.' || resource) IS NOT NULL THEN "
           "EXECUTE format('REVOKE DELETE ON TABLE public.%I FROM jlpm_app', resource); "
           "END IF; END LOOP; "
           "IF to_regclass('public.evidence_read_facets') IS NOT NULL THEN "
           "REVOKE INSERT, UPDATE, DELETE ON evidence_read_facets FROM jlpm_app; END IF; END $$;")
    compose('exec', '-T', 'db', 'psql', '-U', 'postgres', '-d', 'jiulong', '-v', 'ON_ERROR_STOP=1', '-c', sql, envfile=envfile)
    compose("exec", "-T", "db", "psql", "-U", "postgres", "-d", "jiulong", "-v", "ON_ERROR_STOP=1", "-c",
            "UPDATE sessions SET revoked_at=now();", envfile=envfile)
    # A restored runtime is a distinct OAuth host. Keep account/client mappings
    # and encrypted material, but require fresh authorization before any use.
    oauth_reset = ("DO $$ BEGIN IF to_regclass('public.chatgpt_installation') IS NOT NULL THEN "
        "UPDATE chatgpt_installation SET host_id='urn:uuid:' || gen_random_uuid()::text; "
        "UPDATE chatgpt_authorizations SET consumed_at=COALESCE(consumed_at,now()),secret_ref=NULL; "
        "UPDATE chatgpt_accounts SET state=CASE WHEN secret_ref IS NULL THEN 'disconnected' ELSE 'reauthorization_required' END, "
        "revision=revision+1,refresh_started_at=NULL,updated_at=now(); "
        "UPDATE service_connections SET enabled=false,revision=revision+1,last_test=NULL,tested_revision=NULL,updated_at=now() "
        "WHERE template='chatgpt_plan'; END IF; END $$;")
    compose('exec', '-T', 'db', 'psql', '-U', 'postgres', '-d', 'jiulong', '-v', 'ON_ERROR_STOP=1', '-c', oauth_reset, envfile=envfile)
    # A recovered installation is a new maintenance host. It must never resume
    # old source jobs or install a package whose local path belonged elsewhere.
    maintenance_reset = ("DO $$ BEGIN IF to_regclass('public.maintenance_jobs') IS NOT NULL THEN "
        "UPDATE maintenance_jobs SET status='cancelled',stage='restored',message='恢复到新实例，原维护任务已取消。', "
        "lease_token=NULL,lease_until=NULL,completed_at=now() WHERE status IN ('queued','running'); "
        "UPDATE system_settings_state SET active=false,upgrade_job_id=NULL,stage='idle',started_at=NULL, "
        "updater_heartbeat_at=NULL,backup_heartbeat_at=NULL; "
        "UPDATE system_update_state SET status='unchecked',message='恢复到新实例，请重新检查更新。', "
        "checked_at=NULL,manifest=NULL,signature=NULL,downloaded_at=NULL,package_name=NULL,release_id=NULL; "
        "END IF; END $$;")
    compose('exec', '-T', 'db', 'psql', '-U', 'postgres', '-d', 'jiulong', '-v', 'ON_ERROR_STOP=1',
            '-c', maintenance_reset, envfile=envfile)
    code = "import sys,tarfile; t=tarfile.open(fileobj=sys.stdin.buffer,mode='r|'); t.extractall('/workspace',filter='data'); t.close()"
    with (folder / "artifacts.tar").open("rb") as f:
        compose("run", "--rm", "--no-deps", "-T", "api", "python", "-c", code, stdin=f, envfile=envfile)
    if data.get('format') == 2:
        code = ("import os,sys; from pathlib import Path; key=sys.stdin.buffer.read(128); "
                "folder=Path('/master'); folder.mkdir(exist_ok=True); target=folder/'ai_master_key'; "
                "assert not target.exists(), 'Target master volume must be empty'; "
                "target.write_bytes(key); os.chown(folder,10001,10001); folder.chmod(0o700); "
                "os.chown(target,10001,10001); target.chmod(0o400); "
                "credentials=Path('/credentials'); credentials.mkdir(exist_ok=True); "
                "assert not any(credentials.iterdir()), 'Target credential volume must be empty'; "
                "os.chown(credentials,10001,10001); credentials.chmod(0o700)")
        with master_file.open('rb') as source:
            compose('run', '--rm', '--no-deps', '-T', 'credential-init', 'python', '-c', code,
                    stdin=source, envfile=envfile)
        code = "import sys,tarfile; t=tarfile.open(fileobj=sys.stdin.buffer,mode='r|'); t.extractall('/workspace',filter='data'); t.close()"
        with (folder / 'credentials.tar').open('rb') as source:
            compose('run', '--rm', '--no-deps', '-T', 'api', 'python', '-c', code, stdin=source, envfile=envfile)
        verify = ("from sqlalchemy import text; from app.db import engine; from app.ai import vault; "
                  "from app.maintenance.backup import _check_credentials; "
                  "connection=engine().connect(); directory,master=vault.paths(); "
                  "_check_credentials(connection,directory,master); "
                  "assert connection.scalar(text('SELECT count(*) FROM alembic_version'))==1; connection.close()")
        compose('run', '--rm', '--no-deps', '-T', 'api', 'python', '-c', verify, envfile=envfile)
        proof = {'state': 'passed', 'verified_at': datetime.now(timezone.utc).isoformat(),
                 'manifest_sha256': checksum(folder / 'manifest.json'), 'app_mode': data['app_mode'],
                 'checks': ['empty-instance-database-restore', 'bundle-sha256', 'credential-decryption']}
        (folder / 'recovery-verification.json').write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
        if re.fullmatch(r'[a-f0-9-]{36}', data.get('job_id', '')):
            # Only the independent recovered instance is updated here. The
            # original running instance and its history are never inferred or
            # written from a directory name or a different private env file.
            sql = ("UPDATE backup_records SET integrity_state='passed',recovery_state='passed',"
                   "recovery_verified_at=now() WHERE job_id='" + data['job_id'] + "';")
            compose('exec', '-T', 'db', 'psql', '-U', 'postgres', '-d', 'jiulong', '-v', 'ON_ERROR_STOP=1',
                    '-c', sql, envfile=envfile)
    print("已恢复到空实例，旧会话已撤销。下一步运行 start 和 check。")


def start_instance(*, envfile=None):
    ensure_maintenance_configuration(envfile)
    require_locks()
    from source_distribution import source_revision
    revision = source_revision(ROOT)
    instance_env = envfile or ROOT / ".env"
    lines = instance_env.read_text(encoding="utf-8").splitlines()
    instance_env.write_text("\n".join("JL_BUILD_REVISION=" + revision if x.startswith("JL_BUILD_REVISION=") else x for x in lines) + "\n", encoding="utf-8")
    compose("config", "--quiet", envfile=envfile)
    # Compose gives each build-only service its own image tag. Building api alone
    # leaves cached worker/migrate tags on an older schema and implementation.
    compose("build", "api", "worker", "scheduler", "realtime-worker", "realtime-worker-secondary", "recovery-worker", "ai-worker", "read-cache-worker", "maintenance-worker", "credential-init", "migrate", "web", envfile=envfile)
    compose("up", "-d", "--wait", "db", envfile=envfile)
    prepare_maintenance_role(envfile=envfile)
    compose("run", "--rm", "migrate", envfile=envfile)
    prepare_maintenance_role(envfile=envfile)
    compose("up", "-d", "api", "worker", "scheduler", "realtime-worker", "realtime-worker-secondary", "recovery-worker", "ai-worker", "read-cache-worker", "maintenance-worker", "web", envfile=envfile)
    wait_ready(config(instance_env)["JL_PUBLIC_ORIGIN"])
    print("已启动：" + config(instance_env)["JL_PUBLIC_ORIGIN"])


def configure_output():
    # Redirected Windows streams otherwise default to the local code page and
    # cannot write Playwright's check marks, even after a successful browser run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')


def main():
    configure_output()
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init")
    init.add_argument("--mode", choices=["demo", "real"], default="demo")
    init.add_argument("--project", default="jlpm-demo")
    init.add_argument("--port", type=int, choices=range(1024, 65536), default=8088, metavar="PORT")
    locking = subs.add_parser("lock")
    locking.add_argument("--refresh", action="store_true")
    for name in ["start", "stop", "status", "check", "backup", "verify-locks"]:
        subs.add_parser(name)
    subs.add_parser("shortcut", help="为本 Windows 实例创建桌面快捷方式")
    testing = subs.add_parser("test")
    testing.add_argument("--subnet", type=test_network,
        help="仅为本次隔离测试指定未占用的 RFC1918 网段；不修改或清理现有网络。")
    testing.add_argument('--suite', choices=['all', 'backend', 'browser'], default='all',
        help='默认完整验收；修改页面夹具时可单独重跑browser，不把未执行阶段记为通过。')
    testing.add_argument('--spec', nargs='+', metavar='FILE.spec.ts',
        help='仅运行指定的现有页面文件；不改变默认完整验收范围。')
    subs.add_parser("restore").add_argument("directory")
    subs.add_parser("bootstrap").add_argument("--username", required=True)
    args = p.parse_args()
    if args.command == "init":
        initialize(ROOT / ".env", args.mode, args.project, args.port)
        print("已生成本机配置与独立数据库密码；没有创建默认管理员。")
    elif args.command == "lock":
        lock(args.refresh)
    elif args.command == "test":
        test(args.subnet, suite=args.suite, specs=args.spec)
    elif args.command == "verify-locks":
        require_locks()
        print("依赖输入、锁文件与镜像摘要的 hash 全部一致。")
    elif args.command == "backup":
        backup()
    elif args.command == "restore":
        restore(args.directory)
    elif args.command == "start":
        start_instance()
        if os.name == "nt" and config().get("WEB_BIND") == "127.0.0.1":
            from desktop_launcher import create_shortcut
            try:
                create_shortcut()
            except (OSError, subprocess.CalledProcessError):
                print("服务已启动。可运行 python scripts/project.py shortcut 补建桌面入口。")
    elif args.command == "check":
        wait_ready(config()["JL_PUBLIC_ORIGIN"])
        print("数据库、迁移和 worker 心跳已就绪；这不代表业务数据已接入。")
    elif args.command == "bootstrap":
        compose("exec", "api", "python", "-m", "app.cli", "bootstrap", "--username", args.username)
        if os.name == "nt":
            from desktop_launcher import create_shortcut
            try:
                create_shortcut()
                print("桌面快捷方式已创建。")
            except (OSError, subprocess.CalledProcessError):
                print("管理员已创建。可运行 python scripts/project.py shortcut 重试创建桌面入口。")
    elif args.command == "shortcut":
        from desktop_launcher import create_shortcut
        create_shortcut()
        print("桌面快捷方式已创建。")
    else:
        compose({"stop": "stop", "status": "ps"}[args.command])


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"步骤未完成（退出码 {e.returncode}）。请查看上方输出；不会自动清空数据或重试部署。") from None
