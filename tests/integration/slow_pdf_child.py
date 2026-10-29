"""A test seam: the real PDF extraction child, started late.

`extraction.CHILD_SCRIPT` is pointed here by `test_upload_concurrency.py`, so a
parse takes real wall-clock time inside a real child process without any sleep
in production code. `WASLA_TEST_PDF_DELAY` seconds, then the real child runs
with the same arguments and the same stdin.
"""

from __future__ import annotations

import os
import runpy
import sys
import time
from pathlib import Path

time.sleep(float(os.environ.get("WASLA_TEST_PDF_DELAY", "6")))
child = Path(__file__).resolve().parents[2] / "app" / "services" / "pdf_extract_child.py"
sys.argv[0] = str(child)
runpy.run_path(str(child), run_name="__main__")
