"""Uma chamada paralela do numba por vez no processo. SRS-GEN-021.

A camada de threads padrão do numba (workqueue) não aceita dois núcleos paralelos chamados ao mesmo tempo por
linhas diferentes: ele aborta o processo ("Terminating: Concurrent access") — o programa fecha sem mensagem. Com a
automação (plano em segundo plano) e a tela calculando ao mesmo tempo isso pode acontecer; as funções paralelas são
chamadas através desta trava.
"""
from __future__ import annotations

import functools
import threading

TRAVA_NUMBA = threading.RLock()


def serializado(f):
    @functools.wraps(f)
    def g(*a, **kw):
        with TRAVA_NUMBA:
            return f(*a, **kw)
    g.__wrapped_numba__ = f
    return g
