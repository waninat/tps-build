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
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.nome = ""
        self.tipo = ""                        # ct | fantoma
        self.ct: CTVolume | None = None
        self.derivadas: dict = {}
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
        self.resultado: dict = {}
        self.dose_atualizada = False
        self.avisos: list[str] = []
        self.log: list[str] = []

    # ================================================================== carregamento
    def carregar_ct(self, pasta: str | Path, curva: str = "siemens_spirit_oncomed", voxel_mm: float = 3.0):
        pasta = Path(pasta)
        if not pasta.is_dir():
            raise ErroUsuario(f"Pasta não encontrada: {pasta}")
        try:
            self.ct = read_ct_series(pasta)
        except (ValueError, NotImplementedError) as e:
            raise ErroUsuario(str(e)) from e
        if (self.ct.patient_position or "HFS") != "HFS":
            raise ErroUsuario(f"Posição do paciente {self.ct.patient_position}: esta versão só aceita HFS")
        rs = None
        for f in sorted(p for p in pasta.iterdir() if p.is_file()):
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
            except Exception:
                continue
            if getattr(ds, "Modality", "") == "RTSTRUCT":
                rs = f
                if str(getattr(ds, "ReferencedFrameOfReferenceSequence", [{}])[0].get("FrameOfReferenceUID", "")) \
                        not in ("", self.ct.frame_of_reference):
                    raise ErroUsuario("RTSTRUCT de outro referencial (FrameOfReferenceUID diferente do CT)")
                self.paciente = {"id": str(getattr(ds, "PatientID", "")), "nome": str(getattr(ds, "PatientName", ""))}
                break
        if rs is None:
            raise ErroUsuario("Nenhum RTSTRUCT na pasta: exporte o CT junto com os contornos")
        self.tipo, self.pasta_ct, self.arquivo_rs = "ct", pasta, rs
        self.curva = HUtoED.from_csv(curva)
        self.grade = calculo.grade_de_dose(self.ct, self.curva, voxel_mm)
        g = self.grade
        self.estruturas = {}
        self.derivadas = {}
        for k, s in enumerate(read_rtstruct(rs)):
            m = rasterize(s, g.x, g.y, g.z)
            cor = tuple(int(c) for c in s.color) if s.color else PALETA[k % len(PALETA)]
            self.estruturas[s.name] = Estrutura(s.name, s.rt_type or "", cor, m, s.contours)
        self.nome = self.paciente["nome"] or pasta.name
        self._registrar(f"CT carregado: {pasta} ({self.ct.hu.shape[0]} cortes), RTSTRUCT {rs.name}")

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
    def novo_plano(self, rotulo: str, dose_total: float, fracoes: int, alvo: str, metodo: str = "media_alvo",
                   ponto=None, isodose: float = 100.0):
        self._exigir(self.grade is not None, "Abra um CT ou crie um fantoma antes")
        self._exigir(self.maquina is not None, "Escolha a máquina antes")
        rx = Prescricao(float(dose_total), int(fracoes), alvo, metodo, tuple(ponto) if ponto else None, float(isodose))
        erros = rx.validar()
        if metodo == "media_alvo" and alvo not in self.estruturas:
            erros.append(f"Estrutura alvo {alvo} não existe")
        if erros:
            raise ErroUsuario("; ".join(erros))
        ref = referencia_do_ct(sorted(p for p in self.pasta_ct.iterdir() if p.is_file() and self._e_ct(p))[0])
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
            op = OpcoesAjuste(pe, float(spec.get("folga_jaw", 0)))
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
        c.ssd_mm = calculo.ssd_no_eixo(g, c)
        if c.ssd_mm is None:
            raise ErroUsuario(f"{nome}: o eixo central não cruza o paciente")
        c.alvo_conformado = spec.get("alvo") if spec.get("modo", "conformado") != "retangular" else None
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

    def _remover_derivados(self):
        """Ao mudar um campo principal, sub-campos e segmentos calculados deixam de valer."""
        p = self.plano
        p._exigir_rascunho()
        subs = [c for c in p.campos if c.campo_pai]
        for s in subs:
            p.campos.remove(s)
            self.doses_um.pop(s.nome, None)
        for c in p.campos:
            if c.segmentos:
                c.segmentos = None
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
        self.doses_um.pop(nome, None)
        self.dose_atualizada = False
        return {"campo": self._campo_json(c)}

    def remover_campo(self, nome: str):
        self._exigir_plano()
        self._remover_derivados()
        c = self._campo(nome)
        self.plano.campos.remove(c)
        self.doses_um.pop(nome, None)
        self.dose_atualizada = False

    # ================================================================== densidade, bolus, pontos
    def _aplicar_eta(self):
        """η da grade = original do CT + bolus (água) + densidades forçadas. Toda mudança invalida a dose."""
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
        cor = tuple(cor) if cor else ((255, 0, 255) if operacao != "isodose" else (255, 255, 0))
        self.estruturas[nome] = Estrutura(nome, "DERIVADA", cor, m)
        self.derivadas[nome] = {"operacao": operacao, "a": a, "b": b, "margens": margens, "contrair": contrair,
                                "interno_mm": interno_mm, "externo_mm": externo_mm, "nivel_gy": nivel_gy}
        self._atualizar_avaliacao()
        self._registrar(f"Estrutura {nome} criada ({operacao} de {a}{' e ' + b if b else ''})")
        return {"nome": nome, "volume_cc": round(float(m.sum()) * g.voxel_cc, 1)}

    def remover_estrutura(self, nome: str):
        if nome not in self.derivadas:
            raise ErroUsuario("Só estruturas criadas no TPS podem ser removidas")
        if self.plano:
            usos = [c.nome for c in self.plano.campos if (c.__dict__.get("_spec") or {}).get("alvo") == nome]
            if usos or self.plano.prescricao.estrutura_alvo == nome:
                raise ErroUsuario(f"A estrutura {nome} é usada pelo plano ({', '.join(usos) or 'prescrição'})")
        self.estruturas.pop(nome, None)
        self.derivadas.pop(nome, None)
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
        self._exigir_plano()
        p, m, g = self.plano, self.maquina, self.grade
        self._exigir(bool(p.campos), "Plano sem campos")
        if p.estado == "APROVADO":
            return self._recalcular_aprovado()
        p._exigir_rascunho()
        pr = p.prescricao
        if opcoes.get("metodo"):
            pr.metodo = opcoes["metodo"]
            if pr.metodo == "ponto":
                pr.ponto_mm = tuple(float(v) for v in opcoes.get("ponto") or self.isocentro)
        alvo = self.estruturas[pr.estrutura_alvo].mascara if pr.estrutura_alvo in self.estruturas else None
        if pr.metodo == "media_alvo" and alvo is None:
            raise ErroUsuario("Normalização pela média do alvo sem estrutura alvo")
        self._remover_derivados()
        p.modelo_feixe = self._info_modelo()
        self.avisos = []
        try:
            faltam = [c for c in p.campos if c.nome not in self.doses_um]
            if faltam:
                self.doses_um.update(calculo.dose_por_um(g, faltam, m.colim, m.modelo))
            for n in list(self.doses_um):
                if n not in {c.nome for c in p.campos}:
                    self.doses_um.pop(n)
            info = {}
            if opcoes.get("otimizar_pesos") and alvo is not None and len(p.campos) > 1:
                oars = [(self.estruturas[n].mascara, float(i)) for n, i in (opcoes.get("oars") or {}).items()
                        if n in self.estruturas]
                w = otimizar_pesos(self.doses_um, alvo, oars)
                for c in p.campos:
                    c.peso = round(w[c.nome], 4)
            corpo = self._corpo()
            if opcoes.get("field_in_field") and alvo is not None:
                r = field_in_field(p, g, self.doses_um, alvo, corpo, m.colim, m.modelo,
                                   um_min=float(opcoes.get("um_min", 3.0)))
                info["field_in_field"] = {"subcampos": [c.nome for c in r.subcampos], "hi_antes": r.hi_antes,
                                          "hi_depois": r.hi_depois, "avisos": r.avisos}
                self.avisos = list(r.avisos)
            if opcoes.get("compensador") and alvo is not None:
                comp = opcoes["compensador"]
                r = compensar(p, g, self.doses_um, alvo, m.colim, m.modelo, campos=comp.get("campos") or None,
                              niveis=int(comp.get("niveis", 6)), um_min_segmento=float(comp.get("um_min", 2.0)))
                info["compensador"] = {"segmentos": r.n_segmentos, "hi_antes": r.hi_antes, "hi_depois": r.hi_depois,
                                       "historico": r.historico, "avisos": r.avisos}
                self.avisos = list(r.avisos)
            self.dose_fx = calculo.normalizar(p, self.doses_um, g, alvo)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        total = self.dose_fx * pr.fracoes
        hist = {n: dvh(total, e.mascara, g.voxel_cc) for n, e in self.estruturas.items() if e.mascara.any()}
        conf = conferir_plano(p, m.colim, m.tabelas, self.doses_um, g,
                              ponto_mm=pr.ponto_mm if pr.metodo == "ponto" else None) if m.tabelas else []
        for k in conf:
            c = self._campo(k["campo"])
            k["compensado"] = bool(c.segmentos)
        hi = indice_homogeneidade(total, alvo) if alvo is not None and alvo.any() else None
        self.resultado = {"dvh": hist, "conferencia_um": conf, "info": info, "hi": hi,
                          "restricoes": self._avaliar_restricoes(total),
                          "dmax_gy": float(total[corpo].max()) if corpo.any() else float(total.max()),
                          "quando": dt.datetime.now().isoformat(timespec="seconds")}
        self.dose_atualizada = True
        self._registrar(f"Dose calculada ({len(p.campos)} campos)")
        return self.resumo()

    def _avaliar_restricoes(self, total) -> dict:
        """Restrições QUANTEC/ICRU (ou as da clínica) com nomes TG-263. Nunca impede o cálculo."""
        from tps.planejamento import restricoes
        pr = self.plano.prescricao
        try:
            doses = {n: total[e.mascara] for n, e in self.estruturas.items() if e.mascara.any()}
            return restricoes.avaliar(doses, self.grade.voxel_cc, pr.dose_total_gy, pr.fracoes,
                                      pr.estrutura_alvo).json()
        except Exception as e:  # noqa: BLE001
            return {"erro": f"avaliação das restrições falhou: {e}", "resultados": [], "avisos": []}

    def restricoes_json(self) -> dict:
        self._exigir(self.dose_atualizada, "Dose não calculada")
        return self.resultado.get("restricoes") or {"resultados": [], "avisos": []}

    def _recalcular_aprovado(self) -> dict:
        """Plano aprovado (ex.: reaberto): recalcula a dose com as UM gravadas, sem mudar nada no plano, e confere que
        a normalização ainda bate com a prescrição (0,5%)."""
        p, m, g = self.plano, self.maquina, self.grade
        if calculo.hash_modelo(m.modelo) != p.modelo_feixe.get("hash"):
            raise ErroUsuario("O modelo de feixe mudou desde a aprovação: desaprove e recalcule o plano")
        faltam = [c for c in p.campos if c.nome not in self.doses_um]
        if faltam:
            self.doses_um.update(calculo.dose_por_um(g, faltam, m.colim, m.modelo))
        self.dose_fx = sum(c.mu * self.doses_um[c.nome] for c in p.campos)
        pr = p.prescricao
        alvo = self.estruturas[pr.estrutura_alvo].mascara if pr.estrutura_alvo in self.estruturas else None
        val = calculo.metrica_normalizacao(self.dose_fx, p, g, alvo)
        esperado = pr.dose_fracao_gy * 100.0 / pr.isodose_pct
        if abs(val / esperado - 1) > 0.005:
            raise ErroUsuario(f"A dose recalculada não reproduz a prescrição ({val:.4f} × {esperado:.4f} Gy/fx)")
        total = self.dose_fx * pr.fracoes
        hist = {n: dvh(total, e.mascara, g.voxel_cc) for n, e in self.estruturas.items() if e.mascara.any()}
        conf = conferir_plano(p, m.colim, m.tabelas, self.doses_um, g,
                              ponto_mm=pr.ponto_mm if pr.metodo == "ponto" else None) if m.tabelas else []
        corpo = self._corpo()
        self.resultado = {"dvh": hist, "conferencia_um": conf, "info": {"recalculo_aprovado": True},
                          "restricoes": self._avaliar_restricoes(total),
                          "hi": indice_homogeneidade(total, alvo) if alvo is not None and alvo.any() else None,
                          "dmax_gy": float(total[corpo].max()), "quando": dt.datetime.now().isoformat(timespec="seconds")}
        self.dose_atualizada = True
        self._registrar("Dose do plano aprovado recalculada e conferida")
        return self.resumo()

    # ================================================================== aprovação e exportação
    def aprovar(self, usuario: str, papel: str):
        self._exigir_plano()
        self._exigir(self.dose_atualizada, "Calcule a dose antes de aprovar")
        self._exigir(bool(usuario.strip()), "Informe o usuário")
        avisos_um = [k for k in self.resultado.get("conferencia_um", []) if not k["ok"] and not k.get("compensado")]
        if avisos_um:
            raise ErroUsuario("Conferência de UM fora da tolerância em " + ", ".join(k["campo"] for k in avisos_um) +
                              ": verifique o plano")
        try:
            self.plano.aprovar(usuario.strip(), papel)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        self._registrar(f"Aprovado por {usuario} ({papel})")

    def desaprovar(self, usuario: str, motivo: str):
        self._exigir_plano()
        try:
            self.plano.desaprovar(usuario, motivo)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
        self._registrar(f"Desaprovado por {usuario}: {motivo}")

    def exportar(self, destino: str | Path, modo_segmentos: str = "campos_separados",
                 incluir_imagens: bool | None = None) -> dict:
        self._exigir_plano()
        self._exigir(self.dose_atualizada and self.dose_fx is not None, "Calcule a dose antes de exportar")
        destino = Path(destino)
        destino.mkdir(parents=True, exist_ok=True)
        p = self.plano
        base = f"{p.rotulo}_{dt.datetime.now():%Y%m%d_%H%M%S}"
        try:
            rp = exportar_rtplan(p, self.maquina.colim, destino / f"RP_{base}.dcm", modo_segmentos=modo_segmentos)
        except (ValueError, PermissionError) as e:
            raise ErroUsuario(str(e)) from e
        problemas = conferir_rtplan(pydicom.dcmread(destino / f"RP_{base}.dcm"))
        if problemas:
            raise ErroUsuario("RTPLAN reprovado na conferência: " + "; ".join(problemas))
        arquivos = [destino / f"RP_{base}.dcm"]
        g = self.grade
        exportar_rtdose(self.dose_fx * p.prescricao.fracoes, g.x, g.y, g.z, rp, destino / f"RD_{base}.dcm")
        arquivos.append(destino / f"RD_{base}.dcm")
        if incluir_imagens is None:
            incluir_imagens = self.tipo == "fantoma"          # CT real: o Mosaiq normalmente já tem as imagens
        if incluir_imagens:
            img = destino / f"imagens_{base}"
            img.mkdir(exist_ok=True)
            for f in sorted(self.pasta_ct.iterdir()):
                if f.is_file() and (self._e_ct(f) or f == self.arquivo_rs):
                    shutil.copy2(f, img / f.name)
                    arquivos.append(img / f.name)
        self._registrar(f"Exportado para {destino} ({modo_segmentos})")
        return {"arquivos": [str(a) for a in arquivos], "pasta": str(destino)}

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
                               for n, d in self.derivadas.items() if n in self.estruturas}}
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
        pd_ = {**pd_, "prescricao": rx, "campos": campos}
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
        self._aplicar_eta()
        for c in self.plano.campos:
            c.__dict__["_spec"] = (dados.get("specs") or {}).get(c.nome)
        self._registrar(f"Plano aberto de {arquivo} (estado {self.plano.estado}; dose a recalcular)")

    # ================================================================== dados para a tela
    def _campo(self, nome: str) -> Campo:
        c = next((c for c in self.plano.campos if c.nome == nome), None)
        if c is None:
            raise ErroUsuario(f"Campo {nome} não existe")
        return c

    def _campo_json(self, c: Campo) -> dict:
        d = {k: v for k, v in dataclasses.asdict(c).items() if not k.startswith("_")}
        d["n_segmentos"] = len(c.segmentos) if c.segmentos else 0
        d["spec"] = c.__dict__.get("_spec")
        return d

    def resumo(self) -> dict:
        p = self.plano
        out = {"id": self.id, "nome": self.nome, "tipo": self.tipo, "paciente": self.paciente,
               "estruturas": [{"nome": e.nome, "tipo": e.tipo, "cor": e.cor,
                               "volume_cc": round(float(e.mascara.sum()) * self.grade.voxel_cc, 1)}
                              for e in self.estruturas.values()] if self.grade is not None else [],
               "maquina": {"id": self.maquina.id, "energia": self.maquina.energia, "nome_dicom": self.maquina.nome_dicom,
                           "mlc": self.maquina.colim.nome, "n_pares": self.maquina.colim.n_pares,
                           "limites": list(map(float, self.maquina.colim.limites_laminas)),
                           "tem_jaw_x": self.maquina.colim.tem_jaw_x, "gap": self.maquina.colim.gap_minimo_mm,
                           "comissionamento": (self.maquina.modelo.ref or {}).get("comissionamento", "v1")}
               if self.maquina else None,
               "isocentro": self.isocentro, "dose_atualizada": self.dose_atualizada, "avisos": self.avisos,
               "log": self.log[-30:]}
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
                            "pontos": pts, "bolus": p.bolus, "densidades": p.densidades,
                            "prescricao": dataclasses.asdict(p.prescricao), "historico": p.historico,
                            "campos": [self._campo_json(c) for c in p.campos]}
            if self.dose_atualizada and self.resultado:
                r = self.resultado
                out["resultado"] = {"hi": r["hi"], "dmax_gy": r["dmax_gy"], "info": r["info"],
                                    "restricoes": r.get("restricoes"),
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

    def _plano_corte(self, vista: str, idx: int):
        """Coordenada (mm) do corte e eixos (horizontal, vertical) no sistema do paciente."""
        cx, cy, cz = self.ct.axes()
        if vista == "axial":
            return float(cz[idx]), "x", "y"
        if vista == "coronal":
            return float(cy[idx]), "x", "z"
        return float(cx[idx]), "y", "z"

    def sobreposicao(self, vista: str, idx: int, estruturas: list[str] | None = None, isodoses: bool = True) -> dict:
        """Contornos, isodoses e feixes do corte, em mm (h, v)."""
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
        if isodoses and self.dose_atualizada and self.dose_fx is not None:
            rx = self.plano.prescricao.dose_fracao_gy
            img = corte_grade(self.dose_fx / rx * 100.0)
            for nv in NIVEIS_ISODOSE:
                ls = linhas(img, nv)
                if ls:
                    out["isodoses"].append({"nivel": nv, "linhas": ls})
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
                "gap": colim.gap_minimo_mm, "interdigitacao": colim.interdigitacao}

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
