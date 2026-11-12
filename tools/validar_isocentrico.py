"""Validação do modelo de feixe em geometria isocêntrica (SAD), que o comissionamento atual não ajusta.

    python tools/validar_isocentrico.py dados/maquinas/oncomed_synergy_154828/6MV

1. Dose por UM no isocentro (SSD = 100 − d) × cálculo por tabela: calibração × (101,5/100)² × Scp × TMR.
2. Scp a SSD 90 / 10 cm × medida, se existir `scp_ssd90_d10*.csv` na pasta.
Grava `modelo/validacao_isocentrica.csv`.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from tps import paths  # noqa: E402
from tps.engine.beam_model import BeamModelParams  # noqa: E402
from tps.engine.calc import compute_dose  # noqa: E402
from tps.engine.phantom import cax_profile, square_field, water_box  # noqa: E402
from tps.physics.kernels import cones_do_modelo, load_mono  # noqa: E402
from tps.physics.spectrum import Spectrum  # noqa: E402


def dose_eixo(m, cones, fs, ssd_mm, prof_mm):
    vox = 1.0 if fs <= 3 else 2.5
    g = water_box(max(160.0 if fs <= 3 else 200.0, fs * 12.5 + 60), 300 + vox, vox)
    d = compute_dose(g, [square_field(fs, fs, ssd_mm=ssd_mm, mu=1.0)], m, cones)
    y, p = cax_profile(d, g)
    return float(np.interp(prof_mm, y, p)) * 100.0       # cGy/UM


def main():
    pasta = Path(sys.argv[1])
    m = BeamModelParams.load(pasta / "modelo" / "modelo_6MV.yaml")
    cgy = float(m.ref.get("cGy_por_MU_dmax", 1.0))
    dmax = float(m.ref.get("prof_calibracao_dmax_mm", 15.0))
    cones = cones_do_modelo(load_mono(paths.kernels_file()), m)
    tmr = {(float(r["campo_x_cm"]), float(r["profundidade_mm"])): float(r["tmr"])
           for r in csv.DictReader(open(pasta / "tmr.csv", encoding="utf-8")) if r["campo_x_cm"] == r["campo_y_cm"]}
    scp = {float(r["campo_x_cm"]): float(r["fator_output"])
           for r in csv.DictReader(open(pasta / "fatores_output.csv", encoding="utf-8"))}
    linhas = []
    print("1) Isocêntrico × tabela (TMR, Scp)\ncampo prof   modelo   tabela   dif%")
    for fs in (5.0, 10.0, 20.0):
        for dep in (50.0, 100.0, 200.0):
            calc = dose_eixo(m, cones, fs, 1000.0 - dep, dep)
            ref = cgy * ((1000.0 + dmax) / 1000.0) ** 2 * scp[fs] * tmr[(fs, dep)]
            dif = 100 * (calc / ref - 1)
            print(f"{fs:4.0f} {dep / 10:4.0f}  {calc:7.4f}  {ref:7.4f}  {dif:+5.2f}")
            linhas.append(["sad_tabela", fs, 1000 - dep, dep, calc, ref, dif])
    for f in sorted(pasta.glob("scp_ssd90_d10*.csv")):
        med = {float(r["campo_cm"]): float(r["scp"]) for r in csv.DictReader(open(f, encoding="utf-8"))}
        calc = {fs: dose_eixo(m, cones, fs, 900.0, 100.0) for fs in med}
        print(f"2) Scp SSD 90 / 10 cm × {f.name}\ncampo  medido  modelo   dif%")
        for fs in med:
            r = calc[fs] / calc[10.0]
            dif = 100 * (r / med[fs] - 1)
            print(f"{fs:4.0f}   {med[fs]:.3f}   {r:.3f}  {dif:+5.2f}")
            linhas.append(["scp_ssd90_d10", fs, 900, 100, r, med[fs], dif])
    with open(pasta / "modelo" / "validacao_isocentrica.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["teste", "campo_cm", "ssd_mm", "prof_mm", "modelo", "referencia", "dif_pct"])
        w.writerows(linhas)


if __name__ == "__main__":
    main()
