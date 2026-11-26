# 12 — Arco conformacional dinâmico (DCA) e CQ por cálculo

Situação em 26/11/2026: implementado; não liberado para uso clínico.

## DCA (tps/planejamento/dca.py) — SRS-PLN-110
* Arco 3D, sem modulação: em cada ponto de controle (a cada 2–10°, padrão 5°) as lâminas se conformam ao alvo +
  margem no BEV daquele ângulo; UM por grau constante (pesos acumulados lineares no ângulo).
* Mandíbulas fixas no envelope do arco. Pares fechados fecham no meio da própria abertura do ponto aberto mais
  próximo (sem atravessar o campo). MLCi: gap mínimo e sem interdigitação, pelo mesmo reparo do VMAT.
* Colimador "auto": o ângulo (0–90° e 285–345°) com menos abertura além do alvo ao longo do arco; os arcos
  seguintes usam outro ângulo quase tão bom, para espalhar as "orelhas" das lâminas.
* Bloqueio de órgão (opcional): a abertura recua sobre o órgão no BEV, só pelo lado em que ele está, sem abrir
  buraco, até "bloqueio máx." da largura do alvo naquele par (padrão 25%). Órgão no meio do alvo: não bloqueia.
* Pesos: "otimizar pesos" ajusta as UM de cada arco (nenhum abaixo de 30% do maior).
* Dose, exportação (um feixe DYNAMIC por arco), tempo de entrega, colisão e QA de paciente: os mesmos do VMAT.
  Conferência de UM: por arco e composta. Dose final padrão: collapsed cone (Monte Carlo opcional).
* Exportação confere a faixa de UM por grau da máquina (0,1–20).

## CQ por cálculo (tps/planejamento/complexidade.py) — SRS-PLN-111
Mede a complexidade do plano (3D, field-in-field, DCA) e compara com os critérios de dados/criterios_cq_calculo.yaml
(a clínica sobrescreve em <dados>/criterios_cq_calculo.yaml e marca `confirmado_pela_clinica: true`):
técnica sem modulação, menor abertura e quadrado equivalente, fração de fendas, MCS (McNiven 2010), UM/Gy,
UM por grau constante, alvo bloqueado no BEV e a maior diferença da conferência independente de UM.
O deslocamento das lâminas por grau é informativo (acima do limite o gantry só desacelera).

Resultado no checklist ("Elegível para CQ só por cálculo" ou "CQ por medida"), na janela "Complexidade e CQ por
cálculo" (aba Aprovação) e no relatório PDF. A decisão de dispensar a medida é do protocolo da clínica; enquanto os
critérios não forem confirmados, o item fica como aviso.
