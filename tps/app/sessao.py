"""Estado de um caso aberto na interface: imagens, estruturas, máquina, plano, dose. SRS-UI-001..040.

Toda a física fica nos módulos de planejamento; aqui só se orquestra e se prepara o que a tela desenha.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import io
import json
import shutil
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pydicom
import yaml
from contourpy import contour_generator
from PIL import Image

from tps import paths
from tps.dicom_io.rtstruct import dvh, rasterize, read_rtstruct
from tps.engine.beam_model import BeamModelParams
from tps.engine.geometry import beam_frame
from tps.exportacao.rtdose import exportar_rtdose
from tps.exportacao.rtplan import exportar_rtplan, referencia_do_ct
from tps.exportacao.validacao_iod import conferir_rtplan
from tps.imaging.ct import CTVolume, read_ct_series
from tps.imaging.hu_ed import HUtoED
from tps.planejamento import calculo
from tps.planejamento.ajuste_mlc import (Margem, OpcoesAjuste, ajustar, bev_da_estrutura, grade_bev,
                                        otimizar_colimador, verificar_mecanica)
from tps.planejamento.compensador import compensar
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo, Plano, Prescricao
from tps.planejamento.otimizacao3d import field_in_field, indice_homogeneidade, otimizar_pesos
from tps.planejamento.plano_2d import CampoRetangular, FantomaVirtual, campo_de_retangulo
from tps.planejamento.um_tabela import TabelasFeixe, conferir_plano

RAIZ = Path(__file__).resolve().parents[2]
PALETA = [(230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200), (245, 130, 48), (145, 30, 180),
          (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 190), (0, 128, 128), (170, 110, 40)]
NIVEIS_ISODOSE = [30, 50, 70, 80, 90, 95, 100, 105, 107, 110]


def _resumir_entrega(itens: list[dict]) -> list[dict]:
    """Agrupa os pontos da conferência de entregabilidade por feixe e mensagem (sem repetir cada ponto de controle)."""
    import re as _re
    grupos: dict = {}
    for i in itens:
        chave = (i["gravidade"], i["feixe"], _re.sub(r"[\d.]+", "#", i["msg"]))
        g = grupos.setdefault(chave, {"gravidade": i["gravidade"], "feixe": i["feixe"], "msg": i["msg"], "n": 0})
        g["n"] += 1
    return sorted(grupos.values(), key=lambda g: (g["gravidade"] != "erro", g["feixe"]))


def mover_para_lixeira(arq: Path) -> Path | None:
    """Move um plano salvo para <home>/planos/lixeira (com data/hora no nome): apagado, mas recuperável."""
    arq = Path(arq)
    if not arq.exists():
        return None
    lixo = paths.home() / "planos" / "lixeira"
    lixo.mkdir(parents=True, exist_ok=True)
    destino = lixo / f"{arq.stem}_{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    shutil.move(str(arq), str(destino))
    return destino


class ErroUsuario(ValueError):
    """Erro que a tela mostra ao usuário como está."""


def maquinas_disponiveis() -> list[dict]:
    out = []
    for f in sorted((RAIZ / "dados/maquinas").glob("*/maquina.yaml")):
        raw = yaml.safe_load(f.read_text(encoding="utf-8"))
        energias = [d.name for d in f.parent.iterdir() if (d / "modelo/modelo_6MV.yaml").exists() or
                    any(d.glob("modelo/modelo_*.yaml"))]
        out.append({"id": raw["id"], "nome": f"{raw.get('vendor', '')} {raw.get('model', '')} — {raw['id']}",
                    "energias": sorted(energias), "mlc": raw["collimation"]["mlc"].get("preset", "")})
    return out


@dataclass
class Maquina:
    id: str
    raw: dict
    colim: Colimacao
    modelo: BeamModelParams
    tabelas: TabelasFeixe | None
    nome_dicom: str
    energia: str

    @classmethod
    def carregar(cls, maquina_id: str, energia: str = "6MV") -> "Maquina":
        pasta = RAIZ / "dados/maquinas" / maquina_id
        raw = yaml.safe_load((pasta / "maquina.yaml").read_text(encoding="utf-8"))
        colim = Colimacao.de_maquina(raw)
        modelo = BeamModelParams.load(pasta / energia / "modelo" / f"modelo_{energia}.yaml")
        cgy = float(modelo.ref.get("cGy_por_MU_dmax", 1.0)) if modelo.ref else 1.0
        dmax = float(modelo.ref.get("prof_calibracao_dmax_mm", 15.0)) if modelo.ref else 15.0
        try:
            tab = TabelasFeixe.ler(pasta / energia, cgy, dmax)
        except FileNotFoundError:
            tab = None
        nomes = [n for n in raw.get("dicom_machine_names", []) if n and n != "CONFIRMAR"]
        return cls(maquina_id, raw, colim, modelo, tab, nomes[0] if nomes else maquina_id.upper()[:16], energia)


@dataclass
class Estrutura:
    nome: str
    tipo: str
    cor: tuple
    mascara: np.ndarray                      # na grade de dose
    contornos: list = field(default_factory=list)   # polígonos do RTSTRUCT [n, 3] (para o corte axial exato)


class Sessao:
    @property
    def progresso(self) -> str | None:
        return self._progresso

    @progresso.setter
    def progresso(self, msg: str | None) -> None:
        if msg and msg != self._progresso:                # cada etapa vai para tps_saida.log, com a memória
            from tps.diagnostico import registrar
            registrar(f"[{self.nome or self.id}] {msg}")
        self._progresso = msg

    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.nome = ""
        self.tipo = ""                        # ct | fantoma
        self.ct: CTVolume | None = None
        self.derivadas: dict = {}
        self.editadas: set = set()               # estruturas com contorno criado/alterado no TPS
        self.desfazer_pilha: list = []           # (nome, contornos anteriores) para desfazer
        self.planos_dicom: list = []
        self.dose_ref: dict | None = None        # dose de outro TPS (RTDOSE importado), na grade de dose
        self.pasta_ct: Path | None = None
        self.arquivo_rs: Path | None = None
        self.curva: HUtoED | None = None
        self.grade: calculo.GradeDose | None = None
        self.estruturas: dict[str, Estrutura] = {}
        self.maquina: Maquina | None = None
        self.plano: Plano | None = None
        self.paciente = {"id": "", "nome": ""}
        self.isocentro: tuple | None = None
        self.doses_um: dict = {}
        self.dose_fx: np.ndarray | None = None
        self.dose_vista: dict = {}                       # "soma" / "dif": doses só para ver (não são planos)
        self._progresso: str | None = None                # etapa de um cálculo longo (IMRT), lida sem trava
        self.resultado: dict = {}
        self.dose_atualizada = False
        self.avisos: list[str] = []
        self.outros_planos: dict = {}            # rótulo → estado guardado (vários planos no mesmo CT)
        self.log: list[str] = []

    # ================================================================== carregamento
    def carregar_ct(self, pasta: str | Path, curva: str = "siemens_spirit_oncomed", voxel_mm: float = 3.0,
                    sem_rtstruct: bool = False):
        pasta = Path(pasta)
        if not pasta.is_dir():
            raise ErroUsuario(f"Pasta não encontrada: {pasta}")
        try:
            self.ct = read_ct_series(pasta)
        except (ValueError, NotImplementedError) as e:
            raise ErroUsuario(str(e)) from e
        if (self.ct.patient_position or "HFS") != "HFS":
            raise ErroUsuario(f"Posição do paciente {self.ct.patient_position}: esta versão só aceita HFS")
        rs, cands = None, []
        for f in sorted(p for p in pasta.iterdir() if p.is_file()):
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
            except Exception:
                continue
            if getattr(ds, "Modality", "") == "RTSTRUCT":
                ref = getattr(ds, "ReferencedFrameOfReferenceSequence", None)
                fr = str(ref[0].get("FrameOfReferenceUID", "")) if ref else ""
                if fr not in ("", self.ct.frame_of_reference):
                    continue                              # de outro referencial (ex.: outra TC na pasta)
                aprovado = str(getattr(ds, "ApprovalStatus", "") or "").upper() == "APPROVED"
                cands.append((aprovado, f.stat().st_mtime, f, ds))
        if cands:                     # vários RTSTRUCT (automático, do médico…): o aprovado mais recente, senão o último
            _, _, rs, ds = max(cands, key=lambda t: (t[0], t[1]))
            self.paciente = {"id": str(getattr(ds, "PatientID", "")), "nome": str(getattr(ds, "PatientName", ""))}
        elif sem_rtstruct:
            ds0 = pydicom.dcmread(str(next(f for f in sorted(pasta.iterdir()) if f.is_file() and self._e_ct(f))),
                                  stop_before_pixels=True, force=True)
            self.paciente = {"id": str(getattr(ds0, "PatientID", "")), "nome": str(getattr(ds0, "PatientName", ""))}
        else:
            raise ErroUsuario("Nenhum RTSTRUCT na pasta: exporte o CT junto com os contornos")
        self.tipo, self.pasta_ct, self.arquivo_rs = "ct", pasta, rs
        self.secundaria = None                         # fusão: TC secundária (outro caso não herda)
        self.curva = HUtoED.from_csv(curva)
        estruturas_rs = read_rtstruct(rs) if rs is not None else []
        if rs is None:                                    # TC sem contornos: BODY automático define a grade
            from tps.planejamento import contorno as K
            corpo = K.corpo_3d(self.ct.hu)
            if corpo.any():
                cx, cy, cz = self.ct.axes()
                jj, ii = np.nonzero(corpo.any(axis=0))
                m = 30.0
                self._caixa_sem_rs = ((max(cx[0], cx[ii.min()] - m), min(cx[-1], cx[ii.max()] + m)),
                                      (max(cy[0], cy[jj.min()] - m), min(cy[-1], cy[jj.max()] + m)), (cz[0], cz[-1]))
        # grade de dose só em volta do contorno externo (+3 cm para bolus e penumbra): o CT costuma ter 50 cm de
        # campo de visão e a mesa; recortar deixa o cálculo 2–3× mais rápido sem mudar a dose no paciente
        caixa = None
        ext = next((e for e in estruturas_rs if (e.rt_type or "").upper() == "EXTERNAL" and e.contours), None)
        if ext is not None:
            pts = np.concatenate([np.asarray(c, float) for c in ext.contours])
            cx, cy, cz = self.ct.axes()
            m = 30.0
            caixa = ((max(cx[0], pts[:, 0].min() - m), min(cx[-1], pts[:, 0].max() + m)),
                     (max(cy[0], pts[:, 1].min() - m), min(cy[-1], pts[:, 1].max() + m)),
                     (cz[0], cz[-1]))
        if rs is None:
            caixa = getattr(self, "_caixa_sem_rs", None)
        self.grade = calculo.grade_de_dose(self.ct, self.curva, voxel_mm, caixa)
        g = self.grade
        self.estruturas = {}
        self.derivadas = {}
        for k, s in enumerate(estruturas_rs):
            m = rasterize(s, g.x, g.y, g.z)
            cor = tuple(int(c) for c in s.color) if s.color else PALETA[k % len(PALETA)]
            self.estruturas[s.name] = Estrutura(s.name, s.rt_type or "", cor, m, s.contours)
        self.nome = self.paciente["nome"] or pasta.name
        self.eta_ct = self.grade.eta.copy()
        self._mascarar_fora_do_corpo()
        from tps.importacao.rtplan import listar_planos
        try:
            self.planos_dicom = [q for q in listar_planos(pasta)
                                 if q["frame_of_reference"] in ("", self.ct.frame_of_reference)]
        except Exception:  # noqa: BLE001
            self.planos_dicom = []
        if rs is None:
            self.corpo_automatico("BODY")
        self._registrar(f"CT carregado: {pasta} ({self.ct.hu.shape[0]} cortes), "
                        f"{'RTSTRUCT ' + rs.name if rs else 'sem RTSTRUCT (BODY automático)'}"
                        + (f"; {len(self.planos_dicom)} plano(s) DICOM na pasta" if self.planos_dicom else ""))

    def criar_fantoma(self, tipo: str, largura: float, espessura: float, comprimento: float,
                      contorno: list | None = None, voxel_mm: float = 3.0, paciente_id: str = "",
                      paciente_nome: str = "", reutilizar: str | Path | None = None):
        f = FantomaVirtual(tipo, largura, espessura, comprimento, contorno)
        try:
            self.grade = f.grade(voxel_mm)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        g = self.grade
        self.paciente = {"id": paciente_id or "SEM_ID", "nome": paciente_nome}
        # CT virtual em DICOM: o plano 2D fica com geometria de paciente, como um plano de CT (o Mosaiq recebe o
        # mesmo tipo de objeto). A série é marcada como fantoma virtual para ninguém confundir com imagem real.
        from tps.casos_teste import exportar_volume
        if reutilizar and Path(reutilizar).is_dir():
            pasta = Path(reutilizar)                       # plano reaberto: mesmas imagens, mesmos UIDs
        else:
            pasta = paths.home() / "fantomas" / self.id
            hu = np.where(g.eta > 0.01, (g.eta - 1.0) * 1000.0, -1000.0).astype(np.float32)
            exportar_volume(pasta, hu, g.x, g.y, g.z, {"CORPO": (g.eta > 0.5, "EXTERNAL", (0, 200, 0))},
                            (paciente_nome or "SEM^NOME", self.paciente["id"]),
                            descricao="FANTOMA VIRTUAL - NAO E IMAGEM DO PACIENTE")
        from tps.imaging.ct import read_ct_series
        self.ct = read_ct_series(pasta)
        self.pasta_ct, self.arquivo_rs = pasta, pasta / "RS.dcm"
        self.fantoma = f
        self.tipo = "fantoma"
        self.estruturas = {"CORPO": Estrutura("CORPO", "EXTERNAL", (0, 200, 0), g.eta > 0.5)}
        self.derivadas = {}
        self.nome = paciente_nome or "Plano 2D"
        self._registrar(f"Fantoma virtual {tipo} {largura}×{espessura}×{comprimento} mm")

    def definir_maquina(self, maquina_id: str, energia: str = "6MV"):
        try:
            self.maquina = Maquina.carregar(maquina_id, energia)
        except FileNotFoundError as e:
            raise ErroUsuario(f"Máquina sem modelo comissionado: {e}") from e
        self._registrar(f"Máquina {maquina_id} {energia}")
        if self.plano:
            self.plano.maquina_id, self.plano.nome_dicom_maquina = maquina_id, self.maquina.nome_dicom
            self.dose_atualizada = False

    # ================================================================== plano e campos
    def importar_plano(self, arquivo: str) -> dict:
        """Importa um RTPLAN da pasta do CT (ex.: plano do Monaco) como plano novo, editável, com as UM originais.
        A dose do RTDOSE correspondente fica como referência para comparação (DVH e gama)."""
        from tps.importacao.rtplan import ler_dose, ler_plano
        if self.tipo != "ct" or self.pasta_ct is None:
            raise ErroUsuario("Carregue o CT do paciente antes de importar o plano")
        info = next((q for q in self.planos_dicom if q["arquivo"] == arquivo), None)
        if info is None:
            raise ErroUsuario(f"Plano {arquivo} não está na pasta do CT (ou é de outro referencial)")
        try:
            r = ler_plano(self.pasta_ct / arquivo)
        except (ValueError, AttributeError, KeyError, IndexError) as e:
            raise ErroUsuario(f"Não consegui ler o plano: {e}") from e
        if not r["campos"]:
            raise ErroUsuario("O plano não tem campos de tratamento importáveis")
        # máquina pelo TreatmentMachineName
        alvo_maq = None
        for mq in maquinas_disponiveis():
            raw = yaml.safe_load((RAIZ / "dados/maquinas" / mq["id"] / "maquina.yaml").read_text(encoding="utf-8"))
            if r["maquina"] in (raw.get("dicom_machine_names") or []):
                alvo_maq = mq["id"]
                break
        if alvo_maq is None:
            raise ErroUsuario(f"Máquina \"{r['maquina']}\" do plano não está cadastrada no TPS "
                              "(dicom_machine_names em maquina.yaml)")
        energia = f"{int(round(r['campos'][0].energia_mv))}MV"
        self._guardar_plano_atual()                    # o plano atual continua na sessão
        self.definir_maquina(alvo_maq, energia)
        # alvo: estrutura com o nome da descrição da prescrição; senão o PTV de maior dose no nome
        nomes = list(self.estruturas)
        alvo = next((n for n in nomes if n.upper() == (r["alvo_descricao"] or "").upper()), None)
        if alvo is None:
            ptvs = [n for n in nomes if n.upper().startswith("PTV")]
            num = lambda n: float((re.findall(r"[\d.]+", n) or ["0"])[0] or 0)  # noqa: E731
            alvo = max(ptvs, key=num) if ptvs else None
        if alvo is None:
            raise ErroUsuario("Nenhuma estrutura alvo (PTV) para a prescrição do plano importado")
        rot = (r["rotulo"] or "IMPORTADO")[:16]
        base, k = rot, 2
        while rot in self.outros_planos:
            rot = f"{base[:13]}_{k}"
            k += 1
        self.novo_plano(rot, float(r["dose_total"] or 0), int(r["fracoes"]), alvo, metodo="um_fixas")
        p = self.plano
        p.referencia_rtstruct = r["rtstruct"] or p.referencia_rtstruct
        for c in r["campos"]:
            if c.jaw_x is None and self.maquina.colim.tem_jaw_x:
                c.jaw_x = [float(min(c.mlc_a)), float(max(c.mlc_b))]
            p.campos.append(c)
        for pt in r["pontos"]:
            nome = pt["nome"][:16]
            if not any(q["nome"] == nome for q in p.pontos):
                p.pontos.append({"nome": nome, "xyz": [round(v, 1) for v in pt["xyz"]]})
        self.isocentro = tuple(r["campos"][0].isocentro_mm)
        self.doses_um = {}
        self.dose_atualizada = False
        # dose de referência (Monaco) na grade de dose, por fração
        self.dose_ref = None
        avisos = list(r["avisos"])
        if info.get("dose"):
            try:
                from scipy.interpolate import RegularGridInterpolator
                arr, (z, y, x), tipo = ler_dose(self.pasta_ct / info["dose"])
                g = self.grade
                it = RegularGridInterpolator((z, y, x), arr, bounds_error=False, fill_value=0.0)
                Z, Y, X = np.meshgrid(g.z, g.y, g.x, indexing="ij")
                total = it(np.stack([Z, Y, X], -1).reshape(-1, 3)).reshape(Z.shape)
                self.dose_ref = {"fx": total / max(int(r["fracoes"]), 1), "fonte": r["fabricante"] or "outro TPS",
                                 "arquivo": info["dose"]}
            except Exception as e:  # noqa: BLE001
                avisos.append(f"RTDOSE não importado: {e}")
        self.avisos = avisos + [f"Plano importado de {r['fabricante'] or 'outro TPS'} "
                                           f"com as UM originais. Calcule para comparar a dose do TPS com a importada; "
                                           f"para replanejar, troque a normalização."]
        self._registrar(f"Plano {rot} importado de {arquivo} ({len(p.campos)} campos, máquina {alvo_maq}, "
                        f"{r['dose_total']:g} Gy/{r['fracoes']} fx, alvo {alvo})")
        return self.resumo()

    # ---------------------------------------------------------------- soma e comparação de planos
    def _dose_de(self, rotulo: str):
        """(dose total em Gy, plano) de um plano da sessão — o da frente ou um guardado — já calculado."""
        if self.plano is not None and rotulo == self.plano.rotulo:
            self._exigir(self.dose_atualizada and self.dose_fx is not None, f"Calcule a dose do plano {rotulo}")
            return self.dose_fx * self.plano.prescricao.fracoes, self.plano
        est = self.outros_planos.get(rotulo)
        if est is None:
            raise ErroUsuario(f"Plano {rotulo} não está nesta sessão")
        if not (est.get("dose_atualizada") and est.get("dose_fx") is not None):
            raise ErroUsuario(f"Calcule a dose do plano {rotulo} antes")
        return est["dose_fx"] * est["plano"].prescricao.fracoes, est["plano"]

    def _mapa_alfa_beta(self, ab_alvo: float, ab_oar: float, por_estrutura: dict) -> np.ndarray:
        g = self.grade
        ab = np.full((len(g.z), len(g.y), len(g.x)), float(ab_oar), np.float32)
        alvos = [e for n, e in self.estruturas.items() if re.match(r"^(PTV|CTV|GTV|ITV)", n, re.IGNORECASE)]
        for e in alvos:
            ab[e.mascara] = float(ab_alvo)
        for n, v in (por_estrutura or {}).items():            # a estrutura escolhida vale por cima
            e = self.estruturas.get(n)
            if e is not None and v not in (None, ""):
                ab[e.mascara] = float(v)
        return ab

    def soma_planos(self, rotulos: list[str], modo: str = "fisica", ab_alvo: float = 10.0, ab_oar: float = 3.0,
                    ab_estruturas: dict | None = None) -> dict:
        """Soma de planos da mesma TC (ex.: fase 1 + boost). modo "fisica" = Gy somados; "eqd2" = cada plano
        convertido voxel a voxel com o próprio fracionamento: EQD2 = D·(d + α/β)/(2 + α/β), d = D/n.
        α/β: alvos (PTV/CTV/GTV/ITV) = ab_alvo, resto = ab_oar, com valores por estrutura por cima."""
        if len(rotulos) < 2:
            raise ErroUsuario("Escolha pelo menos dois planos")
        if modo not in ("fisica", "eqd2"):
            raise ErroUsuario("Modo deve ser fisica ou eqd2")
        ab = self._mapa_alfa_beta(ab_alvo, ab_oar, ab_estruturas or {}) if modo == "eqd2" else None
        total = None
        rx = 0.0
        partes = []
        for r in rotulos:
            d, p = self._dose_de(r)
            n = p.prescricao.fracoes
            if modo == "eqd2":
                d = d * (d / n + ab) / (2.0 + ab)
                rxp = p.prescricao.dose_total_gy * (p.prescricao.dose_fracao_gy + ab_alvo) / (2.0 + ab_alvo)
            else:
                rxp = p.prescricao.dose_total_gy
            rx += rxp
            total = d if total is None else total + d
            partes.append({"rotulo": r, "dose_total_gy": p.prescricao.dose_total_gy, "fracoes": n,
                           "rx_eqd2_gy" if modo == "eqd2" else "rx_gy": round(rxp, 2)})
        g = self.grade
        corpo = self._corpo()
        hist = {n: dvh(total, e.mascara, g.voxel_cc) for n, e in self.estruturas.items() if e.mascara.any()}
        from tps.planejamento import restricoes
        try:
            av = restricoes.avaliar({n: total[e.mascara] for n, e in self.estruturas.items() if e.mascara.any()},
                                    g.voxel_cc, rx, (max(1, round(rx / 2)) if modo == "eqd2" else
                                                     sum(q["fracoes"] for q in partes)),
                                    self.plano.prescricao.estrutura_alvo if self.plano else None,
                                    extras=self.plano.restricoes if self.plano else None).json()
        except Exception as e:  # noqa: BLE001
            av = {"erro": str(e), "resultados": [], "avisos": []}
        self.dose_vista["soma"] = {"total": total.astype(np.float32), "rx_gy": rx,
                                   "descricao": (" + ".join(rotulos)) + (" (EQD2)" if modo == "eqd2" else "")}
        self._registrar(f"Soma de planos: {' + '.join(rotulos)} ({'EQD2' if modo == 'eqd2' else 'dose física'})")
        return {"rotulos": rotulos, "modo": modo, "rx_gy": round(rx, 2), "partes": partes,
                "dmax_gy": round(float(total[corpo].max() if corpo.any() else total.max()), 2),
                "estatisticas": {n: {k: round(float(h[k]), 2) for k in ("volume_cc", "Dmean", "Dmax", "D95", "D98", "D2")
                                     if k in h} for n, h in hist.items()},
                "dvh": self._dvh_compacto(hist), "restricoes": av,
                "ab": {"alvo": ab_alvo, "oar": ab_oar, "estruturas": ab_estruturas or {}} if modo == "eqd2" else None}

    def _dvh_compacto(self, hist: dict) -> dict:
        out = {}
        for n, h in hist.items():
            d, v = np.asarray(h["dose_gy"]), np.asarray(h["volume_pct"])
            k = np.linspace(0, len(d) - 1, min(len(d), 200)).astype(int)
            out[n] = {"dose": d[k].round(3).tolist(), "volume": v[k].round(2).tolist(), "cor": self.estruturas[n].cor}
        return out

    def comparar_planos(self, a: str, b: str) -> dict:
        """Dois planos da mesma TC lado a lado: DVHs, métricas por estrutura, diferença de dose e gama 3%/3 mm."""
        if a == b:
            raise ErroUsuario("Escolha dois planos diferentes")
        da, pa = self._dose_de(a)
        db, pb = self._dose_de(b)
        g = self.grade
        corpo = self._corpo()
        ha = {n: dvh(da, e.mascara, g.voxel_cc) for n, e in self.estruturas.items() if e.mascara.any()}
        hb = {n: dvh(db, e.mascara, g.voxel_cc) for n, e in self.estruturas.items() if e.mascara.any()}
        tab = {}
        for n in ha:
            linha = {}
            for k in ("Dmean", "Dmax", "D95", "D2"):
                if k in ha[n] and k in hb[n]:
                    linha[k] = [round(float(ha[n][k]), 2), round(float(hb[n][k]), 2)]
            tab[n] = linha
        from tps.compare.gamma import gamma_3d
        try:
            ax = (g.z, g.y, g.x)
            rr = gamma_3d(ax, np.where(corpo, da, 0), ax, np.where(corpo, db, 0), dose_pct=3.0, dta_mm=3.0,
                          cutoff_pct=10.0)
            gam = round(float(getattr(rr, "pass_rate", getattr(rr, "taxa_aprovacao", float("nan")))), 1)
        except Exception:  # noqa: BLE001
            gam = None
        rx = pa.prescricao.dose_total_gy
        dif = np.where(corpo, db - da, 0.0).astype(np.float32)
        self.dose_vista["dif"] = {"total": dif, "rx_gy": rx, "descricao": f"{b} − {a}"}
        self._registrar(f"Planos comparados: {a} × {b}")
        return {"a": a, "b": b, "rx_a": rx, "rx_b": pb.prescricao.dose_total_gy, "tabela": tab, "gama_3_3": gam,
                "dif_max_gy": round(float(dif.max()), 2), "dif_min_gy": round(float(dif.min()), 2),
                "hi": [indice_homogeneidade(da, self.estruturas[pa.prescricao.estrutura_alvo].mascara)
                       if pa.prescricao.estrutura_alvo in self.estruturas else None,
                       indice_homogeneidade(db, self.estruturas[pb.prescricao.estrutura_alvo].mascara)
                       if pb.prescricao.estrutura_alvo in self.estruturas else None],
                "um": [round(sum(c.mu or 0 for c in pa.campos), 1), round(sum(c.mu or 0 for c in pb.campos), 1)],
                "dvh_a": self._dvh_compacto(ha), "dvh_b": self._dvh_compacto(hb)}

    def comparacao_ref(self, total) -> dict | None:
        """DVH e gama 3%/3 mm (global, limiar 10%) da dose do TPS contra a dose importada."""
        if not self.dose_ref:
            return None
        from tps.compare.gamma import gamma_3d
        g = self.grade
        fr = self.plano.prescricao.fracoes
        ref = self.dose_ref["fx"] * fr
        hist = {n: dvh(ref, e.mascara, g.voxel_cc) for n, e in self.estruturas.items() if e.mascara.any()}
        corpo = self._corpo()
        estat = {}
        for n, h in hist.items():
            m = self.estruturas[n].mascara
            estat[n] = {"Dmean_ref": round(float(ref[m].mean()), 2), "Dmean_tps": round(float(total[m].mean()), 2),
                        "dif_pct": round(float(total[m].mean() / max(ref[m].mean(), 1e-6) * 100 - 100), 2)}
        try:
            ax = (g.z, g.y, g.x)
            rr = gamma_3d(ax, np.where(corpo, ref, 0), ax, np.where(corpo, total, 0), dose_pct=3.0, dta_mm=3.0,
                          cutoff_pct=10.0)
            gam = float(getattr(rr, "pass_rate", getattr(rr, "taxa_aprovacao", float("nan"))))
        except Exception:  # noqa: BLE001
            gam = float("nan")
        return {"fonte": self.dose_ref["fonte"], "dvh": hist, "estatisticas": estat, "gama_3_3": round(gam, 1),
                "dmax_ref": round(float(ref[corpo].max()), 2) if corpo.any() else None}

    # ---------------------------------------------------------------- vários planos no mesmo CT
    _ESTADO = ("plano", "doses_um", "dose_fx", "resultado", "dose_atualizada", "eta_original", "avisos",
               "isocentro", "dose_ref", "maquina")

    def _guardar_plano_atual(self):
        """Tira o plano atual da frente (com dose, resultado, bolus e máquina) e guarda na lista da sessão."""
        if self.plano is None:
            return
        est = {k: getattr(self, k, None) for k in self._ESTADO}
        est["bolus"] = {b["nome"]: self.estruturas.pop(b["nome"]) for b in self.plano.bolus
                        if b["nome"] in self.estruturas}
        self.outros_planos[self.plano.rotulo] = est
        self.plano = None
        self.doses_um, self.dose_fx, self.resultado, self.dose_atualizada = {}, None, {}, False
        self.dose_ref = None
        self.avisos = []
        if getattr(self, "eta_ct", None) is not None or self.grade is not None:
            self.eta_original = None
            if self.tipo == "ct":
                self._mascarar_fora_do_corpo()

    def _restaurar_plano(self, rotulo: str):
        est = self.outros_planos.pop(rotulo)
        for k in self._ESTADO:
            setattr(self, k, est.get(k))
        for nome, e in (est.get("bolus") or {}).items():
            self.estruturas[nome] = e
        if self.eta_original is None:
            self.eta_original = self.grade.eta.copy()
        self._aplicar_eta(manter_dose=True)

    def trocar_plano(self, rotulo: str) -> dict:
        if self.plano is not None and self.plano.rotulo == rotulo:
            return self.resumo()
        if rotulo not in self.outros_planos:
            raise ErroUsuario(f"Plano {rotulo} não está nesta sessão")
        self._guardar_plano_atual()
        self._restaurar_plano(rotulo)
        self._registrar(f"Plano {rotulo} em uso")
        return self.resumo()

    def copiar_plano(self, novo_rotulo: str) -> dict:
        """Cópia do plano atual (rascunho, sem aprovação), para variar campos/normalização e comparar."""
        self._exigir_plano()
        novo_rotulo = (novo_rotulo or "").strip()[:16]
        if not novo_rotulo:
            raise ErroUsuario("Dê um rótulo ao plano novo")
        if novo_rotulo == self.plano.rotulo or novo_rotulo in self.outros_planos:
            raise ErroUsuario(f"Já existe um plano {novo_rotulo} nesta sessão")
        novo = copy.deepcopy(self.plano)
        novo.rotulo = novo_rotulo
        novo.estado, novo.aprovacao, novo.historico = "RASCUNHO", None, []
        for c, c0 in zip(novo.campos, self.plano.campos):
            c.__dict__["_spec"] = copy.deepcopy(c0.__dict__.get("_spec"))
        doses = dict(self.doses_um)
        dose_fx, res, atual = self.dose_fx, copy.deepcopy(self.resultado), self.dose_atualizada
        bolus = {b["nome"]: self.estruturas[b["nome"]] for b in self.plano.bolus if b["nome"] in self.estruturas}
        eta_o, iso, ref, maq = self.eta_original, self.isocentro, self.dose_ref, self.maquina
        self._guardar_plano_atual()
        self.plano, self.doses_um, self.dose_fx, self.resultado = novo, doses, dose_fx, res
        self.dose_atualizada, self.eta_original, self.isocentro, self.dose_ref, self.maquina = atual, eta_o, iso, ref, maq
        for nome, e in bolus.items():
            self.estruturas[nome] = copy.deepcopy(e)
        self._aplicar_eta()
        self._registrar(f"Plano {novo_rotulo} copiado")
        return self.resumo()

    def remover_plano(self, rotulo: str) -> dict:
        if self.plano is not None and self.plano.rotulo == rotulo:
            if self.plano.estado != "RASCUNHO":
                raise ErroUsuario("Desaprove o plano antes de removê-lo da sessão")
            for b in self.plano.bolus:
                self.estruturas.pop(b["nome"], None)
            self.plano = None
            self.doses_um, self.dose_fx, self.resultado, self.dose_atualizada = {}, None, {}, False
            if self.outros_planos:
                self._restaurar_plano(next(iter(self.outros_planos)))
        elif rotulo in self.outros_planos:
            if self.outros_planos[rotulo]["plano"].estado != "RASCUNHO":
                raise ErroUsuario("Desaprove o plano antes de removê-lo da sessão")
            self.outros_planos.pop(rotulo)
        else:
            raise ErroUsuario(f"Plano {rotulo} não está nesta sessão")
        self._registrar(f"Plano {rotulo} removido da sessão (o arquivo salvo, se houver, continua)")
        return self.resumo()

    def apagar_plano(self, rotulo: str) -> tuple[dict, list[str]]:
        """Apaga o plano: sai da sessão e o arquivo salvo vai para <home>/planos/lixeira (recuperável). Só rascunhos.
        Devolve o resumo e as chaves do índice de planos que foram para a lixeira."""
        estado = None
        if self.plano is not None and self.plano.rotulo == rotulo:
            estado = self.plano.estado
        elif rotulo in self.outros_planos:
            estado = self.outros_planos[rotulo]["plano"].estado
        if estado is None:
            raise ErroUsuario(f"Plano {rotulo} não está nesta sessão")
        if estado != "RASCUNHO":
            raise ErroUsuario("Plano aprovado não é apagado: desaprove antes (fica registrado na auditoria)")
        movidos = []
        arq = paths.home() / "planos" / f"{self.paciente.get('id') or 'sem_id'}_{rotulo}.json"
        if mover_para_lixeira(arq):
            movidos.append(arq.stem)
        self.remover_plano(rotulo)
        self._registrar(f"Plano {rotulo} apagado" + (" (arquivo na lixeira)" if movidos else ""))
        return self.resumo(), movidos

    def lista_planos(self) -> list[dict]:
        out = []
        if self.plano is not None:
            out.append({"rotulo": self.plano.rotulo, "estado": self.plano.estado, "atual": True,
                        "campos": len(self.plano.campos), "dose": self.dose_atualizada})
        for r, e in self.outros_planos.items():
            out.append({"rotulo": r, "estado": e["plano"].estado, "atual": False, "campos": len(e["plano"].campos),
                        "dose": bool(e.get("dose_atualizada"))})
        return sorted(out, key=lambda q: q["rotulo"])

    def novo_plano(self, rotulo: str, dose_total: float, fracoes: int, alvo: str, metodo: str = "media_alvo",
                   ponto=None, isodose: float = 100.0, cobertura: tuple | None = None):
        self._exigir(self.grade is not None, "Abra um CT ou crie um fantoma antes")
        self._exigir(self.maquina is not None, "Escolha a máquina antes")
        rotulo = (rotulo or "").strip()[:16]
        if rotulo in self.outros_planos or (self.plano is not None and self.plano.rotulo == rotulo):
            raise ErroUsuario(f"Já existe um plano {rotulo} nesta sessão: use outro rótulo (ou troque para ele)")
        rx = Prescricao(float(dose_total), int(fracoes), alvo, metodo, tuple(ponto) if ponto else None, float(isodose))
        if metodo == "cobertura":
            rx.cobertura_volume_pct, rx.cobertura_dose_pct = (float(v) for v in (cobertura or (95.0, 100.0)))
        erros = rx.validar()
        if metodo == "media_alvo" and alvo not in self.estruturas:
            erros.append(f"Estrutura alvo {alvo} não existe")
        if erros:
            raise ErroUsuario("; ".join(erros))
        ref = referencia_do_ct(sorted(p for p in self.pasta_ct.iterdir() if p.is_file() and self._e_ct(p))[0])
        maq, iso = self.maquina, self.isocentro
        self._guardar_plano_atual()                     # o plano anterior continua na sessão
        self.maquina, self.isocentro = maq, iso
        m = self.maquina
        self.plano = Plano(rotulo=rotulo[:16], paciente_id=self.paciente["id"], maquina_id=m.id,
                           nome_dicom_maquina=m.nome_dicom, prescricao=rx,
                           tecnica="3D" if self.tipo == "ct" else "2D",
                           frame_of_reference=self.ct.frame_of_reference,
                           referencia_ct=ref,
                           referencia_rtstruct=str(pydicom.dcmread(str(self.arquivo_rs), stop_before_pixels=True,
                                                                    force=True).SOPInstanceUID) if self.arquivo_rs else None,
                           modelo_feixe=self._info_modelo())
        if self.isocentro is None:
            if alvo in self.estruturas and self.estruturas[alvo].mascara.any():
                kz, ky, kx = np.nonzero(self.estruturas[alvo].mascara)
                g = self.grade
                self.isocentro = (round(float(g.x[kx].mean()), 1), round(float(g.y[ky].mean()), 1),
                                  round(float(g.z[kz].mean()), 1))
            else:
                self.isocentro = tuple(float(v) for v in (ponto or (0.0, 0.0, 0.0)))
        self.doses_um, self.dose_fx, self.dose_atualizada = {}, None, False
        self.eta_original = self.grade.eta.copy()
        self._registrar(f"Plano {rotulo}: {dose_total} Gy / {fracoes} fx em {alvo}")

    def aplicar_modelo(self, modelo_id: str, rotulo: str, dose_total: float | None = None, fracoes: int | None = None,
                       alvo: str | None = None, calcular: bool = True) -> dict:
        """Plano semi-pronto a partir de um modelo por sítio (dados/modelos_plano/*.yaml). SRS-PLN-080."""
        from tps.planejamento import modelos_plano as MP
        from tps.planejamento.conformacao import projetar_bev
        mods = MP.carregar()
        if modelo_id not in mods:
            raise ErroUsuario(f"Modelo {modelo_id} não existe")
        md = mods[modelo_id]
        nomes = list(self.estruturas)
        alvo = alvo or MP.achar_estrutura(nomes, md.get("alvo") or [])
        if not alvo or alvo not in self.estruturas:
            raise ErroUsuario(f"Não achei o alvo para o modelo ({', '.join(md.get('alvo') or [])}): escolha o alvo")
        norm = md.get("normalizacao") or {}
        metodo = norm.get("metodo", "media_alvo")
        self.isocentro = None                           # isocentro no centro do alvo do modelo
        self.novo_plano(rotulo, float(dose_total or md.get("dose_total", 50)), int(fracoes or md.get("fracoes", 25)),
                        alvo, metodo, None, float(norm.get("isodose_pct", 100)),
                        (norm.get("volume_pct", 95), norm.get("dose_pct", 100)))
        g = self.grade
        eixos = (g.x, g.y, g.z)
        am = self.estruturas[alvo].mascara
        corpo = self._corpo()
        info = {"modelo": modelo_id, "alvo": alvo}
        angulos = {}
        if any(str(c.get("gantry", "")).startswith("tangente") for c in md["campos"]):
            lado = md.get("lado") or MP.lateralidade(am, corpo, g.x)
            pul = (self.eta_original < 0.6) & np.asarray(corpo) & ~am
            from scipy.ndimage import binary_erosion
            pul &= binary_erosion(corpo, iterations=3)
            cor_nome = MP.achar_estrutura(nomes, ["tg:Heart"])
            t = MP.tangentes(am, pul, eixos, self.isocentro, lado, coracao=self.estruturas[cor_nome].mascara
                             if cor_nome else None)
            angulos = {"tangente_medial": t["medial"], "tangente_lateral": t["lateral"]}
            info["tangentes"] = t
        lado_tumor = MP.lateralidade(am, corpo, g.x)
        oars = MP.achar_todas(nomes, md.get("oars") or [])
        blind = [{"nome": n, "margem": float(b.get("margem", 3))} for b in md.get("blindagens") or []
                 for n in MP.achar_todas(nomes, [b["oar"]])]
        avisos = []
        for cdef in md["campos"]:
            gtxt = str(cdef.get("gantry", 0))
            if gtxt in angulos:
                gantry = angulos[gtxt]
            elif gtxt.startswith("lado_tumor_"):
                a = float(gtxt.split("_")[-1])
                gantry = a if lado_tumor == "E" else (360 - a) % 360
            else:
                gantry = float(gtxt)
            margem = dict(md.get("margem") or {"x1": 7, "x2": 7, "y1": 7, "y2": 7})
            if md.get("flash_mm"):                      # abre o lado anterior (pele) do BEV: flash
                c0 = Campo("t", float(gantry), isocentro_mm=self.isocentro)
                u_ant, _ = projetar_bev(np.array([[self.isocentro[0], self.isocentro[1] - 150, self.isocentro[2]]]), c0)
                margem["x2" if u_ant[0] > 0 else "x1"] = float(md["flash_mm"])
            spec = {"nome": cdef.get("nome"), "gantry": gantry, "colimador": cdef.get("colimador", 0),
                    "peso": cdef.get("peso", 1.0), "modo": "conformado", "alvo": alvo, "margem": margem,
                    "ponto_encontro": md.get("ponto_encontro", "fora"), "blindagens": blind}
            r = self.salvar_campo(spec)
            avisos += r["avisos"]
        fif = md.get("field_in_field")
        opcoes = {"metodo": metodo}
        if metodo == "cobertura":
            opcoes.update(cobertura_volume_pct=norm.get("volume_pct", 95), cobertura_dose_pct=norm.get("dose_pct", 100))
        if fif:
            gs = fif.get("gantry", "todos")
            campos = [c.nome for c in self.plano.campos if gs == "todos" or
                      any(abs(((c.gantry - float(x)) + 180) % 360 - 180) < 1 for x in gs)]
            opcoes["field_in_field"] = {"modo": fif.get("modo", "filtro"), "niveis": fif.get("niveis", [97, 94, 91, 88]),
                                        "peso_pct": fif.get("peso_pct", 3), "campos": campos,
                                        "ajuste_fino": bool(fif.get("ajuste_fino", False))}
        prot = []
        for o in md.get("proteger") or []:
            for n in MP.achar_todas(nomes, [o["oar"]])[:1]:
                prot.append({"estrutura": n, "metrica": o.get("metrica", "Dmean"), "valor": o.get("valor"),
                             "volume_pct": o.get("volume_pct"), "prioridade": o.get("prioridade", 5)})
        if prot:
            opcoes["proteger_oars"] = {"oars": prot, "subcampos": True}
        info.update(oars=oars, opcoes=opcoes, avisos=avisos)
        self._registrar(f"Plano {rotulo} criado pelo modelo {md.get('nome', modelo_id)} (alvo {alvo})")
        if calcular:
            self.calcular(opcoes)
            self.avisos = avisos + self.avisos
        out = self.resumo()
        out["modelo_aplicado"] = info
        return out

    def _info_modelo(self):
        mo = self.maquina.modelo
        return {"energia_mv": mo.energy_mv, "hash": calculo.hash_modelo(mo), "comissionado": mo.comissionado,
                "demonstracao": mo.demonstracao, "maquina": self.maquina.id}

    @staticmethod
    def _e_ct(p: Path) -> bool:
        try:
            return getattr(pydicom.dcmread(str(p), stop_before_pixels=True, force=True), "Modality", "") == "CT"
        except Exception:
            return False

    def definir_isocentro(self, xyz):
        self._exigir_plano()
        self.isocentro = tuple(round(float(v), 1) for v in xyz)
        for c in self.plano.campos:
            c.isocentro_mm = self.isocentro
            c.ssd_mm = calculo.ssd_no_eixo(self.grade, c)
        self.dose_atualizada = False

    def _margem(self, m) -> Margem:
        if isinstance(m, dict):
            return Margem(float(m.get("x1", 0)), float(m.get("x2", 0)), float(m.get("y1", 0)), float(m.get("y2", 0)),
                          bool(m.get("eliptica", True)))
        return Margem.uniforme(float(m or 0))

    def salvar_campo(self, spec: dict) -> dict:
        """Cria ou altera um campo. spec: nome, gantry, colimador, mesa, peso, modo (conformado | retangular),
        alvo, margem, ponto_encontro, blindagens [{nome, margem}], x1,x2,y1,y2, otimizar_colimador."""
        self._exigir_plano()
        p, m = self.plano, self.maquina
        nome = (spec.get("nome") or f"G{int(round(float(spec.get('gantry', 0)))) % 360:03d}").strip()[:16]
        existente = next((c for c in p.campos if c.nome == (spec.get("nome_antigo") or nome)), None)
        if existente is not None and existente.campo_pai is not None:
            raise ErroUsuario(f"{existente.nome} é um sub-campo gerado (field-in-field / proteção de órgãos) a partir de "
                              f"{existente.campo_pai}: ajuste o campo {existente.campo_pai} e recalcule, ou mova as lâminas "
                              "do sub-campo no editor do BEV")
        if existente is None and any(c.nome == nome for c in p.campos):
            raise ErroUsuario(f"Já existe um campo {nome}")
        c = Campo(nome=nome, gantry=float(spec.get("gantry", 0)) % 360, colimador=float(spec.get("colimador", 0)) % 360,
                  mesa=float(spec.get("mesa", 0)) % 360, isocentro_mm=self.isocentro, energia_mv=m.modelo.energy_mv,
                  peso=float(spec.get("peso", 1.0)))
        if c.peso <= 0:
            raise ErroUsuario("Peso do campo deve ser positivo")
        avisos = []
        g = self.grade
        eixos = (g.x, g.y, g.z)
        if spec.get("modo", "conformado") == "retangular":
            r = CampoRetangular(nome, c.gantry, float(spec.get("x1", 50)), float(spec.get("x2", 50)),
                                float(spec.get("y1", 50)), float(spec.get("y2", 50)), c.colimador, c.peso)
            try:
                rc = campo_de_retangulo(r, m.colim, self.isocentro, m.modelo.energy_mv)
            except ValueError as e:
                raise ErroUsuario(str(e)) from e
            c.jaw_x, c.jaw_y, c.mlc_a, c.mlc_b = rc.jaw_x, rc.jaw_y, rc.mlc_a, rc.mlc_b
        else:
            alvo = spec.get("alvo") or p.prescricao.estrutura_alvo
            if alvo not in self.estruturas:
                raise ErroUsuario(f"Estrutura {alvo} não existe")
            alvos = [(self.estruturas[alvo].mascara, self._margem(spec.get("margem", 7)))]
            blind = []
            for b in spec.get("blindagens", []) or []:
                if b.get("nome") not in self.estruturas:
                    raise ErroUsuario(f"Estrutura de blindagem {b.get('nome')} não existe")
                blind.append((self.estruturas[b["nome"]].mascara, self._margem(b.get("margem", 0))))
            pe = spec.get("ponto_encontro", "fora")
            try:
                pe = float(pe)
            except (TypeError, ValueError):
                pass
            # blindagem no meio de um par: o campo é dividido em segmentos (padrão) — com o envelope o MLC não blinda
            op = OpcoesAjuste(pe, float(spec.get("folga_jaw", 0)),
                              segmentos="dividir" if blind and spec.get("blindagem_meio", "dividir") == "dividir"
                              else "envelope")
            try:
                if spec.get("otimizar_colimador"):
                    c.colimador, _ = otimizar_colimador(c, m.colim, alvos, eixos, angulos=range(0, 180, 5),
                                                        op=OpcoesAjuste(pe, resolucao_mm=1.0), blindagens=blind)
                    avisos.append(f"{nome}: colimador otimizado em {c.colimador:.0f}°")
                ra = ajustar(c, m.colim, alvos, blind, eixos, op, prioridade_alvo=bool(spec.get("prioridade_alvo", True)),
                             nome_alvo=alvo)
            except ValueError as e:
                raise ErroUsuario(f"{nome}: {e}") from e
            avisos += [f"{nome}: {a}" for a in ra.avisos]
            if c.segmentos_blindagem:                 # cada parte recebe 1/K das UM: o peso ×K mantém a fluência
                c.peso *= len(c.segmentos_blindagem)
                avisos.append(f"{nome}: peso ×{len(c.segmentos_blindagem)} (UM repartidas entre os segmentos)")
        c.ssd_mm = calculo.ssd_no_eixo(g, c)
        if c.ssd_mm is None:
            raise ErroUsuario(f"{nome}: o eixo central não cruza o paciente")
        c.alvo_conformado = spec.get("alvo") if spec.get("modo", "conformado") != "retangular" else None
        if existente is not None and existente.e_arco:           # editar um arco não o transforma em campo fixo
            c.arco_fim, c.sentido = existente.arco_fim, existente.sentido
        if spec.get("arco_fim") not in (None, ""):
            from tps.planejamento.vmat import validar_arco
            c.arco_fim, c.sentido = float(spec["arco_fim"]) % 360.0, spec.get("sentido", "CW")
            erros = validar_arco(c.gantry, c.arco_fim, c.sentido)
            if erros:
                raise ErroUsuario("; ".join(erros))
        self._remover_derivados()
        if existente:
            idx = p.campos.index(existente)
            p._exigir_rascunho()
            p.campos[idx] = c
            self.doses_um.pop(existente.nome, None)
        else:
            p.adicionar_campo(c)
        c.__dict__["_spec"] = spec
        self.dose_atualizada = False
        self.avisos = avisos
        return {"campo": self._campo_json(c), "avisos": avisos}

    def aplicar_a_todos(self, spec: dict, campos: list[str] | None = None) -> dict:
        """O mesmo ajuste (alvo, margem, ponto de encontro, blindagens, otimização do colimador, modo) em todos os
        campos estáticos principais (ou nos `campos` dados); cada campo mantém gantry, mesa, peso e nome (e o
        colimador, se não for otimizar)."""
        self._exigir_plano()
        self.plano._exigir_rascunho()
        chaves = ("modo", "alvo", "margem", "ponto_encontro", "blindagens", "otimizar_colimador", "prioridade_alvo",
                  "folga_jaw", "blindagem_meio", "x1", "x2", "y1", "y2")
        alvos = [c for c in self.plano.campos if c.campo_pai is None and not c.e_arco
                 and (not campos or c.nome in campos)]
        if not alvos:
            raise ErroUsuario("Nenhum campo estático para ajustar")
        feitos, avisos, erros = [], [], []
        for c in list(alvos):
            base = dict(c.__dict__.get("_spec") or {})
            base.update({"gantry": c.gantry, "colimador": c.colimador, "mesa": c.mesa,
                         "peso": base.get("peso", c.peso / max(1, len(c.segmentos_blindagem or [])))})
            base.update({k: spec[k] for k in chaves if k in spec})
            base.update({"nome": c.nome, "nome_antigo": c.nome})
            try:
                r = self.salvar_campo(base)
                feitos.append(c.nome)
                avisos += r["avisos"]
            except ErroUsuario as e:
                erros.append(f"{c.nome}: {e}")
        self.avisos = avisos + erros
        self._registrar(f"Ajuste aplicado a {len(feitos)} campo(s): {', '.join(feitos)}"
                        + (f"; falhou em {len(erros)}" if erros else ""))
        if not feitos:
            raise ErroUsuario("; ".join(erros))
        return {"campos": feitos, "avisos": avisos, "erros": erros}

    # ---------------------------------------------------------------- campos de localização (setup)
    def adicionar_setup(self, nome: str, gantry: float, x_cm: float = 20.0, y_cm: float = 20.0,
                        colimador: float = 0.0, mesa: float = 0.0) -> dict:
        """Campo de localização (imagem de posicionamento): sem dose, exportado como SETUP (TreatmentDeliveryType),
        sem UM e fora do grupo de frações. Abertura retangular centrada no isocentro."""
        from tps.planejamento.plano_2d import CampoRetangular, campo_de_retangulo
        self._exigir_plano()
        self.plano._exigir_rascunho()
        nome = (nome or "").strip()[:16] or f"SETUP{int(gantry) % 360:03d}"
        todos = {c.nome for c in self.plano.campos} | {c.nome for c in self.plano.campos_setup}
        if nome in todos:
            raise ErroUsuario(f"Já existe um campo {nome}")
        if not (2 <= x_cm <= 40 and 2 <= y_cm <= 40):
            raise ErroUsuario("Tamanho do campo de localização entre 2 e 40 cm")
        iso = self.isocentro or (self.plano.campos[0].isocentro_mm if self.plano.campos else (0.0, 0.0, 0.0))
        c = campo_de_retangulo(CampoRetangular.simetrico(nome, float(gantry), float(x_cm), float(y_cm),
                                                         colimador=float(colimador)), self.maquina.colim, iso,
                               self.maquina.modelo.energy_mv)
        c.mesa = float(mesa)
        c.descricao = "SETUP"
        c.ssd_mm = calculo.ssd_no_eixo(self.grade, c)
        self.plano.campos_setup.append(c)
        self._registrar(f"Campo de localização {nome} (G{gantry:g}, {x_cm:g}×{y_cm:g} cm)")
        return {"nome": nome}

    def remover_setup(self, nome: str):
        self._exigir_plano()
        self.plano._exigir_rascunho()
        antes = len(self.plano.campos_setup)
        self.plano.campos_setup = [c for c in self.plano.campos_setup if c.nome != nome]
        if len(self.plano.campos_setup) == antes:
            raise ErroUsuario(f"Campo de localização {nome} não existe")

    def _remover_derivados(self):
        """Ao mudar um campo principal, sub-campos e segmentos calculados deixam de valer."""
        p = self.plano
        p._exigir_rascunho()
        subs = [c for c in p.campos if c.campo_pai]
        for s in subs:
            p.campos.remove(s)
            self.doses_um.pop(s.nome, None)
        for c in p.campos:                       # pesos que o field-in-field mudou voltam ao que eram
            if "_peso_antes_fif" in c.__dict__:
                c.peso = c.__dict__.pop("_peso_antes_fif")
        for c in p.campos:
            base = [dict(x) for x in c.segmentos_blindagem] if c.segmentos_blindagem else None
            if c.segmentos and c.segmentos != base:          # a divisão da blindagem faz parte da conformação
                c.segmentos = base
                self.doses_um.pop(c.nome, None)
            elif base and not c.segmentos:
                c.segmentos = base
                self.doses_um.pop(c.nome, None)
            if c.pontos_controle:                # arco VMAT: os pontos de controle saem, o arco fica
                c.pontos_controle = None
                self.doses_um.pop(c.nome, None)
            if p.tecnica == "IMRT":
                p.tecnica = "3D"
            ab = c.__dict__.pop("_abertura_conformada", None)
            if ab is not None:                   # IMRT/compensador: volta à abertura conformada de partida
                c.mlc_a, c.mlc_b, c.jaw_x, c.jaw_y = ab
                self.doses_um.pop(c.nome, None)

    def editar_laminas(self, nome: str, mlc_a: list, mlc_b: list, jaw_x=None, jaw_y=None) -> dict:
        self._exigir_plano()
        c = self._campo(nome)
        self._remover_derivados()
        novo = copy.deepcopy(c)
        novo.mlc_a = [round(float(v), 1) for v in mlc_a]
        novo.mlc_b = [round(float(v), 1) for v in mlc_b]
        if jaw_y is not None:
            novo.jaw_y = tuple(float(v) for v in jaw_y)
        if jaw_x is not None and self.maquina.colim.tem_jaw_x:
            novo.jaw_x = tuple(float(v) for v in jaw_x)
        erros = verificar_mecanica(novo, self.maquina.colim)
        if erros:
            raise ErroUsuario("; ".join(erros))
        c.mlc_a, c.mlc_b, c.jaw_x, c.jaw_y = novo.mlc_a, novo.mlc_b, novo.jaw_x, novo.jaw_y
        c.alvo_conformado = None                      # editado à mão
        c.segmentos_blindagem = None
        c.segmentos = None
        self.doses_um.pop(nome, None)
        self.dose_atualizada = False
        return {"campo": self._campo_json(c)}

    def mover_lamina(self, nome: str, par: int, banco: str, pos_mm: float) -> dict:
        """Arrasto de uma lâmina no BEV, empurrando vizinhas quando o MLC não interdigita (MLCi)."""
        from tps.planejamento.ajuste_mlc import acomodar_lamina
        self._exigir_plano()
        self.plano._exigir_rascunho()
        c = self._campo(nome)
        if banco not in ("a", "b") or not 0 <= int(par) < self.maquina.colim.n_pares:
            raise ErroUsuario("Lâmina inválida")
        a, b = acomodar_lamina(c.mlc_a, c.mlc_b, self.maquina.colim, int(par), banco, float(pos_mm))
        return self.editar_laminas(nome, a, b)

    def mover_jaw(self, nome: str, jaw: str, pos_mm: float) -> dict:
        self._exigir_plano()
        self.plano._exigir_rascunho()
        c = self._campo(nome)
        colim = self.maquina.colim
        pos = round(float(pos_mm), 1)
        if jaw in ("y1", "y2"):
            jy = list(c.jaw_y)
            k = 0 if jaw == "y1" else 1
            jy[k] = float(np.clip(pos, -colim.jaw_y_max_mm, colim.jaw_y_max_mm))
            if jy[1] - jy[0] < 5:
                raise ErroUsuario("Abertura Y menor que 5 mm")
            return self.editar_laminas(nome, c.mlc_a, c.mlc_b, None, jy)
        if jaw in ("x1", "x2") and colim.tem_jaw_x and c.jaw_x:
            jx = list(c.jaw_x)
            k = 0 if jaw == "x1" else 1
            jx[k] = float(np.clip(pos, -colim.jaw_x_max_mm, colim.jaw_x_max_mm))
            if jx[1] - jx[0] < 5:
                raise ErroUsuario("Abertura X menor que 5 mm")
            return self.editar_laminas(nome, c.mlc_a, c.mlc_b, jx, None)
        raise ErroUsuario("Jaw inválido")

    def desenhar_laminas(self, nome: str, pontos: list, modo: str = "abrir", encontro="fora") -> dict:
        """As lâminas e os jaws vão para a forma desenhada no BEV (como no Eclipse)."""
        from tps.planejamento.ajuste_mlc import laminas_para_desenho
        self._exigir_plano()
        self.plano._exigir_rascunho()
        c = self._campo(nome)
        try:
            r = laminas_para_desenho(c, self.maquina.colim, pontos, modo, encontro)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        out = self.editar_laminas(nome, r.campo.mlc_a, r.campo.mlc_b, r.campo.jaw_x, r.campo.jaw_y)
        self._registrar(f"{nome}: lâminas ajustadas a um desenho ({modo}, encontro {encontro})")
        out["avisos"] = r.avisos
        return out

    def remover_campo(self, nome: str):
        self._exigir_plano()
        self._remover_derivados()
        c = self._campo(nome)
        self.plano.campos.remove(c)
        self.doses_um.pop(nome, None)
        self.dose_atualizada = False

    # ================================================================== densidade, bolus, pontos
    def _aplicar_eta(self, manter_dose: bool = False):
        """η da grade = original do CT + bolus (água) + densidades forçadas. Toda mudança invalida a dose
        (manter_dose: só ao trazer de volta um plano guardado, cuja dose foi calculada com esse mesmo η)."""
        p = self.plano
        eta = self.eta_original.copy()
        for b in p.bolus:
            m = self.estruturas.get(b["nome"])
            if m is not None:
                eta[m.mascara] = 1.0
        for nome, val in p.densidades.items():
            if nome in self.estruturas:
                eta[self.estruturas[nome].mascara] = float(val)
        self.grade.eta = eta.astype(np.float32)
        if manter_dose:
            return
        self.doses_um, self.dose_fx, self.dose_atualizada = {}, None, False
        if p.estado == "RASCUNHO":                           # bolus muda a SSD; plano aprovado não é tocado
            for c in p.campos:
                c.ssd_mm = calculo.ssd_no_eixo(self.grade, c)

    def definir_densidade(self, estrutura: str, valor):
        self._exigir_plano()
        self.plano._exigir_rascunho()
        if estrutura not in self.estruturas:
            raise ErroUsuario(f"Estrutura {estrutura} não existe")
        if valor in (None, ""):
            self.plano.densidades.pop(estrutura, None)
        else:
            v = float(valor)
            if not 0.0 <= v <= 4.0:
                raise ErroUsuario("Densidade eletrônica relativa deve estar entre 0 e 4")
            self.plano.densidades[estrutura] = v
        self._aplicar_eta()
        self._registrar(f"Densidade de {estrutura}: {valor if valor not in (None, '') else 'do CT'}")

    def criar_bolus(self, espessura_mm: float, campos: list[str]) -> str:
        """Bolus virtual de água sobre a pele, na entrada dos campos escolhidos (projeção da abertura)."""
        self._exigir_plano()
        self.plano._exigir_rascunho()
        from scipy.ndimage import distance_transform_edt
        from tps.planejamento.um_tabela import _ponto_aberto
        esp = float(espessura_mm)
        if not 2.0 <= esp <= 30.0:
            raise ErroUsuario("Espessura do bolus entre 2 e 30 mm")
        g = self.grade
        corpo = self._corpo()
        h = np.array([abs(g.z[1] - g.z[0]), abs(g.y[1] - g.y[0]), abs(g.x[1] - g.x[0])])
        dist = distance_transform_edt(~corpo, sampling=h)
        casca = (~corpo) & (dist - h.min() / 2 <= esp + 1e-6)     # distância medida da superfície (meio voxel)
        kz, ky, kx = np.nonzero(casca)
        pts = np.column_stack([g.x[kx], g.y[ky], g.z[kz]])
        sel = np.zeros(len(pts), bool)
        from scipy.ndimage import map_coordinates
        cs = [self._campo(n) for n in campos] if campos else [c for c in self.plano.campos if not c.campo_pai]
        if not cs:
            raise ErroUsuario("Crie os campos antes do bolus")
        for c in cs:
            fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, "HFS")
            r = pts - fr.source
            prof = r @ fr.w
            u, v = 1000.0 * (r @ fr.u) / prof, 1000.0 * (r @ fr.v) / prof
            dentro = np.array([_ponto_aberto(c, self.maquina.colim, a, b) for a, b in zip(u, v)])
            # só o lado de entrada: entre a fonte e o ponto não há corpo
            idx = np.nonzero(dentro)[0]
            for k in idx:
                t = np.linspace(0.1, 0.97, 25)
                q = fr.source + np.outer(t, pts[k] - fr.source)
                val = map_coordinates(corpo.astype(np.float32), [(q[:, 2] - g.z[0]) / (g.z[1] - g.z[0]),
                                                                  (q[:, 1] - g.y[0]) / (g.y[1] - g.y[0]),
                                                                  (q[:, 0] - g.x[0]) / (g.x[1] - g.x[0])],
                                      order=0, mode="constant")
                if val.max() < 0.5:
                    sel[k] = True
        if not sel.any():
            raise ErroUsuario("O bolus ficou vazio: confira os campos")
        m = np.zeros_like(corpo)
        m[kz[sel], ky[sel], kx[sel]] = True
        nome = f"BOLUS_{esp:g}mm_{len(self.plano.bolus) + 1}"
        self.estruturas[nome] = Estrutura(nome, "BOLUS", (255, 170, 60), m)
        self.plano.bolus.append({"nome": nome, "espessura_mm": esp, "campos": [c.nome for c in cs],
                                 "volume_cc": round(float(m.sum()) * g.voxel_cc, 1)})
        self._aplicar_eta()
        self._registrar(f"Bolus {nome} em {', '.join(c.nome for c in cs)}")
        return nome

    def remover_bolus(self, nome: str):
        self._exigir_plano()
        self.plano._exigir_rascunho()
        self.plano.bolus = [b for b in self.plano.bolus if b["nome"] != nome]
        self.estruturas.pop(nome, None)
        self._aplicar_eta()

    # ---------------------------------------------------------------- estruturas derivadas
    def criar_estrutura(self, nome: str, operacao: str, a: str, b: str | None = None, margens=None,
                        contrair: bool = False, interno_mm: float = 0.0, externo_mm: float = 0.0,
                        nivel_gy: float | None = None, cor=None) -> dict:
        """Margem (simétrica/assimétrica, expandir ou contrair), união, interseção, subtração, anel ou isodose."""
        from tps.planejamento import estruturas as E
        g = self.grade
        if g is None:
            raise ErroUsuario("Carregue um CT primeiro")
        nome = (nome or "").strip()[:32]
        if not nome:
            raise ErroUsuario("Dê um nome à estrutura")
        if nome in self.estruturas:
            raise ErroUsuario(f"Já existe a estrutura {nome}")
        passo = (abs(g.z[1] - g.z[0]), abs(g.y[1] - g.y[0]), abs(g.x[1] - g.x[0]))
        if operacao != "isodose" and a not in self.estruturas:
            raise ErroUsuario(f"Estrutura {a} não existe")
        try:
            if operacao == "margem":
                m = E.margem(self.estruturas[a].mascara, passo, margens if margens is not None else 0.0, contrair)
            elif operacao in ("uniao", "intersecao", "subtracao", "xor"):
                if b not in self.estruturas:
                    raise ErroUsuario(f"Estrutura {b} não existe")
                m = E.booleana(self.estruturas[a].mascara, self.estruturas[b].mascara, operacao)
            elif operacao == "anel":
                m = E.anel(self.estruturas[a].mascara, passo, float(interno_mm), float(externo_mm))
                m &= self._corpo()
            elif operacao == "isodose":
                self._exigir(self.dose_atualizada, "Calcule a dose antes de criar estrutura de isodose")
                total = self.dose_fx * self.plano.prescricao.fracoes
                m = E.de_isodose(total, float(nivel_gy), self._corpo())
            else:
                raise ErroUsuario(f"Operação desconhecida: {operacao}")
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        if not m.any():
            raise ErroUsuario("A estrutura resultante ficou vazia")
        paleta = [(255, 0, 255), (0, 220, 220), (255, 140, 0), (150, 120, 255), (120, 220, 60), (255, 90, 140),
                  (80, 160, 255), (230, 200, 60)]
        cor = tuple(cor) if cor else ((255, 255, 0) if operacao == "isodose" else paleta[len(self.derivadas) % len(paleta)])
        self.estruturas[nome] = Estrutura(nome, "DERIVADA", cor, m,
                                          self._contornos_de_mascara(m) if self.tipo == "ct" else [])
        self.derivadas[nome] = {"operacao": operacao, "a": a, "b": b, "margens": margens, "contrair": contrair,
                                "interno_mm": interno_mm, "externo_mm": externo_mm, "nivel_gy": nivel_gy}
        self._atualizar_avaliacao()
        self._registrar(f"Estrutura {nome} criada ({operacao} de {a}{' e ' + b if b else ''})")
        return {"nome": nome, "volume_cc": round(float(m.sum()) * g.voxel_cc, 1)}

    # ---------------------------------------------------------------- ferramentas de contorno
    def _contornos_de_mascara(self, mascara: np.ndarray) -> list:
        """Polígonos em cada corte do CT a partir de uma máscara na grade de dose (estruturas derivadas)."""
        from scipy.interpolate import RegularGridInterpolator
        from tps.planejamento.contorno import poligonos_de_mascara
        g = self.grade
        _cx, _cy, cz = self.ct.axes()
        xs = np.arange(g.x[0], g.x[-1] + 1e-6, min(1.0, float(g.x[1] - g.x[0]) / 2))
        ys = np.arange(g.y[0], g.y[-1] + 1e-6, min(1.0, float(g.y[1] - g.y[0]) / 2))
        it = RegularGridInterpolator((g.z, g.y, g.x), mascara.astype(np.float32), bounds_error=False, fill_value=0.0)
        Y, X = np.meshgrid(ys, xs, indexing="ij")
        out = []
        dz = abs(float(g.z[1] - g.z[0])) / 2
        for z in cz:
            if z < g.z[0] - dz or z > g.z[-1] + dz:
                continue
            f = it(np.stack([np.full_like(X, z), Y, X], -1)) >= 0.5
            if f.any():
                out += poligonos_de_mascara(f, xs, ys, float(z))
        return out

    def _rasterizar(self, e: Estrutura):
        from tps.dicom_io.rtstruct import Structure
        g = self.grade
        e.mascara = rasterize(Structure(0, e.nome, e.tipo, list(e.contornos), e.cor), g.x, g.y, g.z) \
            if e.contornos else np.zeros(g.eta.shape, bool)

    def nova_estrutura(self, nome: str, tipo: str = "ORGAN", cor=None) -> dict:
        self._exigir(self.tipo == "ct", "Contorno só em CT de paciente")
        nome = (nome or "").strip()[:32]
        if not nome:
            raise ErroUsuario("Dê um nome à estrutura")
        if nome in self.estruturas:
            raise ErroUsuario(f"Já existe a estrutura {nome}")
        cor = tuple(cor) if cor else PALETA[len(self.estruturas) % len(PALETA)]
        self.estruturas[nome] = Estrutura(nome, (tipo or "ORGAN").upper(), cor, np.zeros(self.grade.eta.shape, bool), [])
        self.editadas.add(nome)
        self._registrar(f"Estrutura nova {nome} ({tipo})")
        return {"nome": nome}

    def _editavel(self, nome: str) -> Estrutura:
        e = self.estruturas.get(nome)
        if e is None:
            raise ErroUsuario(f"Estrutura {nome} não existe")
        if e.tipo == "BOLUS":
            raise ErroUsuario("O bolus é editado na seção Bolus")
        if not e.contornos and e.mascara.any():
            e.contornos = self._contornos_de_mascara(e.mascara)   # derivada: passa a ter contorno editável
        return e

    def editar_contorno(self, nome: str, idx: int, ferramenta: str, pontos=None, raio_mm: float = 5.0,
                        apagar: bool = False, origem: int | None = None) -> dict:
        """Edita o contorno no corte axial idx do CT: pincel, poligono, limpar (o corte) ou copiar (de outro corte)."""
        from tps.planejamento import contorno as K
        e = self._editavel(nome)
        cx, cy, cz = self.ct.axes()
        if not 0 <= int(idx) < len(cz):
            raise ErroUsuario("Corte fora do CT")
        z = float(cz[int(idx)])
        tol = float(self.ct.spacing[2]) / 2 + 0.01
        atuais = K.poligonos_do_corte(e.contornos, z, tol)
        m = K.mascara_de_poligonos(atuais, cx, cy)
        try:
            if ferramenta == "pincel":
                m = K.pincel(m, cx, cy, pontos or [], float(raio_mm), bool(apagar))
            elif ferramenta == "poligono":
                m = K.poligono(m, cx, cy, pontos or [], bool(apagar))
            elif ferramenta == "limpar":
                m = np.zeros_like(m)
            elif ferramenta == "copiar":
                if origem is None or not 0 <= int(origem) < len(cz):
                    raise ErroUsuario("Corte de origem inválido")
                m = K.mascara_de_poligonos(K.poligonos_do_corte(e.contornos, float(cz[int(origem)]), tol), cx, cy)
            else:
                raise ErroUsuario(f"Ferramenta desconhecida: {ferramenta}")
        except ValueError as ex:
            raise ErroUsuario(str(ex)) from ex
        self._guardar_desfazer(e)
        e.contornos = [c for c in e.contornos if abs(float(c[0, 2]) - z) > tol] + K.poligonos_de_mascara(m, cx, cy, z)
        self._depois_de_editar(e)
        return {"nome": nome, "corte": int(idx), "volume_cc": round(float(e.mascara.sum()) * self.grade.voxel_cc, 1)}

    def interpolar_contorno(self, nome: str) -> dict:
        """Preenche os cortes sem contorno entre cortes contornados (distância com sinal)."""
        from tps.planejamento import contorno as K
        e = self._editavel(nome)
        cx, cy, cz = self.ct.axes()
        tol = float(self.ct.spacing[2]) / 2 + 0.01
        cortes = {}
        for k, z in enumerate(cz):
            pol = K.poligonos_do_corte(e.contornos, float(z), tol)
            if pol:
                cortes[k] = K.mascara_de_poligonos(pol, cx, cy)
        if len(cortes) < 2:
            raise ErroUsuario("Contorne pelo menos dois cortes para interpolar")
        novos = K.interpolar(cortes, cz)
        if not novos:
            return {"nome": nome, "novos_cortes": 0}
        self._guardar_desfazer(e)
        for k, m in novos.items():
            e.contornos += K.poligonos_de_mascara(m, cx, cy, float(cz[k]))
        self._depois_de_editar(e)
        return {"nome": nome, "novos_cortes": len(novos)}

    def corpo_automatico(self, nome: str = "BODY", limiar_hu: float = -350.0) -> dict:
        """Contorno externo automático em todos os cortes (limiar de HU + maior componente + buracos preenchidos)."""
        from tps.planejamento import contorno as K
        self._exigir(self.tipo == "ct", "Contorno só em CT de paciente")
        if nome not in self.estruturas:
            self.nova_estrutura(nome, "EXTERNAL", (0, 200, 0))
        e = self._editavel(nome)
        cx, cy, cz = self.ct.axes()
        self._guardar_desfazer(e)
        corpo = K.corpo_3d(self.ct.hu, float(limiar_hu), (float(self.ct.spacing[2]), float(self.ct.spacing[1]),
                                                          float(self.ct.spacing[0])))
        polys, vazios = [], 0
        for k, z in enumerate(cz):
            if not corpo[k].any():
                vazios += 1
                continue
            polys += K.sem_aninhados(K.poligonos_de_mascara(corpo[k], cx, cy, float(z), suavizar=0.7, min_pontos=8))
        e.contornos = polys
        self._depois_de_editar(e)
        return {"nome": nome, "volume_cc": round(float(e.mascara.sum()) * self.grade.voxel_cc, 1),
                "cortes_sem_corpo": vazios}

    def operar_contorno(self, nome: str, operacao: str, outra: str, margem_mm: float = 0.0) -> dict:
        """Booleana no lugar, na resolução do CT: nome = nome − outra (subtrair), ∪ (unir) ou ∩ (intersecao), com
        margem opcional em volta da outra (ex.: RETO − (PTV + 3 mm)). Desfazível."""
        from tps.planejamento import contorno as K
        from tps.planejamento import estruturas as E
        if operacao not in ("subtrair", "unir", "intersecao"):
            raise ErroUsuario(f"Operação desconhecida: {operacao}")
        if outra == nome:
            raise ErroUsuario("Escolha outra estrutura")
        e = self._editavel(nome)
        ob = self.estruturas.get(outra)
        if ob is None:
            raise ErroUsuario(f"Estrutura {outra} não existe")
        pb = list(ob.contornos) if ob.contornos else self._contornos_de_mascara(ob.mascara)
        cx, cy, cz = self.ct.axes()
        tol = float(self.ct.spacing[2]) / 2 + 0.01
        dz = float(abs(cz[1] - cz[0])) if len(cz) > 1 else 3.0
        dy, dx = float(abs(cy[1] - cy[0])), float(abs(cx[1] - cx[0]))
        todos = [c for c in list(e.contornos) + pb]
        if not todos:
            raise ErroUsuario("As duas estruturas estão vazias")
        pts = np.vstack([c[:, :3] for c in todos])
        mg = max(float(margem_mm), 0.0)
        x0, x1 = pts[:, 0].min() - mg - 3 * dx, pts[:, 0].max() + mg + 3 * dx
        y0, y1 = pts[:, 1].min() - mg - 3 * dy, pts[:, 1].max() + mg + 3 * dy
        ix = np.nonzero((cx >= x0) & (cx <= x1))[0]
        iy = np.nonzero((cy >= y0) & (cy <= y1))[0]
        xs, ys = cx[ix], cy[iy]
        A = np.zeros((len(cz), len(ys), len(xs)), bool)
        B = np.zeros_like(A)
        for k, z in enumerate(cz):
            A[k] = K.mascara_de_poligonos(K.poligonos_do_corte(e.contornos, float(z), tol), xs, ys)
            B[k] = K.mascara_de_poligonos(K.poligonos_do_corte(pb, float(z), tol), xs, ys)
        if mg > 0:
            try:
                B = E.margem(B, (dz, dy, dx), mg)
            except ValueError as ex:
                raise ErroUsuario(str(ex)) from ex
        R = A & ~B if operacao == "subtrair" else (A | B if operacao == "unir" else A & B)
        self._guardar_desfazer(e)
        novos = []
        for k, z in enumerate(cz):
            if R[k].any():
                novos += K.poligonos_de_mascara(R[k], xs, ys, float(z))
        e.contornos = novos
        self._depois_de_editar(e)
        txt = {"subtrair": "−", "unir": "∪", "intersecao": "∩"}[operacao]
        self._registrar(f"{nome} = {nome} {txt} {outra}{f' (+{mg:g} mm)' if mg else ''}")
        return {"nome": nome, "volume_cc": round(float(e.mascara.sum()) * self.grade.voxel_cc, 1)}

    def pos_processar_contorno(self, nome: str, suavizar_mm: float = 2.0, fechar_mm: float = 0.0, abrir_mm: float = 0.0,
                               preencher: bool = True, ilhas_cc: float = 0.5, so_maior: bool = False) -> dict:
        """Suavizar (3D, preservando o volume), fechar/abrir, preencher buracos, remover ilhas e manter só a maior
        parte — na resolução do CT. Desfazível. SRS-CNT-020."""
        from tps.planejamento import contorno as K
        from tps.planejamento import suavizacao as S
        e = self._editavel(nome)
        if not e.contornos:
            raise ErroUsuario(f"{nome} está vazia")
        for v, n in ((suavizar_mm, "suavização"), (fechar_mm, "fechamento"), (abrir_mm, "abertura")):
            if not 0 <= float(v) <= 15:
                raise ErroUsuario(f"{n}: use de 0 a 15 mm")
        cx, cy, cz = self.ct.axes()
        tol = float(self.ct.spacing[2]) / 2 + 0.01
        dz = float(abs(cz[1] - cz[0])) if len(cz) > 1 else 3.0
        dy, dx = float(abs(cy[1] - cy[0])), float(abs(cx[1] - cx[0]))
        pts = np.vstack([c[:, :3] for c in e.contornos])
        folga = 3 * max(suavizar_mm, fechar_mm, 1.0) + 3 * max(dx, dy)
        ix = np.nonzero((cx >= pts[:, 0].min() - folga) & (cx <= pts[:, 0].max() + folga))[0]
        iy = np.nonzero((cy >= pts[:, 1].min() - folga) & (cy <= pts[:, 1].max() + folga))[0]
        xs, ys = cx[ix], cy[iy]
        A = np.stack([K.mascara_de_poligonos(K.poligonos_do_corte(e.contornos, float(z), tol), xs, ys) for z in cz])
        vol0 = float(A.sum()) * dx * dy * dz / 1000.0
        try:
            campo, nivel = S.processar(A, (dz, dy, dx), dx * dy * dz / 1000.0, float(suavizar_mm), float(fechar_mm),
                                       float(abrir_mm), bool(preencher), float(ilhas_cc), bool(so_maior))
        except ValueError as ex:
            raise ErroUsuario(str(ex)) from ex
        novos = []
        for k, z in enumerate(cz):
            novos += S.poligonos(campo[k], nivel, xs, ys, float(z))
        if not novos:
            raise ErroUsuario("A estrutura ficou vazia com essas opções")
        self._guardar_desfazer(e)
        e.contornos = novos
        self._depois_de_editar(e)
        vol1 = float(e.mascara.sum()) * self.grade.voxel_cc
        ops = [f"suavizar {suavizar_mm:g} mm" if suavizar_mm else "", f"fechar {fechar_mm:g} mm" if fechar_mm else "",
               f"abrir {abrir_mm:g} mm" if abrir_mm else "", "preencher" if preencher else "",
               f"ilhas < {ilhas_cc:g} cm³" if ilhas_cc else "", "só a maior parte" if so_maior else ""]
        self._registrar(f"{nome}: pós-processamento ({', '.join(o for o in ops if o)})")
        return {"nome": nome, "volume_antes_cc": round(vol0, 1), "volume_cc": round(vol1, 1),
                "cortes": len({round(float(c[0, 2]), 2) for c in novos})}

    def _estrutura_de_volume(self, nome: str, tipo: str, cor, vol: np.ndarray, sobrescrever: bool = False,
                             suavizar_mm: float = 0.0) -> str:
        """Cria (ou substitui, se foi criada no TPS) uma estrutura a partir de uma máscara na grade do CT."""
        from tps.planejamento import contorno as K
        cx, cy, cz = self.ct.axes()
        if nome in self.estruturas and not (sobrescrever and nome in self.editadas):
            base, k = nome, 2
            while nome in self.estruturas:
                nome = f"{base}_{k}"
                k += 1
        if nome not in self.estruturas:
            self.nova_estrutura(nome, tipo, cor)
        e = self.estruturas[nome]
        self._guardar_desfazer(e)
        polys = []
        if suavizar_mm > 0:                              # contorno automático: suaviza em 3D (sem "degraus")
            from tps.planejamento import suavizacao as S
            dz = float(abs(cz[1] - cz[0])) if len(cz) > 1 else 3.0
            passo = (dz, float(abs(cy[1] - cy[0])), float(abs(cx[1] - cx[0])))
            m = S.preencher(vol)
            m = S.remover_ilhas(m, 0.3, passo[0] * passo[1] * passo[2] / 1000.0)
            campo, nivel = S.campo_suave(m, passo, suavizar_mm)
            for k, z in enumerate(cz):
                polys += S.poligonos(campo[k], nivel, cx, cy, float(z))
        else:
            for k, z in enumerate(cz):
                if vol[k].any():
                    polys += K.poligonos_de_mascara(vol[k], cx, cy, float(z), suavizar=0.4, min_pontos=6)
        e.contornos = polys
        self._depois_de_editar(e)
        return nome

    def _corpo_ct(self) -> np.ndarray:
        from tps.planejamento import contorno as K
        cx, cy, cz = self.ct.axes()
        for e in self.estruturas.values():
            if (e.tipo.upper() == "EXTERNAL" or e.nome.lower() in ("body", "external", "corpo", "externo")) \
                    and e.contornos:
                tol = float(self.ct.spacing[2]) / 2 + 0.01
                return np.stack([K.mascara_de_poligonos(K.poligonos_do_corte(e.contornos, float(z), tol), cx, cy)
                                 for z in cz])
        return K.corpo_3d(self.ct.hu)

    def orgaos_automaticos(self, orgaos: list[str]) -> dict:
        """Órgãos por limiar de HU (rápidos, sem IA): pulmões (E/D) e ossos. Os outros vêm da segmentação por IA."""
        from tps.planejamento import contorno as K
        self._exigir(self.tipo == "ct", "Contorno automático só em CT de paciente")
        cx, cy, cz = self.ct.axes()
        dz = float(abs(cz[1] - cz[0])) if len(cz) > 1 else 3.0
        corpo = self._corpo_ct()
        criadas = {}
        if "pulmoes" in orgaos:
            esq, dire = K.pulmoes_automaticos(self.ct.hu, corpo, cx, cy, dz)
            if not (esq.any() or dire.any()):
                raise ErroUsuario("Não encontrei pulmões neste CT")
            if esq.any():
                criadas["Pulmao_E"] = self._estrutura_de_volume("Pulmao_E", "ORGAN", (90, 160, 255), esq, True)
            if dire.any():
                criadas["Pulmao_D"] = self._estrutura_de_volume("Pulmao_D", "ORGAN", (60, 220, 255), dire, True)
        if "ossos" in orgaos:
            o = K.ossos_automaticos(self.ct.hu, corpo)
            criadas["Ossos"] = self._estrutura_de_volume("Ossos", "ORGAN", (240, 240, 200), o, True)
        if not criadas:
            raise ErroUsuario("Escolha pelo menos um órgão")
        self._registrar("Contorno automático (limiar de HU): " + ", ".join(criadas.values()) + " — revise antes de usar")
        return {"criadas": list(criadas.values()),
                "volumes_cc": {n: round(float(self.estruturas[n].mascara.sum()) * self.grade.voxel_cc, 1)
                               for n in criadas.values()}}

    def segmentar_ia(self, orgaos: list[str], reto_zmax_mm: float | None = None, suavizar_mm: float = 2.0) -> dict:
        """Órgãos por IA (TotalSegmentator local) e os derivados dela (reto, parede retal, canal anal, bowel bag,
        mama de cada lado, cabeças femorais). Cria/substitui só estruturas do TPS; o RTSTRUCT original não muda."""
        from tps.ia import posprocessamento as P
        from tps.ia import segmentacao as IA
        from tps.planejamento import contorno as K
        self._exigir(self.tipo == "ct", "Contorno automático só em CT de paciente")
        if not orgaos:
            raise ErroUsuario("Escolha pelo menos um órgão")
        pedidos = list(orgaos)
        if "bowel_bag" in pedidos:                      # a bolsa exclui bexiga e reto: segmenta junto
            pedidos += [o for o in ("bexiga", "reto") if o not in pedidos]
        arquivos = sorted(f for f in self.pasta_ct.iterdir() if f.is_file() and self._e_ct(f))
        self.progresso = f"Segmentação automática rodando ({len(pedidos)} órgão(s); na CPU leva alguns minutos)…"
        try:
            classes = IA.segmentar(arquivos, pedidos)
        except (RuntimeError, ValueError, OSError) as e:
            raise ErroUsuario(str(e)) from e
        except Exception as e:  # noqa: BLE001  (timeout etc.)
            raise ErroUsuario(f"Segmentação por IA falhou: {e}") from e
        classes = {k.lower(): v for k, v in classes.items()}
        cx, cy, cz = self.ct.axes()
        tol = float(self.ct.spacing[2]) / 2 + 0.01
        cache: dict[str, np.ndarray] = {}

        def vol_de(cls):
            chave = ",".join(cls)
            if chave not in cache:
                polys = [c for k in cls for c in classes.get(k, [])]
                cache[chave] = (np.stack([K.mascara_de_poligonos(K.poligonos_do_corte(polys, float(z), tol), cx, cy)
                                          for z in cz]) if polys else None)
            return cache[chave]

        corpo = None
        reto, z_reto = None, None
        vols: dict[str, np.ndarray] = {}
        for o in pedidos:
            nome, _tarefa, cls, cor, pos = IA.ORGAOS[o]
            v = vol_de(cls)
            if v is None or not v.any():
                continue
            if pos in ("reto", "parede_retal", "canal_anal"):
                if reto is None:
                    reto, z_reto = P.reto_do_colon(v, cx, cy, cz, reto_zmax_mm)
                v = {"reto": reto, "parede_retal": P.parede(reto, cx, cy, IA.PAREDE_RETAL_MM),
                     "canal_anal": P.inferior(reto, cz, IA.CANAL_ANAL_MM)}[pos]
            elif pos == "cabeca_femoral":             # só cabeça e colo: os 7 cm superiores do fêmur
                ks = np.nonzero(v.any(axis=(1, 2)))[0]
                v = v.copy()
                v[np.abs(cz - max(cz[ks])) > IA.CABECA_FEMORAL_MM] = False
            elif pos in ("lado_e", "lado_d"):
                corpo = self._corpo_ct() if corpo is None else corpo
                v = P.lado(v, corpo, cx, pos == "lado_e")
            elif pos == "bowel_bag":
                osso = self.ct.hu > 200
                ztopo = None
                pr = self.plano.prescricao.estrutura_alvo if self.plano else None
                alvo = self.estruturas.get(pr) if pr else next((e for n, e in self.estruturas.items()
                                                                  if n.upper().startswith("PTV")), None)
                if alvo is not None and alvo.contornos:
                    ztopo = max(float(c[0, 2]) for c in alvo.contornos)
                self.progresso = "IA: montando a bolsa intestinal (envelope das alças)…"
                passo = (float(abs(cz[1] - cz[0])) if len(cz) > 1 else 3.0, float(abs(cy[1] - cy[0])),
                         float(abs(cx[1] - cx[0])))
                v = P.envelope_alcas(v, passo)
                v = P.bowel_bag(v, cz, [vol_de(["urinary_bladder"]), reto, osso], ztopo,
                                IA.BOWEL_BAG_ACIMA_DO_ALVO_MM)
            vols[o] = v
        criadas, faltam = [], []
        for o in orgaos:
            nome, _t, _c, cor, _p = IA.ORGAOS[o]
            v = vols.get(o)
            if v is None or not v.any():
                faltam.append(nome)
                continue
            self.progresso = f"IA: contornando {nome}…"
            try:
                criadas.append(self._estrutura_de_volume(nome, "ORGAN", cor, v, True, float(suavizar_mm)))
            except MemoryError:                      # suavização 3D sem memória: contorno sem suavizar
                criadas.append(self._estrutura_de_volume(nome, "ORGAN", cor, v, True, 0.0))
        self.progresso = None
        if not criadas:
            raise ErroUsuario("A IA não encontrou nenhum dos órgãos pedidos neste CT")
        self._registrar("Segmentação automática: " + ", ".join(criadas) + " — revise antes de usar")
        out = {"criadas": criadas, "nao_encontrados": faltam}
        if z_reto is not None:
            out["reto_limite_superior_mm"] = round(z_reto, 1)
        return out

    def desfazer_contorno(self) -> dict:
        if not self.desfazer_pilha:
            raise ErroUsuario("Nada para desfazer")
        nome, antes = self.desfazer_pilha.pop()
        e = self.estruturas.get(nome)
        if e is None:
            raise ErroUsuario(f"Estrutura {nome} não existe mais")
        e.contornos = antes
        self._depois_de_editar(e, registrar=False)
        return {"nome": nome}

    def _guardar_desfazer(self, e: Estrutura):
        self.desfazer_pilha.append((e.nome, [c.copy() for c in e.contornos]))
        del self.desfazer_pilha[:-30]

    def _depois_de_editar(self, e: Estrutura, registrar: bool = True):
        self._rasterizar(e)
        self.editadas.add(e.nome)
        if e.nome in self.derivadas:                   # editada à mão: deixa de ser derivada
            self.derivadas.pop(e.nome)
            e.tipo = "ORGAN" if not e.nome.upper().startswith("PTV") else "PTV"
        if e.tipo.upper() == "EXTERNAL":
            self._mascarar_fora_do_corpo()
        elif self.plano and e.nome in (self.plano.densidades or {}):
            self._aplicar_eta()
        self._atualizar_avaliacao()

    def remover_estrutura(self, nome: str):
        if nome not in self.estruturas:
            raise ErroUsuario(f"Estrutura {nome} não existe")
        if self.plano:
            usos = [c.nome for c in self.plano.campos if (c.__dict__.get("_spec") or {}).get("alvo") == nome]
            if usos or self.plano.prescricao.estrutura_alvo == nome:
                raise ErroUsuario(f"A estrutura {nome} é usada pelo plano ({', '.join(usos) or 'prescrição'})")
        if self.plano and self.estruturas[nome].tipo.upper() == "EXTERNAL":
            raise ErroUsuario(f"{nome} é o contorno externo do plano: refaça-o (BODY automático) em vez de apagar")
        dens = bool(self.plano and nome in (self.plano.densidades or {}))
        if dens:
            self.plano.densidades.pop(nome, None)
        self.estruturas.pop(nome, None)
        self.derivadas.pop(nome, None)
        self.editadas.discard(nome)
        if dens:
            self._aplicar_eta()
        self._registrar(f"Estrutura {nome} apagada")
        if self.dose_atualizada and self.resultado:
            self.resultado["dvh"].pop(nome, None)
            self._atualizar_avaliacao()

    def _atualizar_avaliacao(self):
        """Estrutura nova ou removida: atualiza DVH e restrições sem recalcular a dose."""
        if not (self.dose_atualizada and self.resultado and self.plano):
            return
        total = self.dose_fx * self.plano.prescricao.fracoes
        g = self.grade
        self.resultado["dvh"] = {n: dvh(total, e.mascara, g.voxel_cc) for n, e in self.estruturas.items()
                                 if e.mascara.any()}
        self.resultado["restricoes"] = self._avaliar_restricoes(total)

    def _mascarar_fora_do_corpo(self):
        """Como o Monaco e o Eclipse: fora do contorno externo é ar (tira a mesa do tomógrafo, travesseiros…).
        A mesa de tratamento, quando modelada, entra como estrutura com densidade."""
        if self.plano is not None and self.plano.estado != "RASCUNHO":
            return                                   # plano aprovado: a densidade usada fica como estava
        ext = next((e for e in self.estruturas.values() if e.tipo.upper() == "EXTERNAL"), None)
        if ext is None or not ext.mascara.any() or getattr(self, "eta_ct", None) is None:
            self.fora_do_corpo = False
            return
        self.grade.eta = np.where(ext.mascara, self.eta_ct, 0.0).astype(self.eta_ct.dtype)
        self.fora_do_corpo = ext.nome
        if self.plano is not None and self.plano.estado == "RASCUNHO":
            self.eta_original = self.grade.eta.copy()
            self._aplicar_eta()

    def _corpo(self) -> np.ndarray:
        g = self.grade
        for e in self.estruturas.values():
            if e.tipo.upper() == "EXTERNAL" or e.nome.lower() in ("body", "external", "corpo", "externo"):
                return e.mascara
        return self.eta_original > 0.3 if hasattr(self, "eta_original") else g.eta > 0.3

    def adicionar_ponto(self, nome: str, xyz):
        self._exigir_plano()
        nome = (nome or f"P{len(self.plano.pontos) + 1}").strip()[:16]
        if any(p["nome"] == nome for p in self.plano.pontos):
            raise ErroUsuario(f"Já existe o ponto {nome}")
        self.plano.pontos.append({"nome": nome, "xyz": [round(float(v), 1) for v in xyz]})

    def remover_ponto(self, nome: str):
        self._exigir_plano()
        self.plano.pontos = [p for p in self.plano.pontos if p["nome"] != nome]

    def dose_em(self, xyz) -> dict:
        """Dose total e por campo (Gy, plano completo) num ponto."""
        self._exigir(self.dose_atualizada, "Calcule a dose antes")
        p, g = self.plano, self.grade
        xyz = [float(v) for v in xyz]
        dentro = all(a[0] - 1e-6 <= v <= a[-1] + 1e-6 for v, a in zip(xyz, (g.x, g.y, g.z)))
        if not dentro:
            return {"xyz": xyz, "total_gy": None, "campos": {}}
        n = p.prescricao.fracoes
        campos = {c.nome: round(calculo.valor_em_ponto(self.doses_um[c.nome], g, xyz) * c.mu * n, 3) for c in p.campos}
        tot = calculo.valor_em_ponto(self.dose_fx, g, xyz) * n
        return {"xyz": xyz, "total_gy": round(tot, 3), "pct": round(tot / p.prescricao.dose_total_gy * 100, 1),
                "campos": campos}

    # ================================================================== cálculo
    def calcular(self, opcoes: dict) -> dict:
        from tps.diagnostico import registrar
        registrar(f"[{self.nome or self.id}] cálculo: plano {getattr(self.plano, 'rotulo', '?')} "
                  f"({getattr(self.plano, 'tecnica', '?')}), opções {sorted((opcoes or {}).keys())}")
        try:
            r = self._calcular(opcoes)
        except MemoryError as e:
            registrar(f"[{self.nome or self.id}] cálculo: memória insuficiente")
            raise ErroUsuario("Memória insuficiente para este cálculo: feche outros programas ou use uma grade de "
                              "dose maior (3–4 mm)") from e
        registrar(f"[{self.nome or self.id}] cálculo concluído")
        return r

    def _calcular(self, opcoes: dict) -> dict:
        self._exigir_plano()
        p, m, g = self.plano, self.maquina, self.grade
        self._exigir(bool(p.campos), "Plano sem campos")
        if p.estado == "APROVADO":
            return self._recalcular_aprovado()
        p._exigir_rascunho()
        pr = p.prescricao
        so_normalizar = bool(opcoes.get("so_normalizar"))
        if so_normalizar:
            self._exigir(bool(self.doses_um) and all(c.nome in self.doses_um for c in p.campos),
                         "Calcule a dose antes de renormalizar")
            opcoes = {k: v for k, v in opcoes.items() if k not in ("otimizar_pesos", "field_in_field", "compensador",
                                                                    "proteger_oars", "imrt", "vmat")}
        if opcoes.get("metodo"):
            pr.metodo = opcoes["metodo"]
            if pr.metodo == "ponto":
                pr.ponto_mm = tuple(float(v) for v in opcoes.get("ponto") or self.isocentro)
        if opcoes.get("isodose_pct") is not None:
            pr.isodose_pct = float(opcoes["isodose_pct"])
        if pr.metodo == "cobertura":
            pr.cobertura_volume_pct = float(opcoes.get("cobertura_volume_pct") or pr.cobertura_volume_pct or 95.0)
            pr.cobertura_dose_pct = float(opcoes.get("cobertura_dose_pct") or pr.cobertura_dose_pct or 100.0)
        erros = pr.validar()
        if erros:
            raise ErroUsuario("; ".join(erros))
        alvo = self.estruturas[pr.estrutura_alvo].mascara if pr.estrutura_alvo in self.estruturas else None
        if pr.metodo in ("media_alvo", "cobertura") and alvo is None:
            raise ErroUsuario("Normalização pelo alvo sem estrutura alvo")
        manter_imrt = p.tecnica == "IMRT" and not opcoes.get("imrt")   # recálculo de um IMRT pronto: mesmos segmentos
        arcos = [c for c in p.campos if c.e_arco]
        manter_vmat = bool(arcos) and not opcoes.get("vmat")             # idem para o VMAT: mesmos pontos de controle
        if manter_vmat and not all(c.pontos_controle for c in arcos):
            if p.tecnica == "DCA":
                raise ErroUsuario("Arcos conformacionais alterados: recrie os arcos (Criar arcos conformacionais)")
            raise ErroUsuario("Plano com arcos: otimize o VMAT (os arcos ainda não têm pontos de controle)")
        if opcoes.get("vmat") and not arcos:
            raise ErroUsuario("Crie os arcos antes de otimizar o VMAT")
        if pr.metodo != "um_fixas" and not so_normalizar and not manter_imrt and not manter_vmat:
            self._remover_derivados()                           # importado: segmentos e UM são os do plano original
        p.modelo_feixe = self._info_modelo()
        self.avisos = []
        algoritmo = self._algoritmo(opcoes)
        if not so_normalizar:
            self._restaurar_ccc()                               # os otimizadores trabalham com o collapsed cone
        try:
            faltam = [c for c in p.campos if c.nome not in self.doses_um and not (c.e_arco and not c.pontos_controle)]
            if faltam:
                self.doses_um.update(calculo.dose_por_um(g, faltam, m.colim, m.modelo))
            for n in list(self.doses_um):
                if n not in {c.nome for c in p.campos}:
                    self.doses_um.pop(n)
            info = {}
            if opcoes.get("otimizar_pesos") and alvo is not None and len(p.campos) > 1:
                oars = [(self.estruturas[n].mascara, float(i)) for n, i in (opcoes.get("oars") or {}).items()
                        if n in self.estruturas]
                # DCA: nenhum arco abaixo de 30% do maior (UM por grau mínima da máquina e entrega estável)
                w = otimizar_pesos(self.doses_um, alvo, oars, peso_min_rel=0.3 if p.tecnica == "DCA" else 0.05)
                for c in p.campos:
                    c.peso = round(w[c.nome], 4)
            corpo = self._corpo()
            fif = opcoes.get("field_in_field")
            if fif and alvo is not None:
                for c in p.campos:
                    c.__dict__["_peso_antes_fif"] = c.peso
            if fif and alvo is not None and isinstance(fif, dict) and fif.get("modo") == "filtro":
                from tps.planejamento.otimizacao3d import field_in_field_filtro
                r = field_in_field_filtro(p, g, self.doses_um, alvo, corpo, m.colim, m.modelo,
                                          niveis_pct=[float(x) for x in (fif.get("niveis") or (97, 94, 91, 88))],
                                          peso_pct=float(fif.get("peso_pct", 3.0)),
                                          campos_base=fif.get("campos") or None)
                info["field_in_field"] = {"modo": "filtro", "subcampos": [c.nome for c in r.subcampos],
                                          "hi_antes": r.hi_antes, "hi_depois": r.hi_depois,
                                          "dmax_antes": r.dmax_antes_pct, "dmax_depois": r.dmax_depois_pct, "avisos": r.avisos}
                self.avisos = list(r.avisos)
                if fif.get("ajuste_fino") and r.subcampos:
                    from tps.planejamento.protecao_oar import ajuste_fino
                    h0, h1, av = ajuste_fino(p, g, self.doses_um, alvo, corpo)
                    info["field_in_field"]["ajuste_fino"] = {"hi_antes": h0, "hi_depois": h1}
                    info["field_in_field"]["hi_depois"] = h1
                    info["field_in_field"]["subcampos"] = [c.nome for c in p.campos if c.campo_pai]
                    self.avisos += av
            elif fif and alvo is not None:
                r = field_in_field(p, g, self.doses_um, alvo, corpo, m.colim, m.modelo,
                                   um_min=float(opcoes.get("um_min", 3.0)))
                info["field_in_field"] = {"subcampos": [c.nome for c in r.subcampos], "hi_antes": r.hi_antes,
                                          "hi_depois": r.hi_depois, "avisos": r.avisos}
                self.avisos = list(r.avisos)
            if p.tecnica == "DCA" and (fif or opcoes.get("compensador") or opcoes.get("proteger_oars") or
                                       opcoes.get("imrt")):
                raise ErroUsuario("Arco conformacional: field-in-field, compensador, proteção de OARs e IMRT não se "
                                  "aplicam (use o bloqueio de órgão ao criar os arcos)")
            vm = opcoes.get("vmat")
            if vm:
                if fif or opcoes.get("compensador") or opcoes.get("proteger_oars") or opcoes.get("imrt") or \
                        opcoes.get("otimizar_pesos"):
                    raise ErroUsuario("VMAT não se combina com field-in-field, compensador, proteção de OARs ou IMRT")
                info["vmat"] = self._otimizar_vmat(vm, corpo)
            im = opcoes.get("imrt")
            if im:
                if fif or opcoes.get("compensador") or opcoes.get("proteger_oars"):
                    raise ErroUsuario("IMRT não se combina com field-in-field, compensador ou proteção de OARs")
                info["imrt"] = self._otimizar_imrt(im, corpo)
            prot = opcoes.get("proteger_oars")
            if prot and alvo is not None:
                if opcoes.get("compensador"):
                    raise ErroUsuario("Use a proteção de OARs ou o compensador, não os dois no mesmo cálculo")
                from tps.planejamento.protecao_oar import ObjetivoOAR, proteger_oars
                objs = []
                for o in prot.get("oars") or []:
                    e = self.estruturas.get(o.get("estrutura"))
                    if e is None:
                        raise ErroUsuario(f"Estrutura {o.get('estrutura')} não existe")
                    val = o.get("valor")
                    objs.append(ObjetivoOAR(e.nome, e.mascara, o.get("metrica", "Dmean"),
                                            float(val) if val not in (None, "") else None,
                                            float(o["volume_pct"]) if o.get("volume_pct") not in (None, "") else None,
                                            float(o.get("prioridade", 5))))
                r = proteger_oars(p, g, self.doses_um, alvo, corpo, m.colim, m.modelo, objs,
                                  criar_subcampos=bool(prot.get("subcampos", True)),
                                  margem_mm=float(prot.get("margem_mm", 5)))
                info["protecao_oars"] = {"subcampos": [c.nome for c in r.subcampos], "hi_antes": r.hi_antes,
                                         "hi_depois": r.hi_depois, "oars": r.oars, "avisos": r.avisos}
                self.avisos += list(r.avisos)
            if opcoes.get("compensador") and alvo is not None:
                comp = opcoes["compensador"]
                r = compensar(p, g, self.doses_um, alvo, m.colim, m.modelo, campos=comp.get("campos") or None,
                              niveis=int(comp.get("niveis", 6)), um_min_segmento=float(comp.get("um_min", 2.0)),
                              corpo=corpo)
                info["compensador"] = {"segmentos": r.n_segmentos, "hi_antes": r.hi_antes, "hi_depois": r.hi_depois,
                                       "historico": r.historico, "avisos": r.avisos}
                self.avisos = list(r.avisos)
            if not so_normalizar:
                self._aplicar_algoritmo(algoritmo, info)        # Monte Carlo: troca a dose/UM de cada campo
            if pr.metodo == "um_fixas":
                if (opcoes.get("otimizar_pesos") or opcoes.get("field_in_field") or opcoes.get("compensador")
                        or opcoes.get("proteger_oars") or opcoes.get("imrt") or opcoes.get("vmat")):
                    raise ErroUsuario("Plano com UM fixas (importado): troque a normalização antes de otimizar, "
                                      "fazer field-in-field ou compensador")
                faltam_um = [c.nome for c in p.campos if not c.mu]
                if faltam_um:
                    raise ErroUsuario(f"Campos sem UM: {', '.join(faltam_um)}")
                self.dose_fx = sum(float(c.mu) * self.doses_um[c.nome] for c in p.campos)
            else:
                self.dose_fx = calculo.normalizar(p, self.doses_um, g, alvo)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        total = self.dose_fx * pr.fracoes
        hist = {n: dvh(total, e.mascara, g.voxel_cc) for n, e in self.estruturas.items() if e.mascara.any()}
        conf = conferir_plano(p, m.colim, m.tabelas, self.doses_um, g,
                              ponto_mm=pr.ponto_mm if pr.metodo == "ponto" else None) if m.tabelas else []
        for k in conf:
            c = self._campo(k["campo"])
            k["compensado"] = bool((c.segmentos and c.segmentos != c.segmentos_blindagem) or c.pontos_controle)
        if p.tecnica in ("IMRT", "VMAT", "DCA") and m.tabelas and alvo is not None:
            conf.append(self._conferencia_composta(alvo))
        hi = indice_homogeneidade(total, alvo) if alvo is not None and alvo.any() else None
        self.resultado = {"dvh": hist, "conferencia_um": conf, "info": info, "hi": hi,
                          "restricoes": self._avaliar_restricoes(total),
                          "comparacao": self.comparacao_ref(total),
                          "dmax_gy": float(total[corpo].max()) if corpo.any() else float(total.max()),
                          "dmax_xyz": self._posicao_max(total, corpo),
                          "quando": dt.datetime.now().isoformat(timespec="seconds")}
        self.dose_atualizada = True
        self._registrar(f"Dose calculada ({len(p.campos)} campos)")
        return self.resumo()

    # ================================================================== algoritmo da dose final (CCC ou Monte Carlo)
    def _hash_campo(self, c) -> str:
        """Conteúdo do campo que muda a dose por UM (sem UM e peso)."""
        import hashlib
        import json
        d = {k: v for k, v in dataclasses.asdict(c).items() if k not in ("mu", "peso") and not k.startswith("_")}
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def _algoritmo(self, opcoes: dict | None = None) -> dict:
        """Algoritmo da dose final do plano. VMAT: Monte Carlo por padrão; o resto: collapsed cone."""
        p = self.plano
        cd = dict(p.calculo_dose or {})
        if opcoes and opcoes.get("algoritmo"):
            alg = str(opcoes["algoritmo"]).upper()
            if alg not in ("CCC", "MC"):
                raise ErroUsuario("Algoritmo de dose: CCC ou MC")
            cd["algoritmo"] = alg
        if opcoes and opcoes.get("incerteza_mc") not in (None, ""):
            inc = float(opcoes["incerteza_mc"])
            if not 0.3 <= inc <= 5.0:
                raise ErroUsuario("Incerteza do Monte Carlo entre 0,3% e 5%")
            cd["incerteza_pct"] = inc
        if opcoes and opcoes.get("dose_na_agua") is not None:
            cd["dose_na_agua"] = bool(opcoes["dose_na_agua"])
        vmat = p.tecnica == "VMAT" or bool(opcoes and opcoes.get("vmat"))
        cd.setdefault("algoritmo", "MC" if vmat else "CCC")
        cd.setdefault("incerteza_pct", 1.0)
        cd.setdefault("dose_na_agua", False)
        return cd

    def _restaurar_ccc(self) -> None:
        """Volta a dose/UM do collapsed cone dos campos que não mudaram (a do MC fica guardada à parte)."""
        guard = getattr(self, "_ccc_guardado", None)
        if not guard:
            return
        self.doses_um = {}
        for c in self.plano.campos:
            h, d = guard.get(c.nome, (None, None))
            if h == self._hash_campo(c):
                self.doses_um[c.nome] = d
        self._ccc_guardado = None

    def _aplicar_algoritmo(self, cd: dict, info: dict) -> None:
        p, m, g = self.plano, self.maquina, self.grade
        if cd["algoritmo"] != "MC":
            p.calculo_dose = {} if p.tecnica != "VMAT" else {**cd}
            return
        if not hasattr(self, "_mc_cache"):
            self._mc_cache = {}
        self._ccc_guardado = {c.nome: (self._hash_campo(c), self.doses_um[c.nome]) for c in p.campos}
        import hashlib
        eta_h = hashlib.sha1(np.ascontiguousarray(g.eta).tobytes()).hexdigest()[:12]   # densidades forçadas mudam
        chave = (f"{cd['incerteza_pct']}|{cd['dose_na_agua']}|{calculo.hash_modelo(m.modelo)}|{p.posicao_paciente}"
                 f"|{eta_h}")
        faltam = [c for c in p.campos if (self._mc_cache.get(c.nome) or ("",))[0] != self._hash_campo(c) + chave]
        if faltam:
            def prog(msg):
                self.progresso = f"Monte Carlo: {msg}"
            try:
                doses, inf = calculo.dose_por_um_mc(g, faltam, m.colim, m.modelo, posicao=p.posicao_paciente,
                                                    incerteza_pct=float(cd["incerteza_pct"]) * np.sqrt(
                                                        len(faltam) / max(len(p.campos), 1)),
                                                    dose_na_agua=bool(cd["dose_na_agua"]), progresso=prog)
            finally:
                self.progresso = None
            for c in faltam:
                self._mc_cache[c.nome] = (self._hash_campo(c) + chave, doses[c.nome], inf[c.nome])
        self.doses_um = {c.nome: self._mc_cache[c.nome][1] for c in p.campos}
        por_campo = {c.nome: self._mc_cache[c.nome][2] for c in p.campos}
        sig = [v["incerteza_pct"] for v in por_campo.values()]
        inc_plano = float(np.sqrt(np.sum(np.square(sig)))) / max(len(sig), 1)
        p.calculo_dose = {**cd, "incerteza_obtida_pct": round(inc_plano, 2)}
        info["mc"] = {"incerteza_pct": round(inc_plano, 2), "campos": por_campo,
                      "dose_na_agua": bool(cd["dose_na_agua"]),
                      "historias": int(sum(v["historias"] for v in por_campo.values())),
                      "tempo_s": round(sum(v["tempo_s"] for v in por_campo.values()), 1)}
        self._registrar(f"Dose por Monte Carlo (incerteza {inc_plano:.2f}%, "
                        f"{'dose na água' if cd['dose_na_agua'] else 'dose no meio'})")

    def _objetivos_otimizacao(self, im: dict) -> tuple[list, list, list]:
        """Objetivos (imrt.Objetivo), o que fica gravado no plano e a avoidance, a partir do pedido da tela."""
        from tps.planejamento import imrt as I
        from tps.planejamento import perfil_sistema as PS
        objs, guardar = [], []
        perfil = im.get("perfil") or getattr(self.plano, "perfil", "tps") or "tps"
        try:
            linhas = [PS.para_interno(perfil, o) for o in im.get("objetivos") or []]
        except ValueError as ex:
            raise ErroUsuario(str(ex)) from ex
        if linhas and not any(o.get("tipo") == "alvo" for o in linhas):
            # Eclipse/Monaco sem um "alvo" explícito: o alvo é a estrutura da prescrição (ou a de maior Lower)
            pr = self.plano.prescricao
            lower = [o for o in linhas if o.get("tipo") in ("min", "dvh_min") and o.get("dose_gy")]
            nome = pr.estrutura_alvo if any(o["estrutura"] == pr.estrutura_alvo for o in lower) else \
                (max(lower, key=lambda o: o["dose_gy"])["estrutura"] if lower else None)
            if nome:
                dose = pr.dose_total_gy if nome == pr.estrutura_alvo else max(o["dose_gy"] for o in lower
                                                                              if o["estrutura"] == nome)
                linhas.append({"estrutura": nome, "tipo": "alvo", "dose_gy": dose, "peso": 1.0, "auto": True})
        im = {**im, "objetivos": linhas}
        for o in im.get("objetivos") or []:
            e = self.estruturas.get(o.get("estrutura"))
            if e is None or not e.mascara.any():
                raise ErroUsuario(f"Estrutura {o.get('estrutura')} vazia ou inexistente")
            tipo = o.get("tipo", "max")
            if tipo not in I.TIPOS:
                raise ErroUsuario(f"Tipo de objetivo inválido: {tipo}")
            try:
                dose = float(o["dose_gy"])
            except (KeyError, TypeError, ValueError) as ex:
                raise ErroUsuario(f"{e.nome}: informe a dose do objetivo") from ex
            vol = o.get("volume_pct")
            if tipo in ("dvh", "paralelo") and vol in (None, ""):
                raise ErroUsuario(f"{e.nome}: objetivo de {tipo} precisa do volume (%)")
            alvos_def = [x for x in (im.get("objetivos") or []) if x.get("tipo") == "alvo"
                         and x.get("estrutura") in self.estruturas]
            masc = e.mascara
            if tipo != "alvo" and o.get("fora_do_alvo", True):        # a parte do órgão dentro do alvo não briga
                for a in alvos_def:
                    masc = masc & ~self.estruturas[a["estrutura"]].mascara
                if not masc.any():
                    masc = e.mascara
            elif tipo == "alvo":                                       # boost integrado: o alvo de dose maior manda
                for a in alvos_def:
                    if a["estrutura"] != e.nome and float(a["dose_gy"]) > dose:
                        masc = masc & ~self.estruturas[a["estrutura"]].mascara
                if not masc.any():
                    raise ErroUsuario(f"{e.nome} está inteiro dentro de um alvo de dose maior")
            objs.append(I.Objetivo(e.nome, masc, tipo, dose, float(vol) if vol not in (None, "") else None,
                                   float(o.get("peso", 1.0)), bool(o.get("restrito")) and tipo != "alvo"))
            guardar.append({k: o.get(k) for k in ("estrutura", "tipo", "dose_gy", "volume_pct", "peso", "restrito",
                                                  "funcao", "prioridade", "k", "perfil", "auto") if o.get(k) is not None})
        if not any(o.tipo == "alvo" for o in objs):
            raise ErroUsuario("Defina pelo menos um alvo com dose")
        evitar = []
        for a in im.get("avoidance") or []:
            e = self.estruturas.get(a.get("estrutura"))
            if e is None or not e.mascara.any():
                raise ErroUsuario(f"Avoidance: estrutura {a.get('estrutura')} vazia ou inexistente")
            if a.get("modo", "ambos") not in ("entrada", "saida", "ambos"):
                raise ErroUsuario("Avoidance: modo deve ser entrada, saida ou ambos")
            evitar.append((e.nome, e.mascara, a.get("modo", "ambos")))
        return objs, guardar, evitar

    def _otimizar_vmat(self, vm: dict, corpo) -> dict:
        """VMAT nos arcos do plano. SRS-VMT-001."""
        from tps.planejamento import vmat as V
        p, m, g = self.plano, self.maquina, self.grade
        if vm.get("perfil"):
            from tps.planejamento import perfil_sistema as PS
            p.perfil = PS._perfil(vm["perfil"])
        objs, guardar, evitar = self._objetivos_otimizacao(vm)

        def prog(msg):
            self.progresso = msg
        try:
            r = V.otimizar_vmat(p, g, self.doses_um, objs, corpo, m.colim, m.modelo, V.LimitesVMAT.da_maquina(m.raw),
                                setor_graus=float(vm.get("setor_graus", 40)), iteracoes=int(vm.get("iteracoes", 2)),
                                peso_nto=float(vm.get("peso_nto", 1.0)), progresso=prog, evitar=evitar,
                                flash_mm=float(vm.get("flash_mm", 0) or 0),
                                flash_pele_mm=float(vm.get("flash_pele_mm", 5) or 5),
                                espacamento_cp_graus=float(vm["espacamento_cp"]) if vm.get("espacamento_cp") else None,
                                polimento_fino=bool(vm.get("polimento_fino", True)))
        except ValueError as ex:
            raise ErroUsuario(str(ex)) from ex
        finally:
            self.progresso = None
        p.tecnica = "VMAT"
        p.objetivos_imrt = guardar
        self.avisos += r.avisos
        self._registrar(f"VMAT: {len(r.arcos)} arco(s), {r.um_total:.0f} UM, entrega estimada "
                        f"{sum(a['tempo_s'] for a in r.arcos.values()):.0f} s, otimização {r.tempo_s:.0f} s")
        return {"arcos": r.arcos, "um_total": r.um_total, "custo": r.custo, "avisos": r.avisos,
                "tempo_s": r.tempo_s, "restricoes": r.restricoes, "etapas": r.etapas}

    def campos_vmat(self, n_arcos: int = 1, inicio: float = 181.0, fim: float = 179.0, sentido: str = "CW",
                    alvo: str | None = None, margem_mm: float = 5.0, colimadores: list | None = None) -> dict:
        """Troca os campos do plano por arcos (o 2º arco volta no sentido contrário), conformados ao alvo (+ margem)
        no ângulo inicial; a conformação por setor é refeita na otimização."""
        from tps.planejamento.vmat import validar_arco
        self._exigir_plano()
        p = self.plano
        p._exigir_rascunho()
        alvo = alvo or p.prescricao.estrutura_alvo
        if alvo not in self.estruturas:
            raise ErroUsuario("Escolha o alvo dos arcos")
        if not 1 <= int(n_arcos) <= 4:
            raise ErroUsuario("Use de 1 a 4 arcos")
        erros = validar_arco(float(inicio), float(fim), sentido)
        if erros:
            raise ErroUsuario("; ".join(erros))
        cols = list(colimadores or [])
        for c in list(p.campos):
            p.campos.remove(c)
            self.doses_um.pop(c.nome, None)
        for k in range(int(n_arcos)):
            ida = k % 2 == 0
            ini, fi = (float(inicio), float(fim)) if ida else (float(fim), float(inicio))
            sen = sentido if ida else ("CC" if sentido == "CW" else "CW")
            col = float(cols[k]) if k < len(cols) else (0.0 if ida else 90.0)
            nome = f"ARCO{k + 1}"
            self.salvar_campo({"nome": nome, "gantry": ini, "colimador": col, "modo": "conformado", "alvo": alvo,
                               "margem": {q: margem_mm for q in ("x1", "x2", "y1", "y2")}, "ponto_encontro": "fora"})
            c = self._campo(nome)
            c.arco_fim, c.sentido = fi % 360.0, sen
        p.tecnica = "VMAT"
        self.dose_atualizada = False
        self._registrar(f"{n_arcos} arco(s) VMAT {inicio:g}→{fim:g} {sentido} (alvo {alvo} + {margem_mm:g} mm)")
        return self.resumo()

    def campos_dca(self, n_arcos: int = 1, inicio: float = 181.0, fim: float = 179.0, sentido: str = "CW",
                   alvo: str | None = None, margem_mm: float = 5.0, colimadores="auto", passo_cp: float = 5.0,
                   oars: list | None = None, max_bloqueio_pct: float = 25.0, mesas: list | None = None) -> dict:
        """Arco conformacional dinâmico (DCA): troca os campos por arcos com as lâminas conformadas ao alvo em cada
        ponto de controle e UM por grau constante. colimadores "auto" escolhe o ângulo de menor abertura fora do
        alvo; oars [{"estrutura", "margem_mm"}] recua a abertura sobre o órgão (sem buraco). SRS-PLN-110."""
        from tps.planejamento import dca as D
        from tps.planejamento.vmat import validar_arco
        self._exigir_plano()
        p = self.plano
        p._exigir_rascunho()
        alvo = alvo or p.prescricao.estrutura_alvo
        if alvo not in self.estruturas:
            raise ErroUsuario("Escolha o alvo dos arcos")
        if not 1 <= int(n_arcos) <= 6:
            raise ErroUsuario("Use de 1 a 6 arcos")
        if not 1.0 <= float(passo_cp) <= 10.0:
            raise ErroUsuario("Espaçamento dos pontos de controle entre 1° e 10°")
        erros = validar_arco(float(inicio), float(fim), sentido)
        if erros:
            raise ErroUsuario("; ".join(erros))
        g = self.grade
        eixos = (g.x, g.y, g.z)
        masc = self.estruturas[alvo].mascara
        orgaos = []
        for o in oars or []:
            e = self.estruturas.get(o.get("estrutura"))
            if e is None:
                raise ErroUsuario(f"Estrutura {o.get('estrutura')} não existe")
            orgaos.append((e.mascara & ~masc, float(o.get("margem_mm", 3.0))))
        cols = colimadores if isinstance(colimadores, list) else []
        mesas = list(mesas or [])
        for c in list(p.campos):
            p.campos.remove(c)
            self.doses_um.pop(c.nome, None)
        nomes = []
        for k in range(int(n_arcos)):
            ida = k % 2 == 0
            ini, fi = (float(inicio), float(fim)) if ida else (float(fim), float(inicio))
            sen = sentido if ida else ("CC" if sentido == "CW" else "CW")
            nome = f"DCA{k + 1}"
            self.salvar_campo({"nome": nome, "gantry": ini, "colimador": 0.0,
                               "mesa": float(mesas[k]) if k < len(mesas) else 0.0, "modo": "conformado",
                               "alvo": alvo, "margem": {q: margem_mm for q in ("x1", "x2", "y1", "y2")},
                               "ponto_encontro": "fora"})
            c = self._campo(nome)
            c.arco_fim, c.sentido = fi % 360.0, sen
            nomes.append(nome)
        resumo, usados = {}, []
        for k, nome in enumerate(nomes):
            c = self._campo(nome)
            try:
                if k < len(cols) and cols[k] not in (None, "", "auto"):
                    c.colimador = float(cols[k]) % 360.0
                else:
                    best, custo = D.melhor_colimador(c, masc, eixos, self.maquina.colim, float(margem_mm),
                                                     posicao=p.posicao_paciente)
                    # arcos seguintes: outro colimador quase tão bom (espalha as "orelhas" das lâminas)
                    livres = {a: v for a, v in custo.items() if a not in usados}
                    if usados and livres and min(livres.values()) <= 1.25 * custo[best] + 1.0:
                        best = min(livres, key=livres.get)
                    c.colimador = best
                usados.append(c.colimador)
                inf = D.gerar_arco(c, masc, eixos, self.maquina.colim, float(margem_mm), float(passo_cp),
                                   p.posicao_paciente, oars=orgaos, max_bloqueio_pct=float(max_bloqueio_pct))
            except ValueError as e:
                raise ErroUsuario(f"{nome}: {e}") from e
            resumo[nome] = {"colimador": c.colimador, "pontos": inf.n_pontos,
                            "desloc_mm_grau": round(inf.deslocamento_max_mm_grau, 2),
                            "area_fora_cm2": round(inf.area_fora_cm2, 1), "bloqueio_pct": round(inf.bloqueio_pct, 1)}
        p.tecnica = "DCA"
        self.dose_atualizada = False
        self._dca_info = resumo
        self._registrar(f"{n_arcos} arco(s) conformacional(is) {inicio:g}→{fim:g} {sentido} (alvo {alvo} + "
                        f"{margem_mm:g} mm; colimador {', '.join(f'{v['colimador']:g}°' for v in resumo.values())}"
                        + (f"; bloqueio de {', '.join(o.get('estrutura') for o in oars)}" if oars else "") + ")")
        r = self.resumo()
        r["dca"] = resumo
        return r

    def cq_por_calculo(self) -> dict:
        """Elegibilidade do plano para CQ só por cálculo independente (critérios da clínica). SRS-PLN-111."""
        from tps.planejamento import complexidade
        self._exigir_plano()
        self._exigir(self.dose_atualizada, "Calcule a dose primeiro")
        p = self.plano
        alvo = self.estruturas.get(p.prescricao.estrutura_alvo)
        g = self.grade
        return complexidade.avaliar(p, self.maquina.colim, (self.resultado or {}).get("conferencia_um"),
                                    alvo.mascara if alvo is not None else None, (g.x, g.y, g.z) if g is not None else None,
                                    p.posicao_paciente)

    def _otimizar_imrt(self, im: dict, corpo) -> dict:
        """IMRT step-and-shoot nos campos do plano (já conformados aos alvos). SRS-IMR-001."""
        from tps.planejamento import imrt as I
        p, m, g = self.plano, self.maquina, self.grade
        if any(c.e_arco for c in p.campos):
            raise ErroUsuario("O plano tem arcos: use VMAT ou troque por campos estáticos")
        from tps.planejamento import perfil_sistema as PS
        if im.get("perfil"):
            p.perfil = PS._perfil(im["perfil"])
        im = PS.opcoes_otimizacao(p.perfil, im)
        objs, guardar, evitar = self._objetivos_otimizacao(im)
        for c in p.campos:
            if c.campo_pai is None and "_abertura_conformada" not in c.__dict__:
                c.__dict__["_abertura_conformada"] = (list(c.mlc_a), list(c.mlc_b), c.jaw_x, c.jaw_y)

        def prog(msg):
            self.progresso = msg
        try:
            r = I.otimizar_imrt(p, g, self.doses_um, objs, corpo, m.colim, m.modelo,
                                niveis=int(im.get("niveis", 7)), iteracoes=int(im.get("iteracoes", 2)),
                                um_min_segmento=float(im.get("um_min") or (m.raw.get("imrt") or {}).get("um_min_segmento", 3.0)),
                                area_min_cm2=float(im.get("area_min_cm2") or (m.raw.get("imrt") or {}).get("area_min_cm2", 4.0)),
                                peso_nto=float(im.get("peso_nto", 1.0)), progresso=prog, evitar=evitar,
                                suavidade=float(im.get("suavidade", 0.05)),
                                flash_mm=float(im.get("flash_mm", 0) or 0),
                                flash_pele_mm=float(im.get("flash_pele_mm", 5) or 5),
                                forma_segmentos=bool(im.get("forma_segmentos", True)))
        except ValueError as ex:
            raise ErroUsuario(str(ex)) from ex
        finally:
            self.progresso = None
        p.tecnica = "IMRT"
        p.objetivos_imrt = guardar
        p.__dict__["_imrt_opcoes"] = {k: im.get(k) for k in ("avoidance", "flash_mm", "flash_pele_mm")}
        self.avisos += r.avisos
        self._registrar(f"IMRT: {sum(r.n_segmentos.values())} segmentos em {len(r.n_segmentos)} campos, "
                        f"{r.um_total:.0f} UM, {r.tempo_s:.0f} s")
        return {"segmentos": r.n_segmentos, "um_total": r.um_total, "custo": r.custo, "avisos": r.avisos,
                "tempo_s": r.tempo_s, "restricoes": r.restricoes}

    def _fracao_borda(self, ponto, folga_mm: float = 4.0) -> float:
        """Fração das UM do plano em que o ponto fica a menos de folga_mm de uma ponta de lâmina ou dentro de um par
        fechado (abertura ≤ gap mínimo + 1 mm)."""
        from tps.engine.geometry import beam_frame
        colim = self.maquina.colim
        lim = np.asarray(colim.limites_laminas)
        g_min = colim.gap_minimo_mm
        tot, ruim = 0.0, 0.0
        for c in self.plano.campos:
            if c.pontos_controle:
                formas = [(s["gantry"], s["peso"], s) for s in calculo.intervalos_arco(c)]
            elif c.segmentos:
                formas = [(c.gantry, s["peso"], s) for s in c.segmentos]
            else:
                formas = [(c.gantry, 1.0, {"mlc_a": c.mlc_a, "mlc_b": c.mlc_b, "jaw_y": c.jaw_y})]
            for gan, w, s in formas:
                fr = beam_frame(gan, c.colimador, c.mesa, c.isocentro_mm, 1000.0, self.plano.posicao_paciente)
                r = np.asarray(ponto, float) - fr.source
                u, v = 1000.0 * (r @ fr.u) / (r @ fr.w), 1000.0 * (r @ fr.v) / (r @ fr.w)
                i = int(np.searchsorted(lim, v, side="right") - 1)
                peso = float(c.mu or 0) * w
                tot += peso
                if not (0 <= i < colim.n_pares) or not (s["jaw_y"][0] <= v <= s["jaw_y"][1]):
                    continue
                a, b = float(s["mlc_a"][i]), float(s["mlc_b"][i])
                if b - a <= g_min + 1.0 and a - folga_mm <= u <= b + folga_mm:
                    ruim += peso
                elif a < u < b and min(u - a, b - u) < folga_mm:
                    ruim += peso
        return ruim / tot if tot > 0 else 0.0

    def _conferencia_composta(self, alvo, tolerancia_pct: float = 5.0) -> dict:
        """IMRT/VMAT: conferência independente do plano inteiro num ponto de baixo gradiente dentro do alvo, longe de
        pontas de lâmina e de pares fechados (Σ UM · dose/UM por tabela, Clarkson por segmento ou intervalo do arco),
        tolerância de 5% (TG-114/TG-218 para IMRT)."""
        from scipy.ndimage import binary_erosion, gaussian_gradient_magnitude

        from tps.planejamento.um_tabela import dose_por_um_tabela_campo
        g, p, m = self.grade, self.plano, self.maquina
        nucleo = binary_erosion(alvo, iterations=2)
        if not nucleo.any():
            nucleo = alvo
        grad = gaussian_gradient_magnitude(self.dose_fx, 1.0)
        med = float(np.median(self.dose_fx[nucleo]))
        custo = np.where(nucleo, grad / max(med, 1e-9) + np.abs(self.dose_fx - med) / max(med, 1e-9), np.inf)
        # entre os 40 melhores pontos, o que menos fica perto de ponta de lâmina ou de par fechado (fendas de gap
        # mínimo e penumbra de lâmina: onde a tabela e o motor divergem por construção)
        cand = np.argsort(custo, axis=None)[:40]
        cand = cand[np.isfinite(custo.ravel()[cand])]
        melhor, ponto = None, None
        for q in cand:
            k, j, i = np.unravel_index(int(q), custo.shape)
            pt = (float(g.x[i]), float(g.y[j]), float(g.z[k]))
            bordas = self._fracao_borda(pt)
            nota = bordas * 5.0 + float(custo.ravel()[q])
            if melhor is None or nota < melhor:
                melhor, ponto = nota, pt
        if ponto is None:
            k, j, i = np.unravel_index(int(np.argmin(custo)), custo.shape)
            ponto = (float(g.x[i]), float(g.y[j]), float(g.z[k]))
        mot, tab = 0.0, 0.0
        for c in p.campos:
            mot += float(c.mu or 0) * calculo.valor_em_ponto(self.doses_um[c.nome], g, ponto) * 100.0
            t = dose_por_um_tabela_campo(c, m.colim, m.tabelas, ponto, g.eta, (g.x, g.y, g.z),
                                         posicao=p.posicao_paciente)
            tab += float(c.mu or 0) * t["cgy_por_um"]
        dif = 100 * (mot / tab - 1) if tab > 0 else float("nan")
        return {"campo": "PLANO (soma)", "motor_cgy_um": mot, "tabela_cgy_um": tab, "dif_pct": float(dif),
                "ok": bool(abs(dif) <= tolerancia_pct), "composta": True, "ponto_mm": [round(v, 1) for v in ponto],
                "fracao_borda_pct": round(100 * self._fracao_borda(ponto), 1),
                "tolerancia_pct": tolerancia_pct, "eq_abertura_cm": 0.0, "prof_rad_mm": 0.0, "tmr": 0.0}

    def campos_imrt(self, n: int, inicio: float = 0.0, alvo: str | None = None, margem_mm: float = 5.0,
                    colimador: float = 0.0) -> dict:
        """Troca os campos do plano por n campos equiespaçados conformados ao alvo (+ margem), prontos para IMRT."""
        from tps.planejamento.imrt import campos_equiespacados
        self._exigir_plano()
        p = self.plano
        p._exigir_rascunho()
        alvo = alvo or p.prescricao.estrutura_alvo
        if alvo not in self.estruturas:
            raise ErroUsuario("Escolha o alvo dos campos")
        if not 3 <= int(n) <= 15:
            raise ErroUsuario("Use de 3 a 15 campos")
        for c in list(p.campos):
            p.campos.remove(c)
            self.doses_um.pop(c.nome, None)
        p.tecnica = "3D"
        for g in campos_equiespacados(int(n), float(inicio)):
            self.salvar_campo({"nome": f"G{int(round(g)) % 360:03d}", "gantry": g, "colimador": colimador,
                               "modo": "conformado", "alvo": alvo,
                               "margem": {k: margem_mm for k in ("x1", "x2", "y1", "y2")}, "ponto_encontro": "fora"})
        self._registrar(f"{n} campos equiespaçados para IMRT (início {inicio:g}°, alvo {alvo} + {margem_mm:g} mm)")
        return self.resumo()

    def assistente_objetivos(self, texto: str = "", sitio: str | None = None, aplicar: bool = False,
                             motor: str = "regras", proposta: dict | None = None) -> dict:
        """Objetivos dos planos modulados. motor 'regras': protocolo do sítio + leitura do caso por regras; 'claude' ou
        'auto': o Claude raciocina sobre o resumo anônimo do caso (auto cai nas regras se o Claude falhar).
        aplicar + proposta: cria as estruturas combinadas pedidas e devolve os objetivos já revisados na tela."""
        from tps.planejamento import assistente as A
        self._exigir_plano()
        if self.grade is None:
            raise ErroUsuario("Carregue um CT primeiro")
        if aplicar and proposta is not None:
            for c in proposta.get("criar") or []:
                if c.get("nome") and c["nome"] not in self.estruturas:
                    self.criar_estrutura(c["nome"], c.get("operacao", "uniao"), c.get("a"), c.get("b"))
            objs = [o for o in proposta.get("objetivos") or [] if o.get("estrutura") in self.estruturas]
            self._registrar(f"Objetivos do assistente aplicados ({proposta.get('motor') or 'regras'}; "
                            f"{len(objs)} objetivos)")
            return {"objetivos": objs, "criar": proposta.get("criar") or [], "estruturas": list(self.estruturas)}
        ia = False
        if self.tipo == "ct":
            try:
                from tps.ia.segmentacao import executavel
                ia = executavel() is not None
            except Exception:  # noqa: BLE001
                ia = False
        pr = self.plano.prescricao
        est = {n: e.mascara for n, e in self.estruturas.items() if e.mascara is not None and e.mascara.any()}
        r = A.analisar(est, self.grade, self._corpo(), self.grade.voxel_cc, texto or "", sitio or None,
                       pr.dose_total_gy, pr.fracoes, pr.estrutura_alvo, ia_disponivel=ia)
        r["motor"] = "regras"
        if motor in ("claude", "auto"):
            from tps.planejamento import assistente_claude as C
            if not C.estado()["ativo"]:
                if motor == "claude":
                    raise ErroUsuario("Assistente de IA não configurado (Ferramentas › Assistente de IA)")
            else:
                dose = (self.dose_fx * pr.fracoes) if (self.dose_atualizada and self.dose_fx is not None) else None
                ctx = C.Contexto(est, self.grade, self._corpo(), self.grade.voxel_cc,
                                 {"dose_total_gy": pr.dose_total_gy, "fracoes": pr.fracoes,
                                  "dose_fracao_gy": round(pr.dose_total_gy / pr.fracoes, 3) if pr.fracoes else None,
                                  "alvo_da_prescricao": pr.estrutura_alvo, "tecnica": self.plano.tecnica},
                                 dose, r)

                def prog(msg):
                    self.progresso = msg
                try:
                    c = C.propor(ctx, C.anonimizar(texto or "", self.paciente), prog)
                except Exception as e:  # noqa: BLE001
                    if motor == "claude":
                        raise ErroUsuario(f"IA indisponível: {C.mensagem_erro(e)}") from e
                    r["avisos"].insert(0, f"IA indisponível ({C.mensagem_erro(e)}): proposta feita pelas regras")
                else:
                    par = dict(r.get("parametros") or {})
                    arc = c.get("arcos") or None
                    if arc and arc.get("inicio") is not None and arc.get("fim") is not None:
                        par.update({"arcos": int(arc.get("n") or par.get("arcos") or 2), "inicio": float(arc["inicio"]),
                                    "fim": float(arc["fim"]), "sentido": arc.get("sentido") or "CW"})
                    else:
                        par.pop("inicio", None)
                        par.pop("fim", None)
                    sit = c.get("sitio") if c.get("sitio") in A.carregar() else r.get("sitio")
                    r.update({"objetivos": c["objetivos"], "explicacao": c["explicacao"], "avisos": c["avisos"],
                              "parametros": par, "motor": "claude", "modelo": c["modelo"], "tempo_s": c["tempo_s"],
                              "uso_tokens": c["uso_tokens"], "sitio": sit,
                              "sitio_nome": (A.carregar().get(sit) or {}).get("nome") if sit else "Proposta da IA",
                              "fonte": "IA, a partir do resumo anônimo do caso e do protocolo do sítio"})
                    self._registrar(f"Assistente de IA ({c['modelo']}): {len(c['objetivos'])} objetivos "
                                    f"em {c['tempo_s']} s")
                finally:
                    self.progresso = None
        if aplicar:
            for c in r["criar"]:
                if c["nome"] not in self.estruturas:
                    self.criar_estrutura(c["nome"], c["operacao"], c["a"], c["b"])
            self._registrar(f"Objetivos do assistente aplicados (protocolo {r.get('sitio') or '—'}; "
                            f"{len(r['objetivos'])} objetivos)")
        r["estruturas"] = list(self.estruturas)
        return r

    def objetivos_imrt_sugeridos(self, refazer: bool = False, sitio: str | None = None) -> list[dict]:
        from tps.planejamento.imrt import objetivos_sugeridos
        self._exigir_plano()
        if sitio:
            return self.assistente_objetivos("", sitio, aplicar=True)["objetivos"]
        if self.plano.objetivos_imrt and not refazer:
            return list(self.plano.objetivos_imrt)
        pr = self.plano.prescricao
        return objetivos_sugeridos([n for n, e in self.estruturas.items() if e.mascara.any()], pr.estrutura_alvo,
                                   pr.dose_total_gy, self.plano.restricoes)

    def _posicao_max(self, total, corpo) -> list[float]:
        g = self.grade
        d = np.where(corpo, total, -np.inf) if corpo.any() else total
        k, j, i = np.unravel_index(int(np.argmax(d)), d.shape)
        return [round(float(g.x[i]), 1), round(float(g.y[j]), 1), round(float(g.z[k]), 1)]

    def _avaliar_restricoes(self, total) -> dict:
        """Restrições QUANTEC/ICRU (ou as da clínica) com nomes TG-263. Nunca impede o cálculo."""
        from tps.planejamento import restricoes
        pr = self.plano.prescricao
        try:
            doses = {n: total[e.mascara] for n, e in self.estruturas.items() if e.mascara.any()}
            return restricoes.avaliar(doses, self.grade.voxel_cc, pr.dose_total_gy, pr.fracoes,
                                      pr.estrutura_alvo, extras=self.plano.restricoes).json()
        except Exception as e:  # noqa: BLE001
            return {"erro": f"avaliação das restrições falhou: {e}", "resultados": [], "avisos": []}

    def adicionar_restricao(self, r: dict, na_clinica: bool = False) -> dict:
        """Restrição própria (fica no plano; com na_clinica também entra no protocolo da clínica)."""
        from tps.planejamento import restricoes
        self._exigir_plano()
        try:
            item = restricoes.validar_restricao(r, self.estruturas)
            if na_clinica:
                arq = restricoes.salvar_na_clinica(item)
                self._registrar(f"Restrição {item['estrutura']} {item['metrica']} {item['op']} {item['valor']} "
                                f"gravada no protocolo da clínica ({arq.name})")
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        if not na_clinica:
            self.plano.restricoes.append(item)
        self._reavaliar_restricoes()
        return self.resumo()

    def remover_restricao(self, indice: int) -> dict:
        self._exigir_plano()
        if not 0 <= indice < len(self.plano.restricoes):
            raise ErroUsuario("Restrição inexistente")
        self.plano.restricoes.pop(indice)
        self._reavaliar_restricoes()
        return self.resumo()

    def _reavaliar_restricoes(self):
        if self.dose_atualizada and self.dose_fx is not None and self.resultado:
            self.resultado["restricoes"] = self._avaliar_restricoes(self.dose_fx * self.plano.prescricao.fracoes)

    def restricoes_json(self) -> dict:
        self._exigir(self.dose_atualizada, "Dose não calculada")
        return self.resultado.get("restricoes") or {"resultados": [], "avisos": []}

    def _recalcular_aprovado(self) -> dict:
        """Plano aprovado (ex.: reaberto): recalcula a dose com as UM gravadas, sem mudar nada no plano, e confere que
        a normalização ainda bate com a prescrição (0,5%)."""
        p, m, g = self.plano, self.maquina, self.grade
        if calculo.hash_modelo(m.modelo) != p.modelo_feixe.get("hash"):
            raise ErroUsuario("O modelo de feixe mudou desde a aprovação: desaprove e recalcule o plano")
        cd = dict(p.calculo_dose or {})
        info = {"recalculo_aprovado": True}
        if cd.get("algoritmo") == "MC":
            self._restaurar_ccc()
            faltam = [c for c in p.campos if c.nome not in self.doses_um]
            if faltam:
                self.doses_um.update(calculo.dose_por_um(g, faltam, m.colim, m.modelo))
            self._aplicar_algoritmo(self._algoritmo(), info)
        else:
            faltam = [c for c in p.campos if c.nome not in self.doses_um]
            if faltam:
                self.doses_um.update(calculo.dose_por_um(g, faltam, m.colim, m.modelo))
        self.dose_fx = sum(c.mu * self.doses_um[c.nome] for c in p.campos)
        pr = p.prescricao
        alvo = self.estruturas[pr.estrutura_alvo].mascara if pr.estrutura_alvo in self.estruturas else None
        val = calculo.metrica_normalizacao(self.dose_fx, p, g, alvo)
        esperado = pr.dose_fracao_gy * 100.0 / pr.isodose_pct
        # MC: a dose recalculada tem a mesma incerteza estatística do cálculo aprovado
        tol = 0.005 if cd.get("algoritmo") != "MC" else max(0.005, 2 * float(cd.get("incerteza_obtida_pct", 1.0)) / 100)
        if abs(val / esperado - 1) > tol:
            raise ErroUsuario(f"A dose recalculada não reproduz a prescrição ({val:.4f} × {esperado:.4f} Gy/fx)")
        total = self.dose_fx * pr.fracoes
        hist = {n: dvh(total, e.mascara, g.voxel_cc) for n, e in self.estruturas.items() if e.mascara.any()}
        conf = conferir_plano(p, m.colim, m.tabelas, self.doses_um, g,
                              ponto_mm=pr.ponto_mm if pr.metodo == "ponto" else None) if m.tabelas else []
        for k in conf:
            c = self._campo(k["campo"])
            k["compensado"] = bool((c.segmentos and c.segmentos != c.segmentos_blindagem) or c.pontos_controle)
        if p.tecnica in ("IMRT", "VMAT") and m.tabelas and alvo is not None:
            conf.append(self._conferencia_composta(alvo))
        corpo = self._corpo()
        self.resultado = {"dvh": hist, "conferencia_um": conf, "info": info,
                          "restricoes": self._avaliar_restricoes(total),
                          "hi": indice_homogeneidade(total, alvo) if alvo is not None and alvo.any() else None,
                          "dmax_gy": float(total[corpo].max()), "dmax_xyz": self._posicao_max(total, corpo),
                          "quando": dt.datetime.now().isoformat(timespec="seconds")}
        self.dose_atualizada = True
        self._registrar("Dose do plano aprovado recalculada e conferida")
        return self.resumo()

    # ================================================================== aprovação e exportação
    def aprovar(self, usuario: str, papel: str, justificativa: str = "", pode_justificar: bool = False):
        """Conferência de UM fora da tolerância (TG-114): só aprova com a justificativa da investigação, escrita por
        quem pode assinar como físico; a justificativa fica no plano, no histórico e na auditoria."""
        self._exigir_plano()
        self._exigir(self.dose_atualizada, "Calcule a dose antes de aprovar")
        self._exigir(bool(usuario.strip()), "Informe o usuário")
        avisos_um = [k for k in self.resultado.get("conferencia_um", []) if not k["ok"] and not k.get("compensado")]
        justificativa = (justificativa or "").strip()
        from tps.app.checklist import checklist
        lista_chk = checklist(self)
        erros_chk = [i for i in lista_chk if i["estado"] == "erro" and i["id"] != "conferencia_um"]
        if avisos_um:
            lista = ", ".join(f"{k['campo']} ({k['dif_pct']:+.1f}%)" for k in avisos_um)
            if not justificativa:
                raise ErroUsuario(f"Conferência independente de UM fora da tolerância em {lista}. Investigue e, se "
                                  "a diferença estiver explicada, aprove escrevendo a justificativa.")
        if erros_chk and not justificativa:
            raise ErroUsuario("Checklist com pendências: " + "; ".join(f"{i['titulo']}: {i['detalhe']}" for i in erros_chk)
                              + ". Corrija ou aprove escrevendo a justificativa.")
        if avisos_um or erros_chk:
            if not pode_justificar:
                raise ErroUsuario("Só o físico pode aprovar com pendências (conferência de UM ou checklist)")
            if len(justificativa) < 15:
                raise ErroUsuario("Escreva a justificativa por extenso (o que foi investigado e por que é aceitável)")
        try:
            self.plano.aprovar(usuario.strip(), papel)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        if avisos_um:
            self.plano.aprovacao["conferencia_um_justificativa"] = {
                "campos": [{"campo": k["campo"], "dif_pct": round(k["dif_pct"], 2)} for k in avisos_um],
                "justificativa": justificativa, "usuario": usuario.strip()}
        self.plano.aprovacao["checklist"] = [{k: i[k] for k in ("id", "estado", "detalhe")} for i in lista_chk]
        if erros_chk:
            self.plano.aprovacao["checklist_justificativa"] = {"itens": [i["titulo"] for i in erros_chk],
                                                               "justificativa": justificativa,
                                                               "usuario": usuario.strip()}
        self._registrar(f"Aprovado por {usuario} ({papel})" +
                        (f" com pendências ({', '.join(['UM'] * bool(avisos_um) + [i['titulo'] for i in erros_chk])})"
                         f" — justificativa: {justificativa}" if (avisos_um or erros_chk) else ""))

    def desaprovar(self, usuario: str, motivo: str):
        self._exigir_plano()
        try:
            self.plano.desaprovar(usuario, motivo)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        self._registrar(f"Desaprovado por {usuario}: {motivo}")

    def _extras_exportacao(self, modo_segmentos: str) -> dict:
        """Atributos que o Monaco grava para o Mosaiq: distâncias dos colimadores, tabela de tolerância,
        departamento, ponto de referência de dose e dose de cada feixe nesse ponto (Gy/fração)."""
        from tps.exportacao.rtplan import _feixes_para_exportar
        cfg = dict(self.maquina.raw.get("dicom_exportacao") or {})
        p = self.plano
        pr = p.prescricao
        ponto = None
        if pr.metodo == "ponto" and pr.ponto_mm:
            ponto = {"nome": "PRESCRICAO", "xyz": list(pr.ponto_mm)}
        elif p.campos:
            iso = list(p.campos[0].isocentro_mm)
            d = self.dose_em(iso)
            if d.get("pct") is not None and d["pct"] >= 80:
                ponto = {"nome": "ISOCENTRO", "xyz": iso}
            elif p.pontos:
                ponto = {"nome": p.pontos[0]["nome"], "xyz": list(p.pontos[0]["xyz"])}
        dose_feixe = {}
        if ponto and self.tipo == "ct":
            from scipy.ndimage import map_coordinates
            g = self.grade
            idx = [[(ponto["xyz"][2] - g.z[0]) / (g.z[1] - g.z[0])], [(ponto["xyz"][1] - g.y[0]) / (g.y[1] - g.y[0])],
                   [(ponto["xyz"][0] - g.x[0]) / (g.x[1] - g.x[0])]]
            for nome, c, mu, formas, membros in _feixes_para_exportar(p, modo_segmentos):
                if all(m_.nome in self.doses_um for m_, _ in membros):
                    dose_feixe[nome] = sum(float(map_coordinates(self.doses_um[m_.nome], idx, order=1)[0]) * u_
                                           for m_, u_ in membros)
        return {"distancia_fonte_mm": cfg.get("distancia_fonte_mm"), "tolerancia": cfg.get("tolerancia"),
                "departamento": cfg.get("departamento"), "ponto_ref": ponto, "dose_feixe": dose_feixe}

    def gerar_qa(self, destino: str | Path, fantoma: str = "cubo30", modo: str = "real",
                 profundidade_iso_mm: float | None = None) -> dict:
        """Plano de verificação (QA de paciente): os campos de uma fração no fantoma, com RTPLAN, RTDOSE e o plano
        coronal no isocentro. SRS-QA-001."""
        from pydicom.uid import generate_uid

        from tps.exportacao.rtdose import exportar_rtdose
        from tps.planejamento.qa_paciente import gravar_csv_plano, plano_verificacao
        self._exigir_plano()
        self._exigir(self.dose_atualizada and self.dose_fx is not None, "Calcule a dose antes do QA")
        m = self.maquina
        self.progresso = "Calculando o plano no fantoma…"
        try:
            cd = self.plano.calculo_dose or {}
            alg = "MC" if cd.get("algoritmo") == "MC" else "CCC"

            def prog(msg):
                self.progresso = f"QA (Monte Carlo): {msg}"
            try:
                r = plano_verificacao(self.plano, m.colim, m.modelo, fantoma, modo, profundidade_iso_mm,
                                      algoritmo=alg, incerteza_pct=float(cd.get("incerteza_pct", 1.0)), progresso=prog)
            finally:
                self.progresso = None
        except (ValueError, KeyError) as e:
            raise ErroUsuario(str(e)) from e
        finally:
            self.progresso = None
        ref = dict(self.plano.referencia_ct or {})
        r.plano.referencia_ct = {k: ref.get(k, "") for k in ("PatientName", "PatientID", "PatientBirthDate",
                                                             "PatientSex", "ReferringPhysicianName")}
        r.plano.referencia_ct["FrameOfReferenceUID"] = generate_uid()
        r.plano.tecnica = self.plano.tecnica
        destino = Path(destino)
        destino.mkdir(parents=True, exist_ok=True)
        base = f"{r.plano.rotulo}_{modo}_{dt.datetime.now():%Y%m%d_%H%M%S}"
        modo_seg = (m.raw.get("dicom_exportacao") or {}).get("segmentos", "step_and_shoot")
        extras = {k: v for k, v in self._extras_exportacao(modo_seg).items() if k not in ("ponto_ref", "dose_feixe")}
        try:
            rp = exportar_rtplan(r.plano, m.colim, destino / f"RP_{base}.dcm", exigir_aprovado=False,
                                 modo_segmentos=modo_seg, extras=extras)
        except (ValueError, PermissionError) as e:
            raise ErroUsuario(f"RTPLAN de verificação: {e}") from e
        exportar_rtdose(r.dose_fx_gy, r.grade.x, r.grade.y, r.grade.z, rp, destino / f"RD_{base}.dcm")
        gravar_csv_plano(r, destino / f"coronal_iso_{base}.csv")
        iso_gy = calculo.valor_em_ponto(r.dose_fx_gy, r.grade, r.iso_mm)
        self._registrar(f"QA de paciente gerado ({modo}, fantoma {fantoma}): {iso_gy:.3f} Gy no isocentro")
        return {"pasta": str(destino), "arquivos": [str(destino / f"RP_{base}.dcm"), str(destino / f"RD_{base}.dcm"),
                                                     str(destino / f"coronal_iso_{base}.csv")],
                "dose_iso_gy": round(float(iso_gy), 4), "dmax_gy": round(float(r.dose_fx_gy.max()), 4),
                "um_total": round(sum(float(c.mu) for c in r.plano.campos), 1), "modo": modo, "fantoma": fantoma}

    def exportar(self, destino: str | Path, modo_segmentos: str | None = None,
                 incluir_imagens: bool | None = None, incluir_rtstruct: bool = True) -> dict:
        self._exigir_plano()
        self._exigir(self.dose_atualizada and self.dose_fx is not None, "Calcule a dose antes de exportar")
        destino = Path(destino)
        destino.mkdir(parents=True, exist_ok=True)
        p = self.plano
        modo_segmentos = modo_segmentos or (self.maquina.raw.get("dicom_exportacao") or {}).get("segmentos",
                                                                                               "step_and_shoot")
        base = f"{p.rotulo}_{dt.datetime.now():%Y%m%d_%H%M%S}"
        extras = self._extras_exportacao(modo_segmentos)
        arq_rs = None
        if incluir_rtstruct and self.tipo == "ct":
            from tps.exportacao.rtstruct import exportar_rtstruct
            arq_rs = destino / f"RS_{base}.dcm"
            rs = exportar_rtstruct(self, arq_rs)
            extras["rtstruct"] = str(rs.SOPInstanceUID)
        try:
            from tps.planejamento.perfil_sistema import convencoes_exportacao
            extras["convencoes"] = convencoes_exportacao(p.perfil)
            rp = exportar_rtplan(p, self.maquina.colim, destino / f"RP_{base}.dcm", modo_segmentos=modo_segmentos,
                                 extras=extras)
        except (ValueError, PermissionError) as e:
            raise ErroUsuario(str(e)) from e
        problemas = conferir_rtplan(pydicom.dcmread(destino / f"RP_{base}.dcm"))
        if problemas:
            raise ErroUsuario("RTPLAN reprovado na conferência: " + "; ".join(problemas))
        arquivos = [destino / f"RP_{base}.dcm"] + ([arq_rs] if arq_rs else [])
        g = self.grade
        exportar_rtdose(self.dose_fx * p.prescricao.fracoes, g.x, g.y, g.z, rp, destino / f"RD_{base}.dcm")
        arquivos.append(destino / f"RD_{base}.dcm")
        from tps.exportacao.entregabilidade import Regras, conferir as conferir_entrega
        entrega = conferir_entrega(rp, Regras.da_maquina(self.maquina.colim, self.maquina.raw))
        if p.objetivos_imrt and p.tecnica in ("IMRT", "VMAT"):
            from tps.planejamento.perfil_sistema import gravar_ficha
            arquivos += gravar_ficha(p.perfil, p.objetivos_imrt, destino, base,
                                     f"{p.rotulo} — {p.prescricao.dose_total_gy:g} Gy / {p.prescricao.fracoes} fx")
        if incluir_imagens is None:
            incluir_imagens = self.tipo == "fantoma"          # CT real: o Mosaiq normalmente já tem as imagens
        if incluir_imagens:
            img = destino / f"imagens_{base}"
            img.mkdir(exist_ok=True)
            for f in sorted(self.pasta_ct.iterdir()):
                if f.is_file() and (self._e_ct(f) or (f == self.arquivo_rs and not arq_rs)):
                    shutil.copy2(f, img / f.name)
                    arquivos.append(img / f.name)
        self._registrar(f"Exportado para {destino} ({modo_segmentos})"
                        + (f"; entregabilidade: {len(entrega)} ponto(s) de atenção" if entrega else ""))
        return {"arquivos": [str(a) for a in arquivos], "pasta": str(destino), "entregabilidade": _resumir_entrega(entrega)}

    def enviar_dicom(self, arquivos: list[str], host: str, porta: int, ae_destino: str, ae_local: str = "TPS") -> dict:
        from pynetdicom import AE, StoragePresentationContexts
        ae = AE(ae_title=ae_local[:16])
        ae.requested_contexts = StoragePresentationContexts[:120]
        assoc = ae.associate(host, int(porta), ae_title=ae_destino[:16])
        if not assoc.is_established:
            raise ErroUsuario(f"Não conectou em {ae_destino}@{host}:{porta}")
        res = []
        try:
            for f in arquivos:
                st = assoc.send_c_store(pydicom.dcmread(f))
                res.append({"arquivo": Path(f).name, "status": int(getattr(st, "Status", -1)) if st else -1})
        finally:
            assoc.release()
        falhas = [r for r in res if r["status"] != 0]
        self._registrar(f"Enviado a {ae_destino}@{host}:{porta}: {len(res) - len(falhas)}/{len(res)}")
        if falhas:
            raise ErroUsuario(f"Envio recusado: {falhas}")
        return {"enviados": res}

    def salvar(self) -> Path:
        """Salva o plano atual e os outros planos da sessão (um arquivo por plano)."""
        self._exigir_plano()
        atual = self.plano.rotulo
        for r in list(self.outros_planos):
            self.trocar_plano(r)
            self._salvar_atual()
        self.trocar_plano(atual)
        return self._salvar_atual()

    def _salvar_atual(self) -> Path:
        self._exigir_plano()
        d = paths.home() / "planos"
        d.mkdir(parents=True, exist_ok=True)
        dados = {"plano": dataclasses.asdict(self.plano), "isocentro": self.isocentro, "tipo": self.tipo,
                 "pasta_ct": str(self.pasta_ct) if self.pasta_ct else None, "maquina": self.maquina.id,
                 "energia": self.maquina.energia, "paciente": self.paciente,
                 "fantoma": dataclasses.asdict(self.fantoma) if self.tipo == "fantoma" else None,
                 "specs": {c.nome: c.__dict__.get("_spec") for c in self.plano.campos},
                 "bolus_mascaras": {b["nome"]: _empacotar(self.estruturas[b["nome"]].mascara)
                                    for b in self.plano.bolus if b["nome"] in self.estruturas},
                 "derivadas": {n: {**d, "cor": list(self.estruturas[n].cor),
                                   "mascara": _empacotar(self.estruturas[n].mascara)}
                               for n, d in self.derivadas.items() if n in self.estruturas},
                 "editadas": {n: {"tipo": self.estruturas[n].tipo, "cor": list(self.estruturas[n].cor),
                                  "contornos": [np.round(c, 2).tolist() for c in self.estruturas[n].contornos]}
                              for n in self.editadas if n in self.estruturas}}
        f = d / f"{self.paciente['id'] or 'sem_id'}_{self.plano.rotulo}.json"
        f.write_text(json.dumps(dados, indent=1, default=str), encoding="utf-8")
        self._registrar(f"Plano salvo em {f}")
        return f

    def abrir(self, arquivo: str | Path):
        dados = json.loads(Path(arquivo).read_text(encoding="utf-8"))
        if dados["tipo"] == "ct":
            self.carregar_ct(dados["pasta_ct"])
        else:
            fz = dados["fantoma"]
            self.criar_fantoma(fz["tipo"], fz["largura_mm"], fz["espessura_mm"], fz["comprimento_mm"],
                               fz.get("contorno_xy"), paciente_id=dados["paciente"]["id"],
                               paciente_nome=dados["paciente"]["nome"], reutilizar=dados.get("pasta_ct"))
        self.definir_maquina(dados["maquina"], dados.get("energia", "6MV"))
        pd_ = dados["plano"]
        rx = Prescricao(**{k: (tuple(v) if k == "ponto_mm" and v else v) for k, v in pd_["prescricao"].items()})
        campos = [Campo(**{k: (tuple(v) if k in ("isocentro_mm", "jaw_x", "jaw_y") and v else v)
                           for k, v in c.items()}) for c in pd_["campos"]]
        setup = [Campo(**{k: (tuple(v) if k in ("isocentro_mm", "jaw_x", "jaw_y") and v else v)
                          for k, v in c.items()}) for c in pd_.get("campos_setup") or []]
        pd_ = {**pd_, "prescricao": rx, "campos": campos, "campos_setup": setup}
        self.plano = Plano(**pd_)
        self.isocentro = tuple(dados["isocentro"])
        self.eta_original = self.grade.eta.copy()
        for nome, emp in (dados.get("bolus_mascaras") or {}).items():
            self.estruturas[nome] = Estrutura(nome, "BOLUS", (255, 170, 60), _desempacotar(emp, self.grade.eta.shape))
        for nome, d in (dados.get("derivadas") or {}).items():
            d = dict(d)
            self.estruturas[nome] = Estrutura(nome, "DERIVADA", tuple(d.pop("cor")),
                                              _desempacotar(d.pop("mascara"), self.grade.eta.shape))
            self.derivadas[nome] = d
        for nome, d in (dados.get("editadas") or {}).items():
            e = Estrutura(nome, d["tipo"], tuple(d["cor"]), np.zeros(self.grade.eta.shape, bool),
                          [np.asarray(c, float) for c in d["contornos"]])
            self._rasterizar(e)
            self.estruturas[nome] = e
            self.editadas.add(nome)
            self.derivadas.pop(nome, None)
        self._aplicar_eta()
        for c in self.plano.campos:
            c.__dict__["_spec"] = (dados.get("specs") or {}).get(c.nome)
        self._registrar(f"Plano aberto de {arquivo} (estado {self.plano.estado}; dose a recalcular)")
        if dados["tipo"] == "ct":                      # outros planos salvos do mesmo CT entram na sessão
            for f in sorted((paths.home() / "planos").glob("*.json")):
                if Path(f).resolve() == Path(arquivo).resolve():
                    continue
                try:
                    d2 = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if d2.get("tipo") != "ct" or d2.get("pasta_ct") != dados["pasta_ct"]:
                    continue
                if d2["plano"]["rotulo"] in self.outros_planos or d2["plano"]["rotulo"] == self.plano.rotulo:
                    continue
                try:
                    self._anexar_plano_salvo(d2)
                except Exception as e:  # noqa: BLE001
                    self._registrar(f"Plano salvo {f.name} não pôde ser aberto junto: {e}")

    def _anexar_plano_salvo(self, dados: dict):
        """Plano salvo de outro arquivo (mesmo CT) entra na lista da sessão, sem dose."""
        pd_ = dados["plano"]
        rx = Prescricao(**{k: (tuple(v) if k == "ponto_mm" and v else v) for k, v in pd_["prescricao"].items()})
        campos = [Campo(**{k: (tuple(v) if k in ("isocentro_mm", "jaw_x", "jaw_y") and v else v)
                           for k, v in c.items()}) for c in pd_["campos"]]
        setup = [Campo(**{k: (tuple(v) if k in ("isocentro_mm", "jaw_x", "jaw_y") and v else v)
                          for k, v in c.items()}) for c in pd_.get("campos_setup") or []]
        plano = Plano(**{**pd_, "prescricao": rx, "campos": campos, "campos_setup": setup})
        for c in plano.campos:
            c.__dict__["_spec"] = (dados.get("specs") or {}).get(c.nome)
        maq = self.maquina
        if dados["maquina"] != maq.id or dados.get("energia", "6MV") != maq.energia:
            maq = Maquina.carregar(dados["maquina"], dados.get("energia", "6MV"))
        bolus = {nome: Estrutura(nome, "BOLUS", (255, 170, 60), _desempacotar(emp, self.grade.eta.shape))
                 for nome, emp in (dados.get("bolus_mascaras") or {}).items()}
        self.outros_planos[plano.rotulo] = {"plano": plano, "doses_um": {}, "dose_fx": None, "resultado": {},
                                            "dose_atualizada": False, "eta_original": None, "avisos": [],
                                            "isocentro": tuple(dados["isocentro"]), "dose_ref": None,
                                            "maquina": maq, "bolus": bolus}

    # ================================================================== dados para a tela
    def _campo(self, nome: str) -> Campo:
        c = next((c for c in list(self.plano.campos) + list(self.plano.campos_setup) if c.nome == nome), None)
        if c is None:
            raise ErroUsuario(f"Campo {nome} não existe")
        return c

    def _campo_json(self, c: Campo) -> dict:
        d = {k: v for k, v in dataclasses.asdict(c).items() if not k.startswith("_") and k != "pontos_controle"}
        d["n_segmentos"] = len(c.segmentos) if c.segmentos else 0
        d["n_pontos"] = len(c.pontos_controle) if c.pontos_controle else 0
        d["spec"] = c.__dict__.get("_spec")
        return d

    def resumo(self) -> dict:
        p = self.plano
        out = {"id": self.id, "nome": self.nome, "tipo": self.tipo, "paciente": self.paciente,
               "estruturas": [{"nome": e.nome, "tipo": e.tipo, "cor": e.cor, "editada": e.nome in self.editadas,
                               "volume_cc": round(float(e.mascara.sum()) * self.grade.voxel_cc, 1)}
                              for e in self.estruturas.values()] if self.grade is not None else [],
               "maquina": {"id": self.maquina.id, "energia": self.maquina.energia, "nome_dicom": self.maquina.nome_dicom,
                           "mlc": self.maquina.colim.nome, "n_pares": self.maquina.colim.n_pares,
                           "limites": list(map(float, self.maquina.colim.limites_laminas)),
                           "tem_jaw_x": self.maquina.colim.tem_jaw_x, "gap": self.maquina.colim.gap_minimo_mm,
                           "comissionamento": (self.maquina.modelo.ref or {}).get("comissionamento", "v1")}
               if self.maquina else None,
               "isocentro": self.isocentro, "dose_atualizada": self.dose_atualizada, "avisos": self.avisos,
               "log": self.log[-30:], "planos_dicom": self.planos_dicom, "planos_sessao": self.lista_planos(),
               "dose_ref": {"fonte": self.dose_ref["fonte"], "arquivo": self.dose_ref["arquivo"]} if self.dose_ref else None}
        if self.tipo == "ct" and self.pasta_ct is not None:          # caso recebido pela automação
            try:
                from tps.app import automacao
                cid = automacao.caso_da_pasta(self.pasta_ct)
                if cid:
                    c = automacao.caso(cid)
                    out["caso_auto"] = {"id": cid, "estado": c.get("estado"), "sitio": c.get("sitio"),
                                        "estruturas_aprovadas": c.get("estruturas_aprovadas"),
                                        "planos": [{k: p_.get(k) for k in ("rotulo", "estado", "tecnica", "hi")}
                                                   for p_ in c.get("planos") or []]}
            except Exception:  # noqa: BLE001
                pass
        if self.grade is not None:
            g = self.grade
            out["grade"] = {"x": [float(g.x[0]), float(g.x[-1]), len(g.x)], "y": [float(g.y[0]), float(g.y[-1]), len(g.y)],
                            "z": [float(g.z[0]), float(g.z[-1]), len(g.z)]}
            cx, cy, cz = self.ct.axes()
            out["ct"] = {"x": [float(cx[0]), float(cx[-1]), len(cx)], "y": [float(cy[0]), float(cy[-1]), len(cy)],
                         "z": [float(cz[0]), float(cz[-1]), len(cz)]}
        if p:
            pts = []
            for pt in p.pontos:
                d = self.dose_em(pt["xyz"]) if self.dose_atualizada else {}
                pts.append({**pt, "total_gy": d.get("total_gy"), "pct": d.get("pct"), "campos": d.get("campos", {})})
            out["plano"] = {"rotulo": p.rotulo, "estado": p.estado, "aprovacao": p.aprovacao, "tecnica": p.tecnica,
                            "pontos": pts, "restricoes": p.restricoes, "bolus": p.bolus, "densidades": p.densidades,
                            "prescricao": dataclasses.asdict(p.prescricao), "historico": p.historico,
                            "calculo_dose": p.calculo_dose or {}, "perfil": getattr(p, "perfil", "tps"),
                            "campos": [self._campo_json(c) for c in p.campos],
                            "campos_setup": [self._campo_json(c) for c in p.campos_setup]}
            if self.dose_atualizada and self.resultado:
                r = self.resultado
                out["resultado"] = {"hi": r["hi"], "dmax_gy": r["dmax_gy"], "dmax_xyz": r.get("dmax_xyz"), "info": r["info"],
                                    "restricoes": r.get("restricoes"),
                                    "comparacao": {k: v for k, v in (r.get("comparacao") or {}).items() if k != "dvh"}
                                    or None,
                                    "conferencia_um": r["conferencia_um"],
                                    "estatisticas": {n: {k: h[k] for k in ("volume_cc", "Dmin", "Dmean", "Dmax", "D95",
                                                                             "D98", "D2") if k in h}
                                                     for n, h in r["dvh"].items()}}
        return out

    def dvh_json(self) -> dict:
        self._exigir(self.dose_atualizada, "Dose não calculada")
        out = {}
        for n, h in self.resultado["dvh"].items():
            d, v = np.asarray(h["dose_gy"]), np.asarray(h["volume_pct"])
            k = np.linspace(0, len(d) - 1, min(len(d), 200)).astype(int)
            out[n] = {"dose": d[k].round(3).tolist(), "volume": v[k].round(2).tolist(),
                      "cor": self.estruturas[n].cor}
            comp = self.resultado.get("comparacao") or {}
            if n in (comp.get("dvh") or {}):
                hr = comp["dvh"][n]
                dr, vr = np.asarray(hr["dose_gy"]), np.asarray(hr["volume_pct"])
                kr = np.linspace(0, len(dr) - 1, min(len(dr), 200)).astype(int)
                out[n]["ref"] = {"dose": dr[kr].round(3).tolist(), "volume": vr[kr].round(2).tolist()}
        return out

    # ---------------------------------------------------------------- cortes
    def _fatia(self, vista: str, idx: int):
        hu = self.ct.hu
        if vista == "axial":
            return hu[idx]
        if vista == "coronal":
            return hu[::-1, idx, :]
        return hu[::-1, :, idx]

    def imagem_corte(self, vista: str, idx: int, janela: float = 400, nivel: float = 40) -> bytes:
        f = self._fatia(vista, int(idx))
        a = np.clip((f - (nivel - janela / 2)) / janela * 255, 0, 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(a).save(buf, "PNG", optimize=False, compress_level=1)
        return buf.getvalue()

    def _fonte(self, fonte: str = "plano"):
        """(dose total Gy, 100% em Gy, é diferença?) da dose mostrada: o plano, a soma ou a diferença de planos."""
        if fonte in ("soma", "dif"):
            v = self.dose_vista.get(fonte)
            self._exigir(v is not None, "Faça a soma/comparação de planos antes")
            return v["total"], float(v["rx_gy"]), fonte == "dif"
        self._exigir(self.dose_atualizada and self.dose_fx is not None, "Dose não calculada")
        return self.dose_fx * self.plano.prescricao.fracoes, float(self.plano.prescricao.dose_total_gy), False

    def imagem_dose(self, vista: str, idx: int, minimo_pct: float = 20.0, maximo_pct: float = 0.0,
                    opacidade: float = 0.45, fonte: str = "plano", unidade: str = "pct") -> bytes:
        """Colorwash: dose do corte em cores (azul → vermelho), RGBA, na grade de dose, transparente abaixo do mínimo.
        Escala em % da prescrição; máximo 0 = dose máxima. Diferença de planos: azul (menos) ↔ vermelho (mais),
        simétrica em ±máximo (padrão 10%), transparente abaixo de ±1%. Linha 0 = topo da tela."""
        import matplotlib
        total, rx, e_dif = self._fonte(fonte)
        if unidade == "gy" and not e_dif:             # limites em Gy (dose total) → % da prescrição
            minimo_pct = float(minimo_pct) / rx * 100.0
            maximo_pct = float(maximo_pct) / rx * 100.0 if maximo_pct else 0.0
        g = self.grade
        pos, eh, ev = self._plano_corte(vista, int(idx))
        rel = total / rx * 100.0
        k = {"axial": (g.z, 0), "coronal": (g.y, 1), "sagital": (g.x, 2)}[vista]
        i = int(np.argmin(np.abs(k[0] - pos)))
        if abs(k[0][i] - pos) > abs(k[0][1] - k[0][0]):
            img = np.zeros((2, 2))
        else:
            img = np.take(rel, i, axis=k[1])                   # [v, h]
        if vista != "axial":
            img = img[::-1]                                     # z cresce para cima na tela
        if e_dif:
            lim = float(maximo_pct) if maximo_pct and maximo_pct > 0 else 10.0
            t = np.clip((img + lim) / (2 * lim), 0, 1)
            rgba = (matplotlib.colormaps["coolwarm"](t) * 255).astype(np.uint8)
            rgba[..., 3] = np.where(np.abs(img) >= 1.0, int(255 * max(opacidade, 0.55)), 0).astype(np.uint8)
        else:
            corpo = self._corpo()
            rmax = float(rel[corpo].max()) if corpo.any() else float(rel.max())
            top = float(maximo_pct) if maximo_pct and maximo_pct > minimo_pct else max(rmax, minimo_pct + 1)
            t = np.clip((img - minimo_pct) / (top - minimo_pct), 0, 1)
            rgba = (matplotlib.colormaps["jet"](t) * 255).astype(np.uint8)
            rgba[..., 3] = np.where(img >= minimo_pct, int(255 * opacidade), 0).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=1)
        return buf.getvalue()

    def _plano_corte(self, vista: str, idx: int):
        """Coordenada (mm) do corte e eixos (horizontal, vertical) no sistema do paciente."""
        cx, cy, cz = self.ct.axes()
        if vista == "axial":
            return float(cz[idx]), "x", "y"
        if vista == "coronal":
            return float(cy[idx]), "x", "z"
        return float(cx[idx]), "y", "z"

    def sobreposicao(self, vista: str, idx: int, estruturas: list[str] | None = None, isodoses: bool = True,
                     niveis: list | None = None, referencia: str = "rx", unidade: str = "pct",
                     fonte: str = "plano", niveis_gy: list | None = None) -> dict:
        """Contornos, isodoses e feixes do corte, em mm (h, v). `niveis_gy`: linhas extras em dose absoluta (Gy,
        dose total do plano), desenhadas junto com as relativas (ex.: 40 Gy para a medula)."""
        g = self.grade
        pos, eh, ev = self._plano_corte(vista, int(idx))
        out = {"pos": pos, "contornos": [], "isodoses": [], "feixes": [], "iso": self.isocentro}
        ax = {"x": g.x, "y": g.y, "z": g.z}

        def corte_grade(vol):
            if vista == "axial":
                k = int(np.argmin(np.abs(g.z - pos)))
                return vol[k] if abs(g.z[k] - pos) <= abs(g.z[1] - g.z[0]) else None
            if vista == "coronal":
                k = int(np.argmin(np.abs(g.y - pos)))
                return vol[:, k, :] if abs(g.y[k] - pos) <= abs(g.y[1] - g.y[0]) else None
            k = int(np.argmin(np.abs(g.x - pos)))
            return vol[:, :, k] if abs(g.x[k] - pos) <= abs(g.x[1] - g.x[0]) else None

        def linhas(img, nivel):
            if img is None or img.max() < nivel:
                return []
            cg = contour_generator(ax[eh], ax[ev], img.astype(np.float64))   # img[v, h]
            return [np.round(l, 1).tolist() for l in cg.lines(nivel) if len(l) > 2]

        nomes = estruturas if estruturas is not None else list(self.estruturas)
        for n in nomes:
            e = self.estruturas.get(n)
            if e is None:
                continue
            if vista == "axial" and e.contornos:
                dz = float(self.ct.spacing[2]) / 2 + 0.01
                pols = [np.round(c[:, :2], 1).tolist() for c in e.contornos if abs(float(c[0, 2]) - pos) <= dz]
            else:
                pols = linhas(corte_grade(e.mascara.astype(np.float32)), 0.5)
            if pols:
                out["contornos"].append({"nome": n, "cor": e.cor, "linhas": pols})
        tem_dose = (self.dose_atualizada and self.dose_fx is not None) if fonte == "plano" \
            else self.dose_vista.get(fonte) is not None
        if tem_dose:
            total, rx, e_dif = self._fonte(fonte)
            corpo = self._corpo()
            dmax = float(total[corpo].max()) if corpo.any() else float(total.max())
            pas = {k: float(abs(v[1] - v[0])) for k, v in ax.items()}
            out["wash"] = {"h": [float(ax[eh][0]) - pas[eh] / 2, float(ax[eh][-1]) + pas[eh] / 2],
                           "v": [float(ax[ev][0]) - pas[ev] / 2, float(ax[ev][-1]) + pas[ev] / 2],
                           "dmax_pct": round(dmax / rx * 100, 1), "dif": e_dif, "rx_gy": round(float(rx), 4)}
            if isodoses:
                # 100% = prescrição ou dose máxima; níveis em % ou em Gy (dose total). Diferença: ±% da prescrição
                cem = dmax if (referencia == "max" and not e_dif) else rx
                if e_dif:
                    padrao = [-10, -5, -3, 3, 5, 10]
                    lista = sorted(float(v) for v in (niveis or padrao))
                    img = corte_grade(total / rx * 100.0)
                    if unidade == "gy":
                        lista = sorted(float(v) for v in (niveis or [rx * k / 100 for k in padrao]))
                        img = corte_grade(total)
                elif unidade == "gy":
                    img = corte_grade(total)
                    lista = sorted(float(v) for v in (niveis or [cem * k / 100 for k in NIVEIS_ISODOSE]))
                else:
                    img = corte_grade(total / cem * 100.0)
                    lista = sorted(float(v) for v in (niveis or NIVEIS_ISODOSE))
                for nv in lista:
                    if nv < 0:                                   # diferença negativa: linhas de −img
                        ls = linhas(None if img is None else -img, -nv)
                    else:
                        ls = linhas(img, nv)
                    if ls:
                        out["isodoses"].append({"nivel": nv, "linhas": ls})
                extras = sorted({round(float(v), 3) for v in (niveis_gy or []) if float(v) > 0})
                if extras and not e_dif:
                    img_gy = corte_grade(total)
                    for nv in extras:
                        ls = linhas(img_gy, nv)
                        if ls:
                            out["isodoses"].append({"nivel": nv, "linhas": ls, "absoluta": True,
                                                    "pct": round(nv / cem * 100, 1)})
                out["iso_ref"] = {"referencia": referencia, "unidade": unidade, "cem_gy": round(cem, 3),
                                  "dmax_gy": round(dmax, 3), "rx_gy": round(rx, 3), "niveis": lista, "niveis_gy": extras, "fonte": fonte,
                                  "dif": e_dif, "descricao": (self.dose_vista.get(fonte) or {}).get("descricao")}
        if self.plano and vista == "axial":
            for c in self.plano.campos:
                if c.campo_pai:
                    continue
                fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, "HFS")
                iso = np.asarray(c.isocentro_mm, float)
                lo, hi = (c.jaw_x if c.jaw_x else (min(c.mlc_a), max(c.mlc_b)))
                ext = []
                for uu in (lo, hi):
                    alvo = iso + uu * fr.u
                    d = alvo - fr.source
                    ext.append((fr.source + d * 1.25)[:2].round(1).tolist())
                out["feixes"].append({"nome": c.nome, "fonte": fr.source[:2].round(1).tolist(),
                                      "iso": iso[:2].round(1).tolist(), "bordas": ext,
                                      "cax_fim": (fr.source + fr.w * 1250)[:2].round(1).tolist()})
        return out

    # ---------------------------------------------------------------- BEV e DRR
    def bev(self, nome: str, estruturas: list[str] | None = None, res_mm: float = 1.0) -> dict:
        c = self._campo(nome)
        g = self.grade
        colim = self.maquina.colim
        ext = colim.jaw_y_max_mm + 5
        u, v = grade_bev(ext, res_mm)
        nomes = estruturas if estruturas is not None else [n for n, e in self.estruturas.items()
                                                          if e.tipo.upper() != "EXTERNAL"]
        proj = []
        for n in nomes:
            e = self.estruturas.get(n)
            if e is None or not e.mascara.any():
                continue
            r = bev_da_estrutura(e.mascara, (g.x, g.y, g.z), c, u, v)
            if r.m.any():
                cg = contour_generator(u, v, r.m.astype(np.float64))
                proj.append({"nome": n, "cor": e.cor, "linhas": [np.round(l, 1).tolist() for l in cg.lines(0.5)]})
        segs = [{"mlc_a": s["mlc_a"], "mlc_b": s["mlc_b"], "peso": s["peso"]} for s in (c.segmentos or [])]
        return {"campo": self._campo_json(c), "limites": list(map(float, colim.limites_laminas)),
                "projecoes": proj, "segmentos": segs, "ext": ext, "tem_jaw_x": colim.tem_jaw_x,
                "gap": colim.gap_minimo_mm, "interdigitacao": colim.interdigitacao,
                "lamina_max": colim.lamina_max_mm, "overtravel": colim.overtravel_mm}

    def bev_dose(self, nome: str, modo: str = "max", minimo_pct: float = 95.0, maximo_pct: float = 0.0,
                 estrutura: str | None = None, n: int = 200, ext_mm: float = 200.0) -> bytes:
        """Colorwash da dose no BEV (mesma geometria do DRR, ±ext_mm no plano do isocentro). modo "max": dose máxima
        ao longo de cada raio dentro do corpo (ou só dentro de `estrutura`) — mostra atrás de qual lâmina está o
        ponto quente; "iso": dose no plano perpendicular ao feixe que passa pelo isocentro. % da prescrição;
        transparente abaixo do mínimo."""
        import matplotlib
        from scipy.ndimage import map_coordinates
        if modo not in ("max", "iso"):
            raise ErroUsuario("Modo da dose no BEV: max ou iso")
        c = self._campo(nome)
        total, rx, _ = self._fonte("plano")
        rel = (total / rx * 100.0).astype(np.float32)
        g = self.grade
        if estrutura:
            e = self.estruturas.get(estrutura)
            if e is None or not e.mascara.any():
                raise ErroUsuario(f"Estrutura {estrutura} vazia ou inexistente")
            rel = np.where(e.mascara, rel, 0.0).astype(np.float32)
        else:
            rel = np.where(self._corpo(), rel, 0.0).astype(np.float32)
        fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, self.plano.posicao_paciente)
        uu = np.linspace(-ext_mm, ext_mm, n)
        U, V = np.meshgrid(uu, uu[::-1])
        P = fr.source + 1000.0 * fr.w + U[..., None] * fr.u + V[..., None] * fr.v

        def amostra(q):
            return map_coordinates(rel, [(q[..., 2] - g.z[0]) / (g.z[1] - g.z[0]), (q[..., 1] - g.y[0]) / (g.y[1] - g.y[0]),
                                         (q[..., 0] - g.x[0]) / (g.x[1] - g.x[0])], order=1, mode="constant")
        if modo == "iso":
            img = amostra(P)
        else:
            d = P - fr.source
            d /= np.linalg.norm(d, axis=-1, keepdims=True)
            cantos = np.array([[x, y, z] for x in (g.x[0], g.x[-1]) for y in (g.y[0], g.y[-1]) for z in (g.z[0], g.z[-1])])
            dist = np.linalg.norm(cantos - fr.source, axis=1)
            passo = float(min(abs(g.x[1] - g.x[0]), abs(g.z[1] - g.z[0]) if len(g.z) > 1 else 3.0))
            img = np.zeros((n, n), np.float32)
            for t in np.arange(max(0.0, dist.min() - 5), dist.max() + 5, passo):
                np.maximum(img, amostra(fr.source + t * d), out=img)
        topo = float(maximo_pct) if maximo_pct and maximo_pct > 0 else max(float(img.max()), minimo_pct + 1.0)
        t = np.clip((img - minimo_pct) / max(topo - minimo_pct, 1e-6), 0, 1)
        rgba = (matplotlib.colormaps["jet"](0.35 + 0.65 * t) * 255).astype(np.uint8)
        rgba[..., 3] = np.where(img >= minimo_pct, 150, 0).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=False, compress_level=1)
        return buf.getvalue()

    def drr(self, nome: str, n: int = 256, ext_mm: float = 200.0) -> bytes:
        """Radiografia reconstruída (soma de η ao longo do raio), para conferência do campo."""
        from scipy.ndimage import map_coordinates
        c = self._campo(nome)
        g = self.grade
        fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, "HFS")
        uu = np.linspace(-ext_mm, ext_mm, n)
        U, V = np.meshgrid(uu, uu[::-1])
        P = fr.source + 1000.0 * fr.w + U[..., None] * fr.u + V[..., None] * fr.v
        d = P - fr.source
        d /= np.linalg.norm(d, axis=-1, keepdims=True)
        ts = np.arange(700.0, 1300.0, 2.0)
        img = np.zeros((n, n))
        for t in ts:
            q = fr.source + t * d
            img += map_coordinates(g.eta, [(q[..., 2] - g.z[0]) / (g.z[1] - g.z[0]),
                                           (q[..., 1] - g.y[0]) / (g.y[1] - g.y[0]),
                                           (q[..., 0] - g.x[0]) / (g.x[1] - g.x[0])], order=1, mode="constant")
        dentro = img[img > 1e-3]
        if dentro.size:                                   # realce: estica entre os percentis 2 e 99,7 do paciente
            lo, hi = np.percentile(dentro, 2), np.percentile(dentro, 99.7)
            img = np.clip((img - lo) / max(hi - lo, 1e-6), 0, 1) ** 1.4 * (img > 1e-3)
        a = (np.clip(img, 0, 1) * 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(a).save(buf, "PNG")
        return buf.getvalue()

    # ---------------------------------------------------------------- relatório
    def relatorio_html(self) -> str:
        from tps.app.relatorio import relatorio
        return relatorio(self)

    # ---------------------------------------------------------------- utilidades
    def _exigir(self, cond, msg):
        if not cond:
            raise ErroUsuario(msg)

    def _exigir_plano(self):
        self._exigir(self.plano is not None, "Crie o plano antes")

    def _registrar(self, msg: str):
        self.log.append(f"{dt.datetime.now():%H:%M:%S} {msg}")


def _empacotar(m: np.ndarray) -> str:
    import base64
    import zlib
    return base64.b64encode(zlib.compress(np.packbits(m.ravel()).tobytes())).decode()


def _desempacotar(txt: str, forma) -> np.ndarray:
    import base64
    import zlib
    b = np.frombuffer(zlib.decompress(base64.b64decode(txt)), np.uint8)
    return np.unpackbits(b)[: int(np.prod(forma))].reshape(forma).astype(bool)
