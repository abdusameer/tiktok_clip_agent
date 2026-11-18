import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def svc_root(tmp_path):
    (tmp_path / "sourcevideo" / "inbox").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    return tmp_path
