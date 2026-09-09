"""Create a signed release. Run only after Windows build and upgrade acceptance."""
import argparse
import base64
import hashlib
import time
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from update_protocol import canonical, version_tuple, atomic_json, verify_manifest, verify_executable, TRUSTED_KEYS


def publish(exe, version, key_path, key_id, output):
    version_tuple(version)
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('An Ed25519 release signing key is required')
    release = dict(version=version,platform='windows-x64',protocol=1,size=exe.stat().st_size,
                   sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                   issued_at=int(time.time()),expires_at=int(time.time()) + 90*86400)
    envelope = dict(release=release,key_id=key_id,signature=base64.b64encode(key.sign(canonical(release))).decode('ascii'))
    verify_manifest(envelope)
    verify_executable(exe, release)
    output.mkdir(parents=True,exist_ok=True)
    artifact = output / (release['sha256'] + '.exe')
    import shutil
    shutil.copy2(exe, artifact.with_suffix('.tmp'))
    artifact.with_suffix('.tmp').replace(artifact)
    # Advertise only after the immutable, verified artifact is present.
    atomic_json(output/'stable.json', envelope)
    return release


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--key',type=Path,required=True)
    parser.add_argument('--key-id',default='hawkhive-2026')
    parser.add_argument('--output',type=Path,default=Path('release/updates'))
    args=parser.parse_args()
    print(publish(args.exe,args.version,args.key,args.key_id,args.output))
