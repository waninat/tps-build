"""Objetivos do plano modulado traduzidos para as funções de custo do Monaco. SRS-EXP-031.

O DICOM RT Plan não carrega objetivos de otimização (nenhum TPS importa os de outro), então o Monaco lê o plano só
com os feixes e segmentos. Para reotimizar lá, a lista abaixo diz qual função de custo do Monaco corresponde a cada
objetivo do TPS e com quais parâmetros — é a "ficha" que acompanha a exportação. Equivalências aproximadas: o
planejador confere no Monaco.
"""
from __future__ import annotations

import csv
from pathlib import Path


def traduzir(objetivos: list[dict]) -> list[dict]:
    out = []
    for o in objetivos or []:
        n, t = o.get("estrutura", ""), o.get("tipo", "")
        try:
            d = float(o.get("dose_gy"))
        except (TypeError, ValueError):
            continue
        v = o.get("volume_pct")
        v = None if v in (None, "") else float(v)
        tipo = "Restrição" if (o.get("restrito") or t == "alvo") else "Restrição (peso relativo)"
        if t == "alvo":
            out.append({"estrutura": n, "funcao": "Target Penalty", "parametros": f"Prescription {d:g} Gy; Minimum Volume 95%",
                        "papel": "Objetivo (alvo)", "origem": f"alvo {d:g} Gy"})
            out.append({"estrutura": n, "funcao": "Quadratic Overdose", "parametros": f"Maximum Dose {1.07 * d:.2f} Gy; RMS 0.5 Gy",
                        "papel": "Restrição", "origem": f"alvo {d:g} Gy (≤ 107%)"})
        elif t == "min":
            out.append({"estrutura": n, "funcao": "Target Penalty", "parametros": f"Prescription {d:g} Gy; Minimum Volume 98%",
                        "papel": "Objetivo", "origem": f"mínimo {d:g} Gy"})
        elif t == "max":
            out.append({"estrutura": n, "funcao": "Maximum Dose" if o.get("restrito") else "Quadratic Overdose",
                        "parametros": f"Maximum Dose {d:g} Gy" + ("" if o.get("restrito") else "; RMS 0.25 Gy"),
                        "papel": tipo, "origem": f"máximo {d:g} Gy"})
        elif t == "media":
            out.append({"estrutura": n, "funcao": "Serial", "parametros": f"Equivalent Uniform Dose {d:g} Gy; Power Law Exponent 1",
                        "papel": tipo, "origem": f"média {d:g} Gy (k = 1 equivale à média)"})
        elif t == "serial":
            k = v if v else 8
            out.append({"estrutura": n, "funcao": "Serial", "parametros": f"Equivalent Uniform Dose {d:g} Gy; Power Law Exponent {k:g}",
                        "papel": tipo, "origem": f"serial EUD {d:g} Gy, k {k:g}"})
        elif t == "dvh":
            vol = v if v is not None else 50
            out.append({"estrutura": n, "funcao": "Overdose DVH", "parametros": f"Dose {d:g} Gy; Maximum Volume {vol:g}%",
                        "papel": tipo, "origem": f"V{d:g}Gy ≤ {vol:g}%"})
        elif t == "dvh_min":
            vol = v if v is not None else 95
            out.append({"estrutura": n, "funcao": "Underdose DVH", "parametros": f"Dose {d:g} Gy; Minimum Volume {vol:g}%",
                        "papel": "Objetivo", "origem": f"V{d:g}Gy ≥ {vol:g}%"})
        elif t == "paralelo":
            vol = v if v is not None else 50
            out.append({"estrutura": n, "funcao": "Parallel",
                        "parametros": f"Reference Dose {d:g} Gy; Power Law Exponent 3; Mean Organ Damage {vol:g}%",
                        "papel": tipo, "origem": f"V{d:g}Gy ≤ {vol:g}%"})
    return out


def gravar(objetivos: list[dict], destino: str | Path, cabecalho: str = "") -> Path:
    """CSV (abre no Excel) com a correspondência; separador ';' e UTF-8 com BOM."""
    destino = Path(destino)
    linhas = traduzir(objetivos)
    with open(destino, "w", encoding="utf-8-sig", newline="") as f:
        if cabecalho:
            f.write(f"# {cabecalho}\n")
        f.write("# Objetivos do TPS para o Monaco — equivalência aproximada; conferir no Monaco antes de otimizar\n")
        w = csv.DictWriter(f, fieldnames=["estrutura", "funcao", "parametros", "papel", "origem"], delimiter=";")
        w.writeheader()
        w.writerows(linhas)
    return destino
