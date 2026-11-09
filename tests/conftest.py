import pytest

from tps import paths


@pytest.fixture(autouse=True)
def _pasta_de_dados_isolada(tmp_path, monkeypatch):
    """Cada teste usa uma pasta de dados vazia (nunca a da clínica)."""
    home = tmp_path / "tps_home"
    monkeypatch.setenv("TPS_HOME", str(home))
    paths.set_home(home)
    paths.ensure_home()
    yield home
    paths.set_home(None)
