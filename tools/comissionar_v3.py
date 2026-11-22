"""Comissionamento v3 (PDD + saídas medidos + forma lateral do Monaco) e recalibração absoluta.

    python tools/comissionar_v3.py dados/maquinas/radioserra_precise/6MV PASTA_MONACO [--alg CC] [--nfev 6] [--gravar]
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tps import paths  # noqa: E402
from tps.comissionamento_v2 import recalibrar  # noqa: E402
from tps.comissionamento_v3 import ReferenciaPerfis, refinar_v3  # noqa: E402
from tps.engine.beam_model import BeamModelParams  # noqa: E402


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("beamdir")
    ap.add_argument("monaco")
    ap.add_argument("--alg", default="CC")
    ap.add_argument("--nfev", type=int, default=6)
    ap.add_argument("--gravar", action="store_true")
    a = ap.parse_args()
    beamdir = Path(a.beamdir)
    arq = beamdir / "modelo" / "modelo_6MV.yaml"
    p = BeamModelParams.load(arq)
    ref = ReferenciaPerfis.de_pasta(Path(a.monaco), a.alg)
    print("campos de referência:", sorted(ref), flush=True)
    pf, log = refinar_v3(p, beamdir, paths.kernels_file(), ref, progress=lambda s: print(s, flush=True),
                         max_nfev=a.nfev)
    pf = recalibrar(pf, beamdir, float(p.ref.get("cGy_por_MU_dmax", 1.0)), paths.kernels_file())
    pf = pf.__class__(**{**pf.__dict__, "ref": {**pf.ref, "comissionamento": "v3"}})
    saida = beamdir / "modelo" / ("modelo_6MV.yaml" if a.gravar else "modelo_6MV_v3_candidato.yaml")
    if a.gravar:
        shutil.copy2(arq, beamdir / "modelo" / "modelo_6MV_pre_v3.yaml")
    pf.save(saida)
    (beamdir / "modelo" / "log_v3.json").write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")
    print("gravado em", saida)


if __name__ == "__main__":
    main()
