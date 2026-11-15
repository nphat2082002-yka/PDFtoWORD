"""Release packager for VN-Office PDF to Word Pro.

Builds:
1. Standalone Windows GUI executable + _internal runtime (`dist/VN-Office-PDF-to-Word-Pro/VN-Office-PDF-to-Word-Pro.exe`)
2. Bundled offline OCR models (`dist/VN-Office-PDF-to-Word-Pro/models/`)
3. Reference CLI / source scripts & user guide (`HUONG_DAN_SU_DUNG.txt`)
4. Installable Python wheel (`dist/pdf2word_vn-1.0.0-py3-none-any.whl`)
5. Distributable Windows release archive (`dist/VN-Office-PDF-to-Word-Pro-v1.0.0-Win64.zip`)

Copyright © 2026 Huỳnh Ngọc Phát.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
APP_NAME = "VN-Office-PDF-to-Word-Pro"
APP_VERSION = "1.0.0"
RELEASE_DIR = DIST_DIR / APP_NAME


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return True


def stage_offline_models(release_dir: Path) -> None:
    """Copy cached PaddleOCR + VietOCR weights into `<release_dir>/models/` for offline use."""
    models_root = release_dir / "models"
    models_root.mkdir(parents=True, exist_ok=True)

    # 1. VietOCR weights & offline YAML configs (avoid network dependency on offline PCs)
    temp_dir = Path(tempfile.gettempdir())
    vietocr_dir = models_root / "vietocr"
    vietocr_dir.mkdir(parents=True, exist_ok=True)
    for weight_name in ("vgg_transformer.pth", "vgg_seq2seq.pth"):
        for candidate in (
            temp_dir / weight_name,
            Path.home() / ".cache" / "vietocr" / weight_name,
            ROOT / "models" / "vietocr" / weight_name,
        ):
            if _copy_if_exists(candidate, vietocr_dir / weight_name):
                print(f"[models] Bundled VietOCR weight: {candidate}")
                break

    try:
        from vietocr.tool.config import Cfg

        for model_key in ("vgg_transformer", "vgg_seq2seq"):
            cfg_out = vietocr_dir / f"{model_key}.yml"
            try:
                cfg = Cfg.load_config_from_name(model_key)
                cfg.save(str(cfg_out))
                print(f"[models] Bundled VietOCR config: {cfg_out.name}")
            except Exception as exc:
                print(f"[models] Warning: could not cache {model_key}.yml ({exc})")
    except Exception as exc:
        print(f"[models] Warning: vietocr config export skipped ({exc})")

    # 2. PaddleOCR inference models (cached under ~/.paddleocr/whl/)
    paddle_home = Path.home() / ".paddleocr" / "whl"
    paddle_mappings = (
        (
            models_root / "paddleocr" / "det",
            (
                paddle_home / "det" / "ch" / "ch_PP-OCRv4_det_infer",
                ROOT / "models" / "paddleocr" / "det",
            ),
        ),
        (
            models_root / "paddleocr" / "cls",
            (
                paddle_home / "cls" / "ch_ppocr_mobile_v2.0_cls_infer",
                ROOT / "models" / "paddleocr" / "cls",
            ),
        ),
        (
            models_root / "paddleocr" / "rec",
            (
                paddle_home / "rec" / "ch" / "ch_PP-OCRv4_rec_infer",
                ROOT / "models" / "paddleocr" / "rec",
            ),
        ),
    )
    for dst_dir, candidates in paddle_mappings:
        for cand in candidates:
            if (cand / "inference.pdmodel").is_file():
                _copy_if_exists(cand, dst_dir)
                print(f"[models] Bundled PaddleOCR dir: {cand} -> {dst_dir}")
                break


def build_standalone_exe() -> None:
    """Build full standalone `--onedir` Windows GUI bundle via PyInstaller spec."""
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(ROOT / f"{APP_NAME}.spec"),
    ]
    print("[pyinstaller] Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def build_launcher_exe() -> None:
    """Build compact native launcher .exe wrapping staged `app_src/` (fast fallback mode)."""
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name",
        APP_NAME,
        "--distpath",
        str(RELEASE_DIR),
        "--icon",
        str(ROOT / "ui" / "assets" / "app_icon.ico"),
        "--version-file",
        str(ROOT / "packaging" / "version_info.txt"),
        str(ROOT / "packaging" / "launcher_entry.py"),
    ]
    print("[pyinstaller] Running launcher build:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def stage_support_files(release_dir: Path, include_app_src: bool = False) -> None:
    """Copy documentation, assets, and optional CLI/app source helpers into release folder."""
    _copy_if_exists(ROOT / "ui" / "assets", release_dir / "ui" / "assets")
    _copy_if_exists(ROOT / "requirements.txt", release_dir / "requirements.txt")
    _copy_if_exists(ROOT / "README.md", release_dir / "README.md")

    if include_app_src:
        app_src = release_dir / "app_src"
        for sub in ("pdf2word", "ui", "app.py", "convert.py", "requirements.txt"):
            _copy_if_exists(ROOT / sub, app_src / sub)

    guide_text = (
        f"VN-Office PDF to Word Pro v{APP_VERSION}\n"
        "© 2026 Bản quyền bởi Huỳnh Ngọc Phát. All rights reserved.\n"
        "=============================================================\n\n"
        "1. CÁCH CHẠY NHANH (ỨNG DỤNG DESKTOP)\n"
        f"   - Nhấp đúp file `{APP_NAME}.exe` để mở ứng dụng.\n"
        "   - Màn hình Splash Screen sẽ tự động nạp sẵn mô hình AI OCR\n"
        "     từ thư mục `models/` đi kèm (chạy hoàn toàn offline).\n"
        "   - Kéo thả hoặc chọn file PDF cần chuyển đổi, sau đó bấm\n"
        "     'BẮT ĐẦU CHUYỂN ĐỔI'.\n\n"
        "2. THÔNG TIN BẢN QUYỀN\n"
        "   - Tác giả & Chủ sở hữu bản quyền: Huỳnh Ngọc Phát\n"
        "   - Công nghệ lõi: PaddleOCR + VietOCR + 6-Layer ADRT Engine\n"
    )
    (release_dir / "HUONG_DAN_SU_DUNG.txt").write_text(guide_text, encoding="utf-8")


def build_python_wheel() -> Path | None:
    """Build pure-Python distributable wheel package into `dist/`."""
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        str(ROOT),
        "-w",
        str(DIST_DIR),
    ]
    print("[wheel] Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)
    wheels = sorted(DIST_DIR.glob(f"pdf2word_vn-{APP_VERSION}*.whl"))
    return wheels[-1] if wheels else None


def create_release_zip(release_dir: Path) -> Path:
    """Create distributable Windows Win64 `.zip` package inside `dist/`."""
    zip_path = DIST_DIR / f"{APP_NAME}-v{APP_VERSION}-Win64.zip"
    if zip_path.exists():
        zip_path.unlink()

    all_files = [p for p in release_dir.rglob("*") if p.is_file()]
    print(f"[zip] Compressing {len(all_files)} files into {zip_path.name} ...")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in all_files:
            arcname = Path(APP_NAME) / file_path.relative_to(release_dir)
            zf.write(file_path, arcname=str(arcname))
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} release package.")
    parser.add_argument(
        "--mode",
        choices=("standalone", "launcher"),
        default="standalone",
        help="'standalone' bundles Python + AI libraries via PyInstaller spec (default); 'launcher' builds native launcher + app_src.",
    )
    parser.add_argument("--skip-zip", action="store_true", help="Skip creating final .zip archive.")
    parser.add_argument("--skip-wheel", action="store_true", help="Skip building Python .whl package.")
    args = parser.parse_args(argv)

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if RELEASE_DIR.exists():
        shutil.rmtree(RELEASE_DIR)

    if args.mode == "standalone":
        build_standalone_exe()
        stage_support_files(RELEASE_DIR, include_app_src=False)
    else:
        build_launcher_exe()
        stage_support_files(RELEASE_DIR, include_app_src=True)

    stage_offline_models(RELEASE_DIR)

    # Clean intermediate PyInstaller build cache after packaging completes
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)

    if not args.skip_wheel:
        whl = build_python_wheel()
        if whl:
            print(f"[wheel] Built: {whl} ({whl.stat().st_size / 1024:.1f} KB)")

    if not args.skip_zip:
        zip_path = create_release_zip(RELEASE_DIR)
        print(f"[zip] Created: {zip_path} ({zip_path.stat().st_size / (1024 * 1024):.1f} MB)")

    exe_path = RELEASE_DIR / f"{APP_NAME}.exe"
    print(f"[done] Release executable ready: {exe_path} ({exe_path.exists()})")
    return 0 if exe_path.is_file() else 1


if __name__ == "__main__":
    sys.exit(main())
