"""PyInstaller runtime hook: configure DLL & offline model paths before importing PyTorch/Paddle/Qt."""

import os
import sys
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

exe_dir = Path(sys.executable).resolve().parent
models_candidate = exe_dir / "models"
if models_candidate.is_dir():
    os.environ.setdefault("PDF2WORD_MODELS_DIR", str(models_candidate))

meipass = getattr(sys, "_MEIPASS", None)
if meipass:
    meipass_path = Path(meipass)
    if meipass_path.is_dir():
        os.environ["PATH"] = str(meipass_path) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(meipass_path))
            except OSError:
                pass

    torch_lib = meipass_path / "torch" / "lib"
    if torch_lib.is_dir():
        os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(torch_lib))
            except OSError:
                pass

    # Put paddleocr's package directory on sys.path so `import tools` / `import ppocr` resolve reliably
    paddleocr_dir = meipass_path / "paddleocr"
    if paddleocr_dir.is_dir() and str(paddleocr_dir) not in sys.path:
        sys.path.append(str(paddleocr_dir))
