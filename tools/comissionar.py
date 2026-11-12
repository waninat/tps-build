"""Comissiona o modelo de feixe a partir de uma pasta de dados medidos (CSV) e grava modelo + validação.

Uso:
    python tools/comissionar.py dados/maquinas/oncomed_synergy_154828/6MV --energia 6MV --cgy-por-um 1.0

A calibração absoluta (--cgy-por-um, em dmax, 10×10, SSD 100) é obrigatória e tem de vir da dosimetria da clínica.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tps import commissioning as C  # noqa: E402
from tps.engine.beam_model import BeamModelParams  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pasta")
    ap.add_argument("--energia", default="6MV")
    ap.add_argument("--cgy-por-um", type=float, required=True)
    ap.add_argument("--mlc-transmissao", type=float, default=0.005)
    ap.add_argument("--jaw-transmissao", type=float, default=0.005)
    ap.add_argument("--v2", action="store_true", help="refina com o comissionamento v2 (PDD multi-campo + isocêntrico)")
    ap.add_argument("--refinar-de", help="modelo v1 já comissionado (pula o v1)")
    a = ap.parse_args()
    pasta = Path(a.pasta)
    out = pasta / "modelo"
    out.mkdir(exist_ok=True)
    t0 = time.time()

    def log(s):
        print(f"[{time.time() - t0:6.0f}s] {s}", flush=True)

    if a.refinar_de:
        p, info = BeamModelParams.load(a.refinar_de), {"v1": a.refinar_de}
    else:
        base = BeamModelParams(mlc_transmission=a.mlc_transmissao, jaw_transmission=a.jaw_transmissao)
        p, info = C.commission(pasta, a.energia, base, a.cgy_por_um, progress=log)
    if a.v2:
        from tps import comissionamento_v2 as V2
        from tps import paths
        p.save(out / f"modelo_{a.energia}_v1.yaml")
        p, info["v2"] = V2.refinar(p, pasta, paths.kernels_file(), progress=log)
        p = V2.recalibrar(p, pasta, a.cgy_por_um, paths.kernels_file())
    p.save(out / f"modelo_{a.energia}.yaml")
    (out / "comissionamento.json").write_text(json.dumps(info, indent=1, default=str), encoding="utf-8")
    v = C.validate(p, pasta, out, progress=log)
    (out / "validacao.json").write_text(json.dumps(v, indent=1, default=str), encoding="utf-8")
    log("fim")


if __name__ == "__main__":
    main()
