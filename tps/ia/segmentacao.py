"""Segmentação automática de órgãos por IA (TotalSegmentator, Wasserthal et al., Radiology: AI 2023; Apache-2.0).

O TPS não embute a rede (≈ 2 GB com PyTorch): usa o TotalSegmentator instalado no computador, num ambiente Python
próprio, e conversa com ele por arquivos (CT em DICOM → RTSTRUCT). Onde procurar o executável, em ordem:
  1. variável de ambiente TPS_TOTALSEG;
  2. <pasta de dados>/ia/config.yaml  →  executavel: C:\\...\\TotalSegmentator.exe
  3. <pasta de dados>/ia/venv/Scripts/TotalSegmentator.exe (Windows) ou .../bin/TotalSegmentator;
  4. TotalSegmentator no PATH.
Os pesos da rede são baixados pelo próprio TotalSegmentator no primeiro uso (precisa de internet uma vez).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from tps import paths

# id → (nome no TPS, classes do TotalSegmentator, cor). Nomes do TPS reconhecidos pelo TG-263 das restrições.
ORGAOS = {
    "bexiga": ("Bexiga", ["urinary_bladder"], (255, 220, 0)),
    "intestino_delgado": ("Intestino_Delgado", ["small_bowel", "duodenum"], (255, 150, 80)),
    "colon": ("Colon_Sigmoide", ["colon"], (200, 120, 60)),
    "femur_e": ("Cabeca_Femoral_E", ["femur_left"], (150, 220, 150)),
    "femur_d": ("Cabeca_Femoral_D", ["femur_right"], (120, 200, 120)),
    "prostata": ("Prostata", ["prostate"], (0, 200, 255)),
    "rim_e": ("Rim_E", ["kidney_left"], (230, 120, 120)),
    "rim_d": ("Rim_D", ["kidney_right"], (210, 100, 100)),
    "figado": ("Figado", ["liver"], (170, 90, 60)),
    "estomago": ("Estomago", ["stomach"], (255, 180, 200)),
    "medula": ("Medula", ["spinal_cord"], (255, 255, 0)),
    "coracao": ("Coracao", ["heart"], (255, 60, 60)),
    "esofago": ("Esofago", ["esophagus"], (230, 160, 255)),
    "pulmao_e": ("Pulmao_E", ["lung_upper_lobe_left", "lung_lower_lobe_left"], (90, 160, 255)),
    "pulmao_d": ("Pulmao_D", ["lung_upper_lobe_right", "lung_middle_lobe_right", "lung_lower_lobe_right"],
                 (60, 220, 255)),
    "traqueia": ("Traqueia", ["trachea"], (180, 255, 255)),
    "tireoide": ("Tireoide", ["thyroid_gland"], (255, 120, 255)),
    "encefalo": ("Encefalo", ["brain"], (255, 200, 150)),
    "aorta": ("Aorta", ["aorta"], (255, 80, 80)),
    "baco": ("Baco", ["spleen"], (160, 60, 160)),
    "pancreas": ("Pancreas", ["pancreas"], (240, 200, 120)),
}
CABECA_FEMORAL_MM = 70.0        # das classes de fêmur, guarda só os 7 cm superiores (cabeça + colo)


def executavel() -> Path | None:
    env = os.environ.get("TPS_TOTALSEG")
    if env and Path(env).exists():
        return Path(env)
    cfg = paths.home() / "ia" / "config.yaml"
    if cfg.exists():
        try:
            e = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("executavel")
            if e and Path(e).exists():
                return Path(e)
        except Exception:  # noqa: BLE001
            pass
    for c in (paths.home() / "ia" / "venv" / "Scripts" / "TotalSegmentator.exe",
              paths.home() / "ia" / "venv" / "bin" / "TotalSegmentator"):
        if c.exists():
            return c
    w = shutil.which("TotalSegmentator")
    return Path(w) if w else None


def estado() -> dict:
    exe = executavel()
    return {"disponivel": exe is not None, "executavel": str(exe) if exe else None,
            "motivo": None if exe else "TotalSegmentator não instalado (veja menu Iniciar > TPS > Instalar contorno por IA)",
            "orgaos": [{"id": k, "nome": v[0]} for k, v in ORGAOS.items()]}


def segmentar(arquivos_ct: list[Path], orgaos: list[str], rapido: bool = True, tempo_max_s: int = 1800,
              dispositivo: str | None = None) -> tuple[Path, dict]:
    """Roda o TotalSegmentator nos cortes do CT e devolve (RTSTRUCT gerado, {id: [nomes das ROIs]})."""
    exe = executavel()
    if exe is None:
        raise RuntimeError("TotalSegmentator não está instalado neste computador")
    desconhecidos = [o for o in orgaos if o not in ORGAOS]
    if desconhecidos:
        raise ValueError(f"Órgãos desconhecidos: {', '.join(desconhecidos)}")
    classes = sorted({c for o in orgaos for c in ORGAOS[o][1]})
    tmp = Path(tempfile.mkdtemp(prefix="tps_ia_"))
    ent = tmp / "ct"
    ent.mkdir()
    from tps.importacao.rtplan import ler_dicom
    for k, f in enumerate(arquivos_ct):             # regrava com preâmbulo e File Meta (o Monaco grava sem)
        d = ler_dicom(f)
        fm = d.file_meta
        fm.MediaStorageSOPClassUID = d.SOPClassUID
        fm.MediaStorageSOPInstanceUID = d.SOPInstanceUID
        d.save_as(ent / f"CT{k:04d}.dcm", enforce_file_format=True)
    saida = tmp / "seg.dcm"                          # --output_type dicom grava UM arquivo RTSTRUCT
    cmd = [str(exe), "-i", str(ent), "-o", str(saida), "--output_type", "dicom", "--roi_subset", *classes]
    if rapido:
        cmd.append("--fast")
    if dispositivo:
        cmd += ["--device", dispositivo]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    kw = {"creationflags": flags} if sys.platform == "win32" else {"start_new_session": True}
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    try:
        out, err = pr.communicate(timeout=tempo_max_s)
    except subprocess.TimeoutExpired:
        _matar_arvore(pr)                       # o nnU-Net abre processos filhos: não deixar órfãos
        raise RuntimeError(f"TotalSegmentator passou de {tempo_max_s // 60} min e foi interrompido")
    r = subprocess.CompletedProcess(cmd, pr.returncode, out, err)
    if saida.is_dir():
        rs = next(iter(sorted(saida.rglob("*.dcm"))), None)
    else:
        rs = saida if saida.exists() else None
    if r.returncode != 0 or rs is None:
        raise RuntimeError("TotalSegmentator falhou: " + (r.stderr or r.stdout or "")[-800:])
    return rs, {o: ORGAOS[o][1] for o in orgaos}


def _matar_arvore(pr):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pr.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.killpg(os.getpgid(pr.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pr.kill()
