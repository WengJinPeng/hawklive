"""Prune only obsolete digest EXEs; current release and recent downloads survive."""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

DIGEST_EXE = re.compile(r"[0-9a-f]{64}\.exe")


def cleanup(root: Path, *, keep: int = 3, grace_days: int = 7, now: float | None = None, dry_run: bool = False) -> list[str]:
    if keep < 2 or grace_days < 1:
        raise ValueError("Keep at least current and rollback packages, with a download grace period")
    # Fail closed if the channel cannot identify its current package.
    manifest = json.loads((root / "stable.json").read_text())
    digest = manifest["release"]["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid stable digest")
    current = root / (digest + ".exe")
    if current.is_symlink() or not current.is_file():
        raise ValueError("Current package missing")
    packages = sorted((p for p in root.iterdir() if DIGEST_EXE.fullmatch(p.name)
                       and not p.is_symlink() and p.is_file()),
                      key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    protected = {current.name}
    protected.update(p.name for p in [p for p in packages if p != current][:keep - 1])
    cutoff = (time.time() if now is None else now) - grace_days * 86400
    deleted = []
    for package in packages:
        if package.name not in protected and package.stat().st_mtime < cutoff:
            # Abort if a concurrent publication changed the advertised release.
            if json.loads((root / 'stable.json').read_text())['release']['sha256'] != digest:
                raise RuntimeError('Release changed during cleanup')
            if not dry_run:
                package.unlink()
            deleted.append(package.name)
    return deleted


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps({'removed': cleanup(args.root, dry_run=args.dry_run)}))
