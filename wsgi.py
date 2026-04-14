import os
import sys

ROOT_DIR = os.path.dirname(__file__)
APP_DIR = os.path.join(ROOT_DIR, "punit 2")

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from app import app  # noqa: E402

