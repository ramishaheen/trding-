"""Make the dependency-free logic modules importable in tests without installing
freqtrade or the full trading stack."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "user_data", "strategies"))
sys.path.insert(0, os.path.join(ROOT, "risk"))
sys.path.insert(0, os.path.join(ROOT, "research"))
sys.path.insert(0, os.path.join(ROOT, "execution"))
