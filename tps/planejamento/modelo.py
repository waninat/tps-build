"""Modelo de dados do plano de tratamento. SRS-PLN-001..006.

Um `Plano` é o objeto central do TPS: campos, prescrição, máquina, estado de aprovação. Ele não conhece DICOM;
a exportação (tps.exportacao) converte um plano aprovado em RTPLAN/RTDOSE.

Estado e bloqueio (IEC 62083, 7.x — plano aprovado não pode ser alterado sem registro):
    RASCUNHO  → editável
    APROVADO  → congelado; o hash do conteúdo é gravado na aprovação e conferido antes de exportar.
    Para alterar um plano aprovado é preciso `desaprovar(usuario, motivo)`, que volta ao rascunho e fica no histórico.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass, field

import numpy as np


class PlanoBloqueado(PermissionError):
    """Tentativa de alterar um plano aprovado."""


class PlanoAlteradoAposAprovacao(RuntimeError):
    """O conteúdo do plano não corresponde ao hash gravado na aprovação."""


@dataclass
class Prescricao:
    dose_total_gy: float
    fracoes: int
    estrutura_alvo: str                 # nome da estrutura (ex.: PTV)
    metodo: str = "media_alvo"          # media_alvo | isocentro | ponto
    ponto_mm: tuple | None = None       # para metodo="ponto"
    isodose_pct: float = 100.0          # prescrição à isodose de x% do valor de normalização

    @property
    def dose_fracao_gy(self) -> float:
        return self.dose_total_gy / self.fracoes

    def validar(self) -> list[str]:
        erros = []
        if not (0 < self.dose_total_gy <= 100):
            erros.append(f"Dose total fora da faixa aceita (0–100 Gy): {self.dose_total_gy}")
        if not (1 <= self.fracoes <= 60):
            erros.append(f"Número de frações fora da faixa aceita (1–60): {self.fracoes}")
        if self.dose_fracao_gy > 25:
            erros.append(f"Dose por fração acima de 25 Gy: {self.dose_fracao_gy:.2f}")
        if self.metodo not in ("media_alvo", "isocentro", "ponto", "um_fixas"):
            erros.append(f"Método de normalização desconhecido: {self.metodo}")
        if self.metodo == "ponto" and self.ponto_mm is None:
            erros.append("Normalização em ponto sem coordenada")
        if not (50 <= self.isodose_pct <= 110):
            erros.append(f"Isodose de prescrição fora da faixa (50–110%): {self.isodose_pct}")
        return erros


@dataclass
class Campo:
    nome: str
    gantry: float
    colimador: float = 0.0
    mesa: float = 0.0
    isocentro_mm: tuple = (0.0, 0.0, 0.0)
    energia_mv: float = 6.0
    peso: float = 1.0                    # peso relativo na normalização
    jaw_x: tuple | None = None           # mm no isocentro (None = sem colimador X, ex.: Agility)
    jaw_y: tuple | None = None
    mlc_a: list | None = None            # banco A (lado negativo), mm no isocentro
    mlc_b: list | None = None
    mu: float | None = None              # definido pela normalização
    ssd_mm: float | None = None          # calculada no CT
    taxa_dose: float = 600.0
    alvo_conformado: str | None = None   # estrutura usada na conformação (rastreabilidade)
    margem_mm: float | None = None
    campo_pai: str | None = None         # sub-campo (field-in-field) de qual campo
    segmentos: list | None = None        # compensador: [{"mlc_a","mlc_b","jaw_x","jaw_y","peso"}], Σ peso = 1
    descricao: str = ""                  # BeamDescription (ex.: "LD", "ANT" — como o Monaco grava)

    def abertura_definida(self) -> bool:
        return self.jaw_y is not None and self.mlc_a is not None and self.mlc_b is not None


@dataclass
class Plano:
    rotulo: str                          # RTPlanLabel (até 16 caracteres no DICOM)
    paciente_id: str
    maquina_id: str
    nome_dicom_maquina: str              # TreatmentMachineName — tem de bater com o nome no Mosaiq
    prescricao: Prescricao
    campos: list[Campo] = field(default_factory=list)
    posicao_paciente: str = "HFS"
    tecnica: str = "3D"
    frame_of_reference: str = ""
    referencia_ct: dict = field(default_factory=dict)      # UIDs de estudo/série/paciente do CT
    referencia_rtstruct: str | None = None                  # SOPInstanceUID do RTSTRUCT
    modelo_feixe: dict = field(default_factory=dict)        # id, versão e hash do modelo usado no cálculo
    estado: str = "RASCUNHO"
    aprovacao: dict | None = None
    historico: list[dict] = field(default_factory=list)
    densidades: dict = field(default_factory=dict)          # estrutura → densidade eletrônica relativa forçada
    bolus: list = field(default_factory=list)               # [{"nome", "espessura_mm", "campos"}]
    pontos: list = field(default_factory=list)              # [{"nome", "xyz"}] pontos de referência

    # ------------------------------------------------------------------ edição controlada
    def _exigir_rascunho(self):
        if self.estado != "RASCUNHO":
            raise PlanoBloqueado(f"Plano '{self.rotulo}' está {self.estado}: desaprove antes de alterar")

    def adicionar_campo(self, campo: Campo) -> None:
        self._exigir_rascunho()
        if any(c.nome == campo.nome for c in self.campos):
            raise ValueError(f"Já existe um campo chamado {campo.nome}")
        self.campos.append(campo)

    def alterar(self, **kw) -> None:
        self._exigir_rascunho()
        for k, v in kw.items():
            if not hasattr(self, k) or k in ("estado", "aprovacao", "historico"):
                raise AttributeError(k)
            setattr(self, k, v)

    # ------------------------------------------------------------------ integridade
    def conteudo(self) -> dict:
        """Tudo o que define o tratamento (sem estado/aprovação/histórico): base do hash."""
        d = asdict(self)
        for k in ("estado", "aprovacao", "historico", "pontos"):
            d.pop(k)
        for c in d.get("campos", []):           # campo sem descrição: mesmo hash das versões anteriores
            if not c.get("descricao"):
                c.pop("descricao", None)
        return _normalizar(d)

    def hash_conteudo(self) -> str:
        txt = json.dumps(self.conteudo(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(txt.encode("utf-8")).hexdigest()

    def validar(self) -> list[str]:
        erros = list(self.prescricao.validar())
        if not self.campos:
            erros.append("Plano sem campos")
        if len(self.rotulo) > 16:
            erros.append("Rótulo do plano com mais de 16 caracteres (limite do RTPlanLabel)")
        for c in self.campos:
            if not c.abertura_definida():
                erros.append(f"Campo {c.nome}: abertura (jaws/MLC) não definida")
            if c.mu is None or c.mu <= 0:
                erros.append(f"Campo {c.nome}: UM não calculada")
            if len(c.nome) > 16:
                erros.append(f"Campo {c.nome}: nome com mais de 16 caracteres")
        if not self.modelo_feixe.get("hash"):
            erros.append("Dose não calculada com um modelo de feixe identificado")
        return erros

    # ------------------------------------------------------------------ aprovação
    def aprovar(self, usuario: str, papel: str) -> None:
        self._exigir_rascunho()
        erros = self.validar()
        if erros:
            raise ValueError("Plano não pode ser aprovado: " + "; ".join(erros))
        quando = dt.datetime.now().isoformat(timespec="seconds")
        self.aprovacao = {"usuario": usuario, "papel": papel, "quando": quando, "hash": self.hash_conteudo()}
        self.estado = "APROVADO"
        self.historico.append({"evento": "aprovado", "usuario": usuario, "quando": quando,
                               "hash": self.aprovacao["hash"]})

    def desaprovar(self, usuario: str, motivo: str) -> None:
        if self.estado != "APROVADO":
            raise ValueError("Plano não está aprovado")
        if not motivo.strip():
            raise ValueError("Motivo obrigatório para desaprovar")
        self.historico.append({"evento": "desaprovado", "usuario": usuario, "motivo": motivo,
                               "quando": dt.datetime.now().isoformat(timespec="seconds")})
        self.estado, self.aprovacao = "RASCUNHO", None

    def conferir_integridade(self) -> None:
        """Antes de exportar: o plano aprovado ainda é o mesmo que foi aprovado?"""
        if self.estado != "APROVADO" or not self.aprovacao:
            raise PlanoBloqueado("Só um plano aprovado pode ser exportado para tratamento")
        if self.hash_conteudo() != self.aprovacao["hash"]:
            raise PlanoAlteradoAposAprovacao("O conteúdo do plano mudou depois da aprovação")

    def copia_rascunho(self, novo_rotulo: str) -> "Plano":
        p = copy.deepcopy(self)
        p.rotulo, p.estado, p.aprovacao = novo_rotulo, "RASCUNHO", None
        p.historico = [{"evento": "copiado", "de": self.rotulo, "hash_origem": self.hash_conteudo()}]
        return p


def _normalizar(x):
    """Converte para tipos JSON estáveis (floats arredondados a 1e-6 para o hash não depender de ruído)."""
    if isinstance(x, dict):
        return {k: _normalizar(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, np.ndarray)):
        return [_normalizar(v) for v in list(x)]
    if isinstance(x, (np.floating, float)):
        return round(float(x), 6)
    if isinstance(x, np.integer):
        return int(x)
    return x
