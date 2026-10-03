import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sim"))
sys.path.insert(0, str(ROOT / "bridge" / "agent"))
os.environ.setdefault("BRIDGE_TOKEN", "pipeline-test-token-0123456789abcdefghij")
