"""Modelo de máquina (geometria + limites) carregado de YAML. SRS-MCH-001..003."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from tps import paths


def machines_dir() -> Path:
    return paths.machines_dir()


@dataclass
class MachineModel:
    id: str
    raw: dict

    @property
    def mlc(self) -> dict:
        return self.raw["collimation"]["mlc"]

    @property
    def has_x_jaw(self) -> bool:
        return bool(self.raw["collimation"]["jaw_x"]["present"])

    @property
    def energies(self) -> list:
        return self.raw.get("energies_mv", [])

    def matches(self, treatment_machine_name: str) -> bool:
        names = [n for n in self.raw.get("dicom_machine_names", []) if n != "CONFIRMAR"]
        return treatment_machine_name in names


def load_machine(path_or_id: str | Path) -> MachineModel:
    p = Path(path_or_id)
    if not p.exists():
        p = machines_dir() / f"{path_or_id}.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return MachineModel(id=data["id"], raw=data)


def all_machines() -> list[MachineModel]:
    return [load_machine(p) for p in sorted(machines_dir().glob("*.yaml"))]


def find_machine_for(treatment_machine_name: str, n_leaf_pairs: int | None) -> MachineModel | None:
    """Identifica o modelo pelo nome DICOM; se não configurado, usa o nº de pares de lâminas."""
    machines = all_machines()
    for m in machines:
        if m.matches(treatment_machine_name):
            return m
    candidates = [m for m in machines if m.mlc["leaf_pairs"] == n_leaf_pairs]
    return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------- cadastro pela interface
MLC_PRESETS = {
    "agility": {"nome": "Elekta Agility (160 lâminas, 5 mm)", "pairs": 80, "widths": [(80, 5.0)], "jaw_x": False,
                "type": "MLCX", "overtravel": 150.0, "interdig": True},
    "mlci": {"nome": "Elekta MLCi / MLCi2 (80 lâminas, 10 mm)", "pairs": 40, "widths": [(40, 10.0)], "jaw_x": True,
             "type": "MLCX", "overtravel": 125.0, "interdig": False},
    "beam_modulator": {"nome": "Elekta Beam Modulator (80 lâminas, 4 mm)", "pairs": 40, "widths": [(40, 4.0)], "jaw_x": False,
                       "type": "MLCX", "overtravel": 110.0, "interdig": True},
    "millennium120": {"nome": "Varian Millennium 120 (5/10 mm)", "pairs": 60, "widths": [(10, 10.0), (40, 5.0), (10, 10.0)],
                      "jaw_x": True, "type": "MLCX", "overtravel": 150.0, "interdig": True},
    "hd120": {"nome": "Varian HD120 (2,5/5 mm)", "pairs": 60, "widths": [(14, 5.0), (32, 2.5), (14, 5.0)], "jaw_x": True,
              "type": "MLCX", "overtravel": 150.0, "interdig": True},
}


def slugify(text: str) -> str:
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:40] or "maquina"


def save_machine(data: dict) -> Path:
    """Grava machines/<id>.yaml a partir do formulário de cadastro."""
    preset = MLC_PRESETS.get(data["mlc_preset"])
    if preset:
        pairs, width, jaw_x = preset["pairs"], preset["widths"][0][1] if len(preset["widths"]) == 1 else None, preset["jaw_x"]
        widths = preset["widths"]
    else:
        pairs, width, jaw_x = int(data["mlc_pares"]), float(data["mlc_largura"]), bool(data.get("jaw_x"))
        widths = [(pairs, width)]
    raw = {
        "id": data["id"], "vendor": data["fabricante"], "model": data["modelo"],
        "serial": str(data.get("serial", "")).strip(),
        "dicom_machine_names": [n.strip() for n in str(data["nomes_dicom"]).split(",") if n.strip()],
        "sad_mm": float(data.get("sad_mm", 1000.0)),
        "energies_mv": sorted({int(''.join(ch for ch in e if ch.isdigit())) for e in data.get("energias", [])}),
        "energias": list(data.get("energias", [])),
        "fff": any("FFF" in e for e in data.get("energias", [])),
        "techniques": data.get("tecnicas", ["3D", "STEP&SHOOT", "VMAT"]),
        "collimation": {
            "jaw_x": {"present": jaw_x, "dicom_types": ["ASYMX", "X"] if jaw_x else []},
            "jaw_y": {"present": True, "dicom_types": ["ASYMY", "Y"]},
            "mlc": {"dicom_type": "MLCX", "leaf_pairs": pairs, "leaf_width_mm": width, "widths": [list(w) for w in widths],
                    "preset": data["mlc_preset"], "max_overtravel_mm": preset["overtravel"] if preset else 150.0,
                    "min_gap_mm": float(data.get("gap_minimo_mm", 0.0)),
                    "interdigitation": preset["interdig"] if preset else True},
        },
        "parametros_mlc_tps": {k: float(data[k]) for k in ("mlc_transmission", "jaw_transmission", "interleaf_factor",
                                                             "groove_width_mm", "leaf_tip_offset_mm") if k in data},
    }
    path = machines_dir() / f"{data['id']}.yaml"
    path.write_text("# Cadastrado pelo TPS\n" + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path
