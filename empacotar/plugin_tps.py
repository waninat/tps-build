"""Plugin do Nuitka para o TPS: o nosso código vira código de máquina; núcleos numba e terceiros, bytecode.

O numba precisa do bytecode Python da função para gerar o código de máquina dele, então os módulos com @njit ficam
em bytecode embutido no executável (sem arquivo .py no pacote).
"""
import sys

from nuitka.plugins.PluginBase import NuitkaPluginBase

NOSSOS = {"tps", "tps_app"}
NUMBA = {"tps.engine.ccc", "tps.compare.gamma", "tps.mc.transporte"}


class TPSPlugin(NuitkaPluginBase):
    plugin_name = "tps"
    plugin_desc = "Compila só o código do TPS; terceiros e núcleos numba em bytecode."

    def decideCompilation(self, module_name):
        nome = module_name.asString()
        if "-" in nome:
            return None
        if nome in NUMBA:
            return "bytecode"
        topo = nome.split(".")[0]
        if topo in NOSSOS:
            return "compiled"
        if topo in sys.stdlib_module_names or topo.startswith("_"):
            return None
        return "bytecode"
