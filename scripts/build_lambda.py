"""Build build/lambda.zip: the afs package plus its runtime dependencies for Lambda.

    uv run python scripts/build_lambda.py

- Dependency versions come from uv.lock (via `uv export`), so Lambda runs
  exactly what the tests ran against.
- Wheels are fetched for Linux arm64 (Graviton) regardless of the build
  machine's OS. manylinux_2_28 = glibc 2.28+, which Lambda's Amazon Linux
  2023 (glibc 2.34) satisfies.
- boto3 is not packaged: the Lambda Python runtime already provides it.
- The zip is deterministic (sorted entries, fixed timestamps), so rebuilding
  unchanged code produces an identical file and Terraform sees no change.
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
STAGE = BUILD / "lambda"
ZIP = BUILD / "lambda.zip"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def run(*args: str) -> None:
    subprocess.run(args, check=True, cwd=ROOT)


def main() -> None:
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)

    requirements = BUILD / "requirements.txt"
    run("uv", "export", "--no-dev", "--no-hashes", "--no-emit-project", "--quiet", "-o", str(requirements))
    run(
        "uv", "pip", "install", "--quiet",
        "--target", str(STAGE),
        "--python-platform", "aarch64-manylinux_2_28",
        "--python-version", "3.12",
        "--only-binary=:all:",
        "-r", str(requirements),
    )
    shutil.copytree(ROOT / "src" / "afs", STAGE / "afs", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    files = sorted(p for p in STAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(STAGE).as_posix(), date_time=FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())

    print(f"{ZIP.relative_to(ROOT)}: {len(files)} files, {ZIP.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
