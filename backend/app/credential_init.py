"""Initialize private Docker volumes once; never replace an existing master key."""
import base64
import os
from pathlib import Path


def initialize(master_directory='/master', credential_directory='/credentials', *, owner=10001):
    master = Path(master_directory)
    credentials = Path(credential_directory)
    for folder in (master, credentials):
        if folder.is_symlink():
            raise RuntimeError('Credential directory must be a real private directory')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            os.chown(folder, owner, owner)
        folder.chmod(0o700)
    key_file = master / 'ai_master_key'
    if key_file.is_symlink():
        raise RuntimeError('Master key cannot be a symlink')
    if key_file.exists():
        raw = key_file.read_bytes().strip()
        try:
            valid = len(raw) == 44 and len(base64.urlsafe_b64decode(raw)) == 32
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError('Existing master key is invalid; restore its private backup')
    else:
        # Ciphertexts without a master indicate a lost key, never a fresh install.
        if any(credentials.iterdir()):
            raise RuntimeError('Ciphertexts exist without their master key; refusing to generate a replacement')
        descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(base64.urlsafe_b64encode(os.urandom(32)) + b'\n')
            handle.flush()
            os.fsync(handle.fileno())
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        os.chown(key_file, owner, owner)
    key_file.chmod(0o400)


if __name__ == '__main__':
    initialize()
    # Fixed output mounts only. Existing backups are never traversed or changed.
    for name in ('/workspace/backups', '/workspace/updates'):
        folder = Path(name)
        if folder.is_symlink():
            raise RuntimeError('Maintenance output directory cannot be a symlink')
        if folder.is_dir() and hasattr(os, 'geteuid') and os.geteuid() == 0:
            os.chown(folder, 10001, 10001)
            folder.chmod(0o700)
    print('Private credential volumes ready; master key is not returned.')
