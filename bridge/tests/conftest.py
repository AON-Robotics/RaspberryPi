"""Make server/ and agent/ importable the way uvicorn and agent.py see them."""

import os
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE / "server"))
sys.path.insert(0, str(BRIDGE / "agent"))

# loop.py and server.py refuse to import without a token.
os.environ.setdefault("BRIDGE_TOKEN", "test-token")
