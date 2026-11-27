"""Registro para o suporte: memória e etapas dos cálculos longos, e falhas que derrubam o programa. SRS-GEN-020."""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

_T0 = time.time()


def memoria() -> tuple[float, float, float]:
    """(GB livres no sistema, GB totais, GB usados por este processo); -1 se não souber."""
    livre = total = proc = -1.0
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class MEM(ctypes.Structure):
                _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEM()
            m.dwLength = ctypes.sizeof(MEM)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            livre, total = m.ullAvailPhys / 1e9, m.ullTotalPhys / 1e9

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                           [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize",
                                                           "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                                                           "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                                                           "PagefileUsage", "PeakPagefileUsage")]
            p = PMC()
            p.cb = ctypes.sizeof(PMC)
            if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(p), p.cb):
                proc = p.WorkingSetSize / 1e9
        else:
            with open("/proc/meminfo") as f:
                d = {ln.split(":")[0]: float(ln.split()[1]) for ln in f}
            livre, total = d.get("MemAvailable", -1e6) / 1e6, d.get("MemTotal", -1e6) / 1e6
            with open("/proc/self/statm") as f:
                proc = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e9
    except Exception:  # noqa: BLE001
        pass
    return livre, total, proc


def registrar(msg: str) -> None:
    """Uma linha em tps_saida.log (a saída do programa): hora, memória e a etapa."""
    livre, total, proc = memoria()
    mem = f"mem livre {livre:.1f}/{total:.1f} GB, TPS {proc:.1f} GB" if livre >= 0 else ""
    try:
        print(f"[{dt.datetime.now():%H:%M:%S}] {msg}  ({mem})", flush=True)
    except Exception:  # noqa: BLE001
        pass


_falhas = None


def ativar_registro_de_falhas(pasta) -> None:
    """Se o programa cair (falha nativa no numba, memória, etc.), a pilha de todas as threads vai para falhas.log."""
    global _falhas
    import faulthandler
    try:
        os.makedirs(pasta, exist_ok=True)
        _falhas = open(os.path.join(pasta, "falhas.log"), "a", encoding="utf-8", buffering=1)
        from tps.version import __version__
        livre, total, _ = memoria()
        _falhas.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S}  TPS {__version__} iniciado  "
                      f"(Python {sys.version.split()[0]}, {os.cpu_count()} núcleos, memória {total:.1f} GB)\n")
        faulthandler.enable(file=_falhas, all_threads=True)
    except Exception:  # noqa: BLE001
        pass
