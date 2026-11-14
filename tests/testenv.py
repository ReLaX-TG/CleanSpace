"""Shared test environment: a temporary instance folder (DB, secret key, admin hash).

Imported first by every test module, before `config`/`app`, so tests never touch
the real ./instance folder.
"""

import atexit
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_tmpdir = tempfile.TemporaryDirectory(prefix="cleanspace-test-")
INSTANCE = Path(_tmpdir.name)
TEST_DB = INSTANCE / "test.db"

os.environ["CLEANSPACE_INSTANCE"] = str(INSTANCE)
os.environ["CLEANSPACE_DB"] = str(TEST_DB)

import config  # noqa: E402

assert config.DATABASE_PATH == TEST_DB, "tests must not touch the real database"
assert config.INSTANCE_DIR == INSTANCE, "tests must not touch the real instance folder"


atexit.register(_tmpdir.cleanup)  # after ALL test modules have finished
