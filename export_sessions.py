import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

main = importlib.import_module("ai_session_export.cli").main


if __name__ == "__main__":
    main()
