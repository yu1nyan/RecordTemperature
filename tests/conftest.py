import os
import sys

os.environ["RECORDTEMP_NO_KEYRING"] = "1"   # never touch the real Credential Manager in tests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402


@pytest.fixture
def env(tmp_path):
    from recordtemp.config import Config
    from recordtemp.db import Database
    from recordtemp.scheduler import Recorder
    cfg = Config(tmp_path / "data")
    cfg.update({"capture_retries": 1})
    db = Database(cfg.db_path)
    yield cfg, db, Recorder(cfg, db)
    db.close()
