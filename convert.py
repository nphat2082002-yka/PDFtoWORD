"""Single-command CLI wrapper: `python convert.py input.pdf output.docx`."""

from __future__ import annotations

import sys

from pdf2word.cli import main

if __name__ == "__main__":
    sys.exit(main())
