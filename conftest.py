"""
Pytest configuration ensuring project root is in sys.path.
"""

import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Unit tests never use live provider integrations, even when a developer's .env has keys.
os.environ.setdefault("MOCK_MODE", "true")
