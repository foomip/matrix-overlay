"""Entry point: `python3 -m matrix_overlay` (invoked by ~/.local/bin/matrix-rain)."""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
