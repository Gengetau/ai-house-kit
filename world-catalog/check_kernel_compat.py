"""Run own tests in a temporary copy with ONLY its vocabulary module replaced.

The source directory is never modified. Passing means data-interface compatibility, not
acceptance of the world kernel or proof that guard_request has been wired into it.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    args = parser.parse_args()
    source = args.kernel.resolve()
    if not (source / "worldkernel/vocab.py").is_file():
        parser.error("--kernel must point to the world-kernel directory")
    package = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="nell-w5-compat-") as folder:
        target = Path(folder)
        for path in source.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        (target / "worldkernel/vocab.py").write_text("from worldcatalog.kernel_vocab import *\n", encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(package))
        drift = subprocess.run([sys.executable, "-c", "from worldkernel import kernel as K; from worldcatalog import kernel_vocab as V; assert K.BASE_DRIFT == V.BASE_DRIFT, 'body base drift differs from catalogue'; assert V.validate() == []"], cwd=target, env=env, check=False)
        if drift.returncode:
            return drift.returncode
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"], cwd=target, env=env, check=False)
    print(json.dumps({"catalogue_adapter_compatibility": "PASS" if result.returncode == 0 else "FAIL",
                      "guard_wiring": "NOT_VERIFIED: required integration hooks", "source_modified": False}))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
