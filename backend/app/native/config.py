"""Validated, non-secret installation configuration and private host state."""
from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
from urllib.parse import quote


ROLES = ('scheduler', 'realtime', 'recovery', 'general')


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.pending')
    with temporary.open('w', encoding='utf-8', newline='\n') as output:
        json.dump(value, output, ensure_ascii=False, separators=(',', ':'))
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class NativeConfig:
    file: Path
    program: Path
    data: Path
    http_port: int = 8088
    database_port: int = 55438
    collection_enabled: bool = True
    mode: str = 'real'

    @classmethod
    def read(cls, path):
        path = Path(path)
        if path.is_symlink() or path.is_junction():
            raise ValueError('native_symlink_not_allowed')
        path = path.resolve(strict=True)
        raw = json.loads(path.read_text(encoding='utf-8'))
        if raw.get('format') != 1 or set(raw) - {
            'format', 'program', 'data', 'http_port', 'database_port', 'collection_enabled', 'mode'
        }:
            raise ValueError('native_configuration_invalid')
        program, data = Path(raw['program']), Path(raw['data'])
        if not program.is_absolute() or not data.is_absolute():
            raise ValueError('native_paths_must_be_absolute')
        if any(candidate.is_symlink() or candidate.is_junction() for candidate in (program, data)):
            raise ValueError('native_symlink_not_allowed')
        program, data = program.resolve(), data.resolve()
        if program == data or program in data.parents or data in program.parents:
            raise ValueError('native_program_data_must_be_separate')
        for key, default in (('http_port', 8088), ('database_port', 55438)):
            if type(raw.get(key, default)) is not int or not 1024 <= raw.get(key, default) <= 65535:
                raise ValueError('native_port_invalid')
        if raw.get('http_port', 8088) == raw.get('database_port', 55438):
            raise ValueError('native_ports_conflict')
        if type(raw.get('collection_enabled', True)) is not bool or raw.get('mode', 'real') not in {'real', 'demo'}:
            raise ValueError('native_configuration_invalid')
        for candidate in (program, data, path):
            if candidate.is_symlink():
                raise ValueError('native_symlink_not_allowed')
        return cls(path, program, data, raw.get('http_port', 8088), raw.get('database_port', 55438),
                   raw.get('collection_enabled', True), raw.get('mode', 'real'))

    @property
    def python(self):
        return self.program / 'runtime' / 'python.exe'

    @property
    def pg_bin(self):
        return self.program / 'postgresql' / 'bin'

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.http_port}'

    def private(self):
        raw = json.loads((self.data / 'private' / 'host.json').read_text(encoding='utf-8'))
        import re
        if (not isinstance(raw, dict) or set(raw.get('passwords', {})) != {'admin', 'app', 'migrate', 'maintenance'}
                or any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
                       for value in raw['passwords'].values())):
            raise ValueError('native_private_state_invalid')
        return raw

    def database_url(self, role='app'):
        private = self.private()
        names = {'app': 'jlpm_app', 'migrate': 'jlpm_migrate', 'maintenance': 'jlpm_maintenance', 'admin': 'postgres'}
        if role not in names:
            raise ValueError('native_role_invalid')
        return (f"postgresql+psycopg://{names[role]}:{quote(private['passwords'][role], safe='')}"
                f'@127.0.0.1:{self.database_port}/jiulong')

    def environment(self, role='app'):
        # Ambient developer variables must never select another database or leak
        # the machine owner's provider credentials into an installed instance.
        env = {key: value for key, value in os.environ.items() if not key.startswith('JL_')}
        if os.name == 'nt':
            windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
            executable_path = os.pathsep.join(map(str, (self.pg_bin, self.program / 'runtime',
                windows / 'System32', windows, windows / 'System32' / 'Wbem')))
        else:
            executable_path = str(self.pg_bin) + os.pathsep + env.get('PATH', '')
        revision = json.loads((self.program / 'release.json').read_text(encoding='utf-8'))['build_revision']
        env.update({
            'PYTHONPATH': str(self.program / 'backend'), 'PYTHONNOUSERSITE': '1',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
            'PATH': executable_path,
            'JL_DATABASE_URL': self.database_url(role), 'JL_APP_MODE': self.mode,
            'JL_PUBLIC_ORIGIN': self.origin, 'JL_COOKIE_SECURE': 'false',
            'JL_SESSION_COOKIE': 'tianyan_native_' + hashlib.sha256(str(self.data).encode()).hexdigest()[:12],
            'JL_BUILD_REVISION': revision,
            'JL_DATA_COLLECTION_ENABLED': str(self.collection_enabled).lower(),
            'JL_MODEL_ENABLED': 'false', 'JL_READ_CACHE_ENABLED': 'true',
            'JL_REQUIRED_WORKER_ROLES': json.dumps(list(ROLES)),
            'JL_STATIC_DIRECTORY': str(self.program / 'frontend'),
            'JL_ARTIFACT_DIRECTORY': str(self.data / 'artifacts'),
            'JL_RUNTIME_DIRECTORY': str(self.program),
            'JL_BACKUP_DIRECTORY': str(self.data / 'backups'),
            'JL_UPDATE_DIRECTORY': str(self.data / 'updates'),
            'JL_MAINTENANCE_STATUS_DIRECTORY': str(self.data / 'maintenance-status'),
            'JL_AI_SECRET_DIRECTORY': str(self.data / 'credentials'),
            'JL_AI_MASTER_KEY_FILE': str(self.data / 'master' / 'ai_master_key'),
            'JL_NATIVE_CONFIG_FILE': str(self.file), 'JL_UPDATE_INSTALLATION': 'source',
        })
        return env


def prepare_directories(config):
    for suffix in ('private', 'database', 'logs', 'artifacts', 'credentials', 'master', 'backups',
                   'updates', 'maintenance-status', 'control'):
        folder = config.data / suffix
        if folder.is_symlink():
            raise ValueError('native_data_symlink_not_allowed')
        folder.mkdir(parents=True, exist_ok=True)
    host = config.data / 'private' / 'host.json'
    if not host.exists():
        if (config.data / 'database' / 'PG_VERSION').exists() or any((config.data / 'credentials').iterdir()):
            raise ValueError('native_private_state_missing_restore_backup')
        atomic_json(host, {'passwords': {key: secrets.token_hex(32) for key in ('admin', 'migrate', 'app', 'maintenance')},
                           'setup_token': secrets.token_urlsafe(48)})
    master = config.data / 'master' / 'ai_master_key'
    if not master.exists():
        if any((config.data / 'credentials').iterdir()):
            raise ValueError('native_master_missing_restore_backup')
        import base64
        with master.open('xb') as output:
            output.write(base64.urlsafe_b64encode(os.urandom(32)) + b'\n')
            output.flush()
            os.fsync(output.fileno())
    import base64
    try:
        if len(base64.urlsafe_b64decode(master.read_bytes().strip())) != 32:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('native_master_invalid_restore_backup') from None


def require_free_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Never silently select another port: cookies, browser origin and the
        # service's configured address must stay the same across restarts.
        probe.bind(('127.0.0.1', port))
