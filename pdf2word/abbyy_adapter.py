"""Optional local ABBYY FineReader 16 CLI adapter (Approach A).

Uses the local ABBYY FineReader 16 installation strictly as an external read-only
process when explicitly requested, with clean fallback to `CleanRoomConverter` if
CLI automation flags are restricted by the local license profile.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .converter import CleanRoomConverter, ConversionResult, LayoutMode, _postprocess_docx

_DEFAULT_ABBYY_PATHS = (
    Path(r"C:\Program Files\ABBYY FineReader 16\finereaderocr.exe"),
    Path(r"C:\Program Files\ABBYY FineReader 16\FineReader.exe"),
    Path(r"C:\Program Files (x86)\ABBYY FineReader 16\finereaderocr.exe"),
    Path(r"C:\Program Files (x86)\ABBYY FineReader 16\FineReader.exe"),
)


class AbbyyLocalAdapter:
    """Optional wrapper around locally installed ABBYY FineReader 16 executables."""

    def __init__(self, executable_path: str | Path | None = None) -> None:
        if executable_path is not None:
            self.executable = Path(executable_path)
        else:
            self.executable = next((p for p in _DEFAULT_ABBYY_PATHS if p.is_file()), _DEFAULT_ABBYY_PATHS[0])

    def is_available(self) -> bool:
        """Return True if an ABBYY FineReader 16 executable is present on disk."""
        return self.executable.is_file()

    def convert(
        self,
        input_pdf: str | Path,
        output_docx: str | Path,
        mode: LayoutMode | str = LayoutMode.EDITABLE,
        lang: str = "Vietnamese",
        timeout_sec: int = 120,
        fallback_to_cleanroom: bool = True,
    ) -> ConversionResult:
        """Attempt conversion via local ABBYY CLI flags; fall back cleanly if unattended CLI is unsupported."""
        in_path = Path(input_pdf).resolve()
        out_path = Path(output_docx).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        layout_mode = LayoutMode(mode) if isinstance(mode, str) else mode

        if self.is_available():
            cmd = [
                str(self.executable),
                str(in_path),
                "/NoSplash",
                "/lang",
                lang,
                "/send",
                "Word",
                "/out",
                str(out_path),
                "/quit",
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)
                if proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
                    tables_count, paragraphs_count, nfc_ok = _postprocess_docx(out_path, layout_mode)
                    return ConversionResult(
                        input_path=str(in_path),
                        output_path=str(out_path),
                        engine="abbyy",
                        mode=layout_mode.value,
                        pages_total=0,
                        digital_pages=0,
                        ocr_pages=0,
                        empty_pages=0,
                        tables_count=tables_count,
                        paragraphs_count=paragraphs_count,
                        nfc_verified=nfc_ok,
                    )
            except (subprocess.TimeoutExpired, OSError):
                pass

        if not fallback_to_cleanroom:
            raise RuntimeError(
                "ABBYY local CLI execution did not produce an output DOCX "
                "(likely restricted by FineReader|Feature|CommandLine or requires interactive GUI)."
            )

        return CleanRoomConverter().convert(in_path, out_path, mode=layout_mode, lang="vie")
