"""Command-line interface for PDFtoWord."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __copyright__, __version__, convert_pdf_to_docx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2word",
        description="Convert Vietnamese PDF documents to layout-preserved Word (.docx) files.",
        epilog=__copyright__,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"VN-Office PDF to Word Pro {__version__} — {__copyright__}",
    )
    parser.add_argument("input_pdf", type=Path, help="Path to the input PDF file.")
    parser.add_argument(
        "output_docx",
        type=Path,
        nargs="?",
        default=None,
        help="Path to the output .docx file (defaults to <input_stem>.docx).",
    )
    parser.add_argument(
        "--mode",
        choices=("editable", "formatted", "exact"),
        default="editable",
        help="Layout retention mode (default: editable).",
    )
    parser.add_argument(
        "--engine",
        choices=("cleanroom", "abbyy", "auto"),
        default="cleanroom",
        help="Conversion engine (default: cleanroom).",
    )
    parser.add_argument(
        "--lang",
        default="vie",
        help="Primary language (default: vie).",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="Rasterization DPI for scanned pages (default: 300).",
    )
    parser.add_argument(
        "--vietocr-model",
        choices=("vgg_transformer", "vgg_seq2seq"),
        default="vgg_transformer",
        help="VietOCR recognition architecture (default: vgg_transformer; use vgg_seq2seq for faster CPU).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print conversion summary as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    output_docx = args.output_docx or args.input_pdf.with_suffix(".docx")
    result = convert_pdf_to_docx(
        input_pdf=args.input_pdf,
        output_docx=output_docx,
        mode=args.mode,
        engine=args.engine,
        lang=args.lang,
        ocr_dpi=args.dpi,
        ocr_model=args.vietocr_model,
    )

    if args.json:
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    else:
        print(
            f"Converted '{result.input_path}' -> '{result.output_path}' "
            f"[engine={result.engine}, mode={result.mode}, pages={result.pages_total} "
            f"(digital={result.digital_pages}, ocr={result.ocr_pages}), "
            f"tables={result.tables_count}, paragraphs={result.paragraphs_count}, "
            f"nfc_verified={result.nfc_verified}]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
