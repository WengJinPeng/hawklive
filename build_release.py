from __future__ import annotations

import argparse
import hashlib
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROOT_SUFFIXES = {".py", ".sql", ".md", ".txt", ".yml", ".ps1", ".sh", ".bat", ".vbs", ".cloud"}
ROOT_NAMES = {".dockerignore", ".env.example", ".gitignore", "Caddyfile", "Caddyfile.ip", "test_i18n_runtime.js", "test_email_ui_runtime.js"}
PUBLIC_SUFFIXES = {".html", ".css", ".js"}
FORBIDDEN_NAMES = {".env", "collector_settings.json"}
FORBIDDEN_SUFFIXES = {".sqlite3", ".sqlite3-shm", ".sqlite3-wal", ".dump", ".pyc"}
EXCLUDED_SOURCE_NAMES = {"configure_addvalue.py"}


def release_files() -> list[Path]:
    files = [
        path for path in ROOT.iterdir()
        if path.is_file() and not path.is_symlink()
        and path.name not in EXCLUDED_SOURCE_NAMES
        and (path.name in ROOT_NAMES or path.suffix in ROOT_SUFFIXES)
    ]
    public = ROOT / "public"
    if public.exists():
        files.extend(
            path for path in public.rglob("*")
            if path.is_file() and not path.is_symlink() and path.suffix in PUBLIC_SUFFIXES
        )
    release = ROOT / "release"
    for name in (
        ".gitkeep",
        "HawkHive-DPC8001-Collector.exe",
        "HawkHive-DPC8001-Collector.exe.sha256",
    ):
        path = release / name
        if path.is_file() and not path.is_symlink():
            files.append(path)
    manifest = release / "updates" / "stable.json"
    if manifest.exists():
        import json
        from update_protocol import verify_executable, verify_manifest
        if manifest.is_symlink():
            raise ValueError("Update manifest cannot be a symbolic link")
        payload = verify_manifest(json.loads(manifest.read_text(encoding="utf-8")))
        artifact = manifest.parent / (payload["sha256"] + ".exe")
        if artifact.is_symlink():
            raise ValueError("Update executable cannot be a symbolic link")
        verify_executable(artifact, payload)
        files.extend([manifest, artifact])
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def assert_safe_release_path(path: Path) -> None:
    relative = path.relative_to(ROOT)
    if any(part in {"backups", "__pycache__", "dist"} for part in relative.parts):
        raise ValueError(f"Runtime directory cannot be packaged: {relative}")
    if path.name in FORBIDDEN_NAMES or any(path.name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        raise ValueError(f"Runtime or secret file cannot be packaged: {relative}")


def build_release(output: Path) -> tuple[list[str], str]:
    output = output.resolve()
    if output.suffix.casefold() != ".zip":
        raise ValueError("Release output must use the .zip extension")
    files = release_files()
    if not files:
        raise RuntimeError("No source files found")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            assert_safe_release_path(path)
            relative = path.relative_to(ROOT)
            info = zipfile.ZipInfo(f"dcp8001-source/{relative.as_posix()}", (1980, 1, 1, 0, 0, 0))
            permissions = 0o755 if os.access(path, os.X_OK) else 0o644
            info.external_attr = permissions << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    temporary.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    return [path.relative_to(ROOT).as_posix() for path in files], digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a source-only DCP8001 release archive.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "dcp8001-source.zip",
        help="Output ZIP path (default: dist/dcp8001-source.zip)",
    )
    args = parser.parse_args()
    files, digest = build_release(args.output)
    print(f"Packaged {len(files)} source files")
    print(f"SHA-256: {digest}")
    print(f"Output: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
