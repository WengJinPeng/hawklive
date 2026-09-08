from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = "DPC8001-G-Windows-EXE-Test"
DEFAULT_EXE = ROOT / "dist" / "HawkHive-DPC8001-Collector-auto-discovery-win11-tested.exe"
DOCUMENTS = ("WINDOWS-EXE-TEST.md", "WINDOWS-EXE-VERIFICATION.md")
LAUNCHERS = ("01-START-DEMO.cmd", "02-START-DEVICE.cmd")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(f"{PACKAGE_ROOT}/{name}", (1980, 1, 1, 0, 0, 0))
    info.external_attr = 0o644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build_windows_exe_package(output: Path, exe_source: Path = DEFAULT_EXE) -> tuple[list[str], str]:
    output = output.resolve()
    exe_source = exe_source.resolve()
    if output.suffix.casefold() != ".zip":
        raise ValueError("Package output must use the .zip extension")
    if not exe_source.is_file():
        raise FileNotFoundError(f"Tested Windows EXE not found: {exe_source}")

    exe_name = "HawkHive-DPC8001-Collector.exe"
    entries: list[tuple[str, bytes]] = [(exe_name, exe_source.read_bytes())]
    for filename in (*LAUNCHERS, *DOCUMENTS):
        path = ROOT / filename
        if not path.is_file():
            raise FileNotFoundError(f"Required delivery file not found: {path}")
        entries.append((filename, path.read_bytes()))

    manifest_lines = [
        f"{hashlib.sha256(data).hexdigest()}  {name}"
        for name, data in entries
    ]
    checksum_name = f"{exe_name}.sha256"
    checksum_data = f"{manifest_lines[0]}\n".encode("ascii")
    entries.append((checksum_name, checksum_data))
    manifest_lines.append(f"{hashlib.sha256(checksum_data).hexdigest()}  {checksum_name}")
    manifest_data = ("\n".join(manifest_lines) + "\n").encode("ascii")
    entries.append(("MANIFEST.sha256", manifest_data))

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in entries:
            archive.writestr(_zip_info(name), data)
    temporary.replace(output)

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii"
    )
    return [name for name, _data in entries], digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Package the Windows-tested portable EXE.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "DPC8001-G-Windows-EXE-Test.zip",
    )
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    args = parser.parse_args()
    files, digest = build_windows_exe_package(args.output, args.exe)
    print(f"Packaged {len(files)} files")
    print(f"SHA-256: {digest}")
    print(f"Output: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
