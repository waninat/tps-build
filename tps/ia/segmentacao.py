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

# id → (nome no TPS, tarefa do TotalSegmentator, classes, cor, pós-processamento). Nomes reconhecidos pelo TG-263.
# Só tarefas gratuitas (as que pedem licença comercial ficam de fora: tronco, câmaras cardíacas, coronárias).
ORGAOS = {
    "bexiga": ("Bexiga", "total", ["urinary_bladder"], (255, 220, 0), None),
    "reto": ("Reto", "total", ["colon"], (150, 90, 40), "reto"),
    "parede_retal": ("Parede_Retal", "total", ["colon"], (190, 110, 50), "parede_retal"),
    "canal_anal": ("Canal_Anal", "total", ["colon"], (120, 70, 30), "canal_anal"),
    # bowel bag pelo envelope das alças do modelo rápido (o modelo de cavidades é de alta resolução: 10–20 min e
    # mais de 8 GB de memória na CPU — derrubava o TPS em computadores de 8 GB)
    "bowel_bag": ("Bowel_Bag", "total", ["small_bowel", "duodenum", "colon"], (255, 170, 110), "bowel_bag"),
    "intestino_delgado": ("Intestino_Delgado", "total", ["small_bowel", "duodenum"], (255, 150, 80), None),
    "colon": ("Colon_Sigmoide", "total", ["colon"], (200, 120, 60), None),
    "femur_e": ("Cabeca_Femoral_E", "total", ["femur_left"], (150, 220, 150), "cabeca_femoral"),
    "femur_d": ("Cabeca_Femoral_D", "total", ["femur_right"], (120, 200, 120), "cabeca_femoral"),
    "prostata": ("Prostata", "total", ["prostate"], (0, 200, 255), None),
    "rim_e": ("Rim_E", "total", ["kidney_left"], (230, 120, 120), None),
    "rim_d": ("Rim_D", "total", ["kidney_right"], (210, 100, 100), None),
    "figado": ("Figado", "total", ["liver"], (170, 90, 60), None),
    "estomago": ("Estomago", "total", ["stomach"], (255, 180, 200), None),
    "medula": ("Medula", "total", ["spinal_cord"], (255, 255, 0), None),
    "coracao": ("Coracao", "total", ["heart"], (255, 60, 60), None),
    "esofago": ("Esofago", "total", ["esophagus"], (230, 160, 255), None),
    "pulmao_e": ("Pulmao_E", "total", ["lung_upper_lobe_left", "lung_lower_lobe_left"], (90, 160, 255), None),
    "pulmao_d": ("Pulmao_D", "total", ["lung_upper_lobe_right", "lung_middle_lobe_right", "lung_lower_lobe_right"],
                 (60, 220, 255), None),
    "traqueia": ("Traqueia", "total", ["trachea"], (180, 255, 255), None),
    "tireoide": ("Tireoide", "total", ["thyroid_gland"], (255, 120, 255), None),
    "encefalo": ("Encefalo", "total", ["brain"], (255, 200, 150), None),
    "mama_e": ("Mama_E", "breasts", ["breast"], (255, 190, 220), "lado_e"),
    "mama_d": ("Mama_D", "breasts", ["breast"], (255, 160, 200), "lado_d"),
    "parotida_e": ("Parotida_E", "head_glands_cavities", ["parotid_gland_left"], (0, 230, 160), None),
    "parotida_d": ("Parotida_D", "head_glands_cavities", ["parotid_gland_right"], (0, 200, 140), None),
    "submandibular_e": ("Submandibular_E", "head_glands_cavities", ["submandibular_gland_left"], (100, 230, 200), None),
    "submandibular_d": ("Submandibular_D", "head_glands_cavities", ["submandibular_gland_right"], (80, 200, 180), None),
    "olho_e": ("Olho_E", "head_glands_cavities", ["eye_left"], (120, 200, 255), None),
    "olho_d": ("Olho_D", "head_glands_cavities", ["eye_right"], (100, 180, 255), None),
    "cristalino_e": ("Cristalino_E", "head_glands_cavities", ["eye_lens_left"], (255, 255, 255), None),
    "cristalino_d": ("Cristalino_D", "head_glands_cavities", ["eye_lens_right"], (230, 230, 230), None),
    "nervo_optico_e": ("Nervo_Optico_E", "head_glands_cavities", ["optic_nerve_left"], (255, 230, 0), None),
    "nervo_optico_d": ("Nervo_Optico_D", "head_glands_cavities", ["optic_nerve_right"], (255, 210, 0), None),
    "cavidade_oral": ("Cavidade_Oral", "head_glands_cavities", ["oropharynx", "soft_palate"], (255, 140, 140), None),
    "constritores": ("Constritores_Faringe", "headneck_muscles",
                     ["superior_pharyngeal_constrictor", "middle_pharyngeal_constrictor",
                      "inferior_pharyngeal_constrictor"], (210, 140, 255), None),
    "mandibula": ("Mandibula", "craniofacial_structures", ["mandible"], (240, 240, 200), None),
}
GRUPOS = {
    "Pelve": ["bexiga", "reto", "parede_retal", "canal_anal", "bowel_bag", "intestino_delgado", "colon", "femur_e",
              "femur_d", "prostata"],
    "Tórax e abdome": ["coracao", "pulmao_e", "pulmao_d", "esofago", "medula", "traqueia", "mama_e", "mama_d",
                       "figado", "estomago", "rim_e", "rim_d"],
    "Cabeça e pescoço": ["parotida_e", "parotida_d", "submandibular_e", "submandibular_d", "olho_e", "olho_d",
                         "cristalino_e", "cristalino_d", "nervo_optico_e", "nervo_optico_d", "cavidade_oral",
                         "constritores", "mandibula", "tireoide", "encefalo"],
}
CABECA_FEMORAL_MM = 70.0        # das classes de fêmur, guarda só os 7 cm superiores (cabeça + colo)
CANAL_ANAL_MM = 30.0            # canal anal = 3 cm inferiores do reto segmentado
PAREDE_RETAL_MM = 3.0
BOWEL_BAG_ACIMA_DO_ALVO_MM = 20.0


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
            "motivo": None if exe else "Segmentação automática não instalada: menu Ferramentas > Segmentação automática de órgãos… (ou menu Iniciar > TPS > Instalar segmentação automática)",
            "orgaos": [{"id": k, "nome": ORGAOS[k][0], "grupo": g} for g, ks in GRUPOS.items() for k in ks]}


def _rodar(exe: Path, ent: Path, saida: Path, tarefa: str, classes: list[str], rapido: bool, tempo_max_s: int,
           dispositivo: str | None) -> Path:
    cmd = [str(exe), "-i", str(ent), "-o", str(saida), "--output_type", "dicom",
           "--nr_thr_resamp", "1", "--nr_thr_saving", "1"]        # menos memória ao reamostrar e gravar
    if tarefa == "total":
        cmd += ["--roi_subset", *classes]
        if rapido:
            cmd.append("--fast")
    else:
        cmd += ["--task", tarefa, "--force_split"]                # alta resolução: em 3 partes (menos memória)
    if dispositivo:
        cmd += ["--device", dispositivo]
    # prioridade abaixo do normal: o TPS (e o Windows) continuam respondendo enquanto a rede roda
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)) \
        if sys.platform == "win32" else 0
    kw = {"creationflags": flags} if sys.platform == "win32" else {"start_new_session": True}
    pr = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    try:
        out, err = pr.communicate(timeout=tempo_max_s)
    except subprocess.TimeoutExpired:
        _matar_arvore(pr)                       # o nnU-Net abre processos filhos: não deixar órfãos
        raise RuntimeError(f"Segmentação automática ({tarefa}) passou de {tempo_max_s // 60} min e foi interrompido")
    rs = next(iter(sorted(saida.rglob("*.dcm"))), None) if saida.is_dir() else (saida if saida.exists() else None)
    if pr.returncode != 0 or rs is None:
        raise RuntimeError(f"Segmentação automática ({tarefa}) falhou: " + (err or out or "")[-800:])
    return rs


def segmentar(arquivos_ct: list[Path], orgaos: list[str], rapido: bool = True, tempo_max_s: int = 1800,
              dispositivo: str | None = None) -> dict[str, list]:
    """Roda o TotalSegmentator (uma vez por tarefa) e devolve {classe: [polígonos Nx3 em mm]}."""
    from tps.dicom_io.rtstruct import read_rtstruct
    from tps.importacao.rtplan import ler_dicom
    exe = executavel()
    if exe is None:
        raise RuntimeError("Segmentação automática não está instalada neste computador")
    desconhecidos = [o for o in orgaos if o not in ORGAOS]
    if desconhecidos:
        raise ValueError(f"Órgãos desconhecidos: {', '.join(desconhecidos)}")
    tarefas: dict[str, set] = {}
    for o in orgaos:
        tarefas.setdefault(ORGAOS[o][1], set()).update(ORGAOS[o][2])
    tmp = Path(tempfile.mkdtemp(prefix="tps_ia_"))
    try:
        ent = tmp / "ct"
        ent.mkdir()
        for k, f in enumerate(arquivos_ct):         # regrava com preâmbulo e File Meta (o Monaco grava sem)
            d = ler_dicom(f)
            fm = d.file_meta
            fm.MediaStorageSOPClassUID = d.SOPClassUID
            fm.MediaStorageSOPInstanceUID = d.SOPInstanceUID
            d.save_as(ent / f"CT{k:04d}.dcm", enforce_file_format=True)
        out: dict[str, list] = {}
        for tarefa, classes in tarefas.items():
            rs = _rodar(exe, ent, tmp / f"{tarefa}.dcm", tarefa, sorted(classes), rapido, tempo_max_s, dispositivo)
            for st in read_rtstruct(rs):
                if st.name in classes:
                    out.setdefault(st.name, []).extend(st.contours)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _matar_arvore(pr):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pr.pid)], stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.killpg(os.getpgid(pr.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pr.kill()
