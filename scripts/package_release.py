"""Build an offline program-only release and an Ed25519-signed HTTPS feed.

Use the backend Python environment (cryptography is already locked). The signing
key is an operator-supplied private file outside Git. Never ship that file.
"""
import argparse
import base64
import hashlib
import json
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from app.evidence.common import canonical  # noqa: E402
from app.updates.manifest import Manifest, SERVICES, verified  # noqa: E402


def package(args):
    config = json.loads(args.config.read_text(encoding='utf-8'))
    images, revision = {}, None
    for name in sorted(SERVICES):
        configured = config['services'][name]['image']
        row = json.loads(subprocess.check_output(['docker', 'image', 'inspect', configured]))[0]
        build = row['Config'].get('Labels', {}).get('org.opencontainers.image.revision')
        if not build or len(build) != 40 or revision not in {None, build}:
            raise ValueError('all release services must have the same full build revision')
        if any(value.split('=', 1)[0] in {'JL_TUSHARE_TOKEN', 'JL_MAIRUI_LICENCE', 'JL_TICKFLOW_API_KEY', 'JL_MODEL_API_KEY'}
               and value.split('=', 1)[-1] for value in row['Config'].get('Env', [])):
            raise ValueError('runtime credentials must not be embedded in program images')
        revision, images[name] = build, row['Id']
    code = "import json; from app.version import APP_VERSION,SCHEMA_REVISION,SCHEMA_COMPATIBLE_REVISIONS; print(json.dumps({'version':APP_VERSION,'schema':SCHEMA_REVISION,'compatible':sorted(SCHEMA_COMPATIBLE_REVISIONS)}))"
    program = json.loads(subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--read-only', images['api'], 'python', '-c', code]))
    if (program['version'] != args.version or program['schema'] != args.to_revision
        or set(program['compatible']) != set(args.compatible_revision)):
        raise ValueError('release version and compatibility must match the actual API program')
    args.output.mkdir(parents=True, exist_ok=False)
    saved = args.output / 'images.tar'
    archive = args.output / 'program.tar'
    subprocess.run(['docker', 'image', 'save', '-o', str(saved), *sorted(set(images.values()))], check=True)
    with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as target:
        target.add(saved, arcname='images.tar', recursive=False)
    image_size = saved.stat().st_size
    saved.unlink()
    with archive.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = {'format': 1, 'id': args.release_id, 'version': args.version, 'build_revision': revision,
        'channel': 'stable', 'published_at': datetime.now(UTC).isoformat(), 'notes': args.notes.read_text(encoding='utf-8'),
        'automatic_supported': not args.manual, 'manual_guide': args.guide or '', 'package_url': args.package_url,
        'package_sha256': checksum, 'size_bytes': archive.stat().st_size, 'unpacked_bytes': image_size,
        'minimum_free_bytes': args.minimum_free_gib * 1024**3, 'postgres_major': 17,
        'schema': {'from_revisions': args.from_revision, 'to_revision': args.to_revision,
                   'compatible_revisions': args.compatible_revision, 'backward_compatible': args.backward_compatible},
        'images': images}
    Manifest.model_validate(manifest)
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(args.signing_key_file.read_bytes().strip(), validate=True))
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    signature = base64.b64encode(private.sign(canonical(manifest))).decode()
    verified(manifest, signature, public)
    (args.output / 'stable.json').write_bytes(canonical({'manifest': manifest, 'signature': signature}) + b'\n')
    (args.output / 'public-key.txt').write_text(public + '\n', encoding='ascii')
    print('Program-only release saved; signature verified. Publish only program.tar, stable.json and the public key.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--signing-key-file', type=Path, required=True)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--package-url', required=True)
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--from-revision', action='append', required=True)
    parser.add_argument('--to-revision', required=True)
    parser.add_argument('--compatible-revision', action='append', required=True)
    parser.add_argument('--backward-compatible', action='store_true')
    parser.add_argument('--manual', action='store_true')
    parser.add_argument('--guide')
    parser.add_argument('--minimum-free-gib', type=int, default=2)
    package(parser.parse_args())


if __name__ == '__main__':
    main()
