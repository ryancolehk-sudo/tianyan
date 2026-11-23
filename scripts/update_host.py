"""Independent, fixed-template local updater. Python standard library + Docker CLI.

Run on the installation host, never inside the API container. The web/API can
queue a signed version but cannot supply a shell command, Compose file or path.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

PROGRAM = ('api', 'worker', 'scheduler', 'realtime-worker', 'realtime-worker-secondary', 'recovery-worker', 'ai-worker', 'read-cache-worker', 'maintenance-worker')
ALL_SERVICES = frozenset((*PROGRAM, 'web', 'migrate', 'credential-init'))


class UpgradeError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def atomic_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        if path.name.endswith('.private.json'):
            temporary.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def single_host(directory):
    with (directory / 'updater.lock').open('a+b') as stream:
        stream.seek(0)
        stream.write(b'1')
        stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class FixedCompose:
    def __init__(self, config, project, work, packages, progress, source=None):
        self.config, self.project = config.resolve(strict=True), project
        self.work, self.packages, self.progress, self.source = work, packages, progress, source
        original = json.loads(self.config.read_text(encoding='utf-8'))
        if not ALL_SERVICES <= original.get('services', {}).keys() or 'db' not in original['services']:
            raise UpgradeError('update_template_invalid', '当前部署缺少完整的固定服务模板，请按手动指引升级。')
        self.control_config = work / 'control.private.json'
        if not self.control_config.exists():
            atomic_json(self.control_config, original)

    def command(self, *args, timeout=120, input=None, allow_failure=False):
        result = subprocess.run(list(args), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode and not allow_failure:
            # Docker diagnostics can include environment values. Only categories
            # cross the control boundary; full private output is never published.
            raise UpgradeError('update_command_failed', '本机升级步骤未完成，已记录阶段，请查看恢复指引。')
        return result.stdout

    def compose(self, *args, control=False, **kwargs):
        return self.command('docker', 'compose', '-p', self.project, '-f', str(self.control_config if control else self.config), *args, **kwargs)

    def bridge(self, action, job=None, **values):
        request = {'action': action, **values}
        if job:
            request.update(id=job['id'], token=job['token'])
        output = self.compose('run', '--rm', '-T', '--no-deps', 'maintenance-worker', 'python', '-m',
            'app.updates.bridge', control=True, input=json.dumps(request).encode(),
            timeout=3600 if action == 'backup' else 660 if action == 'freeze' else 45, allow_failure=True)
        try:
            reply = json.loads(output)
        except (ValueError, UnicodeError):
            raise UpgradeError('updater_control_unavailable', '本机升级控制服务暂不可用，现场及阶段记录已保留。') from None
        if 'error' in reply:
            raise UpgradeError(**reply['error'])
        return reply['data']

    def preflight(self, job):
        manifest = job['params']['manifest']
        if (set(manifest['images']) != ALL_SERVICES or manifest['channel'] != 'stable'
            or not manifest['automatic_supported'] or job['current_schema'] not in manifest['schema']['from_revisions']):
            raise UpgradeError('update_incompatible', '当前版本不支持直接升级，请按迁移指引操作。')
        if self.source and (self.source / '.git').exists():
            changes = self.command('git', '-C', str(self.source), 'status', '--porcelain', '--untracked-files=normal')
            if changes.strip():
                raise UpgradeError('update_local_changes', '检测到本地源码修改，请保存修改后按手动升级指引操作。')
        if job['params']['package_name'] != manifest['package_sha256'] + '.tar':
            raise UpgradeError('update_package_invalid', '更新包标识与官方说明不一致。')
        package = self.packages / job['params']['package_name']
        if (package.name != manifest['package_sha256'] + '.tar' or package.is_symlink()
            or not package.is_file() or package.stat().st_size != manifest['size_bytes']):
            raise UpgradeError('update_package_invalid', '更新包缺失或长度不符，请重新下载。')
        with package.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['package_sha256']:
                raise UpgradeError('update_package_invalid', '更新包完整性校验失败，当前版本已保留。')
        if shutil.disk_usage(self.work).free < manifest['minimum_free_bytes'] + manifest['unpacked_bytes']:
            raise UpgradeError('update_disk_full', '程序更新所需磁盘空间不足。')
        storage = self.bridge('describe')
        if (storage['backup_free_bytes'] < storage['backup_estimate_bytes']
            or storage['runtime_free_bytes'] < manifest['minimum_free_bytes'] + manifest['unpacked_bytes']):
            raise UpgradeError('update_disk_full', '本机备份或程序运行磁盘空间不足。')
        return package

    def load(self, package, job):
        image_tar = self.work / 'images.tar'
        try:
            with tarfile.open(package, 'r:') as archive:
                members = archive.getmembers()
                if (len(members) != 1 or members[0].name != 'images.tar' or not members[0].isfile()
                    or members[0].size != job['params']['manifest']['unpacked_bytes']):
                    raise UpgradeError('update_package_layout_invalid', '更新包不符合固定程序制品格式。')
                with archive.extractfile(members[0]) as source, image_tar.open('wb') as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            self.command('docker', 'image', 'load', '-i', str(image_tar), timeout=600)
            for image in set(job['params']['manifest']['images'].values()):
                inspection = json.loads(self.command('docker', 'image', 'inspect', image))[0]
                if (inspection['Id'] != image or inspection['Config'].get('Labels', {}).get('org.opencontainers.image.revision')
                    != job['params']['manifest']['build_revision']):
                    raise UpgradeError('update_image_invalid', '更新程序的版本标识不一致，已停止安装。')
            info = json.loads(self.command('docker', 'run', '--rm', '--network', 'none', '--read-only',
                job['params']['manifest']['images']['api'], 'python', '-c',
                "import json; from app.version import APP_VERSION,SCHEMA_REVISION,SCHEMA_COMPATIBLE_REVISIONS; print(json.dumps({'version':APP_VERSION,'schema':SCHEMA_REVISION,'compatible':sorted(SCHEMA_COMPATIBLE_REVISIONS)}))"))
            manifest = job['params']['manifest']
            if (info['version'] != manifest['version'] or info['schema'] != manifest['schema']['to_revision']
                or set(info['compatible']) != set(manifest['schema']['compatible_revisions'])):
                raise UpgradeError('update_image_version_invalid', '程序实际版本或数据库兼容范围与签名说明不符。')
        finally:
            image_tar.unlink(missing_ok=True)

    def switch(self, job):
        config = json.loads(self.config.read_text(encoding='utf-8'))
        for name, image in job['params']['manifest']['images'].items():
            service = config['services'][name]
            service.pop('build', None)
            service['image'] = image
            if name != 'web':
                service.setdefault('environment', {})['JL_BUILD_REVISION'] = job['params']['manifest']['build_revision']
        atomic_json(self.config, config)

    def stop(self):
        self.compose('stop', '--timeout', '30', *PROGRAM, timeout=180)

    def migrate(self):
        self.compose('run', '--rm', '-T', '--no-deps', 'migrate', timeout=600)

    def start(self):
        self.compose('up', '-d', '--no-deps', *PROGRAM, timeout=180)

    def verify(self, job):
        manifest = job['params']['manifest']
        rows = json.loads(self.command('docker', 'inspect', *[self.project + '-' + n + '-1' for n in PROGRAM]))
        if any(row['Image'] != manifest['images'][row['Config']['Labels']['com.docker.compose.service']] for row in rows):
            raise UpgradeError('update_service_mismatch', '服务程序版本未对齐，已停止切换。')
        for _ in range(60):
            try:
                result = self.compose('exec', '-T', 'api', 'python', '-c',
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3)", timeout=10)
                if result is not None:
                    self.compose('up', '-d', '--no-deps', 'web', timeout=120)
                    return
            except UpgradeError:
                time.sleep(2)
        raise UpgradeError('update_health_failed', '新版服务校验未通过，正在按兼容范围处理。')

    def restore_program(self, before):
        atomic_json(self.config, json.loads(before.read_text(encoding='utf-8')))
        self.start()
        self.compose('up', '-d', '--no-deps', 'web', timeout=120)
        for _ in range(30):
            try:
                self.compose('exec', '-T', 'api', 'python', '-c',
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3)", timeout=10)
                return
            except UpgradeError:
                time.sleep(2)
        raise UpgradeError('update_rollback_failed', '兼容旧程序未恢复可用，已保留现场和备份，请按恢复指引处理。')


class Updater:
    def __init__(self, adapter, *, drain_seconds=600, wait=time.sleep):
        self.adapter, self.drain_seconds, self.wait = adapter, drain_seconds, wait
        self.journal = adapter.work / 'upgrade.private.json'
        self.before = adapter.work / 'before.private.json'
        self.progress_lock = threading.Lock()

    def publish(self, job, phase, message, *, terminal=False, error_code=None, backup=None):
        path = self.adapter.progress / 'status.json'
        with self.progress_lock:
            if path.exists() and not terminal:
                previous = json.loads(path.read_text(encoding='utf-8'))
                if previous.get('job_id') == job['id'] and previous.get('terminal'):
                    return
            atomic_json(path, {'job_id': job['id'], 'stage': phase,
                'message': message, 'terminal': terminal, 'error_code': error_code, 'backup_available': bool(backup),
                'updated_at': datetime.now(UTC).isoformat()})

    def stage(self, record, phase, message):
        record['phase'] = phase
        record['message'] = message
        atomic_json(self.journal, record)
        self.publish(record['job'], phase, message, backup=record.get('backup'))
        self.adapter.bridge('stage', record['job'], stage=phase, message=message)

    def end(self, record, status, code, message, rollback=False):
        result = {'release_id': record['job']['params']['release_id'], 'version': record['job']['params']['manifest']['version'],
                  'rollback': rollback, 'manual_recovery': status == 'manual_recovery'}
        if record.get('backup'):
            result['backup_id'] = record['backup']['backup_id']
        self.adapter.bridge('finish', record['job'], status=status, error_code=code, message=message, result=result)
        self.publish(record['job'], status, message, terminal=True, error_code=code, backup=record.get('backup'))
        record['phase'] = status
        atomic_json(self.journal, record)

    def recover(self, record):
        if record['phase'] in {'succeeded', 'failed', 'manual_recovery'}:
            return
        job = record['job']
        existing = self.adapter.bridge('observe', job)
        if existing['status'] in {'succeeded', 'failed', 'manual_recovery'}:
            record['phase'] = existing['status']
            atomic_json(self.journal, record)
            self.publish(job, existing['status'], existing['message'], terminal=True,
                         error_code=existing.get('error_code'), backup=record.get('backup'))
            return
        migration_started = record['phase'] in {'migrating', 'starting', 'verifying', 'rollback'}
        compatible = (job['params']['manifest']['schema']['backward_compatible']
            and job['params']['manifest']['schema']['to_revision'] in job['compatible_revisions'])
        if not migration_started or compatible:
            try:
                self.adapter.restore_program(self.before)
            except Exception:
                self.end(record, 'manual_recovery', 'update_rollback_failed', '旧程序未能恢复可用，现场、阶段记录与备份已保留，请按恢复指引处理。')
                return
            self.end(record, 'failed', 'update_interrupted', '升级中断，已恢复兼容的原程序。备份和阶段记录已保留。', rollback=True)
        else:
            self.end(record, 'manual_recovery', 'update_interrupted', '升级中断且可能已迁移资料。现场和完整备份已保留，请按独立恢复指引处理。')

    def once(self):
        if self.journal.exists():
            record = json.loads(self.journal.read_text(encoding='utf-8'))
            if record['phase'] not in {'succeeded', 'failed', 'manual_recovery'}:
                self.recover(record)
                return True
        atomic_json(self.adapter.control_config, json.loads(self.adapter.config.read_text(encoding='utf-8')))
        self.adapter.bridge('heartbeat')
        job = self.adapter.bridge('claim')
        if not job:
            return False
        atomic_json(self.before, json.loads(self.adapter.config.read_text(encoding='utf-8')))
        record = {'job': job, 'phase': 'preflight', 'backup': None}
        atomic_json(self.journal, record)
        stop = threading.Event()
        def keep_alive():
            while not stop.wait(20):
                try:
                    self.adapter.bridge('heartbeat', job)
                    if record['phase'] not in {'succeeded', 'failed', 'manual_recovery'}:
                        self.publish(job, record['phase'], record.get('message', '维护正在进行。'), backup=record.get('backup'))
                except Exception:
                    pass
        heartbeat = threading.Thread(target=keep_alive, daemon=True)
        heartbeat.start()
        try:
            self.stage(record, 'preflight', '正在检查版本、安装方式、磁盘和运行状态。')
            package = self.adapter.preflight(job)
            began = time.monotonic()
            self.stage(record, 'draining', '正在保存维护状态并等待在途任务完成。')
            self.adapter.bridge('freeze', job)
            while self.adapter.bridge('describe')['running']:
                if time.monotonic() - began >= self.drain_seconds:
                    raise UpgradeError('update_drain_timeout', '十分钟内仍有任务未完成，本次升级已中止，当前版本继续运行。')
                self.wait(2)
            self.stage(record, 'backup', '正在生成并校验完整备份；实际恢复验证单独记录。')
            record['backup'] = self.adapter.bridge('backup', job)
            atomic_json(self.journal, record)
            self.stage(record, 'loading', '正在校验固定服务制品，原始资料与配置保留。')
            self.adapter.load(package, job)
            self.stage(record, 'stopping', '正在停止旧程序，维护进度页继续运行。')
            self.adapter.stop()
            self.stage(record, 'switching', '正在切换固定的新版服务制品。')
            self.adapter.switch(job)
            self.stage(record, 'migrating', '正在运行新版的数据迁移。')
            self.adapter.migrate()
            self.stage(record, 'starting', '正在启动完整的新版服务。')
            self.adapter.start()
            self.stage(record, 'verifying', '正在核对服务版本与系统状态。')
            self.adapter.verify(job)
            self.end(record, 'succeeded', None, '升级完成，可以进入新版。')
        except Exception as failure:
            code = getattr(failure, 'code', 'update_step_failed')
            message = getattr(failure, 'message', '升级步骤未完成，请查看阶段记录。')
            try:
                outcome = self.adapter.bridge('observe', job)
            except Exception:
                # The final commit may already have succeeded. Never roll back
                # a program that has reopened writes while the outcome is unknown.
                self.publish(job, 'state_unknown', '暂时无法核对升级结果，已保留现场和备份。恢复连接后将先核对终态。',
                             error_code='update_outcome_unknown', backup=record.get('backup'))
                return True
            if outcome['status'] in {'succeeded', 'failed', 'manual_recovery'}:
                record['phase'] = outcome['status']
                atomic_json(self.journal, record)
                self.publish(job, outcome['status'], outcome['message'], terminal=True,
                             error_code=outcome.get('error_code'), backup=record.get('backup'))
                return True
            migrated = record['phase'] in {'migrating', 'starting', 'verifying'}
            compatible = (job['params']['manifest']['schema']['backward_compatible']
                and job['params']['manifest']['schema']['to_revision'] in job['compatible_revisions'])
            if migrated and not compatible:
                self.end(record, 'manual_recovery', code, message + '现场和备份已保留，请按独立恢复指引处理。')
            else:
                # No database restore here. Only the known previous program
                # descriptor is restored, and only inside its schema range.
                if record['phase'] not in {'preflight', 'draining', 'backup', 'loading'}:
                    try:
                        self.adapter.restore_program(self.before)
                    except Exception:
                        self.end(record, 'manual_recovery', 'update_rollback_failed', '原程序未恢复可用，现场和完整备份已保留，请按恢复指引处理。')
                        return True
                self.end(record, 'failed', code, message, rollback=migrated)
        finally:
            stop.set()
            heartbeat.join(timeout=2)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='Private fixed Compose JSON deployment descriptor')
    parser.add_argument('--project', required=True)
    parser.add_argument('--work', type=Path, required=True, help='Private host updater state; do not expose to the web')
    parser.add_argument('--packages', type=Path, required=True)
    parser.add_argument('--progress', type=Path, required=True, help='Public sanitized progress directory mounted read-only by nginx')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    for path in (args.work, args.packages, args.progress):
        path.mkdir(parents=True, exist_ok=True)
    args.work.chmod(0o700)
    with single_host(args.work):
        source = args.source or Path(__file__).resolve().parents[1]
        updater = Updater(FixedCompose(args.config, args.project, args.work, args.packages, args.progress, source))
        while True:
            try:
                updater.once()
            except Exception as failure:
                print('updater_unavailable error=' + type(failure).__name__, file=sys.stderr)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(10)


if __name__ == '__main__':
    raise SystemExit(main())
