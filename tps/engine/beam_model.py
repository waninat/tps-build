"""Modelo de fonte e fluência no plano do isocentro. SRS-ENG-020..024.

Fluência de energia de um segmento:
    Ψ(u,v) = H(r) · [ (1-w_ef)·(O ⊗ G_σs) + w_ef·(O ⊗ G_σef) ]
onde O é a abertura (MLC ∩ jaws, com transmissão), G_σs o borramento da fonte
primária (tamanho focal + penumbra geométrica), G_σef a fonte extrafocal
(espalhamento no cabeçote) projetada no isocentro e H(r) o perfil radial
(“horns” do filtro aplanador) ajustado no comissionamento.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import yaml
from scipy.ndimage import gaussian_filter


@dataclass
class BeamModelParams:
    energy_mv: float = 6.0
    spectrum_hardening_k: float = 0.0       # ver physics/spectrum.py
    sigma_src_mm: float = 1.5               # borramento primário no isocentro
    sigma_src_x_mm: float | None = None     # anisotropia: X = direção das lâminas (ponta arredondada), Y = jaws
    sigma_src_y_mm: float | None = None
    w_extrafocal: float = 0.06
    sigma_extrafocal_mm: float = 25.0
    mlc_transmission: float = 0.005
    jaw_transmission: float = 0.001
    leaf_tip_offset_mm: float = 0.0         # >0 abre o campo efetivo (ponta arredondada)
    interleaf_factor: float = 1.0           # transmissão na faixa entre lâminas = fator × mlc_transmission
    groove_width_mm: float = 0.0            # largura da faixa tongue-and-groove (no isocentro)
    horn: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # H(r)=1+c1 r+c2 r²+c3 r³ (r em cm)
    e_contam_amp: float = 0.0               # contaminação eletrônica (fração da dose em dmax)
    e_contam_k_cm: float = 2.0
    e_contam_ref_raw: float = 0.0           # dose bruta em dmax (10×10, 1 MU) usada como escala da contaminação
    head_leakage: float = 0.0               # vazamento do cabeçote (fração da fluência aberta, uniforme)
    escala_espalhamento: float = 1.0        # kernel = primária + s·(total − primária)  (comissionamento v2)
    e_contam_sigma_mm: float = 0.0          # >0: contaminação ∝ fluência borrada (cresce com o campo)
    norm_dose_per_mu: float = 1.0           # fator absoluto (Gy/MU por unidade de cálculo)
    ref: dict = field(default_factory=dict)  # condições de calibração
    # Origem do modelo. Só um modelo comissionado (ajustado aos dados medidos, com calibração absoluta) calcula
    # planos reais. O modelo de demonstração (parâmetros padrão) só serve a fantomas e dados fictícios.
    comissionado: bool = False
    demonstracao: bool = False

    def save(self, path: str | Path) -> None:
        def plain(v):
            if isinstance(v, dict):
                return {k: plain(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [plain(x) for x in v]
            if isinstance(v, np.generic):
                return v.item()
            return v
        Path(path).write_text(yaml.safe_dump(plain(asdict(self)), sort_keys=False, allow_unicode=True),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "BeamModelParams":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if "comissionado" not in raw:
            # modelos gravados até a versão 0.6 não têm o campo: comissionado é o que tem calibração absoluta
            raw["comissionado"] = bool((raw.get("ref") or {}).get("cGy_por_MU_dmax")) or \
                float(raw.get("norm_dose_per_mu", 1.0)) != 1.0
        return cls(**raw)


class ModeloNaoComissionado(ValueError):
    """O modelo de feixe ativo não foi comissionado: calcular com ele daria dose sem calibração absoluta."""


@dataclass
class FluenceGrid:
    u0: float = -200.0      # mm
    v0: float = -200.0
    du: float = 1.0
    dv: float = 1.0
    nu: int = 401
    nv: int = 401

    @property
    def u(self):
        return self.u0 + np.arange(self.nu) * self.du

    @property
    def v(self):
        return self.v0 + np.arange(self.nv) * self.dv


def aperture(grid: FluenceGrid, jaw_x, jaw_y, mlc_a, mlc_b, leaf_boundaries,
             p: BeamModelParams, oversample: int = 4) -> np.ndarray:
    """Transmissão da abertura [nv, nu] com média de sub-amostras por pixel."""
    us = grid.u0 + (np.arange(grid.nu * oversample) + 0.5) * grid.du / oversample - grid.du / 2
    vs = grid.v0 + (np.arange(grid.nv * oversample) + 0.5) * grid.dv / oversample - grid.dv / 2
    U, V = np.meshgrid(us, vs)
    off = p.leaf_tip_offset_mm
    open_mlc = np.zeros(U.shape, bool)
    if mlc_a is not None and leaf_boundaries is not None:
        idx = np.searchsorted(leaf_boundaries, vs, side="right") - 1
        valid = (idx >= 0) & (idx < len(mlc_a))
        a = np.where(valid, np.asarray(mlc_a)[np.clip(idx, 0, len(mlc_a) - 1)] - off, 0.0)
        b = np.where(valid, np.asarray(mlc_b)[np.clip(idx, 0, len(mlc_b) - 1)] + off, 0.0)
        open_mlc = (U > a[:, None]) & (U < b[:, None]) & valid[:, None]
        t_row = np.where(valid, p.mlc_transmission, 1.0)          # fora do banco: sem MLC
        if p.groove_width_mm > 0 and p.interleaf_factor != 1.0:
            j = np.clip(np.searchsorted(leaf_boundaries, vs), 1, len(leaf_boundaries) - 1)
            dist = np.minimum(np.abs(vs - leaf_boundaries[j - 1]), np.abs(vs - leaf_boundaries[j]))
            band = valid & (dist <= p.groove_width_mm / 2)
            t_row = np.where(band, p.mlc_transmission * p.interleaf_factor, t_row)
        t_mlc = t_row[:, None]
    else:
        open_mlc[:] = True
        t_mlc = 1.0
    T = np.where(open_mlc, 1.0, t_mlc)
    # transmissões em série: MLC × jaws
    if jaw_y is not None:
        T = T * np.where((V > jaw_y[0]) & (V < jaw_y[1]), 1.0, p.jaw_transmission)
    if jaw_x is not None:
        T = T * np.where((U > jaw_x[0]) & (U < jaw_x[1]), 1.0, p.jaw_transmission)
    return T.reshape(grid.nv, oversample, grid.nu, oversample).mean(axis=(1, 3))


def aperture_rapida(grid: FluenceGrid, jaw_x, jaw_y, mlc_a, mlc_b, leaf_boundaries,
                    p: BeamModelParams, oversample: int = 4) -> np.ndarray:
    """Mesma transmissão de `aperture`, com a cobertura exata em u (núcleo numba): ~30× mais rápida. Usada na
    entrega reconstruída do log, que tem milhares de aberturas (entregue e planejada passam pela mesma função)."""
    from tps.engine.ccc import abertura_analitica
    vs = grid.v0 + (np.arange(grid.nv * oversample) + 0.5) * grid.dv / oversample - grid.dv / 2
    us = grid.u
    off = p.leaf_tip_offset_mm
    if mlc_a is not None and leaf_boundaries is not None:
        idx = np.searchsorted(leaf_boundaries, vs, side="right") - 1
        valid = (idx >= 0) & (idx < len(mlc_a))
        ci = np.clip(idx, 0, len(mlc_a) - 1)
        a = np.where(valid, np.asarray(mlc_a, float)[ci] - off, 0.0)
        b = np.where(valid, np.asarray(mlc_b, float)[ci] + off, 0.0)
        t_row = np.where(valid, p.mlc_transmission, 1.0)
        if p.groove_width_mm > 0 and p.interleaf_factor != 1.0:
            j = np.clip(np.searchsorted(leaf_boundaries, vs), 1, len(leaf_boundaries) - 1)
            dist = np.minimum(np.abs(vs - leaf_boundaries[j - 1]), np.abs(vs - leaf_boundaries[j]))
            t_row = np.where(valid & (dist <= p.groove_width_mm / 2), p.mlc_transmission * p.interleaf_factor, t_row)
    else:
        valid = np.zeros(len(vs), bool)
        a = b = t_row = np.zeros(len(vs))
    out = np.empty((len(vs), len(us)))
    jy = jaw_y if jaw_y is not None else (0.0, 0.0)
    jx = jaw_x if jaw_x is not None else (0.0, 0.0)
    abertura_analitica(np.ascontiguousarray(us, float), float(grid.du), vs, valid, np.ascontiguousarray(a, float),
                       np.ascontiguousarray(b, float), np.ascontiguousarray(t_row, float), float(jy[0]), float(jy[1]),
                       jaw_y is not None, float(jx[0]), float(jx[1]), jaw_x is not None, float(p.jaw_transmission), out)
    return out.reshape(grid.nv, oversample, grid.nu).mean(axis=1)


def energy_fluence(apert: np.ndarray, grid: FluenceGrid, p: BeamModelParams) -> np.ndarray:
    sx = (p.sigma_src_x_mm if p.sigma_src_x_mm is not None else p.sigma_src_mm) / grid.du
    sy = (p.sigma_src_y_mm if p.sigma_src_y_mm is not None else p.sigma_src_mm) / grid.dv
    s2 = p.sigma_extrafocal_mm / grid.du
    prim = gaussian_filter(apert, (sy, sx), mode="constant")      # apert [nv, nu]: v = Y, u = X
    ef = gaussian_filter(apert, s2, mode="constant")
    psi = (1 - p.w_extrafocal) * prim + p.w_extrafocal * ef
    U, V = np.meshgrid(grid.u, grid.v)
    r = np.hypot(U, V) / 10.0
    c1, c2, c3 = (list(p.horn) + [0, 0, 0])[:3]
    return psi * (1 + c1 * r + c2 * r ** 2 + c3 * r ** 3) + p.head_leakage
