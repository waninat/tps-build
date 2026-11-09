"""Extração das tabelas de um relatório de dosimetria em PDF (formato RXT/OncoMed) para os CSV de dados de feixe.

Tabelas reconhecidas: PDD (SSD 100) e TMR por campo quadrado, Sc/Sp/Scp e razão off-axis diagonal (campo aberto).
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

NUM = re.compile(r"^-?\d+(,\d+)?$")


def f(s: str) -> float:
    return float(s.replace(",", "."))


def parse_grid_tables(lines: list[str], title_pat: str) -> list[tuple[list[str], list[list[float]]]]:
    """Tabelas profundidade × campo (PDD/TMR). Retorna [(campos, linhas)] por página."""
    out = []
    i = 0
    while i < len(lines):
        if re.search(title_pat, lines[i]):
            # cabeçalho de campos
            j = i
            while j < len(lines) and not re.search(r"\(cm\)\s+\d+x\d+", lines[j]):
                j += 1
            fields = re.findall(r"\d+x\d+", lines[j])
            rows = []
            k = j + 1
            while k < len(lines):
                tok = lines[k].split()
                if len(tok) >= len(fields) + 1 and all(NUM.match(t) for t in tok[: len(fields) + 1]):
                    rows.append([f(t) for t in tok[: len(fields) + 1]])
                elif rows and not lines[k].strip():
                    break
                elif rows:
                    break
                k += 1
            out.append((fields, rows))
            i = k
        else:
            i += 1
    return out


def pdf_text(pdf_path: str | Path) -> str:
    import pdfplumber
    with pdfplumber.open(str(pdf_path)) as pdf:
        return "\n".join(p.extract_text(layout=True) or "" for p in pdf.pages)


def extract_from_text(text: str, outdir: str | Path, maquina: str, energia: str, log=print) -> None:
    lines = text.splitlines()
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    # --- PDD e TMR campo aberto (exclui W60) ---
    for kind, pat, col in (("pdd", r"Percentual de Dose Profunda \(PDD\) - ELEKTA", "dose_rel"),
                           ("tmr", r"Razão Tecido Máximo \(TMR\) - ELEKTA", "tmr")):
        tables = parse_grid_tables(lines, pat)
        with (out / f"{kind}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            hdr = ["maquina", "energia", "campo_x_cm", "campo_y_cm"]
            hdr += (["ssd_cm", "profundidade_mm", col] if kind == "pdd" else ["profundidade_mm", col])
            hdr.append("detector")
            w.writerow(hdr)
            n = 0
            for fields, rows in tables:
                for r in rows:
                    depth_mm = r[0] * 10
                    for fs, v in zip(fields, r[1:]):
                        a, b = fs.split("x")
                        if kind == "pdd":
                            w.writerow([maquina, energia, a, b, 100, f"{depth_mm:g}", v, "relatorio_RXT_2019"])
                        else:
                            w.writerow([maquina, energia, a, b, f"{depth_mm:g}", v, "relatorio_RXT_2019"])
                        n += 1
            log(f"{kind}: {len(tables)} páginas, {n} pontos")

    # --- Sc, Sp, Scp ---
    with (out / "fatores_output.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["maquina", "energia", "campo_x_cm", "campo_y_cm", "ssd_cm", "profundidade_mm",
                    "fator_output", "sc", "sp", "detector", "correcao_aplicada"])
        n = 0
        for ln in lines:
            m = re.match(r"^\s*(\d+)x(\d+)\s+(\d,\d+)\s+(\d,\d+)\s+(\d,\d+)\s*$", ln)
            if m:
                a, b, sc, sp, scp = m.groups()
                # relatório não informa SSD/profundidade de medida: convenção dmax, SAD 100
                w.writerow([maquina, energia, a, b, "", "", f(scp), f(sc), f(sp), "relatorio_RXT_2019",
                            "Sp de BJR25"])
                n += 1
        log(f"fatores_output: {n} campos")

    # --- OAR diagonal campo aberto 40x40 ---
    with (out / "oar_diagonal.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["maquina", "energia", "campo_x_cm", "campo_y_cm", "ssd_cm", "profundidade_mm",
                    "eixo", "posicao_mm", "dose_rel", "detector"])
        start = next((i for i, l in enumerate(lines) if "OFF_AXIS* - CAMPO ABERTO" in l), None)
        if start is None:
            log("oar_diagonal: tabela não encontrada")
            return
        hdr = next(i for i in range(start, start + 10) if re.search(r"\(cm\)\s+1,5", lines[i]))
        depths = [f(t) for t in lines[hdr].split()[1:]]
        n = 0
        for ln in lines[hdr + 1: hdr + 30]:
            tok = ln.split()
            if len(tok) == len(depths) + 1 and all(NUM.match(t) for t in tok):
                for d, v in zip(depths, tok[1:]):
                    w.writerow([maquina, energia, 40, 40, 100, f"{d*10:g}", "diagonal",
                                f"{f(tok[0])*10:g}", f(v) * 100, "relatorio_RXT_2019"])
                    n += 1
        log(f"oar_diagonal: {n} pontos")



