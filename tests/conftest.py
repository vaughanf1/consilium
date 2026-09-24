import os
import pytest

os.environ["CONSILIUM_NO_LLM"] = "1"
os.environ["CONSILIUM_NO_BLOB"] = "1"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.consilium so the ledger and caches never leak."""
    monkeypatch.setenv("CONSILIUM_HOME", str(tmp_path / "home"))
    import importlib
    import consilium.core.paths as paths
    importlib.reload(paths)
    yield
