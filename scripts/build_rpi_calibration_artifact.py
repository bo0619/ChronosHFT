"""Compatibility CLI for the public calibration governance implementation."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from governance import calibration_artifact as _implementation  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

# Preserve the historical module identity for external offline callers while
# keeping the implementation under the neutral governance boundary. Replacing
# the module object also preserves test/tool monkeypatch behavior.
sys.modules[__name__] = _implementation
