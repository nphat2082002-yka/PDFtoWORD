"""Unified frozen .exe / portable launcher entry point for VN-Office PDF to Word Pro.

Copyright © 2026 Huỳnh Ngọc Phát. All rights reserved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _bootstrap_paths() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
    else:
        exe_dir = Path(__file__).resolve().parent.parent

    models_dir = exe_dir / "models"
    if models_dir.is_dir():
        os.environ.setdefault("PDF2WORD_MODELS_DIR", str(models_dir))

    for src_candidate in (exe_dir / "app_src", exe_dir):
        if (src_candidate / "app.py").is_file() and str(src_candidate) not in sys.path:
            sys.path.insert(0, str(src_candidate))
    return exe_dir


def _run_in_process() -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    # Import PyTorch first on Windows before PaddlePaddle / PySide6 to avoid OpenMP/MKL symbol conflicts.
    import torch  # noqa: F401
    from app import main as app_main

    return int(app_main(sys.argv[1:]))


def _run_via_python_runtime(exe_dir: Path) -> int:
    app_script = exe_dir / "app_src" / "app.py"
    if not app_script.is_file():
        app_script = exe_dir / "app.py"
    if not app_script.is_file():
        raise FileNotFoundError(f"Cannot find app.py near {exe_dir}")

    env = os.environ.copy()
    app_parent = str(app_script.parent)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = app_parent if not existing_pp else f"{app_parent}{os.pathsep}{existing_pp}"

    smoke = "--smoke-test" in sys.argv[1:]
    py_exe = shutil.which("python" if smoke else "pythonw") or shutil.which("python") or sys.executable
    cmd = [py_exe, str(app_script), *sys.argv[1:]]
    proc = subprocess.run(cmd, env=env, check=False)
    return int(proc.returncode)


def main() -> int:
    exe_dir = _bootstrap_paths()
    try:
        return _run_in_process()
    except ImportError:
        if not getattr(sys, "frozen", False):
            raise
        return _run_via_python_runtime(exe_dir)


if __name__ == "__main__":
    sys.exit(main())
