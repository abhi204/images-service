"""Build the Python 3.13 Lambda archive with Linux wheels for one architecture."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "image_service"
LOCAL = ROOT / ".local"
ARCHES = {
    "arm64": ("manylinux_2_28_aarch64", "manylinux2014_aarch64"),
    "x86_64": ("manylinux_2_28_x86_64", "manylinux2014_x86_64"),
}
DEFAULT_ARCH = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"


def build(arch: str) -> Path:
    if not PACKAGE.is_dir() or not (PACKAGE / "__init__.py").is_file():
        raise FileNotFoundError(f"Missing application package: {PACKAGE}")
    requirements = ROOT / "requirements.txt"
    if not requirements.is_file():
        raise FileNotFoundError(f"Missing pinned dependencies: {requirements}")
    if arch not in ARCHES:
        raise ValueError(f"Unsupported Lambda architecture: {arch}")

    LOCAL.mkdir(exist_ok=True)
    target = LOCAL / "package"
    archive = LOCAL / "function.zip"
    cache = LOCAL / "pip-cache"
    cache.mkdir(exist_ok=True)
    if target.is_symlink():
        raise RuntimeError(f"Refusing to replace symlink: {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-compile",
            "--only-binary=:all:",
            "--implementation=cp",
            "--python-version=3.13",
            "--abi=cp313",
            *(f"--platform={wheel_platform}" for wheel_platform in ARCHES[arch]),
            "--target",
            str(target),
            "-r",
            str(requirements),
        ],
        check=True,
        env={**os.environ, "PIP_CACHE_DIR": str(cache)},
    )
    shutil.copytree(PACKAGE, target / PACKAGE.name)
    temporary = LOCAL / "function.zip.tmp"
    with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as zip_file:
        for path in sorted(target.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                zip_file.write(path, path.relative_to(target).as_posix())
    temporary.replace(archive)
    print(f"Built {archive} ({archive.stat().st_size:,} bytes) for {arch}")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=ARCHES, default=DEFAULT_ARCH)
    build(parser.parse_args().arch)
