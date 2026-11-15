# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for standalone Windows release of VN-Office PDF to Word Pro.

Copyright © 2026 Huỳnh Ngọc Phát.
"""

from __future__ import annotations

from pathlib import Path

# Import torch first on Windows so subsequent `collect_all` resolves DLLs cleanly.
import torch  # noqa: F401
import torchvision
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

ROOT = Path(SPECPATH).resolve()

datas = [
    (str(ROOT / "ui" / "assets"), "ui/assets"),
    (str(ROOT / "app.py"), "."),
]
binaries = []
hiddenimports = [
    "app",
    "pdf2word",
    "pdf2word.ocr_engine",
    "pdf2word.analyzer",
    "pdf2word.layout_engine",
    "pdf2word.docx_synthesizer",
    "pdf2word.converter",
    "pdf2word.vietnamese",
    "pdf2word.abbyy_adapter",
    "pdf2word.cli",
    "ui",
    "ui.styles",
    "ui.splash_screen",
    "ui.main_window",
    "ui.workers",
    "pymupdf",
    "fitz",
    "pdf2docx",
    "cv2",
    "PIL",
    "PIL._tkinter_finder",
    "einops",
    "yaml",
    "lmdb",
    "gdown",
    "pyclipper",
    "shapely",
    "imgaug",
    "albumentations",
    "albucore",
    "rapidfuzz",
    "requests",
    "tqdm",
    "packaging",
    "fontTools",
    "scipy",
    "skimage",
    "prefetch_generator",
]

# Collect packages that rely on dynamic imports, native libs, or non-code templates/dicts
for pkg in (
    "paddle",
    "paddleocr",
    "vietocr",
    "torchvision",
    "docx",
    "certifi",
    "skimage",
    "Cython",
    "tokenizers",
):
    pkg_datas, pkg_bins, pkg_hidden = collect_all(
        pkg,
        include_py_files=(pkg in ("paddleocr", "paddle", "skimage")),
    )
    datas += pkg_datas
    binaries += pkg_bins
    hiddenimports += pkg_hidden

# Bundle torchvision's C++ extension `.pyd` files alongside torchvision.__file__ (`_internal/torchvision/`)
tv_dir = Path(torchvision.__file__).resolve().parent
for pyd_file in tv_dir.glob("*.pyd"):
    binaries.append((str(pyd_file), "torchvision"))

# Include vietocr YAML configs if any exist under site-packages
datas += collect_data_files("vietocr", include_py_files=False)

# Bundle package dist-info metadata for libraries calling `importlib.metadata` at import/runtime
for dist_pkg in (
    "gdown",
    "torchvision",
    "pymupdf",
    "pdf2docx",
    "requests",
    "packaging",
    "einops",
    "scipy",
    "shapely",
    "rapidfuzz",
    "imageio",
    "lazy_loader",
    "pillow",
    "imgaug",
    "lmdb",
    "pyclipper",
    "PyYAML",
    "fonttools",
    "PySide6",
):
    try:
        datas += copy_metadata(dist_pkg)
    except Exception:
        pass

excludes = [
    "tkinter",
    "_tkinter",
    "pytest",
    "IPython",
    "jupyter",
    "notebook",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.QtDesigner",
    "PySide6.QtSql",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=list(dict.fromkeys(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "pyi_rth_torch_first.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
    module_collection_mode={"paddleocr": "py", "skimage": "py"},
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VN-Office-PDF-to-Word-Pro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "ui" / "assets" / "app_icon.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VN-Office-PDF-to-Word-Pro",
)
