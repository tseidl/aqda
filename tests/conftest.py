import pytest

import aqda.db as db_module


@pytest.fixture
def use_data_dir(monkeypatch):
    """Point every AQDA connection at a caller-provided temporary directory."""
    def activate(path):
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(db_module, "DATA_DIR", path)
        return path

    return activate
