"""Janela própria do TPS (sem navegador). SRS-UI-001.

A interface continua sendo a mesma página (HTML/JS) servida pelo servidor local; aqui ela abre numa janela nativa
pelo pywebview, que no Windows usa o WebView2 (o motor do Edge que já vem no Windows 10/11). Se a janela não puder
ser criada (WebView2 ausente, Linux sem GTK/Qt…), o TPS volta a abrir no navegador, como antes.

Fluxo: a janela abre na hora com "Carregando…"; numa linha paralela carrega numpy/numba/servidor e, quando o
servidor responde, a janela navega para ele. Fechar a janela encerra o servidor.
"""
from __future__ import annotations

import os
import socket
import threading
import time
import traceback
from pathlib import Path

CARREGANDO = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0e12;color:#c9d3df;
font:15px system-ui,Segoe UI,sans-serif} .c{text-align:center} h1{font-weight:600;letter-spacing:.5px;color:#e8eef5}
#e{color:#7f8b99;font-size:13px;margin-top:10px;min-height:18px} .b{width:220px;height:3px;background:#1b222b;
margin:18px auto 0;overflow:hidden;border-radius:2px} .b i{display:block;width:40%;height:100%;background:#4aa3ff;
animation:a 1.2s infinite ease-in-out} @keyframes a{0%{margin-left:-40%}100%{margin-left:100%}}
pre{white-space:pre-wrap;text-align:left;max-width:900px;max-height:60vh;overflow:auto;background:#141a21;
padding:12px;border-radius:6px;color:#ffb4b4;font-size:12px}</style></head><body><div class="c">
<h1>TPS</h1><div>Carregando… <span style="color:#7f8b99">(a primeira vez pode levar 1–2 minutos)</span></div>
<div class="b"><i></i></div><div id="e"></div></div></body></html>"""


class ApiJanela:
    """Funções do sistema que a página pode chamar (window.pywebview.api.*)."""

    def __init__(self):
        self._janela = None

    def escolher_pasta(self, inicial: str = "") -> str:
        import webview
        tipo = getattr(webview, "FileDialog", None)
        tipo = tipo.FOLDER if tipo else webview.FOLDER_DIALOG
        r = self._janela.create_file_dialog(tipo, directory=inicial if inicial and Path(inicial).is_dir() else "")
        return (r[0] if isinstance(r, (list, tuple)) and r else r) or ""

    def abrir_pasta(self, caminho: str) -> bool:
        p = Path(caminho)
        if not p.exists():
            return False
        if os.name == "nt":
            os.startfile(str(p if p.is_dir() else p.parent))  # noqa: S606
        return True

    def caminho_dados(self) -> str:
        from tps import paths
        return str(paths.home())


def disponivel() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _esperar_porta(porta: int, limite_s: float = 600) -> bool:
    t0 = time.time()
    while time.time() - t0 < limite_s:
        with socket.socket() as so:
            so.settimeout(1)
            if so.connect_ex(("127.0.0.1", porta)) == 0:
                return True
        time.sleep(0.3)
    return False


def rodar(preparar, iniciar_servidor, titulo: str = "TPS", teste: bool = False) -> int:
    """preparar() → (host, porta, url) importa o que é pesado; iniciar_servidor(host, porta) → servidor uvicorn já
    rodando numa linha paralela (tem .should_exit). Devolve o código de saída. Levanta RuntimeError se a janela não
    puder ser criada (o chamador volta ao navegador)."""
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    api = ApiJanela()
    janela = webview.create_window(titulo, html=CARREGANDO, js_api=api, width=1600, height=960, min_size=(1000, 640), maximized=True,
                                   background_color="#0b0e12", confirm_close=not teste, text_select=True)
    api._janela = janela
    estado = {"servidor": None, "codigo": 0}

    def etapa_na_tela(msg):
        try:
            janela.evaluate_js(f"document.getElementById('e')&&(document.getElementById('e').textContent={msg!r})")
        except Exception:  # noqa: BLE001
            pass

    def trabalho():
        try:
            host, porta, url = preparar(etapa_na_tela)
            estado["servidor"] = iniciar_servidor(host, porta)
            if not _esperar_porta(porta):
                raise RuntimeError(f"O servidor não respondeu em {url}")
            janela.load_url(url)
            if teste:                                  # autoteste da janela: página carregou e tem o título certo
                for _ in range(120):
                    time.sleep(0.5)
                    try:
                        t = janela.evaluate_js("document.title")
                        if t and "TPS" in str(t) and janela.evaluate_js("typeof E") == "object":
                            print(f"janela ok: {t}", flush=True)
                            break
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    print("janela: a página não carregou", flush=True)
                    estado["codigo"] = 1
                janela.destroy()
        except Exception:  # noqa: BLE001
            txt = traceback.format_exc()
            print(txt, flush=True)
            estado["codigo"] = 1
            try:
                from tps import paths
                (paths.home() / "erro_inicializacao.log").write_text(txt, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            import html
            janela.load_html(CARREGANDO.replace("Carregando…", "O TPS não conseguiu iniciar.")
                             .replace('<div class="b"><i></i></div>', "<pre>" + html.escape(txt) + "</pre>"))
            if teste:
                janela.destroy()

    def ao_fechar():
        s = estado["servidor"]
        if s is not None:
            s.should_exit = True

    janela.events.closed += ao_fechar
    from tps import paths
    dados_web = Path(os.environ.get("LOCALAPPDATA") or paths.home()) / "TPS" / "janela"
    dados_web.mkdir(parents=True, exist_ok=True)
    icone = Path(__file__).resolve().parents[2] / "tps.ico"
    gui = os.environ.get("TPS_GUI") or ("edgechromium" if os.name == "nt" else None)   # nunca o IE/MSHTML
    webview.start(trabalho, gui=gui, private_mode=False, storage_path=str(dados_web),
                  icon=str(icone) if icone.exists() else None)
    ao_fechar()
    return estado["codigo"]
