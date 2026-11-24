"use strict";
// TPS — interface. Toda a física roda no servidor; aqui só desenho e interação.

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const E = {
  sid: null, resumo: null, info: null,
  idx: { axial: 0, coronal: 0, sagital: 0 }, cruz: null,
  wl: { janela: 400, nivel: 40 },
  imgs: {}, sobre: {}, campoSel: null, bev: null, bevSeg: 0, drrImg: null,
  ocultas: new Set(), dvh: null, arrasto: null, token: null, usuario: null,
  isoCfg: { niveis: [], ref: "rx", unidade: "pct" }, fonte: "plano", dvhAlt: {}, bevDes: { pts: [], mouse: null, livre: false },
};
try { const c = JSON.parse(localStorage.getItem("tps_iso") || "null"); if (c) E.isoCfg = c; } catch (e) { /* ok */ }
try { E.token = sessionStorage.getItem("tps_token"); } catch (e) { /* sem storage */ }
const ISO_CORES = { 30: "#3355ff", 50: "#00b7ff", 70: "#00e08a", 80: "#9be000", 90: "#ffe100", 95: "#ffae00",
                    100: "#ff7a00", 105: "#ff2d55", 107: "#ff00c8", 110: "#ffffff" };

const PALETA_ISO = ["#3355ff", "#00b7ff", "#00e08a", "#9be000", "#ffe100", "#ffae00", "#ff7a00", "#ff2d55", "#ff00c8", "#ffffff"];
function corIso(nivel) {
  if (E.isoRef && E.isoRef.dif) return nivel < 0 ? (nivel <= -5 ? "#3b6bff" : "#8db0fe") : (nivel >= 5 ? "#ff3030" : "#ff9a7b");
  // cor pela fração da referência (100% = prescrição ou máximo): mesma cor para o mesmo % em Gy ou em %
  const ref = E.isoRef, pct = ref && ref.unidade === "gy" ? nivel / ref.cem_gy * 100 : nivel;
  if (ISO_CORES[Math.round(pct * 10) / 10]) return ISO_CORES[Math.round(pct * 10) / 10];
  const marcos = [30, 50, 70, 80, 90, 95, 100, 105, 107, 110];
  let k = marcos.findIndex(m => pct < m); if (k < 0) k = marcos.length - 1; else if (k > 0 && pct - marcos[k - 1] < marcos[k] - pct) k--;
  return PALETA_ISO[k];
}
function lerNiveis(txt) {
  // aceita "97 94 91", "97; 94", "97,94,91" e vírgula decimal ("97,5")
  const out = [];
  for (const tk of String(txt).trim().split(/[\s;]+/).filter(Boolean)) {
    const p = tk.split(",");
    if (p.length > 2 || (p.length === 2 && +p[1] >= 10)) p.forEach(x => x && out.push(+x));
    else out.push(+tk.replace(",", "."));
  }
  if (out.some(v => !isFinite(v) || v <= 0)) throw new Error("Níveis inválidos: use números separados por espaço");
  return out;
}

// ------------------------------------------------------------------ comunicação
async function api(metodo, url, corpo, msg) {
  if (msg) ocupado(msg);
  try {
    const op = { method: metodo, headers: {} };
    if (E.token) op.headers["X-Token"] = E.token;
    if (corpo instanceof FormData) op.body = corpo;
    else if (corpo !== undefined) { op.body = JSON.stringify(corpo); op.headers["Content-Type"] = "application/json"; }
    const r = await fetch(url, op);
    const j = await r.json().catch(() => ({ erro: `Resposta inválida (${r.status})` }));
    if (r.status === 401 && j.login) { ocupado(null); await entrar(); return api(metodo, url, corpo, msg); }
    if (!r.ok) throw new Error(j.erro || `Erro ${r.status}`);
    return j;
  } finally { if (msg) ocupado(null); }
}
function ocupado(msg) { $("#ocupado").classList.toggle("oculto", !msg); if (msg) $("#ocupado-txt").textContent = msg; }
let toastT = null;
function aviso(msg, info = false) {
  const t = $("#toast"); t.textContent = msg; t.className = info ? "info" : "";
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.add("oculto"), info ? 3500 : 8000);
}
async function tentar(fn) { try { await fn(); } catch (e) { if (e.message !== "cancelado") aviso(e.message); } }

function modal(titulo, html, { ok = "OK", cancelar = true } = {}) {
  return new Promise((resolve, reject) => {
    $("#modal-titulo").textContent = titulo; $("#modal-corpo").innerHTML = html;
    $("#modal-ok").textContent = ok; $("#modal-cancelar").classList.toggle("oculto", !cancelar);
    $("#modal").classList.remove("oculto");
    const campos = $$("#modal-corpo input, #modal-corpo select, #modal-corpo textarea");
    if (campos[0]) setTimeout(() => campos[0].focus(), 50);
    const fechar = (v, erro) => { $("#modal").classList.add("oculto"); $("#modal-ok").onclick = $("#modal-cancelar").onclick = null; erro ? reject(new Error("cancelado")) : resolve(v); };
    const valores = () => Object.fromEntries(campos.filter(c => c.name).map(c => [c.name, c.value]));
    $("#modal-ok").onclick = () => fechar(valores());
    $("#modal-cancelar").onclick = () => fechar(null, true);
    $("#modal-corpo").onkeydown = (ev) => { if (ev.key === "Enter" && ev.target.tagName !== "TEXTAREA") fechar(valores()); };
  });
}

async function entrar() {
  const info = E.info || await (await fetch("/api/info")).json();
  for (;;) {
    let r;
    if (info.configurar) {
      const v = await modal("Primeiro uso: crie o seu usuário",
        `<p class="pequeno-txt">Será o primeiro físico do sistema, com direito de cadastrar os outros usuários.</p>
         <label>Login<input name="login"></label><label>Nome completo<input name="nome"></label>
         <label>Senha (mín. 8 caracteres)<input name="senha" type="password"></label>`, { ok: "Criar", cancelar: false });
      r = await fetch("/api/configurar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(v) });
    } else {
      const v = await modal("Entrar no TPS", `<label>Login<input name="login" autocomplete="username"></label>
        <label>Senha<input name="senha" type="password" autocomplete="current-password"></label>`, { ok: "Entrar", cancelar: false });
      r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(v) });
    }
    const j = await r.json();
    if (r.ok) {
      E.token = j.token; E.usuario = j.usuario; info.configurar = false;
      try { sessionStorage.setItem("tps_token", j.token); } catch (e) { /* ok */ }
      mostrarUsuario();
      return;
    }
    aviso(j.erro || "Falha no login");
  }
}
function mostrarUsuario() {
  if (E.usuario) $("#usuario-txt").textContent = `${E.usuario.nome} · ${E.info ? E.info.papeis[E.usuario.papel] : E.usuario.papel}`;
}

async function assinatura(acao) {
  const v = await modal(`Assinar: ${acao}`, `<p class="pequeno-txt">Confirme com a sua senha. A ação fica registrada na
    trilha de auditoria com o seu nome.</p><label>Senha<input name="senha" type="password"></label>`, { ok: "Assinar" });
  return v.senha;
}

// ------------------------------------------------------------------ início
async function iniciar() {
  E.info = await api("GET", "/api/info");
  $("#versao").textContent = E.info.versao;
  $("#maquina").innerHTML = E.info.maquinas.map(m => `<option value="${m.id}">${m.nome}</option>`).join("");
  const pm = E.info.maquinas.find(m => m.id === "radioserra_precise"); if (pm) $("#maquina").value = pm.id;
  ligar();
  if (!E.token) await entrar();
  else {
    try { E.usuario = await api("GET", "/api/eu"); } catch (e) { await entrar(); }
  }
  mostrarUsuario();
  await listarPlanos();
  try {
    E.modelos = await api("GET", "/api/modelos_plano");
    $("#mod-sel").innerHTML = E.modelos.map(m => `<option value="${m.id}">${m.sitio} · ${m.nome}</option>`).join("");
    $("#mod-sel").onchange();
  } catch (e) { /* sem modelos */ }
  mostrarIA();
  window.addEventListener("resize", () => desenharTudo());
}

function ligar() {
  $$(".aba").forEach(b => b.onclick = () => {
    $$(".aba").forEach(x => x.classList.toggle("ativa", x === b));
    $$(".painel-aba").forEach(p => p.classList.toggle("oculto", p.id !== "aba-" + b.dataset.aba));
  });
  $$(".aba-d").forEach(b => b.onclick = () => {
    $$(".aba-d").forEach(x => x.classList.toggle("ativa", x === b));
    $$(".painel-d").forEach(p => p.classList.toggle("oculto", p.id !== "pd-" + b.dataset.abad));
    if (b.dataset.abad === "aval") desenharDvh();
  });
  $("#plano-sel").onchange = () => tentar(async () => {
    const r = await api("POST", `/api/${E.sid}/plano/trocar`, { rotulo: $("#plano-sel").value }, "Trocando de plano…");
    E.campoSel = null; atualizar(r); preencherPrescricao(r);
  });
  $("#b-plano-copiar").onclick = () => tentar(async () => {
    const v = await modal("Copiar plano", `<label>Rótulo da cópia<input name="rotulo" maxlength="16" value="${(E.resumo.plano.rotulo + "_2").slice(0, 16)}"></label>
      <div class="pequeno-txt">A cópia é um rascunho com os mesmos campos, prescrição e opções (sem a aprovação).</div>`, { ok: "Copiar" });
    if (!v) return;
    const r = await api("POST", `/api/${E.sid}/plano/copiar`, { rotulo: v.rotulo }, "Copiando…");
    atualizar(r); preencherPrescricao(r);
  });
  $("#b-plano-rem").onclick = () => tentar(async () => {
    const rot = E.resumo.plano.rotulo;
    if (!confirm(`Tirar o plano ${rot} desta sessão? (o arquivo salvo, se houver, continua)`)) return;
    const r = await api("DELETE", `/api/${E.sid}/plano/${encodeURIComponent(rot)}`);
    E.campoSel = null; atualizar(r); preencherPrescricao(r);
  });
  $("#o-fif-modo").onchange = () => $("#o-filtro").classList.toggle("oculto", $("#o-fif-modo").value !== "filtro");
  $("#b-renorm").onclick = () => tentar(async () => {
    const corpo = { so_normalizar: true, ...normalizacaoDoForm() };
    atualizar(await api("POST", `/api/${E.sid}/calcular`, corpo, "Renormalizando…"));
    aviso("Plano renormalizado (dose relativa igual, UM novas)", true);
  });
  $("#b-iso-cfg").onclick = () => tentar(configurarIsodoses);
  $("#fonte-dose").onchange = () => { E.fonte = $("#fonte-dose").value; E.sobre = {}; carregarSobre(); desenharDvh(); };
  const mostrarFonte = (f) => {
    const sel = $("#fonte-dose"); sel.classList.remove("oculto");
    sel.querySelector(`option[value=${f}]`).disabled = false;
    E.fonte = f; sel.value = f; E.fonteVer = (E.fonteVer || 0) + 1; E.sobre = {}; carregarSobre(); desenharDvh();
  };
  $("#b-comparar").onclick = () => tentar(async () => {
    const a = $("#cmp-a").value, b = $("#cmp-b").value;
    const r = await api("POST", `/api/${E.sid}/comparar_planos`, { a, b }, "Comparando os planos…");
    const dados = {};
    for (const [n, d] of Object.entries(r.dvh_a)) dados[n] = { ...d, ref: r.dvh_b[n] };
    E.dvhAlt.dif = { dados, rx: r.rx_a, titulo: `${a} (cheio) × ${b} (tracejado)` };
    $("#cmp-planos").innerHTML = `UM total: ${a} ${r.um[0]} · ${b} ${r.um[1]} · HI ${r.hi[0] != null ? r.hi[0].toFixed(3) : "—"} × ${r.hi[1] != null ? r.hi[1].toFixed(3) : "—"}` +
      ` · gama 3%/3 mm (${b} vs ${a}): ${r.gama_3_3 != null ? r.gama_3_3 + "%" : "—"} · diferença de ${f1(r.dif_min_gy, 2)} a +${f1(r.dif_max_gy, 2)} Gy`;
    const nomes = { Dmean: "Dméd", D95: "D95", D2: "D2", Dmax: "Dmax" };
    $("#tab-cmp-planos tbody").innerHTML = Object.entries(r.tabela).map(([n, t]) => Object.entries(t).map(([k, [va, vb]], i) => {
      const dif = vb - va, cls = Math.abs(dif) < 0.5 ? "" : (dif > 0 ? "ruim" : "bom");
      return `<tr>${i ? "<td></td>" : `<td>${n}</td>`}<td>${nomes[k] || k}</td><td>${f1(va, 2)}</td><td>${f1(vb, 2)}</td><td class="${cls}">${dif > 0 ? "+" : ""}${f1(dif, 2)}</td></tr>`;
    }).join("")).join("");
    $("#tab-cmp-planos").classList.remove("oculto");
    mostrarFonte("dif");
  });
  $("#b-somar").onclick = () => tentar(async () => {
    const rotulos = $$("#soma-lista input:checked").map(i => i.value);
    const r = await api("POST", `/api/${E.sid}/soma_planos`, { rotulos, modo: $("#soma-modo").value,
      ab_alvo: +$("#soma-ab-alvo").value, ab_oar: +$("#soma-ab-oar").value }, "Somando os planos…");
    E.dvhAlt.soma = { dados: r.dvh, rx: r.rx_gy, titulo: `Soma ${r.rotulos.join(" + ")}${r.modo === "eqd2" ? " (EQD2)" : ""}` };
    $("#soma-res").innerHTML = `Prescrição somada ${r.modo === "eqd2" ? "(EQD2, α/β alvo " + r.ab.alvo + ")" : ""}: <b>${f1(r.rx_gy, 2)} Gy</b> · Dmax ${f1(r.dmax_gy, 2)} Gy`;
    $("#tab-soma tbody").innerHTML = Object.entries(r.estatisticas).map(([n, q]) =>
      `<tr><td>${n}</td><td>${f1(q.Dmean, 1)}</td><td>${f1(q.D95, 1)}</td><td>${f1(q.D2, 1)}</td><td>${f1(q.Dmax, 1)}</td></tr>`).join("");
    $("#tab-soma").classList.remove("oculto");
    const rr = (r.restricoes && r.restricoes.resultados) || [];
    $("#tab-soma-rest tbody").innerHTML = rr.map(q => `<tr><td>${q.estrutura}</td><td>${q.metrica}</td><td>${q.op.replace(">=", "≥").replace("<=", "≤")} ${q.limite}</td>` +
      `<td class="${q.ok ? "ok" : (q.tipo === "objetivo" ? "avisotxt" : "falha")}">${f1(q.valor, 1)}</td></tr>`).join("");
    $("#tab-soma-rest").classList.toggle("oculto", !rr.length);
    mostrarFonte("soma");
  });
  // janela própria (pywebview): seletor de pasta do sistema e "abrir pasta"
  const comJanela = () => {
    if (!(window.pywebview && window.pywebview.api)) return;
    E.janela = true;
    $$(".so-janela").forEach(b => b.classList.remove("oculto"));
    $$("[data-pasta]").forEach(b => b.onclick = () => tentar(async () => {
      const alvo = $("#" + b.dataset.pasta), p = await window.pywebview.api.escolher_pasta(alvo.value || "");
      if (p) { alvo.value = p; if (b.dataset.pasta === "ct-pasta") $("#b-ct").click(); }
    }));
  };
  window.addEventListener("pywebviewready", comJanela); comJanela();
  $("#mod-sel").onchange = () => {
    const m = (E.modelos || []).find(x => x.id === $("#mod-sel").value); if (!m) return;
    $("#mod-desc").textContent = `${m.descricao} Sugestão: ${m.dose_total} Gy / ${m.fracoes} fx.`;
    $("#p-dose").value = m.dose_total; $("#p-fx").value = m.fracoes;
  };
  $("#b-modelo").onclick = () => tentar(async () => {
    if (E.resumo.plano && E.resumo.plano.rotulo === $("#p-rotulo").value.trim()) {
      const v = await modal("Novo plano a partir do modelo", `<label>Rótulo do novo plano<input name="rotulo" maxlength="16"></label>`, { ok: "Criar" });
      if (!v || !v.rotulo.trim()) return;
      $("#p-rotulo").value = v.rotulo.trim();
    }
    E.campoSel = null;
    const r = await api("POST", `/api/${E.sid}/modelo_plano`, { modelo: $("#mod-sel").value, rotulo: $("#p-rotulo").value,
      dose_total: +$("#p-dose").value, fracoes: +$("#p-fx").value, calcular: true }, "Criando o plano do modelo e calculando…");
    atualizar(r); preencherPrescricao(r);
    const mi = r.modelo_aplicado || {}, op = mi.opcoes || {};
    if (op.field_in_field) {
      $("#o-fif-modo").value = op.field_in_field.modo === "filtro" ? "filtro" : "otimizado"; $("#o-fif-modo").onchange();
      $("#o-fil-niveis").value = (op.field_in_field.niveis || []).join(" "); $("#o-fil-peso").value = op.field_in_field.peso_pct;
      Array.from($("#o-fil-campos").options).forEach(o => o.selected = (op.field_in_field.campos || []).includes(o.value));
      $("#o-fil-fino").checked = !!op.field_in_field.ajuste_fino;
    }
    E.oarCfg = {};
    for (const o of ((op.proteger_oars || {}).oars || [])) E.oarCfg[o.estrutura] = { on: true, metrica: o.metrica, valor: o.valor ?? "", vol: o.volume_pct ?? "", prio: o.prioridade };
    listarOars();
    const t = mi.tangentes;
    aviso(`Plano do modelo criado (alvo ${mi.alvo})` + (t ? ` · tangentes ${t.medial}° / ${t.lateral}° (pulmão no campo ${t.fracao_pulmao}%)` : ""), true);
    if (r.isocentro) irPara(r.isocentro);
  });
  $("#b-k-op").onclick = () => tentar(async () => {
    const nome = $("#k-est").value, outra = $("#k-outra").value;
    if (!nome || !outra) throw new Error("Escolha as duas estruturas");
    const r = await api("POST", `/api/${E.sid}/contorno/operar`, { nome, operacao: $("#k-op").value, outra, margem_mm: +$("#k-mg").value || 0 }, "Calculando a estrutura…");
    atualizar(r); aviso(`${nome}: ${r.contorno.volume_cc} cc (desfazer volta ao anterior)`, true);
  });
  const auto = (orgaos, msg) => tentar(async () => {
    const r = await api("POST", `/api/${E.sid}/contorno/orgaos`, { orgaos }, msg);
    atualizar(r); aviso("Criado: " + r.contorno.criadas.map(n => `${n} (${r.contorno.volumes_cc[n]} cc)`).join(", ") + " — revise", true);
  });
  $("#b-auto-body").onclick = () => tentar(async () => { atualizar(await api("POST", `/api/${E.sid}/contorno/corpo`, { nome: "BODY" }, "Contornando o corpo…")); aviso("BODY criado — revise", true); });
  $("#b-auto-pulm").onclick = () => auto(["pulmoes"], "Contornando os pulmões…");
  $("#b-auto-ossos").onclick = () => auto(["ossos"], "Contornando os ossos…");
  $("#b-ia").onclick = () => tentar(async () => {
    const orgaos = Array.from($("#ia-orgaos").selectedOptions).map(o => o.value);
    if (!orgaos.length) throw new Error("Escolha os órgãos (Ctrl+clique para vários)");
    const zr = $("#ia-reto-z").value.trim();
    const r = await api("POST", `/api/${E.sid}/contorno/ia`, { orgaos, reto_zmax_mm: zr === "" ? null : +zr.replace(",", ".") },
      "Segmentando com IA (1–5 min por grupo na CPU)…");
    atualizar(r);
    const c = r.contorno;
    aviso("Criado: " + c.criadas.join(", ") + (c.nao_encontrados.length ? ` · não encontrado: ${c.nao_encontrados.join(", ")}` : "") +
      (c.reto_limite_superior_mm != null ? ` · reto até z = ${c.reto_limite_superior_mm} mm (junção retossigmoide)` : "") + " — revise", true);
    if (c.reto_limite_superior_mm != null) $("#ia-reto-z").placeholder = `auto (${c.reto_limite_superior_mm})`;
  });
  $("#b-rest").onclick = () => tentar(async () => {
    const corpo = { estrutura: $("#r-est").value, metrica: $("#r-met").value.trim(), op: $("#r-op").value,
      valor: $("#r-val").value, tipo: $("#r-tipo").value, na_clinica: $("#r-clin").checked };
    atualizar(await api("POST", `/api/${E.sid}/restricao`, corpo));
    aviso(corpo.na_clinica ? "Restrição gravada no protocolo da clínica" : "Restrição acrescentada ao plano", true);
    $("#r-val").value = "";
  });
  const setupCampo = async (g, nome) => api("POST", `/api/${E.sid}/setup`, { nome: nome || "", gantry: g, x_cm: +$("#s-x").value,
    y_cm: +$("#s-y").value, mesa: +$("#s-mesa").value });
  $("#b-setup").onclick = () => tentar(async () => { atualizar(await setupCampo(+$("#s-gantry").value, $("#s-nome").value.trim())); $("#s-nome").value = ""; });
  $("#b-setup-ap-lat").onclick = () => tentar(async () => {
    const lat = await modal("Setup AP + lateral", `<label>Lateral<select name="g"><option value="90">90° (esquerda do paciente, HFS)</option>
      <option value="270">270° (direita do paciente, HFS)</option></select></label>`, { ok: "Adicionar" });
    if (!lat) return;
    await setupCampo(0, "SETUP AP"); atualizar(await setupCampo(+lat.g, `SETUP LAT${lat.g === "90" ? "E" : "D"}`));
  });
  $("#b-setup-ortog").onclick = () => tentar(async () => {
    let r; for (const [g, n] of [[0, "SETUP AP"], [90, "SETUP LATE"], [180, "SETUP PA"], [270, "SETUP LATD"]]) r = await setupCampo(g, n);
    atualizar(r);
  });
  $("#bev-ferr").onchange = () => {
    const f = $("#bev-ferr").value, des = f !== "editar";
    $("#bev-enc").classList.toggle("oculto", !des);
    $("#bev-dica").textContent = des ? "clique os vértices (duplo clique fecha) ou arraste à mão livre · Esc cancela" : "arraste a ponta da lâmina ou o jaw (Shift + arrastar: jaw sobre a lâmina)";
    E.bevDes = { pts: [], mouse: null, livre: false }; desenharBev();
  };
  $("#b-ct").onclick = () => tentar(async () => abrirCaso(await api("POST", "/api/caso/ct", { pasta: $("#ct-pasta").value.trim() }, "Lendo CT e estruturas…")));
  $("#ct-upload").onchange = (ev) => tentar(async () => {
    const fd = new FormData(); for (const f of ev.target.files) fd.append("arquivos", f, f.name);
    abrirCaso(await api("POST", "/api/caso/upload", fd, `Enviando ${ev.target.files.length} arquivos…`));
  });
  $("#b-fantoma").onclick = () => tentar(async () => abrirCaso(await api("POST", "/api/caso/fantoma", {
    tipo: $("#f-tipo").value, largura: +$("#f-larg").value * 10, espessura: +$("#f-esp").value * 10,
    comprimento: +$("#f-comp").value * 10, paciente_id: $("#f-id").value, paciente_nome: $("#f-nome").value }, "Criando fantoma…")));
  $("#b-abrir").onclick = () => tentar(async () => abrirCaso(await api("POST", "/api/caso/abrir", { arquivo: $("#planos-salvos").value }, "Abrindo plano…")));
  $("#maquina").onchange = () => tentar(async () => { if (E.sid) atualizar(await api("POST", `/api/${E.sid}/maquina`, { id: $("#maquina").value })); });
  $("#p-metodo").onchange = mostrarPonto; mostrarPonto();
  $("#b-plano").onclick = () => tentar(criarPlano);
  $("#b-iso").onclick = () => tentar(async () => {
    if (!E.cruz) throw new Error("Clique no corte axial para posicionar a cruz");
    atualizar(await api("POST", `/api/${E.sid}/isocentro`, { xyz: E.cruz }));
  });
  $("#c-modo").onchange = () => {
    const r = $("#c-modo").value === "retangular";
    $("#c-conformado").classList.toggle("oculto", r); $("#c-retangular").classList.toggle("oculto", !r);
  };
  $("#b-campo").onclick = () => tentar(salvarCampo);
  $("#b-campo-novo").onclick = () => { E.campoSel = null; preencherForm(null); listarCampos(); };
  $("#b-campo-rem").onclick = () => tentar(async () => {
    if (!E.campoSel) return;
    atualizar(await api("DELETE", `/api/${E.sid}/campo/${encodeURIComponent(E.campoSel)}`));
    E.campoSel = null; preencherForm(null);
  });
  $$("[data-arranjo]").forEach(b => b.onclick = () => tentar(() => arranjo(b.dataset.arranjo)));
  $("#b-calc").onclick = () => tentar(calcular);
  $("#b-aprovar").onclick = () => tentar(async () => {
    const fora = ((E.resumo.resultado || {}).conferencia_um || []).filter(k => !k.ok && !k.compensado);
    const chk = await carregarChecklist();
    const errs = chk.filter(i => i.estado === "erro" && i.id !== "conferencia_um");
    let corpo;
    if (fora.length || errs.length) {
      const v = await modal("Aprovar com pendências",
        `${fora.length ? `<p class="pequeno-txt">Conferência independente fora de ±3% em: <b>${fora.map(k => `${k.campo} (${k.dif_pct >= 0 ? "+" : ""}${f1(k.dif_pct, 1)}%)`).join(", ")}</b>.</p>` : ""}
         ${errs.length ? `<ul class="pequeno-txt falha">${errs.map(i => `<li><b>${i.titulo}:</b> ${i.detalhe}</li>`).join("")}</ul>` : ""}
         <p class="pequeno-txt">Só aprove depois de investigar. A justificativa fica no plano, no relatório e na auditoria.</p>
         <label>Justificativa<textarea name="justificativa" rows="3"></textarea></label>
         <label>Senha<input name="senha" type="password"></label>`, { ok: "Aprovar" });
      if (!v) return;
      corpo = v;
    } else {
      corpo = { senha: await assinatura("aprovar o plano") };
    }
    atualizar(await api("POST", `/api/${E.sid}/aprovar`, corpo));
    aviso("Plano aprovado e bloqueado para edição", true);
  });
  $("#b-desaprovar").onclick = () => tentar(async () => {
    const v = await modal("Desaprovar o plano", `<label>Motivo (fica no histórico)<input name="motivo"></label>
      <label>Senha<input name="senha" type="password"></label>`, { ok: "Desaprovar" });
    if (!v.motivo) throw new Error("Informe o motivo");
    atualizar(await api("POST", `/api/${E.sid}/desaprovar`, v));
  });
  $("#b-exportar").onclick = () => tentar(exportar);
  $("#b-salvar").onclick = () => tentar(async () => aviso("Salvo em " + (await api("POST", `/api/${E.sid}/salvar`)).arquivo, true));
  $("#b-relatorio").onclick = () => window.open(`/api/${E.sid}/relatorio.pdf?t=${encodeURIComponent(E.token)}`, "_blank");
  $("#t-wash").onchange = () => desenharTudo();
  $("#wash-min").onchange = () => desenharTudo();
  $("#b-rel-medico").onclick = () => window.open(`/api/${E.sid}/relatorio_medico.pdf?t=${encodeURIComponent(E.token)}`, "_blank");
  $("#b-checkfilm").onclick = () => window.open(`/api/${E.sid}/checkfilm.pdf?t=${encodeURIComponent(E.token)}`, "_blank");
  $("#b-sair").onclick = () => tentar(async () => {
    await api("POST", "/api/logout"); E.token = null;
    try { sessionStorage.removeItem("tps_token"); } catch (e) { /* ok */ }
    location.reload();
  });
  $("#b-auditoria").onclick = () => tentar(verAuditoria);
  $("#b-bolus").onclick = () => tentar(async () => {
    const campos = Array.from($("#bo-campos").selectedOptions).map(o => o.value);
    atualizar(await api("POST", `/api/${E.sid}/bolus`, { espessura_mm: +$("#bo-esp").value, campos }, "Criando bolus…"));
  });
  $("#ne-op").onchange = () => {
    const op = $("#ne-op").value;
    $("#ne-margens").classList.toggle("oculto", !["margem", "contrair"].includes(op));
    $("#ne-anel").classList.toggle("oculto", op !== "anel");
    $("#ne-iso").classList.toggle("oculto", op !== "isodose");
    $("#ne-lb").classList.toggle("oculto", !["uniao", "intersecao", "subtracao"].includes(op));
  };
  $("#ne-op").onchange();
  $("#b-nova-est").onclick = () => tentar(async () => {
    const op = $("#ne-op").value, margens = {};
    $$("#ne-margens [data-m]").forEach(i => margens[i.dataset.m] = +i.value || 0);
    const corpo = { nome: $("#ne-nome").value, operacao: op === "contrair" ? "margem" : op, contrair: op === "contrair",
      a: $("#ne-a").value, b: $("#ne-b").value, margens, interno_mm: +$("#ne-int").value, externo_mm: +$("#ne-ext").value,
      nivel_gy: +$("#ne-gy").value };
    atualizar(await api("POST", `/api/${E.sid}/estrutura`, corpo, "Criando estrutura…"));
    $("#ne-nome").value = "";
  });
  $("#b-ponto").onclick = () => tentar(async () => {
    if (!E.cruz) throw new Error("Clique num corte para posicionar a cruz");
    atualizar(await api("POST", `/api/${E.sid}/ponto`, { nome: $("#pt-nome").value, xyz: E.cruz }));
    $("#pt-nome").value = "";
  });
  $("#v-axial .wl").onchange = (e) => { const [j, n] = e.target.value.split(",").map(Number); E.wl = { janela: j, nivel: n }; E.imgs = {}; carregarCortes(); };
  $("#v-axial .t-iso").onchange = () => desenharTudo();
  $("#bev-campo").onchange = () => tentar(() => carregarBev($("#bev-campo").value));
  $("#t-drr").onchange = () => tentar(() => carregarBev(E.bev && E.bev.campo.nome));
  $("#bev-seg-r").oninput = (e) => { E.bevSeg = +e.target.value; desenharBev(); };
  for (const v of ["axial", "coronal", "sagital"]) {
    const cv = $(`#v-${v} canvas`);
    cv.addEventListener("wheel", (ev) => { ev.preventDefault(); mudarCorte(v, ev.deltaY > 0 ? 1 : -1); }, { passive: false });
    cv.addEventListener("click", (ev) => { if (v === "axial" && contornoAtivo()) return; clicarCorte(v, ev); });
  }
  iniciarContorno();
  const bc = $("#v-bev canvas");
  bc.addEventListener("mousedown", bevBaixo); bc.addEventListener("mousemove", bevMove);
  bc.addEventListener("dblclick", bevDuplo);
  window.addEventListener("mouseup", bevSolta);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && E.bevDes.pts.length) { E.bevDes = { pts: [], mouse: null, livre: false }; desenharBev(); }
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT" || ev.target.tagName === "TEXTAREA") return;
    if (ev.key === "ArrowUp" || ev.key === "PageUp") mudarCorte("axial", -1);
    if (ev.key === "ArrowDown" || ev.key === "PageDown") mudarCorte("axial", 1);
  });
}

function mostrarPonto() {
  const m = $("#p-metodo").value;
  $("#p-ponto-linha").classList.toggle("oculto", m !== "ponto");
  $("#p-cob-linha").classList.toggle("oculto", m !== "cobertura");
  $("#p-iso-linha").classList.toggle("oculto", m === "um_fixas" || m === "cobertura");
}
function normalizacaoDoForm() {
  const metodo = $("#p-metodo").value, o = { metodo };
  if (metodo === "ponto") o.ponto = [+$("#p-px").value, +$("#p-py").value, +$("#p-pz").value];
  if (metodo === "cobertura") { o.cobertura_volume_pct = +$("#p-cobv").value; o.cobertura_dose_pct = +$("#p-cobd").value; o.isodose_pct = 100; }
  else if (metodo !== "um_fixas") o.isodose_pct = +$("#p-isopct").value || 100;
  return o;
}
function preencherPrescricao(r) {
  const p = r.plano; if (!p) return;
  const rx = p.prescricao;
  $("#p-rotulo").value = p.rotulo; $("#p-dose").value = rx.dose_total_gy; $("#p-fx").value = rx.fracoes;
  if (rx.estrutura_alvo) $("#p-alvo").value = rx.estrutura_alvo;
  $("#p-metodo").value = rx.metodo; $("#p-isopct").value = rx.isodose_pct || 100;
  if (rx.cobertura_volume_pct) $("#p-cobv").value = rx.cobertura_volume_pct;
  if (rx.cobertura_dose_pct) $("#p-cobd").value = rx.cobertura_dose_pct;
  if (rx.ponto_mm) { $("#p-px").value = rx.ponto_mm[0]; $("#p-py").value = rx.ponto_mm[1]; $("#p-pz").value = rx.ponto_mm[2]; }
  mostrarPonto();
}
async function configurarIsodoses() {
  const c = E.isoCfg, ref = E.isoRef;
  const v = await modal("Isodoses", `
    <label>Curvas (vazio = padrão 30 50 70 80 90 95 100 105 107 110 %)<input name="niveis" value="${(c.niveis || []).join(" ")}"
      placeholder="ex.: 95 100 105  ou em Gy: 45 50 52.5"></label>
    <div class="linha2">
      <label>100% é<select name="ref"><option value="rx" ${c.ref !== "max" ? "selected" : ""}>a prescrição</option>
        <option value="max" ${c.ref === "max" ? "selected" : ""}>o máximo do plano</option></select></label>
      <label>Mostrar em<select name="unidade"><option value="pct" ${c.unidade !== "gy" ? "selected" : ""}>% (relativo)</option>
        <option value="gy" ${c.unidade === "gy" ? "selected" : ""}>Gy (absoluto, dose total)</option></select></label>
    </div>
    <div class="pequeno-txt">O ponto de dose máxima do plano aparece sempre (▲) com o valor na legenda.${ref ? ` Máximo atual: ${f1(ref.dmax_gy, 2)} Gy
      (${f1(ref.dmax_gy / ref.rx_gy * 100, 1)}% da prescrição).` : ""}</div>`, { ok: "Aplicar" });
  if (!v) return;
  E.isoCfg = { niveis: v.niveis.trim() ? lerNiveis(v.niveis) : [], ref: v.ref, unidade: v.unidade };
  try { localStorage.setItem("tps_iso", JSON.stringify(E.isoCfg)); } catch (e) { /* ok */ }
  E.sobre = {}; carregarSobre();
}

// ------------------------------------------------------------------ caso e plano
async function abrirCaso(r) {
  E.sid = r.id; E.imgs = {}; E.sobre = {}; E.campoSel = null; E.bev = null; E.dvh = null; E.ocultas = new Set();
  const ct = r.ct;
  E.idx = { axial: Math.floor(ct.z[2] / 2), coronal: Math.floor(ct.y[2] / 2), sagital: Math.floor(ct.x[2] / 2) };
  if (r.tipo === "fantoma") { $("#p-metodo").value = "ponto"; mostrarPonto(); }
  if (!r.maquina) atualizar(await api("POST", `/api/${E.sid}/maquina`, { id: $("#maquina").value }));
  else atualizar(r);
  preencherPrescricao(E.resumo);
  if (r.isocentro) irPara(r.isocentro);
  carregarCortes();
}

async function criarPlano() {
  const n = normalizacaoDoForm();
  if (E.resumo.plano && E.resumo.plano.rotulo === $("#p-rotulo").value.trim()) {
    const v = await modal("Novo plano nesta TC", `<label>Rótulo do novo plano<input name="rotulo" maxlength="16" value=""></label>
      <div class="pequeno-txt">O plano ${E.resumo.plano.rotulo} continua na sessão; troque entre eles no seletor do topo.</div>`, { ok: "Criar" });
    if (!v || !v.rotulo.trim()) return;
    $("#p-rotulo").value = v.rotulo.trim();
  }
  E.campoSel = null;
  atualizar(await api("POST", `/api/${E.sid}/plano`, {
    rotulo: $("#p-rotulo").value, dose_total: +$("#p-dose").value, fracoes: +$("#p-fx").value,
    alvo: $("#p-alvo").value, metodo: n.metodo, ponto: n.ponto || null, isodose: n.isodose_pct || 100,
    cobertura_volume_pct: n.cobertura_volume_pct, cobertura_dose_pct: n.cobertura_dose_pct,
  }));
  if (E.resumo.isocentro) irPara(E.resumo.isocentro);
}

function atualizar(r) {
  E.resumo = r;
  const p = r.plano;
  $("#caso-nome").textContent = r.nome ? `${r.nome} ${r.paciente && r.paciente.id ? "(" + r.paciente.id + ")" : ""}` : "Caso";
  $("#estado").textContent = p ? `${p.rotulo} · ${p.estado}${r.dose_atualizada ? "" : " · dose desatualizada"}` : "sem plano";
  $("#estado").className = "selo " + (p ? p.estado : "");
  if (r.maquina) $("#maquina").value = r.maquina.id;
  // estruturas
  const alvos = r.estruturas.map(e => e.nome);
  const preferido = alvos.find(n => /ptv/i.test(n)) || alvos[0] || "";
  for (const sel of ["#p-alvo", "#c-alvo"]) {
    const atual = $(sel).value;
    $(sel).innerHTML = (sel === "#p-alvo" && r.tipo === "fantoma" ? `<option value="">(ponto)</option>` : "") +
      alvos.map(n => `<option>${n}</option>`).join("");
    $(sel).value = alvos.includes(atual) ? atual : (sel === "#p-alvo" && r.tipo === "fantoma" ? "" : preferido);
  }
  $("#c-blind").innerHTML = alvos.map(n => `<option>${n}</option>`).join("");
  { const atual = $("#k-est").value, nomes = r.estruturas.filter(e => e.tipo !== "BOLUS").map(e => e.nome);
    $("#k-est").innerHTML = nomes.map(n => `<option>${n}</option>`).join("");
    if (nomes.includes(atual)) $("#k-est").value = atual; }
  { const atual = $("#r-est").value; $("#r-est").innerHTML = alvos.map(n => `<option>${n}</option>`).join(""); if (alvos.includes(atual)) $("#r-est").value = atual; }
  $("#b-rest").disabled = !p;
  for (const sel of ["#ne-a", "#ne-b"]) { const atual = $(sel).value; $(sel).innerHTML = alvos.map(n => `<option>${n}</option>`).join(""); if (alvos.includes(atual)) $(sel).value = atual; }
  $("#b-nova-est").disabled = !r.estruturas.length;
  { const atual = $("#k-outra").value; $("#k-outra").innerHTML = alvos.map(n => `<option>${n}</option>`).join(""); if (alvos.includes(atual)) $("#k-outra").value = atual; }
  $("#b-modelo").disabled = !E.sid || r.tipo !== "ct";
  listarOars();
  const dens = (p && p.densidades) || {};
  $("#lista-estruturas").innerHTML = r.estruturas.map(e => `<li><input type="checkbox" data-est="${e.nome}" ${E.ocultas.has(e.nome) ? "" : "checked"}>
    <span class="cor" style="background:rgb(${e.cor})"></span>${e.nome}<span class="vol">${e.volume_cc} cc</span>${(e.tipo === "DERIVADA" || e.editada) ? `<button class="pequeno" data-rem-est="${e.nome}" title="remover estrutura criada/editada no TPS">×</button>` : ""}
    <input class="dens ${dens[e.nome] != null ? "ativa" : ""}" data-dens="${e.nome}" title="densidade eletrônica relativa forçada (vazio = do CT)"
      placeholder="ρe" value="${dens[e.nome] != null ? dens[e.nome] : ""}" ${p && p.estado === "RASCUNHO" ? "" : "disabled"}></li>`).join("");
  $$("[data-dens]").forEach(c => c.onchange = () => tentar(async () =>
    atualizar(await api("POST", `/api/${E.sid}/densidade`, { estrutura: c.dataset.dens, valor: c.value.trim() || null }))));
  $$("[data-rem-est]").forEach(b => b.onclick = () => tentar(async () => atualizar(await api("DELETE", `/api/${E.sid}/estrutura/${encodeURIComponent(b.dataset.remEst)}`))));
  $$("[data-est]").forEach(c => c.onchange = () => { c.checked ? E.ocultas.delete(c.dataset.est) : E.ocultas.add(c.dataset.est); desenharTudo(); desenharDvh(); });
  $("#iso-txt").textContent = r.isocentro ? r.isocentro.map(v => v.toFixed(1)).join(", ") : "—";
  // estados dos botões
  const temPlano = !!p, aprovado = p && p.estado === "APROVADO";
  $("#b-plano").disabled = !E.sid;
  $("#b-plano").textContent = temPlano ? "Novo plano" : "Criar plano";
  const ps = r.planos_sessao || [];
  $("#planos-sessao").classList.toggle("oculto", !ps.length);
  $("#plano-sel").innerHTML = ps.map(q => `<option value="${q.rotulo}" ${q.atual ? "selected" : ""}>${q.rotulo} · ${q.estado.toLowerCase()} · ${q.campos} campo(s)${q.dose ? "" : " · sem dose"}</option>`).join("");
  $("#b-plano-copiar").disabled = !temPlano; $("#b-plano-rem").disabled = !temPlano || aprovado;
  { const comDose = ps.filter(q => q.dose), opts = comDose.map(q => `<option>${q.rotulo}</option>`).join("");
    const va = $("#cmp-a").value, vb = $("#cmp-b").value;
    $("#cmp-a").innerHTML = opts; $("#cmp-b").innerHTML = opts;
    if (comDose.some(q => q.rotulo === va)) $("#cmp-a").value = va;
    if (comDose.some(q => q.rotulo === vb)) $("#cmp-b").value = vb; else if (comDose.length > 1) $("#cmp-b").selectedIndex = 1;
    $("#b-comparar").disabled = comDose.length < 2; $("#b-somar").disabled = comDose.length < 2;
    const marc = new Set($$("#soma-lista input:checked").map(i => i.value));
    $("#soma-lista").innerHTML = comDose.map(q => `<label class="check"><input type="checkbox" value="${q.rotulo}" ${marc.has(q.rotulo) ? "checked" : ""}> ${q.rotulo}</label>`).join("") ||
      `<div class="pequeno-txt">Calcule pelo menos dois planos desta TC.</div>`; }
  $("#b-renorm").disabled = !temPlano || aprovado || !r.dose_atualizada;
  const setup = (p && p.campos_setup) || [];
  $("#lista-setup").innerHTML = setup.map(c => `<li>${c.nome} · G${c.gantry.toFixed(0)} M${c.mesa.toFixed(0)} · ${f1((c.jaw_x ? c.jaw_x[1] - c.jaw_x[0] : c.mlc_b[20] - c.mlc_a[20]) / 10, 0)}×${f1((c.jaw_y[1] - c.jaw_y[0]) / 10, 0)} cm
    ${aprovado ? "" : `<button class="pequeno" data-rem-setup="${c.nome}">×</button>`}</li>`).join("");
  $$("[data-rem-setup]").forEach(b => b.onclick = () => tentar(async () => atualizar(await api("DELETE", `/api/${E.sid}/setup/${encodeURIComponent(b.dataset.remSetup)}`))));
  $$("#sec-setup button").forEach(b => { if (!b.dataset.remSetup) b.disabled = !temPlano || aprovado; });
  { const sel = new Set(Array.from($("#o-fil-campos").selectedOptions).map(o => o.value));
    const base = p ? p.campos.filter(c => !c.campo_pai) : [];
    if (!sel.size) base.filter(c => [90, 270].includes(Math.round(c.gantry) % 360)).forEach(c => sel.add(c.nome));
    $("#o-fil-campos").innerHTML = base.map(c => `<option ${sel.has(c.nome) ? "selected" : ""}>${c.nome}</option>`).join(""); }
  $("#form-campo").disabled = !temPlano || aprovado;
  $("#b-iso").disabled = !temPlano || aprovado;
  $("#b-calc").disabled = !temPlano || aprovado || !p.campos.length;
  $("#b-aprovar").disabled = !temPlano || aprovado || !r.dose_atualizada;
  $("#b-desaprovar").disabled = !aprovado;
  $("#b-exportar").disabled = !aprovado;
  $("#b-salvar").disabled = !temPlano; $("#b-relatorio").disabled = !temPlano; $("#b-rel-medico").disabled = !(temPlano && r.dose_atualizada);
  $("#b-checkfilm").disabled = !(temPlano && E.resumo.plano.campos.length);
  const pd = r.planos_dicom || [];
  $("#planos-dicom").classList.toggle("oculto", !pd.length);
  $("#lista-planos-dicom").innerHTML = pd.map(q => `<li><b>${q.rotulo}</b> · ${q.campos} campo(s) · ${q.maquina} · ${q.fabricante}
    ${q.dose ? "· com dose" : ""} <button class="pequeno" data-imp="${q.arquivo}">importar</button></li>`).join("");
  $$("[data-imp]").forEach(b => b.onclick = () => tentar(async () => {

    const r2 = await api("POST", `/api/${E.sid}/importar_plano`, { arquivo: b.dataset.imp }, "Importando o plano…");
    atualizar(r2); preencherPrescricao(r2);
    aviso("Plano importado como mais um plano desta TC (o anterior continua no seletor do topo)", true);
    if (r2.isocentro) irPara(r2.isocentro);
  }));
  if (p && p.prescricao && p.prescricao.metodo === "um_fixas") $("#p-metodo").value = "um_fixas";
  const cmp = r.resultado && r.resultado.comparacao;
  $("#comparacao").classList.toggle("oculto", !cmp);
  if (cmp) {
    $("#cmp-fonte").textContent = cmp.fonte;
    $("#cmp-resumo").textContent = `Gama 3%/3 mm (global, limiar 10%): ${f1(cmp.gama_3_3, 1)}% · Dmax do outro: ${f1(cmp.dmax_ref, 2)} Gy · DVH do outro tracejado`;
    $("#tab-cmp tbody").innerHTML = Object.entries(cmp.estatisticas).map(([n, q]) =>
      `<tr><td>${n}</td><td>${f1(q.Dmean_ref, 2)}</td><td>${f1(q.Dmean_tps, 2)}</td><td class="${Math.abs(q.dif_pct) > 3 ? "avisotxt" : ""}">${q.dif_pct > 0 ? "+" : ""}${f1(q.dif_pct, 1)}%</td></tr>`).join("");
  }
  $$("[data-arranjo]").forEach(b => b.disabled = !temPlano || aprovado);
  $("#b-ponto").disabled = !temPlano; $("#b-bolus").disabled = !temPlano || aprovado;
  $("#bo-campos").innerHTML = p ? p.campos.filter(c => !c.campo_pai).map(c => `<option>${c.nome}</option>`).join("") : "";
  $("#lista-bolus").innerHTML = p ? p.bolus.map(b => `<li>${b.nome} · ${b.volume_cc} cc · ${b.campos.join(", ")}
    ${aprovado ? "" : `<button class="pequeno" data-rem-bolus="${b.nome}">remover</button>`}</li>`).join("") : "";
  $$("[data-rem-bolus]").forEach(b => b.onclick = () => tentar(async () => atualizar(await api("DELETE", `/api/${E.sid}/bolus/${encodeURIComponent(b.dataset.remBolus)}`))));
  $("#tab-pontos tbody").innerHTML = p ? p.pontos.map(q => `<tr title="${Object.entries(q.campos || {}).map(([n, d]) => n + ": " + d + " Gy").join(" | ")}">
    <td>${q.nome}</td><td>${f1(q.total_gy, 2)}</td><td>${f1(q.pct)}</td><td><button data-ir="${q.xyz}">ir</button>
    <button data-rem-pt="${q.nome}">×</button></td></tr>`).join("") : "";
  $$("[data-ir]").forEach(b => b.onclick = () => irPara(b.dataset.ir.split(",").map(Number)));
  $$("[data-rem-pt]").forEach(b => b.onclick = () => tentar(async () => atualizar(await api("DELETE", `/api/${E.sid}/ponto/${encodeURIComponent(b.dataset.remPt)}`))));
  doseCruz();
  listarCampos(); tabelas();
  $("#avisos").innerHTML = (r.avisos || []).map(a => `<li>${a}</li>`).join("");
  $("#n-avisos").textContent = (r.avisos || []).length || "";
  if (p && r.dose_atualizada) carregarChecklist(); else $("#checklist").innerHTML = p ? `<div class="pequeno-txt">Calcule a dose para conferir o plano.</div>` : "";
  { const res = r.resultado, b = $("#dmax-topo");
    const ok = r.dose_atualizada && res && res.dmax_gy != null && p;
    b.classList.toggle("oculto", !ok);
    if (ok) {
      const pct = res.dmax_gy / p.prescricao.dose_total_gy * 100;
      b.innerHTML = `Dmax <b>${f1(res.dmax_gy, 2)} Gy</b> · ${f1(pct, 1)}%`;
      b.className = pct > 107 ? "quente" : (pct > 105 ? "morno" : "");
      b.onclick = () => res.dmax_xyz && irPara(res.dmax_xyz);
    } }
  $("#log").innerHTML = (r.log || []).slice().reverse().map(a => `<li>${a}</li>`).join("");
  // BEV
  const nomes = p ? [...p.campos.map(c => c.nome), ...(p.campos_setup || []).map(c => c.nome)] : [];
  const bsel = $("#bev-campo").value;
  $("#bev-campo").innerHTML = nomes.map(n => `<option>${n}</option>`).join("");
  if (nomes.length) {
    const alvo = nomes.includes(E.campoSel) ? E.campoSel : (nomes.includes(bsel) ? bsel : nomes[0]);
    $("#bev-campo").value = alvo; carregarBev(alvo).catch(e => aviso(e.message));
  } else { E.bev = null; desenharBev(); }
  E.sobre = {}; E.wash = {}; E.doseVer = (E.doseVer || 0) + 1; carregarSobre();
  if (r.dose_atualizada) carregarDvh(); else { E.dvh = null; desenharDvh(); }
}

function listarCampos() {
  const p = E.resumo && E.resumo.plano;
  $("#tab-campos tbody").innerHTML = !p ? "" : p.campos.map(c => `<tr data-campo="${c.nome}" class="${c.campo_pai ? "sub" : ""} ${c.nome === E.campoSel ? "sel" : ""}">
    <td>${c.nome}</td><td>${c.gantry.toFixed(0)}</td><td>${c.colimador.toFixed(0)}</td><td>${c.mu != null ? c.mu.toFixed(1) : "—"}</td>
    <td>${c.n_segmentos || (c.campo_pai ? "sub" : 1)}</td></tr>`).join("");
  $$("#tab-campos tbody tr").forEach(tr => tr.onclick = () => {
    E.campoSel = tr.dataset.campo; const c = p.campos.find(x => x.nome === E.campoSel);
    preencherForm(c); listarCampos(); $("#bev-campo").value = c.nome; carregarBev(c.nome).catch(e => aviso(e.message));
  });
  $("#b-campo").textContent = E.campoSel ? "Atualizar campo" : "Adicionar campo";
}

function preencherForm(c) {
  const s = c && c.spec ? c.spec : {};
  $("#c-nome").value = c ? c.nome : "";
  $("#c-gantry").value = c ? c.gantry : 0; $("#c-col").value = c ? c.colimador : 0; $("#c-mesa").value = c ? c.mesa : 0;
  $("#c-peso").value = c ? c.peso : 1;
  $("#c-modo").value = s.modo || "conformado"; $("#c-modo").onchange();
  if (s.alvo) $("#c-alvo").value = s.alvo;
  const m = s.margem && typeof s.margem === "object" ? s.margem : { x1: 7, x2: 7, y1: 7, y2: 7 };
  $("#c-x1m").value = m.x1; $("#c-x2m").value = m.x2; $("#c-y1m").value = m.y1; $("#c-y2m").value = m.y2;
  $("#c-encontro").value = s.ponto_encontro || "fora";
  $("#c-otcol").checked = !!s.otimizar_colimador;
  const bl = (s.blindagens || []).map(b => b.nome);
  Array.from($("#c-blind").options).forEach(o => o.selected = bl.includes(o.value));
  if (s.blindagens && s.blindagens[0]) $("#c-blind-m").value = s.blindagens[0].margem;
  if (s.modo === "retangular") { $("#c-x1").value = s.x1 / 10; $("#c-x2").value = s.x2 / 10; $("#c-y1").value = s.y1 / 10; $("#c-y2").value = s.y2 / 10; }
}

function specDoForm(extra = {}) {
  const blind = Array.from($("#c-blind").selectedOptions).map(o => ({ nome: o.value, margem: +$("#c-blind-m").value }));
  const s = {
    nome: $("#c-nome").value.trim() || undefined, gantry: +$("#c-gantry").value, colimador: +$("#c-col").value,
    mesa: +$("#c-mesa").value, peso: +$("#c-peso").value, modo: $("#c-modo").value, alvo: $("#c-alvo").value,
    margem: { x1: +$("#c-x1m").value, x2: +$("#c-x2m").value, y1: +$("#c-y1m").value, y2: +$("#c-y2m").value },
    ponto_encontro: $("#c-encontro").value, blindagens: blind, otimizar_colimador: $("#c-otcol").checked,
    x1: +$("#c-x1").value * 10, x2: +$("#c-x2").value * 10, y1: +$("#c-y1").value * 10, y2: +$("#c-y2").value * 10,
  };
  if (E.campoSel) s.nome_antigo = E.campoSel;
  return Object.assign(s, extra);
}

async function salvarCampo() {
  const r = await api("POST", `/api/${E.sid}/campo`, specDoForm(), "Ajustando o campo…");
  E.campoSel = r.campo_salvo.campo.nome;
  atualizar(r);
  (r.campo_salvo.avisos || []).forEach(a => aviso(a, true));
}

async function arranjo(tipo) {
  const angulos = { appa: [0, 180], lat: [90, 270], caixa: [0, 90, 180, 270], tres: [0, 90, 270] }[tipo];
  const p = E.resumo.plano;
  for (const c of p.campos.filter(c => !c.campo_pai)) await api("DELETE", `/api/${E.sid}/campo/${encodeURIComponent(c.nome)}`);
  E.campoSel = null;
  let r;
  for (const g of angulos) {
    const s = specDoForm({ nome: undefined, gantry: g, colimador: 0 }); delete s.nome_antigo;
    r = await api("POST", `/api/${E.sid}/campo`, s, `Ajustando campos (${g}°)…`);
  }
  atualizar(r);
}

async function calcular() {
  const comp = $("#o-comp").checked ? { niveis: +$("#o-niveis").value, um_min: +$("#o-ummin").value } : null;
  const modo = $("#o-fif-modo").value;
  let fif = false;
  if (modo === "otimizado") fif = true;
  if (modo === "filtro") fif = { modo: "filtro", niveis: lerNiveis($("#o-fil-niveis").value), peso_pct: +$("#o-fil-peso").value,
    campos: Array.from($("#o-fil-campos").selectedOptions).map(o => o.value) };
  if (fif && fif.modo === "filtro") fif.ajuste_fino = $("#o-fil-fino").checked;
  const corpo = { otimizar_pesos: $("#o-pesos").checked, field_in_field: fif, compensador: comp, ...normalizacaoDoForm() };
  const oars = Object.entries(E.oarCfg || {}).filter(([n, o]) => o.on && E.resumo.estruturas.some(e => e.nome === n))
    .map(([n, o]) => ({ estrutura: n, metrica: o.metrica, valor: o.valor === "" ? null : +o.valor, volume_pct: o.vol === "" ? null : +o.vol, prioridade: +o.prio || 5 }));
  if (oars.length) corpo.proteger_oars = { oars, subcampos: $("#oar-sub").checked, margem_mm: +$("#oar-mg").value };
  const r = await api("POST", `/api/${E.sid}/calcular`, corpo, "Calculando a dose (collapsed cone)…");
  atualizar(r);
  const i = r.resultado.info || {};
  if (i.protecao_oars) {
    const t = Object.entries(i.protecao_oars.oars).map(([n, q]) => `${n} Dméd ${f1(q.antes.Dmean, 1)}→${f1(q.depois.Dmean, 1)} Gy`).join(" · ");
    aviso(`Proteção de OARs: ${t} · HI ${i.protecao_oars.hi_antes.toFixed(3)} → ${i.protecao_oars.hi_depois.toFixed(3)}`, true);
  }
  if (i.compensador) aviso(`Compensador: HI ${i.compensador.hi_antes.toFixed(3)} → ${i.compensador.hi_depois.toFixed(3)}`, true);
  if (i.field_in_field) aviso(`Field-in-field${i.field_in_field.modo === "filtro" ? " (filtro)" : ""}: HI ${i.field_in_field.hi_antes.toFixed(3)} → ${i.field_in_field.hi_depois.toFixed(3)}` +
    (i.field_in_field.dmax_depois ? ` · Dmax ${f1(i.field_in_field.dmax_antes, 1)}% → ${f1(i.field_in_field.dmax_depois, 1)}%` : ""), true);
}

async function exportar() {
  const corpo = { modo_segmentos: $("#e-modo").value, pasta: $("#e-pasta").value.trim() || null, rtstruct: $("#e-rs").checked };
  if ($("#m-enviar").checked) corpo.enviar = { host: $("#m-host").value, porta: +$("#m-porta").value, ae_destino: $("#m-ae").value };
  corpo.senha = await assinatura("exportar o plano");
  if ($("#e-imagens").checked) corpo.incluir_imagens = true;
  const r = await api("POST", `/api/${E.sid}/exportar`, corpo, "Exportando…");
  $("#exportados").innerHTML = r.arquivos.filter(a => !/imagens_/.test(a)).map(a => `<div><a href="/api/${E.sid}/arquivo?t=${encodeURIComponent(E.token)}&caminho=${encodeURIComponent(a)}">${a.split(/[\\/]/).pop()}</a></div>`).join("") +
    (r.envio ? `<div class="ok">Enviado: ${r.envio.enviados.length} arquivo(s)</div>` : "") + `<div class="pequeno-txt">${r.pasta}</div>` +
    (E.janela ? `<button class="pequeno" id="b-abrir-exp">Abrir a pasta</button>` : "");
  if (E.janela) $("#b-abrir-exp").onclick = () => window.pywebview.api.abrir_pasta(r.pasta);
  aviso("RTPLAN exportado e conferido", true);
}

function listarOars() {
  const r = E.resumo; if (!r) return;
  E.oarCfg = E.oarCfg || {};
  const p = r.plano, alvo = p && p.prescricao.estrutura_alvo;
  const nomes = r.estruturas.filter(e => e.nome !== alvo && !/^(EXTERNAL|BOLUS)$/i.test(e.tipo) && !/^(body|external|corpo|externo)$/i.test(e.nome) && !/^(PTV|CTV|GTV|ITV)/i.test(e.nome)).map(e => e.nome);
  $("#tab-oars tbody").innerHTML = nomes.map(n => {
    const o = E.oarCfg[n] || { on: false, metrica: "Dmean", valor: "", vol: "", prio: 5 };
    return `<tr data-oar="${n}"><td><input type="checkbox" data-k="on" ${o.on ? "checked" : ""}></td><td>${n}</td>
      <td><select data-k="metrica"><option value="Dmean" ${o.metrica === "Dmean" ? "selected" : ""}>Dméd ≤</option><option value="Dmax" ${o.metrica === "Dmax" ? "selected" : ""}>Dmáx ≤</option>
        <option value="V" ${o.metrica === "V" ? "selected" : ""}>V(Gy) ≤ vol</option></select></td>
      <td><input data-k="valor" value="${o.valor}" placeholder="mín."></td><td><input data-k="vol" value="${o.vol}" ${o.metrica === "V" ? "" : "disabled"}></td>
      <td><input data-k="prio" type="number" min="1" max="10" value="${o.prio}"></td></tr>`;
  }).join("");
  $$("#tab-oars tbody tr").forEach(tr => tr.querySelectorAll("[data-k]").forEach(i => i.onchange = () => {
    const n = tr.dataset.oar, o = E.oarCfg[n] || { on: false, metrica: "Dmean", valor: "", vol: "", prio: 5 };
    o[i.dataset.k] = i.type === "checkbox" ? i.checked : i.value;
    if (i.dataset.k !== "on") o.on = true;
    E.oarCfg[n] = o; listarOars();
  }));
}
async function carregarChecklist() {
  const el = $("#checklist");
  if (!E.sid || !E.resumo || !E.resumo.plano) { el.innerHTML = ""; return []; }
  let lista = [];
  try { lista = await api("GET", `/api/${E.sid}/checklist`); } catch (e) { return []; }
  const ic = { ok: "✓", aviso: "!", erro: "✗" };
  const n = lista.filter(i => i.estado === "erro").length, a = lista.filter(i => i.estado === "aviso").length;
  el.innerHTML = `<div class="chk-titulo">Checklist do plano ${n ? `<span class="falha">${n} pendência(s)</span>` : (a ? `<span class="avisotxt">${a} aviso(s)</span>` : `<span class="ok">tudo certo</span>`)}</div>` +
    lista.map(i => `<div class="chk chk-${i.estado}" title="${i.detalhe.replace(/"/g, "&quot;")}"><span>${ic[i.estado]}</span> ${i.titulo}<small>${i.detalhe}</small></div>`).join("");
  return lista;
}

function mostrarIA() {
  const ia = (E.info && E.info.ia) || {};
  $("#ia-estado").textContent = ia.disponivel ? "instalado" : (ia.motivo || "não instalado neste computador");
  const grupos = {};
  (ia.orgaos || []).forEach(o => (grupos[o.grupo || "Órgãos"] = grupos[o.grupo || "Órgãos"] || []).push(o));
  $("#ia-orgaos").innerHTML = Object.entries(grupos).map(([g, os]) => `<optgroup label="${g}">` +
    os.map(o => `<option value="${o.id}">${o.nome.replace(/_/g, " ")}</option>`).join("") + "</optgroup>").join("");
  $("#ia-orgaos").disabled = !ia.disponivel; $("#b-ia").disabled = !ia.disponivel;
}

// ------------------------------------------------------------------ tabelas
const f1 = (v, n = 1) => v == null || isNaN(v) ? "—" : (+v).toFixed(n);
function tabelas() {
  const r = E.resumo, res = r && r.resultado;
  if (!res) {
    $("#tab-estat tbody").innerHTML = ""; $("#resumo-dose").textContent = r && r.plano ? "Dose ainda não calculada" : "—";
  } else {
    const rx = r.plano.prescricao;
    $("#resumo-dose").innerHTML = `Prescrição ${rx.dose_total_gy} Gy / ${rx.fracoes} fx · Dmax ${f1(res.dmax_gy, 2)} Gy (${f1(res.dmax_gy / rx.dose_total_gy * 100)}%)` +
      (res.hi != null ? ` · HI ${f1(res.hi, 3)}` : "");
    $("#tab-estat tbody").innerHTML = Object.entries(res.estatisticas).map(([n, h]) =>
      `<tr><td>${n}</td><td>${f1(h.Dmean, 2)}</td><td>${f1(h.D95, 2)}</td><td>${f1(h.D2, 2)}</td><td>${f1(h.Dmax, 2)}</td></tr>`).join("");
  }
  const rr = res && res.restricoes;
  $("#rest-prot").textContent = rr && rr.protocolo ? `(${rr.protocolo})` : "";
  $("#rest-avisos").textContent = rr ? [...(rr.avisos || []), ...(rr.erro ? [rr.erro] : [])].join(" ") : "";
  $("#tab-rest tbody").innerHTML = !rr ? "" : (rr.resultados || []).map(q => {
    const cls = q.ok ? "ok" : (q.tipo === "limite" ? "falha" : "avisotxt");
    return `<tr title="${q.tg263} · ${q.tipo} · ${q.fonte}${q.obs ? " · " + q.obs : ""}" class="${q.extra != null ? "propria" : ""}"><td>${q.estrutura}</td><td>${q.metrica}</td>` +
      `<td>${q.op.replace(">=", "≥").replace("<=", "≤")} ${q.limite} ${q.unidade === "Gy" ? "" : q.unidade.startsWith("%") ? "%" : q.unidade}</td><td class="${cls}">${f1(q.valor, 1)}</td>` +
      `<td>${q.extra != null ? `<button class="pequeno" data-rem-rest="${q.extra}" title="remover restrição própria">×</button>` : ""}</td></tr>`;
  }).join("");
  $$("[data-rem-rest]").forEach(b => b.onclick = () => tentar(async () => atualizar(await api("DELETE", `/api/${E.sid}/restricao/${b.dataset.remRest}`))));
  const p = r && r.plano;
  const conf = res ? Object.fromEntries(res.conferencia_um.map(k => [k.campo, k])) : {};
  $("#tab-um tbody").innerHTML = !p ? "" : p.campos.map(c => {
    const k = conf[c.nome];
    const t = !k ? "—" : k.informativo ? `<span class="pequeno-txt" title="subcampo: conferido junto com o campo principal (mesmo feixe)">no feixe</span>` :
      `<span class="${k.ok ? "ok" : (k.compensado ? "avisotxt" : "falha")}" title="motor ${f1(k.motor_cgy_um, 4)} × tabela ${f1(k.tabela_cgy_um, 4)} cGy/UM${k.feixe ? " — feixe inteiro (campo + " + k.subcampos.join(", ") + ")" : ""}">${k.dif_pct >= 0 ? "+" : ""}${f1(k.dif_pct, 2)}%${k.feixe ? "*" : ""}</span>`;
    return `<tr class="${c.campo_pai ? "sub" : ""}"><td>${c.nome}</td><td>${f1(c.mu)}</td><td>${c.ssd_mm ? f1(c.ssd_mm / 10) : "—"}</td><td>${t}</td></tr>`;
  }).join("");
}

// ------------------------------------------------------------------ cortes
const VISTAS = {
  axial:   { h: "x", v: "y", invV: false },
  coronal: { h: "x", v: "z", invV: true },
  sagital: { h: "y", v: "z", invV: true },
};
const EIXO_IDX = { axial: "z", coronal: "y", sagital: "x" };
function eixo(k) { const [a, b, n] = E.resumo.ct[k]; return { a, b, n, d: n > 1 ? (b - a) / (n - 1) : 1 }; }
function posDoIdx(vista, i) { const e = eixo(EIXO_IDX[vista]); return e.a + i * e.d; }
function idxDaPos(vista, mm) { const e = eixo(EIXO_IDX[vista]); return Math.max(0, Math.min(e.n - 1, Math.round((mm - e.a) / e.d))); }

function mudarCorte(vista, d) {
  if (!E.resumo) return;
  const n = eixo(EIXO_IDX[vista]).n;
  E.idx[vista] = Math.max(0, Math.min(n - 1, E.idx[vista] + d));
  carregarCorte(vista);
}
function irPara(xyz) {
  E.cruz = xyz.slice();
  E.idx.axial = idxDaPos("axial", xyz[2]); E.idx.coronal = idxDaPos("coronal", xyz[1]); E.idx.sagital = idxDaPos("sagital", xyz[0]);
  carregarCortes();
}
function carregarCortes() { for (const v of Object.keys(VISTAS)) carregarCorte(v); }
function carregarCorte(vista) {
  if (!E.sid) return;
  const i = E.idx[vista], chave = `${vista}:${i}`;
  if (!E.imgs[chave]) {
    const img = new Image();
    img.onload = () => desenharCorte(vista);
    img.src = `/api/${E.sid}/corte/${vista}/${i}.png?janela=${E.wl.janela}&nivel=${E.wl.nivel}&t=${encodeURIComponent(E.token)}`;
    E.imgs[chave] = img;
  }
  carregarSobreVista(vista);
  desenharCorte(vista);
}
function carregarSobre() { for (const v of Object.keys(VISTAS)) carregarSobreVista(v); }
async function carregarSobreVista(vista) {
  if (!E.sid) return;
  const i = E.idx[vista], chave = `${vista}:${i}`;
  if (E.sobre[chave]) return desenharCorte(vista);
  E.sobre[chave] = "carregando";
  const c = E.isoCfg, q = `?ref=${c.ref}&unidade=${c.unidade}&fonte=${E.fonte}&niveis=${encodeURIComponent(E.fonte === "dif" ? "" : (c.niveis || []).join(","))}`;
  try { E.sobre[chave] = await api("GET", `/api/${E.sid}/sobreposicao/${vista}/${i}${q}`); if (E.sobre[chave].iso_ref) E.isoRef = E.sobre[chave].iso_ref; }
  catch (e) { delete E.sobre[chave]; return; }
  desenharCorte(vista);
}

function prepararCanvas(cv) {
  const dpr = window.devicePixelRatio || 1, r = cv.getBoundingClientRect();
  if (cv.width !== Math.round(r.width * dpr) || cv.height !== Math.round(r.height * dpr)) {
    cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
  }
  const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { g, w: r.width, h: r.height };
}

function transf(vista, w, h) {
  // mm → px preservando proporção; a imagem cobre [a − d/2, b + d/2] em cada eixo
  const V = VISTAS[vista], eh = eixo(V.h), ev = eixo(V.v);
  const H0 = eh.a - eh.d / 2, H1 = eh.b + eh.d / 2, V0 = ev.a - ev.d / 2, V1 = ev.b + ev.d / 2;
  const s = Math.min(w / (H1 - H0), h / (V1 - V0));
  const ox = (w - (H1 - H0) * s) / 2, oy = (h - (V1 - V0) * s) / 2;
  return {
    s, ox, oy, H0, H1, V0, V1,
    px: (hh, vv) => [ox + (hh - H0) * s, V.invV ? oy + (V1 - vv) * s : oy + (vv - V0) * s],
    mm: (x, y) => [H0 + (x - ox) / s, V.invV ? V1 - (y - oy) / s : V0 + (y - oy) / s],
  };
}

function desenharTudo() { for (const v of Object.keys(VISTAS)) desenharCorte(v); desenharBev(); desenharDvh(); }

function desenharCorte(vista) {
  const cv = $(`#v-${vista} canvas`), { g, w, h } = prepararCanvas(cv);
  g.fillStyle = "#000"; g.fillRect(0, 0, w, h);
  if (!E.resumo || !E.resumo.ct) return;
  const T = transf(vista, w, h), i = E.idx[vista];
  const img = E.imgs[`${vista}:${i}`];
  if (img && img.complete && img.naturalWidth) {
    g.imageSmoothingEnabled = true;
    g.drawImage(img, T.ox, T.oy, (T.H1 - T.H0) * T.s, (T.V1 - T.V0) * T.s);
  }
  const sobW = E.sobre[`${vista}:${i}`];
  if ($("#t-wash") && $("#t-wash").checked && sobW && sobW !== "carregando" && sobW.wash) {
    const min = $("#wash-min").value, chave = `${vista}:${i}:${min}:${E.doseVer || 0}:${E.fonte}:${E.fonteVer || 0}`;
    E.wash = E.wash || {};
    let wi = E.wash[chave];
    if (!wi) {
      wi = new Image(); wi.onload = () => desenharCorte(vista);
      wi.src = `/api/${E.sid}/dose_corte/${vista}/${i}.png?minimo=${min}&fonte=${E.fonte}&v=${E.doseVer || 0}-${E.fonteVer || 0}&t=${encodeURIComponent(E.token)}`;
      E.wash[chave] = wi;
    }
    if (wi.complete && wi.naturalWidth) {
      const [xa, ya] = T.px(sobW.wash.h[0], sobW.wash.v[0]), [xb, yb] = T.px(sobW.wash.h[1], sobW.wash.v[1]);
      g.imageSmoothingEnabled = true;
      g.drawImage(wi, Math.min(xa, xb), Math.min(ya, yb), Math.abs(xb - xa), Math.abs(yb - ya));
    }
    if (vista === "axial") {          // barra de cores
      const dif = sobW.wash.dif;
      const x0 = 10, y0 = h - 22, larg = Math.min(220, w - 20), lo = dif ? -10 : +min, hi = dif ? 10 : Math.max(sobW.wash.dmax_pct, lo + 1);
      const grad = g.createLinearGradient(x0, 0, x0 + larg, 0);
      (dif ? ["#3b4cc0", "#8db0fe", "#dddddd", "#f49a7b", "#b40426"] : ["#00008f", "#0000ff", "#00ffff", "#7fff7f", "#ffff00", "#ff0000", "#800000"])
        .forEach((c, k, a) => grad.addColorStop(k / (a.length - 1), c));
      g.fillStyle = grad; g.fillRect(x0, y0, larg, 8);
      g.fillStyle = "#fff"; g.font = "10px system-ui";
      g.fillText(`${lo}%`, x0, y0 - 3); g.fillText(`${hi.toFixed(0)}%`, x0 + larg - 26, y0 - 3);
    }
  }
  const pos = posDoIdx(vista, i);
  $(`#v-${vista} .pos`).textContent = `${EIXO_IDX[vista]} = ${pos.toFixed(1)} mm (${i + 1}/${eixo(EIXO_IDX[vista]).n})`;
  const sob = E.sobre[`${vista}:${i}`];
  const linha = (pts, fechar) => { g.beginPath(); pts.forEach(([a, b], k) => { const [x, y] = T.px(a, b); k ? g.lineTo(x, y) : g.moveTo(x, y); }); if (fechar) g.closePath(); g.stroke(); };
  if (sob && sob !== "carregando") {
    g.lineWidth = 1.4;
    for (const c of sob.contornos) {
      if (E.ocultas.has(c.nome)) continue;
      g.strokeStyle = `rgb(${c.cor})`; c.linhas.forEach(l => linha(l, true));
    }
    if ($("#v-axial .t-iso").checked) {
      g.lineWidth = 1.2;
      for (const iso of sob.isodoses) { g.strokeStyle = corIso(iso.nivel); iso.linhas.forEach(l => linha(l, false)); }
    }
    if (vista === "axial") {
      g.setLineDash([5, 4]);
      for (const f of sob.feixes) {
        g.strokeStyle = f.nome === E.campoSel ? "#ffd84d" : "rgba(120,200,255,.75)"; g.lineWidth = 1;
        for (const b of f.bordas) linha([f.fonte, b], false);
        g.strokeStyle = "rgba(255,255,255,.45)"; linha([f.fonte, f.cax_fim], false);
        // rótulo do lado da entrada do feixe (entre o isocentro e a fonte)
        const ent = [f.iso[0] + (f.fonte[0] - f.iso[0]) * 0.17, f.iso[1] + (f.fonte[1] - f.iso[1]) * 0.17];
        const [lx, ly] = T.px(...ent); g.fillStyle = f.nome === E.campoSel ? "#ffd84d" : "#9fd3ff"; g.font = "11px system-ui";
        g.fillText(f.nome, Math.min(Math.max(lx + 4, 4), w - 50), Math.min(Math.max(ly - 4, 12), h - 4));
      }
      g.setLineDash([]);
    }
  }
  // isocentro e cruz
  const iso = E.resumo.isocentro;
  if (iso) {
    const V = VISTAS[vista], k = { x: 0, y: 1, z: 2 };
    const [x, y] = T.px(iso[k[V.h]], iso[k[V.v]]);
    g.strokeStyle = "#ff4d4d"; g.lineWidth = 1.5; g.beginPath(); g.arc(x, y, 5, 0, 2 * Math.PI);
    g.moveTo(x - 9, y); g.lineTo(x + 9, y); g.moveTo(x, y - 9); g.lineTo(x, y + 9); g.stroke();
  }
  if (E.cruz) {
    const V = VISTAS[vista], k = { x: 0, y: 1, z: 2 };
    const [x, y] = T.px(E.cruz[k[V.h]], E.cruz[k[V.v]]);
    g.strokeStyle = "rgba(255,230,0,.5)"; g.lineWidth = 1; g.beginPath();
    g.moveTo(x, 0); g.lineTo(x, h); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
  }
  if (vista === "axial") desenharContornoPreview(g, T);
  // ponto de dose máxima do plano (sempre que estiver neste corte)
  const res = E.resumo.resultado, mx = E.resumo.dose_atualizada && res && res.dmax_xyz;
  if (mx) {
    const V = VISTAS[vista], k = { x: 0, y: 1, z: 2 }, ax = EIXO_IDX[vista];
    if (Math.abs(mx[k[ax]] - posDoIdx(vista, i)) <= Math.max(eixo(ax).d, 2.5) * 0.75) {
      const [x, y] = T.px(mx[k[V.h]], mx[k[V.v]]);
      g.fillStyle = "#ff00c8"; g.strokeStyle = "#000"; g.lineWidth = 1;
      g.beginPath(); g.moveTo(x, y - 7); g.lineTo(x + 6, y + 4); g.lineTo(x - 6, y + 4); g.closePath(); g.fill(); g.stroke();
    }
  }
  // legenda de isodoses (curvas escolhidas + máximo do plano)
  if (vista === "axial" && sob && sob.iso_ref && $("#v-axial .t-iso").checked) {
    const ir = sob.iso_ref, gy = ir.unidade === "gy";
    g.font = "11px system-ui"; let yy = 16;
    const txtMax = ir.dif ? `máx +${f1(ir.dmax_gy, 2)} Gy` : gy ? `máx ${f1(ir.dmax_gy, 2)} Gy` : `máx ${f1(ir.dmax_gy / ir.cem_gy * 100, 1)}%`;
    g.fillStyle = "#ff00c8"; g.textAlign = "right"; g.fillText("▲ " + txtMax, w - 6, yy); yy += 14;
    for (const n of ir.niveis.slice().reverse()) {
      const sn = ir.dif && n > 0 ? "+" : "";
      g.fillStyle = corIso(n); g.fillText(gy ? `${sn}${+n.toFixed(2)} Gy` : `${sn}${+n.toFixed(1)}%`, w - 6, yy); yy += 13;
    }
    g.fillStyle = "#8b95a3"; g.fillText(ir.dif ? `${ir.descricao} · % de ${f1(ir.rx_gy, 2)} Gy` : (ir.referencia === "max" ? `100% = máx (${f1(ir.cem_gy, 2)} Gy)` : `100% = ${f1(ir.rx_gy, 2)} Gy`) + (ir.fonte === "soma" ? ` · ${ir.descricao}` : ""), w - 6, yy + 2);
    g.textAlign = "left";
  }
}

function clicarCorte(vista, ev) {
  if (!E.resumo || !E.resumo.ct) return;
  const cv = $(`#v-${vista} canvas`), r = cv.getBoundingClientRect();
  const T = transf(vista, r.width, r.height), [a, b] = T.mm(ev.clientX - r.left, ev.clientY - r.top);
  const V = VISTAS[vista], k = { x: 0, y: 1, z: 2 };
  const c = E.cruz ? E.cruz.slice() : [0, 0, posDoIdx("axial", E.idx.axial)];
  c[k[V.h]] = a; c[k[V.v]] = b;
  c[k[EIXO_IDX[vista]]] = posDoIdx(vista, E.idx[vista]);
  E.cruz = c.map(v => Math.round(v * 10) / 10);
  for (const outra of Object.keys(VISTAS)) if (outra !== vista) E.idx[outra] = idxDaPos(outra, E.cruz[k[EIXO_IDX[outra]]]);
  carregarCortes(); doseCruz();
  if ($("#p-metodo").value === "ponto" && ev.shiftKey) { $("#p-px").value = E.cruz[0]; $("#p-py").value = E.cruz[1]; $("#p-pz").value = E.cruz[2]; }
}

// ------------------------------------------------------------------ BEV
async function carregarBev(nome) {
  if (!nome || !E.sid) return;
  E.bev = await api("GET", `/api/${E.sid}/bev/${encodeURIComponent(nome)}`);
  E.bevSeg = 0;
  const ns = E.bev.segmentos.length;
  $("#bev-seg").classList.toggle("oculto", ns === 0);
  $("#bev-seg-r").max = Math.max(0, ns); $("#bev-seg-r").value = 0;
  if ($("#t-drr").checked) {
    const img = new Image(); img.onload = () => desenharBev(); img.src = `/api/${E.sid}/drr/${encodeURIComponent(nome)}.png?t=${encodeURIComponent(E.token)}&r=${Date.now()}`; E.drrImg = img;
  } else E.drrImg = null;
  desenharBev();
}

function bevT(w, h) {
  const ext = 110; // mm mostrados em volta do isocentro (ajustado ao campo abaixo)
  let r = ext;
  if (E.bev) { const c = E.bev.campo; const m = Math.max(Math.abs(c.jaw_y[0]), Math.abs(c.jaw_y[1]), ...(c.jaw_x || [Math.min(...c.mlc_a.filter(isFinite)), Math.max(...c.mlc_b)]).map(Math.abs)); r = Math.min(200, Math.max(60, m * 1.35)); }
  const s = Math.min(w, h) / (2 * r);
  return { s, r, px: (u, v) => [w / 2 + u * s, h / 2 - v * s], mm: (x, y) => [(x - w / 2) / s, (h / 2 - y) / s] };
}

function formaAtual() {
  const c = E.bev.campo;
  if (E.bevSeg > 0 && E.bev.segmentos[E.bevSeg - 1]) { const s = E.bev.segmentos[E.bevSeg - 1]; return { a: s.mlc_a, b: s.mlc_b, peso: s.peso }; }
  return { a: c.mlc_a, b: c.mlc_b };
}

function desenharBev() {
  const cv = $("#v-bev canvas"), { g, w, h } = prepararCanvas(cv);
  g.fillStyle = "#05070a"; g.fillRect(0, 0, w, h);
  if (!E.bev) return;
  const T = bevT(w, h), B = E.bev, c = B.campo, lim = B.limites, F = formaAtual();
  if (E.drrImg && E.drrImg.complete && E.drrImg.naturalWidth) {
    const [x0, y0] = T.px(-200, 200), [x1, y1] = T.px(200, -200);
    g.globalAlpha = 0.85; g.drawImage(E.drrImg, x0, y0, x1 - x0, y1 - y0); g.globalAlpha = 1;
  }
  // lâminas
  const L = T.r * 1.1;
  for (let i = 0; i < lim.length - 1; i++) {
    const [, ya] = T.px(0, lim[i + 1]), [, yb] = T.px(0, lim[i]);
    if (yb < 0 || ya > h) continue;
    const [xa] = T.px(F.a[i], 0), [xb] = T.px(F.b[i], 0), [xe] = T.px(-L, 0), [xd] = T.px(L, 0);
    const sel = E.arrasto && E.arrasto.par === i;
    g.fillStyle = sel ? "rgba(255,216,77,.55)" : "rgba(92,108,124,.62)";
    g.fillRect(xe, ya, xa - xe, yb - ya); g.fillRect(xb, ya, xd - xb, yb - ya);
    g.strokeStyle = "rgba(10,14,18,.9)"; g.lineWidth = 0.6; g.strokeRect(xe, ya, xa - xe, yb - ya); g.strokeRect(xb, ya, xd - xb, yb - ya);
  }
  // jaws
  g.strokeStyle = "#ffa62b"; g.lineWidth = 2;
  for (const v of c.jaw_y) { const [, y] = T.px(0, v); g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
  if (c.jaw_x) { g.strokeStyle = "#4db8ff"; g.setLineDash([6, 4]); for (const u of c.jaw_x) { const [x] = T.px(u, 0); g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); } g.setLineDash([]); }
  // projeções
  g.lineWidth = 1.6;
  for (const p of B.projecoes) {
    if (E.ocultas.has(p.nome)) continue;
    g.strokeStyle = `rgb(${p.cor})`;
    for (const l of p.linhas) { g.beginPath(); l.forEach(([u, v], k) => { const [x, y] = T.px(u, v); k ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke(); }
  }
  // eixo central
  const [cx, cy] = T.px(0, 0); g.strokeStyle = "rgba(255,255,255,.5)"; g.lineWidth = 1;
  g.beginPath(); g.moveTo(cx - 10, cy); g.lineTo(cx + 10, cy); g.moveTo(cx, cy - 10); g.lineTo(cx, cy + 10); g.stroke();
  g.fillStyle = "#cfd8e3"; g.font = "11px system-ui";
  g.fillText(`${c.nome} · G${c.gantry.toFixed(0)} C${c.colimador.toFixed(0)} M${c.mesa.toFixed(0)} · ${c.mu != null ? c.mu.toFixed(1) + " UM" : "UM —"}` +
    (F.peso ? ` · segmento ${E.bevSeg}/${B.segmentos.length} (${(F.peso * 100).toFixed(0)}%)` : ""), 8, 14);
  g.fillText("A ←   → B", 8, h - 8);
  const D = E.bevDes;
  if (D.pts.length) {
    g.strokeStyle = "#ffd84d"; g.fillStyle = "rgba(255,216,77,.12)"; g.lineWidth = 1.5; g.setLineDash([4, 3]);
    g.beginPath(); D.pts.forEach(([u, v], k) => { const [x, y] = T.px(u, v); k ? g.lineTo(x, y) : g.moveTo(x, y); });
    if (D.mouse && !D.livre) { const [x, y] = T.px(...D.mouse); g.lineTo(x, y); }
    g.closePath(); g.fill(); g.stroke(); g.setLineDash([]);
  }
  if (E.arrasto && E.arrasto.pos !== undefined) {
    g.fillStyle = "#ffd84d";
    g.fillText(E.arrasto.jaw ? `${E.arrasto.jaw.toUpperCase()} ${(E.arrasto.pos / 10).toFixed(2)} cm` :
      `par ${E.arrasto.par + 1} ${E.arrasto.banco.toUpperCase()} ${(E.arrasto.pos / 10).toFixed(2)} cm`, 8, 28);
  }
  $("#bev-seg-t").textContent = E.bevSeg ? `${E.bevSeg}/${B.segmentos.length}` : "envelope";
}

function bevPos(ev) {
  const cv = $("#v-bev canvas"), r = cv.getBoundingClientRect(), T = bevT(r.width, r.height);
  return { T, mm: T.mm(ev.clientX - r.left, ev.clientY - r.top) };
}
function bevEditavel() { return E.bev && E.bevSeg === 0 && E.resumo.plano && E.resumo.plano.estado === "RASCUNHO"; }
// cópia em JS de ajuste_mlc.acomodar_lamina (prévia durante o arrasto; o servidor refaz e confere a mecânica)
function acomodarLamina(a, b, par, banco, pos) {
  const B = E.bev, g = B.gap || 0, n = a.length, lmax = B.lamina_max || 200, ot = B.overtravel || 125;
  a = a.slice(); b = b.slice();
  if (banco === "a") { a[par] = Math.max(-lmax, Math.min(pos, ot, lmax - g)); if (b[par] - a[par] < g) b[par] = a[par] + g; }
  else { b[par] = Math.min(lmax, Math.max(pos, -ot, -lmax + g)); if (b[par] - a[par] < g) a[par] = b[par] - g; }
  if (B.interdigitacao === false) {
    const fila = [par]; let k = 0;
    while (fila.length && k++ < 8 * n) {
      const i = fila.shift();
      for (const j of [i - 1, i + 1]) {
        if (j < 0 || j >= n) continue;
        const fechado = b[j] - a[j] <= g + 1e-6; let mudou = false;
        if (a[i] > b[j] - g + 1e-9) { b[j] = a[i] + g; if (fechado) a[j] = b[j] - g; mudou = true; }
        if (b[i] < a[j] + g - 1e-9) { a[j] = b[i] - g; if (fechado) b[j] = a[j] + g; mudou = true; }
        if (mudou) { if (b[j] - a[j] < g) b[j] = a[j] + g; fila.push(j); }
      }
    }
  }
  return [a, b];
}
function bevBaixo(ev) {
  if (!bevEditavel()) return;
  const { T, mm: [u, v] } = bevPos(ev), c = E.bev.campo, lim = E.bev.limites, tol = 6 / T.s;
  const ferr = $("#bev-ferr").value;
  if (ferr !== "editar") {                                  // desenho: vértice ou início de traço à mão livre
    E.bevDes.pts.push([u, v]); E.bevDes.baixo = [u, v]; E.bevDes.livre = false; ev.preventDefault(); desenharBev(); return;
  }
  // jaws primeiro (linha a até 6 px)
  const jaws = [["y1", c.jaw_y[0], Math.abs(v - c.jaw_y[0])], ["y2", c.jaw_y[1], Math.abs(v - c.jaw_y[1])]];
  if (c.jaw_x) jaws.push(["x1", c.jaw_x[0], Math.abs(u - c.jaw_x[0])], ["x2", c.jaw_x[1], Math.abs(u - c.jaw_x[1])]);
  let par = -1; for (let i = 0; i < lim.length - 1; i++) if (v >= lim[i] && v < lim[i + 1]) par = i;
  const da = par >= 0 ? Math.abs(u - c.mlc_a[par]) : Infinity, db = par >= 0 ? Math.abs(u - c.mlc_b[par]) : Infinity;
  const jmin = jaws.reduce((m, j) => j[2] < m[2] ? j : m, ["", 0, Infinity]);
  // a lâmina tem prioridade (o jaw X do MLCi costuma coincidir com a lâmina mais aberta); Shift pega o jaw
  if (jmin[2] <= tol && (ev.shiftKey || Math.min(da, db) > tol)) {
    E.arrasto = { jaw: jmin[0], jy: c.jaw_y.slice(), jx: c.jaw_x ? c.jaw_x.slice() : null, a: c.mlc_a.slice(), b: c.mlc_b.slice() };
  } else {
    if (par < 0 || Math.min(da, db) > tol) return;
    // par fechado: o lado é o do clique em relação ao gap
    const banco = Math.abs(da - db) < 1e-6 ? (u < c.mlc_a[par] ? "a" : "b") : (da < db ? "a" : "b");
    E.arrasto = { par, banco, a: c.mlc_a.slice(), b: c.mlc_b.slice() };
  }
  ev.preventDefault();
}
function bevMove(ev) {
  if (!E.bev) return;
  const { mm: [u, v] } = bevPos(ev);
  if ($("#bev-ferr").value !== "editar") {
    const D = E.bevDes; D.mouse = [u, v];
    if (D.baixo && ev.buttons & 1) {                        // arrastando: mão livre
      const ult = D.pts[D.pts.length - 1];
      if (Math.hypot(u - ult[0], v - ult[1]) > 1.5) { D.pts.push([u, v]); D.livre = true; }
    }
    desenharBev(); return;
  }
  if (!E.arrasto) return;
  const A = E.arrasto, c = E.bev.campo, uu = Math.round(u * 2) / 2, vv = Math.round(v * 2) / 2;
  if (A.jaw) {
    if (A.jaw[0] === "y") { const k = A.jaw === "y1" ? 0 : 1; c.jaw_y = A.jy.slice(); c.jaw_y[k] = vv; }
    else { const k = A.jaw === "x1" ? 0 : 1; c.jaw_x = A.jx.slice(); c.jaw_x[k] = uu; }
    A.pos = A.jaw[0] === "y" ? vv : uu;
  } else {
    [c.mlc_a, c.mlc_b] = acomodarLamina(A.a, A.b, A.par, A.banco, uu); A.pos = uu;
  }
  desenharBev();
}
function bevSolta() {
  if ($("#bev-ferr") && $("#bev-ferr").value !== "editar") {
    const D = E.bevDes; const livre = D.livre; D.baixo = null;
    if (livre && D.pts.length >= 3) enviarDesenho();
    return;
  }
  if (!E.arrasto) return;
  const A = E.arrasto; E.arrasto = null;
  const c = E.bev.campo;
  if (A.pos === undefined) { desenharBev(); return; }
  tentar(async () => {
    const corpo = A.jaw ? { jaw: A.jaw, pos: A.pos } : { par: A.par, banco: A.banco, pos: A.pos };
    try { atualizar(await api("POST", `/api/${E.sid}/campo/${encodeURIComponent(c.nome)}/lamina`, corpo)); }
    catch (e) { c.mlc_a = A.a; c.mlc_b = A.b; if (A.jy) c.jaw_y = A.jy; if (A.jx) c.jaw_x = A.jx; desenharBev(); throw e; }
  });
}
function bevDuplo(ev) {
  if ($("#bev-ferr").value === "editar" || !E.bevDes.pts.length) return;
  ev.preventDefault();
  // o duplo clique acrescenta 2 vértices repetidos: tira-os
  const P = E.bevDes.pts;
  while (P.length > 1 && Math.hypot(P[P.length - 1][0] - P[P.length - 2][0], P[P.length - 1][1] - P[P.length - 2][1]) < 1) P.pop();
  if (P.length >= 3) enviarDesenho();
}
function enviarDesenho() {
  const pts = E.bevDes.pts.map(([u, v]) => [Math.round(u * 10) / 10, Math.round(v * 10) / 10]);
  E.bevDes = { pts: [], mouse: null, livre: false };
  const nome = E.bev.campo.nome;
  tentar(async () => {
    const r = await api("POST", `/api/${E.sid}/campo/${encodeURIComponent(nome)}/desenho`,
      { pontos: pts, modo: $("#bev-ferr").value, encontro: $("#bev-enc").value }, "Ajustando as lâminas ao desenho…");
    atualizar(r); (r.desenho_avisos || []).forEach(a => aviso(a, true));
  });
  desenharBev();
}

// ------------------------------------------------------------------ DVH
async function carregarDvh() { E.dvh = await api("GET", `/api/${E.sid}/dvh`); desenharDvh(); }
function desenharDvh() {
  const cv = $("#dvh"), { g, w, h } = prepararCanvas(cv);
  g.fillStyle = "#0c0f13"; g.fillRect(0, 0, w, h);
  const m = { l: 34, r: 8, t: 8, b: 24 }, W = w - m.l - m.r, H = h - m.t - m.b;
  g.strokeStyle = "#2b323c"; g.fillStyle = "#8b95a3"; g.font = "10px system-ui"; g.lineWidth = 1;
  const alt = E.fonte !== "plano" ? E.dvhAlt[E.fonte] : null;
  const dados = alt ? alt.dados : E.dvh;
  if (!dados) { g.fillText("Calcule a dose para ver o DVH", m.l + 8, m.t + 20); return; }
  const rx = alt ? alt.rx : E.resumo.plano.prescricao.dose_total_gy;
  const dmax = Math.max(rx * 1.15, ...Object.values(dados).map(d => d.dose[d.dose.length - 1] || 0),
    ...Object.values(dados).map(d => d.ref ? d.ref.dose[d.ref.dose.length - 1] || 0 : 0));
  for (let k = 0; k <= 5; k++) {
    const y = m.t + H - k / 5 * H; g.beginPath(); g.moveTo(m.l, y); g.lineTo(m.l + W, y); g.stroke(); g.fillText(`${k * 20}`, 6, y + 3);
  }
  const passo = dmax > 60 ? 20 : 10;
  for (let d = 0; d <= dmax; d += passo) { const x = m.l + d / dmax * W; g.beginPath(); g.moveTo(x, m.t); g.lineTo(x, m.t + H); g.stroke(); g.fillText(`${d}`, x - 6, h - 8); }
  const xr = m.l + rx / dmax * W; g.strokeStyle = "rgba(255,255,255,.35)"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(xr, m.t); g.lineTo(xr, m.t + H); g.stroke(); g.setLineDash([]);
  g.lineWidth = 1.6;
  for (const [n, d] of Object.entries(dados)) {
    if (E.ocultas.has(n)) continue;
    g.strokeStyle = `rgb(${d.cor})`; g.beginPath();
    d.dose.forEach((x, k) => { const px = m.l + x / dmax * W, py = m.t + H - d.volume[k] / 100 * H; k ? g.lineTo(px, py) : g.moveTo(px, py); });
    g.stroke();
    if (d.ref) {
      g.setLineDash([4, 3]); g.beginPath();
      d.ref.dose.forEach((x, k) => { const px = m.l + x / dmax * W, py = m.t + H - d.ref.volume[k] / 100 * H; k ? g.lineTo(px, py) : g.moveTo(px, py); });
      g.stroke(); g.setLineDash([]);
    }
  }
  g.fillStyle = "#8b95a3"; g.fillText("Gy", w - 18, h - 8); g.fillText("%", 4, 10);
  if (alt) { g.fillStyle = "#ffd84d"; g.textAlign = "right"; g.fillText(alt.titulo, w - 10, m.t + 12); g.textAlign = "left"; }
}

async function doseCruz() {
  const el = $("#dose-cruz");
  if (!E.cruz) { el.textContent = "Clique num corte"; return; }
  const pos = `(${E.cruz.map(v => v.toFixed(1)).join(", ")}) mm`;
  if (!E.resumo || !E.resumo.dose_atualizada) { el.textContent = pos; return; }
  try {
    const d = await api("GET", `/api/${E.sid}/dose?x=${E.cruz[0]}&y=${E.cruz[1]}&z=${E.cruz[2]}`);
    el.innerHTML = d.total_gy == null ? pos + " — fora da grade" :
      `${pos} · <b>${d.total_gy.toFixed(2)} Gy</b> (${d.pct.toFixed(1)}%)<br>` +
      Object.entries(d.campos).map(([n, v]) => `${n} ${v.toFixed(2)}`).join(" · ");
  } catch (e) { el.textContent = pos; }
}

async function listarPlanos() {
  try {
    const ps = await api("GET", "/api/planos");
    $("#planos-salvos").innerHTML = ps.map(p => `<option value="${p.arquivo}">${p.paciente_id} · ${p.rotulo} · ${p.estado} · ${p.atualizado.slice(0, 16).replace("T", " ")}</option>`).join("");
  } catch (e) { /* sem planos */ }
}

async function verAuditoria() {
  const a = await api("GET", "/api/auditoria");
  const us = await api("GET", "/api/usuarios");
  const v = a.verificacao;
  const html = `<p class="${v.integra ? "selo-ok" : "selo-falha"}">${v.integra ? "Trilha íntegra" : "TRILHA ADULTERADA no registro " + v.registro}
    (${v.verificados} registros conferidos pelo encadeamento de hashes)</p>
    <table><thead><tr><th>Quando</th><th>Usuário</th><th>Ação</th><th>Caso</th></tr></thead><tbody>
    ${a.registros.slice(0, 80).map(r => `<tr><td>${r.quando.replace("T", " ")}</td><td>${r.usuario || ""}</td><td>${r.acao}</td><td>${r.caso || ""}</td></tr>`).join("")}
    </tbody></table>
    <h3>Usuários</h3><table><tbody>${us.map(u => `<tr><td>${u.login}</td><td>${u.nome}</td><td>${E.info.papeis[u.papel]}</td><td>${u.ativo ? "ativo" : "inativo"}</td></tr>`).join("")}</tbody></table>
    <h3>Novo usuário (opcional)</h3><div class="linha2"><label>Login<input name="login"></label><label>Nome<input name="nome"></label></div>
    <div class="linha2"><label>Papel<select name="papel">${Object.entries(E.info.papeis).map(([k, n]) => `<option value="${k}">${n}</option>`).join("")}</select></label>
    <label>Senha inicial<input name="senha" type="password"></label></div>
    <label>Sua senha (para autorizar)<input name="senha_admin" type="password"></label>`;
  const r = await modal("Auditoria e usuários", html, { ok: "Criar usuário / fechar" });
  if (r && r.login) { await api("POST", "/api/usuarios", r); aviso(`Usuário ${r.login} criado`, true); }
}

iniciar().catch(e => aviso(e.message));


// ------------------------------------------------------------------ ferramentas de contorno (corte axial)
E.k = { traco: null, poli: [], mouse: null };
function contornoAtivo() { return E.sid && $("#k-ativo").checked && $("#k-est").value; }
function ferramentaK() { return document.querySelector("input[name=k-f]:checked").value; }
function mmAxial(ev) {
  const cv = $("#v-axial canvas"), r = cv.getBoundingClientRect();
  const T = transf("axial", r.width, r.height);
  return T.mm(ev.clientX - r.left, ev.clientY - r.top).map(v => Math.round(v * 10) / 10);
}
async function enviarContorno(corpo) {
  const r = await api("POST", `/api/${E.sid}/contorno/editar`, { nome: $("#k-est").value, idx: E.idx.axial, ...corpo });
  atualizar(r);
}
function iniciarContorno() {
  const cv = $("#v-axial canvas");
  $("#k-raio").oninput = () => { $("#k-raio-t").textContent = $("#k-raio").value; desenharCorte("axial"); };
  cv.addEventListener("mousedown", (ev) => {
    if (!contornoAtivo() || ev.button !== 0) return;
    const f = ferramentaK();
    if (f === "pincel" || f === "borracha") { E.k.traco = [mmAxial(ev)]; ev.preventDefault(); }
  });
  cv.addEventListener("mousemove", (ev) => {
    if (!contornoAtivo()) { if (E.k.mouse) { E.k.mouse = null; desenharCorte("axial"); } return; }
    E.k.mouse = mmAxial(ev);
    if (E.k.traco) E.k.traco.push(E.k.mouse);
    desenharCorte("axial");
  });
  cv.addEventListener("mouseleave", () => { E.k.mouse = null; desenharCorte("axial"); });
  window.addEventListener("mouseup", () => {
    if (!E.k.traco) return;
    const pts = E.k.traco; E.k.traco = null;
    tentar(() => enviarContorno({ ferramenta: "pincel", pontos: pts, raio_mm: +$("#k-raio").value, apagar: ferramentaK() === "borracha" }));
  });
  cv.addEventListener("click", (ev) => {
    if (!contornoAtivo() || !ferramentaK().startsWith("poligono")) return;
    E.k.poli.push(mmAxial(ev)); desenharCorte("axial");
  });
  cv.addEventListener("dblclick", (ev) => {
    if (!contornoAtivo() || !ferramentaK().startsWith("poligono")) return;
    ev.preventDefault();
    const pts = E.k.poli.slice(); E.k.poli = [];
    if (pts.length >= 3) tentar(() => enviarContorno({ ferramenta: "poligono", pontos: pts, apagar: ferramentaK() === "poligono-" }));
    desenharCorte("axial");
  });
  window.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && E.k.poli.length) { E.k.poli = []; desenharCorte("axial"); } });
  $("#b-k-nova").onclick = () => tentar(async () => {
    const nome = $("#k-nova").value.trim();
    const r = await api("POST", `/api/${E.sid}/contorno/nova`, { nome, tipo: nome.toUpperCase().startsWith("PTV") ? "PTV" : "ORGAN" });
    $("#k-nova").value = ""; atualizar(r); $("#k-est").value = nome; $("#k-ativo").checked = true;
  });
  $("#b-k-ant").onclick = () => tentar(() => enviarContorno({ ferramenta: "copiar", origem: E.idx.axial - 1 }));
  $("#b-k-seg").onclick = () => tentar(() => enviarContorno({ ferramenta: "copiar", origem: E.idx.axial + 1 }));
  $("#b-k-limpar").onclick = () => tentar(() => enviarContorno({ ferramenta: "limpar" }));
  $("#b-k-interp").onclick = () => tentar(async () => {
    const r = await api("POST", `/api/${E.sid}/contorno/interpolar`, { nome: $("#k-est").value }, "Interpolando…");
    atualizar(r); aviso(`${r.contorno.novos_cortes} corte(s) preenchido(s)`);
  });
  $("#b-k-desf").onclick = () => tentar(async () => atualizar(await api("POST", `/api/${E.sid}/contorno/desfazer`, {})));
  $("#b-k-corpo").onclick = () => tentar(async () => {
    const nome = E.resumo.estruturas.some(e => e.nome === "BODY") ? "BODY_AUTO" : "BODY";
    atualizar(await api("POST", `/api/${E.sid}/contorno/corpo`, { nome }, "Contornando o corpo…"));
  });
}
function desenharContornoPreview(g, T) {
  if (!contornoAtivo()) return;
  const f = ferramentaK();
  g.save(); g.strokeStyle = "#ffef5a"; g.lineWidth = 1.2;
  if ((f === "pincel" || f === "borracha") && E.k.mouse) {
    const [x, y] = T.px(...E.k.mouse), r = +$("#k-raio").value * T.s;
    g.setLineDash(f === "borracha" ? [3, 3] : []); g.beginPath(); g.arc(x, y, r, 0, 2 * Math.PI); g.stroke();
  }
  if (E.k.traco && E.k.traco.length > 1) {
    g.globalAlpha = 0.5; g.lineWidth = 2 * +$("#k-raio").value * T.s; g.lineCap = "round"; g.lineJoin = "round";
    g.strokeStyle = f === "borracha" ? "#ff5050" : "#ffef5a"; g.beginPath();
    E.k.traco.forEach((p, i) => { const [x, y] = T.px(...p); i ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke();
  }
  if (f.startsWith("poligono") && E.k.poli.length) {
    g.setLineDash([]); g.globalAlpha = 1; g.lineWidth = 1.2; g.beginPath();
    [...E.k.poli, ...(E.k.mouse ? [E.k.mouse] : [])].forEach((p, i) => { const [x, y] = T.px(...p); i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  }
  g.restore();
}
