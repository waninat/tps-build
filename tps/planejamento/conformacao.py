"""Conformação de MLC e jaws ao alvo na visão do feixe (BEV). SRS-PLN-010..016.

Método: os centros dos voxels do alvo (mais os 8 cantos de cada voxel de borda, para não perder meio voxel) são
projetados no plano do isocentro pela geometria do feixe (tps.engine.geometry). Para cada par de lâminas, as
lâminas abrem até o menor/maior u dos pontos cuja coordenada v cai na faixa do par ampliada pela margem
(método "fora": a lâmina nunca corta o alvo + margem). Pares sem alvo ficam fechados com o gap mínimo da
máquina, escondidos atrás do jaw Y quando possível. Jaws: retângulo envolvente do alvo + margem.

Limitações (Fase 1): margem isotrópica no plano do isocentro; sem otimização da posição do colimador; sem
blocos/cunha (a cunha motorizada entra como requisito separado).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tps.engine.geometry import beam_frame
from tps.planejamento.modelo import Campo


@dataclass
class Colimacao:
    """Geometria de colimação de uma máquina (mm no isocentro)."""
    limites_laminas: np.ndarray          # LeafPositionBoundaries (n_pares + 1)
    tem_jaw_x: bool
    gap_minimo_mm: float = 0.0
    lamina_max_mm: float = 200.0         # posição extrema (|x|) de cada lâmina
    overtravel_mm: float = 150.0         # quanto a lâmina pode passar do eixo central
    jaw_y_max_mm: float = 200.0
    jaw_x_max_mm: float = 200.0
    tipo_mlc: str = "MLCX"
    interdigitacao: bool = True          # MLCi/MLCi2: False (lâmina A não passa a B dos pares vizinhos)
    extensao_banco_mm: float | None = None   # máx. diferença entre lâminas do mesmo banco (se houver)
    nome: str = ""
    largura_min_segmento_mm: float = 0.0     # IMRT: abertura mínima de um par aberto (Monaco: "min. segment width")

    @property
    def n_pares(self) -> int:
        return len(self.limites_laminas) - 1

    @classmethod
    def de_maquina(cls, raw: dict) -> "Colimacao":
        """A partir do YAML da máquina (tps.machine.model)."""
        col = raw["collimation"]
        mlc = col["mlc"]
        widths = mlc.get("widths") or [[mlc["leaf_pairs"], mlc["leaf_width_mm"]]]
        larg = np.concatenate([np.full(int(n), float(w)) for n, w in widths])
        lim = np.concatenate([[0.0], np.cumsum(larg)]) - larg.sum() / 2
        return cls(limites_laminas=lim, tem_jaw_x=bool(col.get("jaw_x", {}).get("present", False)),
                   gap_minimo_mm=float(mlc.get("min_gap_mm") or 0.0),
                   overtravel_mm=float(mlc.get("max_overtravel_mm") or 150.0), tipo_mlc=mlc.get("dicom_type", "MLCX"),
                   interdigitacao=bool(mlc.get("interdigitation", True)),
                   extensao_banco_mm=mlc.get("max_bank_span_mm"), nome=str(raw.get("id", "")),
                   largura_min_segmento_mm=float(mlc.get("min_segment_width_mm") or 0.0))

    @classmethod
    def agility(cls) -> "Colimacao":
        return cls(np.linspace(-200, 200, 81), tem_jaw_x=False, gap_minimo_mm=0.0, overtravel_mm=150.0,
                   interdigitacao=True, nome="Agility")

    @classmethod
    def mlci2(cls) -> "Colimacao":
        # Relatório de validação do Monaco (SynergyP06x, MLCi2): LeafGap 5 mm
        return cls(np.linspace(-200, 200, 41), tem_jaw_x=True, gap_minimo_mm=5.0, overtravel_mm=125.0,
                   interdigitacao=False, nome="MLCi2")

    @classmethod
    def mlci(cls) -> "Colimacao":
        # Elekta Precise: MLCi, 40 pares × 10 mm, diafragma X de reserva, sem interdigitação.
        # gap mínimo e overtravel a CONFIRMAR no comissionamento.
        return cls(np.linspace(-200, 200, 41), tem_jaw_x=True, gap_minimo_mm=5.0, overtravel_mm=125.0,
                   interdigitacao=False, nome="MLCi")


def pontos_do_alvo(mascara: np.ndarray, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Pontos [n, 3] (mm) que representam o alvo: centros dos voxels + cantos dos voxels de borda."""
    if not mascara.any():
        raise ValueError("Alvo vazio: a estrutura não tem voxels na grade")
    kz, ky, kx = np.nonzero(mascara)
    centros = np.column_stack([x[kx], y[ky], z[kz]])
    # voxels de borda: algum vizinho fora
    pad = np.pad(mascara, 1)
    interior = pad[1:-1, 1:-1, 1:-1].copy()
    for ax in range(3):
        for s in (-1, 1):
            interior &= np.roll(pad, s, axis=ax)[1:-1, 1:-1, 1:-1]
    borda = mascara & ~interior
    bz, by, bx = np.nonzero(borda)
    h = np.array([abs(x[1] - x[0]) if len(x) > 1 else 1.0, abs(y[1] - y[0]) if len(y) > 1 else 1.0,
                  abs(z[1] - z[0]) if len(z) > 1 else 1.0]) / 2
    cb = np.column_stack([x[bx], y[by], z[bz]])
    cantos = [cb + h * np.array([sx, sy, sz]) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    return np.vstack([centros, *cantos])


def projetar_bev(pontos: np.ndarray, campo: Campo, sad_mm: float = 1000.0,
                 posicao: str = "HFS") -> tuple[np.ndarray, np.ndarray]:
    """Coordenadas (u, v) no plano do isocentro (u = direção das lâminas, v = direção dos pares)."""
    fr = beam_frame(campo.gantry, campo.colimador, campo.mesa, campo.isocentro_mm, sad_mm, posicao)
    r = pontos - fr.source
    prof = r @ fr.w
    if np.any(prof <= 0):
        raise ValueError("Alvo atrás da fonte: verifique isocentro e ângulos")
    return sad_mm * (r @ fr.u) / prof, sad_mm * (r @ fr.v) / prof


def conformar(campo: Campo, mascara: np.ndarray, eixos: tuple, colim: Colimacao, margem_mm: float,
              sad_mm: float = 1000.0, posicao: str = "HFS", nome_alvo: str | None = None) -> Campo:
    """Define jaws e lâminas do campo conformados ao alvo + margem. Devolve o mesmo campo, alterado."""
    if margem_mm < 0:
        raise ValueError("Margem negativa")
    x, y, z = eixos
    u, v = projetar_bev(pontos_do_alvo(mascara, x, y, z), campo, sad_mm, posicao)
    lim = colim.limites_laminas
    vmin, vmax = v.min() - margem_mm, v.max() + margem_mm
    umin, umax = u.min() - margem_mm, u.max() + margem_mm
    if max(abs(vmin), abs(vmax)) > colim.jaw_y_max_mm or max(abs(umin), abs(umax)) > colim.lamina_max_mm:
        raise ValueError(f"Alvo + margem não cabe no campo máximo ({umin:.0f}..{umax:.0f} × {vmin:.0f}..{vmax:.0f} mm)")

    a = np.empty(colim.n_pares)
    b = np.empty(colim.n_pares)
    centro_u = 0.5 * (umin + umax)
    for i in range(colim.n_pares):
        sel = (v >= lim[i] - margem_mm) & (v <= lim[i + 1] + margem_mm)
        if sel.any():
            a[i], b[i] = u[sel].min() - margem_mm, u[sel].max() + margem_mm
            if b[i] - a[i] < colim.gap_minimo_mm:
                c = 0.5 * (a[i] + b[i])
                a[i], b[i] = c - colim.gap_minimo_mm / 2, c + colim.gap_minimo_mm / 2
        else:
            # par fechado: gap mínimo no centro do campo (fica sob o jaw Y se o par estiver fora dele)
            a[i], b[i] = centro_u - colim.gap_minimo_mm / 2, centro_u + colim.gap_minimo_mm / 2
    # limites mecânicos de overtravel: a lâmina A não passa de +overtravel, nem a B de −overtravel
    if np.any(a > colim.overtravel_mm) or np.any(b < -colim.overtravel_mm):
        raise ValueError("Posição de lâmina além do overtravel permitido")
    campo.jaw_y = (round(float(vmin), 1), round(float(vmax), 1))
    campo.jaw_x = (round(float(umin), 1), round(float(umax), 1)) if colim.tem_jaw_x else None
    campo.mlc_a = [round(float(t), 1) for t in a]
    campo.mlc_b = [round(float(t), 1) for t in b]
    campo.alvo_conformado, campo.margem_mm = nome_alvo, float(margem_mm)
    return campo


def area_aberta_cm2(campo: Campo, colim: Colimacao) -> float:
    """Área (cm²) da abertura MLC ∩ jaws no isocentro — usada em checagens e no relatório."""
    lim = colim.limites_laminas
    jy0, jy1 = campo.jaw_y
    jx0, jx1 = campo.jaw_x if campo.jaw_x else (-1e9, 1e9)
    area = 0.0
    for i in range(colim.n_pares):
        h = max(0.0, min(lim[i + 1], jy1) - max(lim[i], jy0))
        w = max(0.0, min(campo.mlc_b[i], jx1) - max(campo.mlc_a[i], jx0))
        area += h * w
    return area / 100.0
