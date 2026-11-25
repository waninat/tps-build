"""Usuários, sessões de login, trilha de auditoria encadeada e índice de planos (SQLite). SRS-SEG-001..012.

- Senhas: PBKDF2-SHA256 com sal individual e 240 000 iterações; nunca guardadas em claro.
- Papéis: admin (gerencia usuários), fisico (planeja, aprova, exporta), medico (aprova), dosimetrista (planeja).
- Assinatura: aprovar, desaprovar e exportar pedem a senha de novo (não basta estar logado).
- Auditoria: cada registro guarda o hash SHA-256 do anterior; `verificar_auditoria` detecta qualquer alteração ou
  remoção de linha no meio da trilha.
- Bloqueio: 5 senhas erradas seguidas bloqueiam o usuário por 15 minutos.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from pathlib import Path

PAPEIS = {"admin": "Administrador", "fisico": "Físico Médico", "medico": "Radio-oncologista",
          "dosimetrista": "Dosimetrista"}
PODE_APROVAR = {"fisico", "medico"}
PODE_EXPORTAR = {"fisico"}
ITERACOES = 240_000
TENTATIVAS = 5
BLOQUEIO_MIN = 15
VALIDADE_TOKEN_H = 12


class ErroAcesso(PermissionError):
    pass


def _agora() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _hash_senha(senha: str, sal: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, ITERACOES).hex()


class Banco:
    def __init__(self, arquivo: str | Path):
        self.arquivo = Path(arquivo)
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        self._trava = threading.Lock()
        self.tokens: dict[str, dict] = {}
        with self._con() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS usuarios (login TEXT PRIMARY KEY, nome TEXT NOT NULL, papel TEXT NOT NULL,
                sal TEXT NOT NULL, hash TEXT NOT NULL, ativo INTEGER NOT NULL DEFAULT 1, criado TEXT NOT NULL,
                falhas INTEGER NOT NULL DEFAULT 0, bloqueado_ate TEXT);
            CREATE TABLE IF NOT EXISTS auditoria (id INTEGER PRIMARY KEY AUTOINCREMENT, quando TEXT NOT NULL,
                usuario TEXT, acao TEXT NOT NULL, caso TEXT, detalhe TEXT, hash_anterior TEXT NOT NULL,
                hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planos (chave TEXT PRIMARY KEY, paciente_id TEXT, paciente_nome TEXT,
                rotulo TEXT, estado TEXT, maquina TEXT, arquivo TEXT NOT NULL, atualizado TEXT NOT NULL,
                usuario TEXT, hash_plano TEXT);
            """)

    def _con(self):
        c = sqlite3.connect(self.arquivo, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # ------------------------------------------------------------------ usuários
    def tem_usuarios(self) -> bool:
        with self._con() as c:
            return c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] > 0

    def criar_usuario(self, login: str, nome: str, papel: str, senha: str, por: str | None = None):
        login = login.strip().lower()
        if not login or not nome.strip():
            raise ValueError("Login e nome são obrigatórios")
        if papel not in PAPEIS:
            raise ValueError(f"Papel desconhecido: {papel}")
        if len(senha) < 8:
            raise ValueError("A senha precisa ter pelo menos 8 caracteres")
        sal = os.urandom(16)
        with self._trava, self._con() as c:
            if c.execute("SELECT 1 FROM usuarios WHERE login=?", (login,)).fetchone():
                raise ValueError(f"Usuário {login} já existe")
            c.execute("INSERT INTO usuarios (login, nome, papel, sal, hash, criado) VALUES (?,?,?,?,?,?)",
                      (login, nome.strip(), papel, sal.hex(), _hash_senha(senha, sal), _agora()))
        self.auditar(por or login, "usuario_criado", detalhe={"login": login, "papel": papel})

    def trocar_senha(self, login: str, senha_atual: str, nova: str):
        self.conferir_senha(login, senha_atual)
        if len(nova) < 8:
            raise ValueError("A senha precisa ter pelo menos 8 caracteres")
        sal = os.urandom(16)
        with self._trava, self._con() as c:
            c.execute("UPDATE usuarios SET sal=?, hash=? WHERE login=?", (sal.hex(), _hash_senha(nova, sal), login))
        self.auditar(login, "senha_trocada")

    def desativar(self, login: str, por: str):
        with self._trava, self._con() as c:
            c.execute("UPDATE usuarios SET ativo=0 WHERE login=?", (login,))
        for t in [t for t, u in self.tokens.items() if u["login"] == login]:
            self.tokens.pop(t, None)
        self.auditar(por, "usuario_desativado", detalhe={"login": login})

    def usuarios(self) -> list[dict]:
        with self._con() as c:
            return [{"login": r["login"], "nome": r["nome"], "papel": r["papel"], "ativo": bool(r["ativo"])}
                    for r in c.execute("SELECT * FROM usuarios ORDER BY login")]

    def conferir_senha(self, login: str, senha: str) -> dict:
        login = (login or "").strip().lower()
        with self._trava, self._con() as c:
            r = c.execute("SELECT * FROM usuarios WHERE login=?", (login,)).fetchone()
            if r is None or not r["ativo"]:
                raise ErroAcesso("Usuário ou senha inválidos")
            if r["bloqueado_ate"] and r["bloqueado_ate"] > _agora():
                raise ErroAcesso(f"Usuário bloqueado até {r['bloqueado_ate'][11:16]} por senhas erradas")
            if not hmac.compare_digest(_hash_senha(senha or "", bytes.fromhex(r["sal"])), r["hash"]):
                falhas = r["falhas"] + 1
                ate = (dt.datetime.now() + dt.timedelta(minutes=BLOQUEIO_MIN)).isoformat(timespec="seconds") \
                    if falhas >= TENTATIVAS else None
                c.execute("UPDATE usuarios SET falhas=?, bloqueado_ate=? WHERE login=?",
                          (0 if ate else falhas, ate, login))
                erro = True
            else:
                c.execute("UPDATE usuarios SET falhas=0, bloqueado_ate=NULL WHERE login=?", (login,))
                erro = False
        if erro:
            self.auditar(login, "senha_errada")
            raise ErroAcesso("Usuário ou senha inválidos")
        return {"login": r["login"], "nome": r["nome"], "papel": r["papel"]}

    # ------------------------------------------------------------------ sessões
    def entrar(self, login: str, senha: str) -> tuple[str, dict]:
        u = self.conferir_senha(login, senha)
        token = secrets.token_urlsafe(32)
        u["expira"] = (dt.datetime.now() + dt.timedelta(hours=VALIDADE_TOKEN_H)).isoformat(timespec="seconds")
        self.tokens[token] = u
        self.auditar(u["login"], "login")
        return token, u

    def sair(self, token: str):
        u = self.tokens.pop(token, None)
        if u:
            self.auditar(u["login"], "logout")

    def usuario_do_token(self, token: str | None) -> dict:
        u = self.tokens.get(token or "")
        if u is None or u["expira"] < _agora():
            self.tokens.pop(token or "", None)
            raise ErroAcesso("Faça login")
        return u

    def assinar(self, u: dict, senha: str, papeis: set[str], acao: str) -> dict:
        """Confirma a senha do usuário logado e o papel exigido para a ação."""
        if u["papel"] not in papeis:
            raise ErroAcesso(f"O papel {PAPEIS[u['papel']]} não pode {acao}")
        return self.conferir_senha(u["login"], senha)

    # ------------------------------------------------------------------ auditoria
    def auditar(self, usuario: str | None, acao: str, caso: str | None = None, detalhe: dict | None = None):
        det = json.dumps(detalhe or {}, ensure_ascii=False, sort_keys=True, default=str)
        with self._trava, self._con() as c:
            ult = c.execute("SELECT hash FROM auditoria ORDER BY id DESC LIMIT 1").fetchone()
            anterior = ult["hash"] if ult else "0" * 64
            quando = _agora()
            h = hashlib.sha256("|".join([anterior, quando, usuario or "", acao, caso or "", det]).encode()).hexdigest()
            c.execute("INSERT INTO auditoria (quando, usuario, acao, caso, detalhe, hash_anterior, hash) "
                      "VALUES (?,?,?,?,?,?,?)", (quando, usuario, acao, caso, det, anterior, h))

    def auditoria(self, limite: int = 200, caso: str | None = None) -> list[dict]:
        with self._con() as c:
            q = "SELECT * FROM auditoria" + (" WHERE caso=?" if caso else "") + " ORDER BY id DESC LIMIT ?"
            return [dict(r) for r in c.execute(q, ((caso, limite) if caso else (limite,)))]

    def verificar_auditoria(self) -> dict:
        anterior = "0" * 64
        n = 0
        with self._con() as c:
            for r in c.execute("SELECT * FROM auditoria ORDER BY id"):
                h = hashlib.sha256("|".join([anterior, r["quando"], r["usuario"] or "", r["acao"], r["caso"] or "",
                                             r["detalhe"]]).encode()).hexdigest()
                if r["hash_anterior"] != anterior or r["hash"] != h:
                    return {"integra": False, "registro": r["id"], "verificados": n}
                anterior = r["hash"]
                n += 1
        return {"integra": True, "verificados": n}

    # ------------------------------------------------------------------ planos
    def registrar_plano(self, chave: str, paciente_id: str, paciente_nome: str, rotulo: str, estado: str,
                        maquina: str, arquivo: str, usuario: str, hash_plano: str | None):
        with self._trava, self._con() as c:
            c.execute("INSERT OR REPLACE INTO planos VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (chave, paciente_id, paciente_nome, rotulo, estado, maquina, arquivo, _agora(), usuario,
                       hash_plano))

    def apagar_plano(self, chave: str) -> dict | None:
        with self._trava, self._con() as c:
            r = c.execute("SELECT * FROM planos WHERE chave = ?", (chave,)).fetchone()
            if r is None:
                return None
            c.execute("DELETE FROM planos WHERE chave = ?", (chave,))
            return dict(r)

    def planos(self) -> list[dict]:
        with self._con() as c:
            return [dict(r) for r in c.execute("SELECT * FROM planos ORDER BY atualizado DESC")]
