"""Todo módulo do TPS com @njit precisa ir em bytecode no programa compilado (o numba lê o bytecode da função) e ter
o marcador do cache: senão o cálculo falha só no Windows instalado (aconteceu com o Monte Carlo na 0.9.5)."""
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def test_modulos_numba_listados_no_empacotamento():
    com_njit = {str(p.relative_to(RAIZ).with_suffix("")).replace("/", ".").replace("\\", ".")
                for p in (RAIZ / "tps").rglob("*.py") if re.search(r"@(njit|jit)\b", p.read_text(encoding="utf-8"))}
    plugin = (RAIZ / "empacotar" / "plugin_tps.py").read_text(encoding="utf-8")
    compilar = (RAIZ / "empacotar" / "compilar.py").read_text(encoding="utf-8")
    no_plugin = set(re.findall(r'"(tps\.[\w.]+)"', re.search(r"NUMBA = \{([^}]*)\}", plugin).group(1)))
    no_compilar = set(re.findall(r'"(tps\.[\w.]+)"', re.search(r"NUMBA = \[([^\]]*)\]", compilar).group(1)))
    assert com_njit <= no_plugin, com_njit - no_plugin
    assert com_njit <= no_compilar, com_njit - no_compilar


def test_vigia_da_inicializacao_e_desligado():
    """O vigia de 30 s (pilhas em inicio.log) só pode rodar durante a abertura: despejar a pilha de linhas que estão
    trabalhando derrubava o TPS (access violation em falhas.log)."""
    from pathlib import Path
    src = (Path(__file__).parents[1] / "tps_app.py").read_text(encoding="utf-8")
    assert "dump_traceback_later(30, repeat=True" in src
    assert src.count("_fim_do_vigia_de_inicio()") >= 2            # janela própria e navegador
    assert "faulthandler.cancel_dump_traceback_later()" in src


def test_json_sem_infinito():
    from tps.app.servidor import _JSONSeguro
    assert _JSONSeguro({"a": float("inf"), "b": [float("nan"), 1.5]}).body == b'{"a":null,"b":[null,1.5]}'
