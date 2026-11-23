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
};
try { E.token = sessionStorage.getItem("tps_token"); } catch (e) { /* sem storage */ }
const ISO_CORES = { 30: "#3355ff", 50: "#00b7ff", 70: "#00e08a", 80: "#9be000", 90: "#ffe100", 95: "#ffae00",
                    100: "#ff7a00", 105: "#ff2d55", 107: "#ff00c8", 110: "#ffffff" };

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
  window.addEventListener("resize", () => desenharTudo());
}

function ligar() {
  $$(".aba").forEach(b => b.onclick = () => {
    $$(".aba").forEach(x => x.classList.toggle("ativa", x === b));
    $$(".painel-aba").forEach(p => p.classList.toggle("oculto", p.id !== "aba-" + b.dataset.aba));
  });
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
    let corpo;
    if (fora.length) {
      const v = await modal("Aprovar com conferência de UM fora da tolerância",
        `<p class="pequeno-txt">Conferência independente fora de ±3% em: <b>${fora.map(k => `${k.campo} (${k.dif_pct >= 0 ? "+" : ""}${f1(k.dif_pct, 1)}%)`).join(", ")}</b>.
         Só aprove depois de investigar (ex.: heterogeneidade, campo muito irregular, ponto em penumbra). A justificativa fica no plano, no relatório e na auditoria.</p>
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
  window.addEventListener("mouseup", bevSolta);
  document.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
    if (ev.key === "ArrowUp" || ev.key === "PageUp") mudarCorte("axial", -1);
    if (ev.key === "ArrowDown" || ev.key === "PageDown") mudarCorte("axial", 1);
  });
}

function mostrarPonto() { $("#p-ponto-linha").classList.toggle("oculto", $("#p-metodo").value !== "ponto"); }

// ------------------------------------------------------------------ caso e plano
async function abrirCaso(r) {
  E.sid = r.id; E.imgs = {}; E.sobre = {}; E.campoSel = null; E.bev = null; E.dvh = null; E.ocultas = new Set();
  const ct = r.ct;
  E.idx = { axial: Math.floor(ct.z[2] / 2), coronal: Math.floor(ct.y[2] / 2), sagital: Math.floor(ct.x[2] / 2) };
  if (r.tipo === "fantoma") { $("#p-metodo").value = "ponto"; mostrarPonto(); }
  if (!r.maquina) atualizar(await api("POST", `/api/${E.sid}/maquina`, { id: $("#maquina").value }));
  else atualizar(r);
  if (r.isocentro) irPara(r.isocentro);
  carregarCortes();
}

async function criarPlano() {
  const metodo = $("#p-metodo").value;
  atualizar(await api("POST", `/api/${E.sid}/plano`, {
    rotulo: $("#p-rotulo").value, dose_total: +$("#p-dose").value, fracoes: +$("#p-fx").value,
    alvo: $("#p-alvo").value, metodo, ponto: metodo === "ponto" ? [+$("#p-px").value, +$("#p-py").value, +$("#p-pz").value] : null,
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
  for (const sel of ["#ne-a", "#ne-b"]) { const atual = $(sel).value; $(sel).innerHTML = alvos.map(n => `<option>${n}</option>`).join(""); if (alvos.includes(atual)) $(sel).value = atual; }
  $("#b-nova-est").disabled = !r.estruturas.length;
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
  $("#b-plano").textContent = temPlano ? "Recriar plano (apaga os campos)" : "Criar plano";
  $("#form-campo").disabled = !temPlano || aprovado;
  $("#b-iso").disabled = !temPlano || aprovado;
  $("#b-calc").disabled = !temPlano || aprovado || !p.campos.length;
  $("#b-aprovar").disabled = !temPlano || aprovado || !r.dose_atualizada;
  $("#b-desaprovar").disabled = !aprovado;
  $("#b-exportar").disabled = !aprovado;
  $("#b-salvar").disabled = !temPlano; $("#b-relatorio").disabled = !temPlano;
  $("#b-checkfilm").disabled = !(temPlano && E.resumo.plano.campos.length);
  const pd = r.planos_dicom || [];
  $("#planos-dicom").classList.toggle("oculto", !pd.length);
  $("#lista-planos-dicom").innerHTML = pd.map(q => `<li><b>${q.rotulo}</b> · ${q.campos} campo(s) · ${q.maquina} · ${q.fabricante}
    ${q.dose ? "· com dose" : ""} <button class="pequeno" data-imp="${q.arquivo}">importar</button></li>`).join("");
  $$("[data-imp]").forEach(b => b.onclick = () => tentar(async () => {
    if (temPlano && !confirm("Importar substitui o plano atual desta sessão (salve-o antes, se precisar). Continuar?")) return;
    const r2 = await api("POST", `/api/${E.sid}/importar_plano`, { arquivo: b.dataset.imp }, "Importando o plano…");
    atualizar(r2); $("#p-metodo").value = "um_fixas"; mostrarPonto();
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
  $("#log").innerHTML = (r.log || []).slice().reverse().map(a => `<li>${a}</li>`).join("");
  // BEV
  const nomes = p ? p.campos.map(c => c.nome) : [];
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
  const corpo = { otimizar_pesos: $("#o-pesos").checked, field_in_field: $("#o-fif").checked, compensador: comp,
                  metodo: $("#p-metodo").value };
  if (corpo.metodo === "ponto") corpo.ponto = [+$("#p-px").value, +$("#p-py").value, +$("#p-pz").value];
  const r = await api("POST", `/api/${E.sid}/calcular`, corpo, "Calculando a dose (collapsed cone)…");
  atualizar(r);
  const i = r.resultado.info || {};
  if (i.compensador) aviso(`Compensador: HI ${i.compensador.hi_antes.toFixed(3)} → ${i.compensador.hi_depois.toFixed(3)}`, true);
  if (i.field_in_field) aviso(`Field-in-field: HI ${i.field_in_field.hi_antes.toFixed(3)} → ${i.field_in_field.hi_depois.toFixed(3)}`, true);
}

async function exportar() {
  const corpo = { modo_segmentos: $("#e-modo").value, pasta: $("#e-pasta").value.trim() || null, rtstruct: $("#e-rs").checked };
  if ($("#m-enviar").checked) corpo.enviar = { host: $("#m-host").value, porta: +$("#m-porta").value, ae_destino: $("#m-ae").value };
  corpo.senha = await assinatura("exportar o plano");
  if ($("#e-imagens").checked) corpo.incluir_imagens = true;
  const r = await api("POST", `/api/${E.sid}/exportar`, corpo, "Exportando…");
  $("#exportados").innerHTML = r.arquivos.filter(a => !/imagens_/.test(a)).map(a => `<div><a href="/api/${E.sid}/arquivo?t=${encodeURIComponent(E.token)}&caminho=${encodeURIComponent(a)}">${a.split(/[\\/]/).pop()}</a></div>`).join("") +
    (r.envio ? `<div class="ok">Enviado: ${r.envio.enviados.length} arquivo(s)</div>` : "") + `<div class="pequeno-txt">${r.pasta}</div>`;
  aviso("RTPLAN exportado e conferido", true);
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
    return `<tr title="${q.tg263} · ${q.tipo} · ${q.fonte}${q.obs ? " · " + q.obs : ""}"><td>${q.estrutura}</td><td>${q.metrica}</td>` +
      `<td>${q.op.replace(">=", "≥").replace("<=", "≤")} ${q.limite} ${q.unidade === "Gy" ? "" : q.unidade.startsWith("%") ? "%" : q.unidade}</td><td class="${cls}">${f1(q.valor, 1)}</td></tr>`;
  }).join("");
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
  try { E.sobre[chave] = await api("GET", `/api/${E.sid}/sobreposicao/${vista}/${i}`); }
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
    const min = $("#wash-min").value, chave = `${vista}:${i}:${min}:${E.doseVer || 0}`;
    E.wash = E.wash || {};
    let wi = E.wash[chave];
    if (!wi) {
      wi = new Image(); wi.onload = () => desenharCorte(vista);
      wi.src = `/api/${E.sid}/dose_corte/${vista}/${i}.png?minimo=${min}&v=${E.doseVer || 0}&t=${encodeURIComponent(E.token)}`;
      E.wash[chave] = wi;
    }
    if (wi.complete && wi.naturalWidth) {
      const [xa, ya] = T.px(sobW.wash.h[0], sobW.wash.v[0]), [xb, yb] = T.px(sobW.wash.h[1], sobW.wash.v[1]);
      g.imageSmoothingEnabled = true;
      g.drawImage(wi, Math.min(xa, xb), Math.min(ya, yb), Math.abs(xb - xa), Math.abs(yb - ya));
    }
    if (vista === "axial") {          // barra de cores
      const x0 = 10, y0 = h - 22, larg = Math.min(220, w - 20), lo = +min, hi = Math.max(sobW.wash.dmax_pct, lo + 1);
      const grad = g.createLinearGradient(x0, 0, x0 + larg, 0);
      ["#00008f", "#0000ff", "#00ffff", "#7fff7f", "#ffff00", "#ff0000", "#800000"].forEach((c, k, a) => grad.addColorStop(k / (a.length - 1), c));
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
      for (const iso of sob.isodoses) { g.strokeStyle = ISO_CORES[iso.nivel] || "#fff"; iso.linhas.forEach(l => linha(l, false)); }
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
  // legenda de isodoses
  if (vista === "axial" && sob && sob.isodoses && sob.isodoses.length && $("#v-axial .t-iso").checked) {
    g.font = "11px system-ui"; let yy = 16;
    for (const n of Object.keys(ISO_CORES).map(Number).reverse()) { g.fillStyle = ISO_CORES[n]; g.fillText(`${n}%`, w - 40, yy); yy += 13; }
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
  $("#bev-seg-t").textContent = E.bevSeg ? `${E.bevSeg}/${B.segmentos.length}` : "envelope";
}

function bevBaixo(ev) {
  if (!E.bev || E.bevSeg > 0 || (E.resumo.plano && E.resumo.plano.estado !== "RASCUNHO")) return;
  const cv = $("#v-bev canvas"), r = cv.getBoundingClientRect(), T = bevT(r.width, r.height);
  const [u, v] = T.mm(ev.clientX - r.left, ev.clientY - r.top), lim = E.bev.limites, c = E.bev.campo;
  let par = -1; for (let i = 0; i < lim.length - 1; i++) if (v >= lim[i] && v < lim[i + 1]) par = i;
  if (par < 0) return;
  const tol = 6 / T.s, da = Math.abs(u - c.mlc_a[par]), db = Math.abs(u - c.mlc_b[par]);
  if (Math.min(da, db) > tol) return;
  E.arrasto = { par, banco: da < db ? "a" : "b", a: c.mlc_a.slice(), b: c.mlc_b.slice() };
  ev.preventDefault();
}
function bevMove(ev) {
  if (!E.arrasto) return;
  const cv = $("#v-bev canvas"), r = cv.getBoundingClientRect(), T = bevT(r.width, r.height);
  const [u] = T.mm(ev.clientX - r.left, ev.clientY - r.top), A = E.arrasto, gap = E.bev.gap;
  const uu = Math.round(u * 2) / 2;
  if (A.banco === "a") E.bev.campo.mlc_a[A.par] = Math.min(uu, E.bev.campo.mlc_b[A.par] - gap);
  else E.bev.campo.mlc_b[A.par] = Math.max(uu, E.bev.campo.mlc_a[A.par] + gap);
  desenharBev();
}
function bevSolta() {
  if (!E.arrasto) return;
  const A = E.arrasto; E.arrasto = null;
  const c = E.bev.campo;
  tentar(async () => {
    try { atualizar(await api("POST", `/api/${E.sid}/campo/${encodeURIComponent(c.nome)}/laminas`, { mlc_a: c.mlc_a, mlc_b: c.mlc_b })); }
    catch (e) { c.mlc_a = A.a; c.mlc_b = A.b; desenharBev(); throw e; }
  });
}

// ------------------------------------------------------------------ DVH
async function carregarDvh() { E.dvh = await api("GET", `/api/${E.sid}/dvh`); desenharDvh(); }
function desenharDvh() {
  const cv = $("#dvh"), { g, w, h } = prepararCanvas(cv);
  g.fillStyle = "#0c0f13"; g.fillRect(0, 0, w, h);
  const m = { l: 34, r: 8, t: 8, b: 24 }, W = w - m.l - m.r, H = h - m.t - m.b;
  g.strokeStyle = "#2b323c"; g.fillStyle = "#8b95a3"; g.font = "10px system-ui"; g.lineWidth = 1;
  if (!E.dvh) { g.fillText("Calcule a dose para ver o DVH", m.l + 8, m.t + 20); return; }
  const rx = E.resumo.plano.prescricao.dose_total_gy;
  const dmax = Math.max(rx * 1.15, ...Object.values(E.dvh).map(d => d.dose[d.dose.length - 1] || 0));
  for (let k = 0; k <= 5; k++) {
    const y = m.t + H - k / 5 * H; g.beginPath(); g.moveTo(m.l, y); g.lineTo(m.l + W, y); g.stroke(); g.fillText(`${k * 20}`, 6, y + 3);
  }
  const passo = dmax > 60 ? 20 : 10;
  for (let d = 0; d <= dmax; d += passo) { const x = m.l + d / dmax * W; g.beginPath(); g.moveTo(x, m.t); g.lineTo(x, m.t + H); g.stroke(); g.fillText(`${d}`, x - 6, h - 8); }
  const xr = m.l + rx / dmax * W; g.strokeStyle = "rgba(255,255,255,.35)"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(xr, m.t); g.lineTo(xr, m.t + H); g.stroke(); g.setLineDash([]);
  g.lineWidth = 1.6;
  for (const [n, d] of Object.entries(E.dvh)) {
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
