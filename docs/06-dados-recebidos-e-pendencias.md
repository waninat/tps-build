# Dados recebidos e pendências

Atualizado em 12/11/2026.

## Elekta Precise 6 MV — Radioserra, Petrópolis (recebido em 12/11/2026)
Relatório RXT Dosimetria, novembro/2019 (29 páginas) e CSVs já extraídos (`dados/maquinas/radioserra_precise/6MV/`).
Conferidos por amostragem contra o PDF: PDD 10×10 a 10 cm = 67,6; 3×3 a 20 cm = 34,3; 40×40 a 20 cm = 46,8;
TMR 10×10 a 10 cm = 0,787; 40×40 a 20 cm = 0,644; OCR a 10 cm, 2 cm do eixo = 1,003 — todos iguais.

| Conteúdo | Situação |
|---|---|
| PDD 3×3 a 40×40 (16 campos), 0–30 cm, SSD 100 | extraído (512 pontos) |
| TMR, mesmos campos | extraído |
| FAC (fator de rendimento = **Scp medido**) 3×3 a 40×40 | extraído. Sp = NPSF publicado (Li 1999, MCC); Sc = FAC/NPSF |
| OCR × profundidade (1–25 cm, 0–25 cm do eixo; 40×40, tratado como diagonal) | extraído (650 pontos) |
| Calibração | **1,000 cGy/UM em dmax 1,5 cm, 10×10, SSD 100** (página "Fator de Calibração") |
| Qualidade do feixe | D20/D10 = 0,589; TPR20,10 = 0,685 |
| Cunha 60° | não usada (fora do escopo) |
| Perfis, campos < 3×3, MLC | não existem no documento |

### Modelo do Precise 6 MV — comissionamento v1 × v2 (12/11/2026)
O v1 (algoritmo herdado do SecDose) não serviu: até 5% de erro no PDD logo após dmax nos campos grandes e de −5,4% a
+2,7% em geometria isocêntrica. O v2 (`tps/comissionamento_v2.py`; 44 min de CPU) ajusta junto o PDD de 5 campos e
as saídas em dmax e no isocentro, com dois parâmetros novos: escala do espalhamento no fantoma (s = 0,78) e
contaminação eletrônica que cresce com o campo.

| Teste | v1 | v2 |
|---|---|---|
| PDD, máximo além de dmax (3×3 a 40×40) | 4,95% | **2,05%** (só o 40×40 a 2 cm passa de 2%; ≤ 1,6% nos demais) |
| Scp em dmax (3×3 a 40×40) | 0,74% | **0,50%** |
| OCR 40×40, 1–25 cm de profundidade | 2,5% | **1,4%** |
| Isocêntrico × TMR/Scp (5/10/20 cm, 5×5 a 20×20) | −5,4% a +2,7% | **−0,45% a +0,96%** |
| Conferência por tabela num AP/PA 2D e numa caixa 3D | — | −0,8% a −0,2% |

Arquivos: `modelo/modelo_6MV.yaml` (v2), `modelo_6MV_v1.yaml`, `validacao*.json`, `validacao_isocentrica*.csv`,
`pdd.png`. Pontos de atenção: o endurecimento do espectro ficou alto (k = 23,5) e a amplitude da contaminação
encostou no limite do ajuste — sinais de que os parâmetros compensam limitações do kernel (sem endurecimento com a
profundidade). Funciona para os dados medidos, mas **perfis medidos** são necessários para validar a penumbra e a dose
fora do campo, e o kernel com endurecimento em profundidade continua no backlog do motor.

### Synergy 6 MV (OncoMed) — também recomissionado com o v2
| Teste | v1 | v2 |
|---|---|---|
| PDD, máximo além de dmax | 2,9% | 2,6% (40×40 perto de dmax; ≤ 1,6% até 20×20) |
| Scp em dmax | 0,7% | 0,56% |
| Isocêntrico × TMR/Scp | −4,5% a +2,5% | **−0,58% a +1,12%** |
| Scp SSD 90 / 10 cm × medida Monaco 2018 (época do MLCi2) | −2,9% a +1,6% | −1,5% a +2,4% (≥ 4×4: dentro de 1,5%) |

O modelo v2 agora concorda com as tabelas da RXT; a medida do Monaco de 2018 discorda das tabelas em até ~2% nos
campos de 2–3 cm. Campos pequenos precisam de medida própria com detector adequado (TRS-483).


## Recebidos
### 1. Planilha de dosimetria 6 MV — Elekta Synergy S/N 154828 (OncoMed Niterói, RXT, fevereiro de 2019)
Extraída para `dados/maquinas/oncomed_synergy_154828/6MV/` pelo extrator do SecDose e conferida por amostragem
contra o PDF (PDD 10×10 a 10 cm = 67,7; PDD 40×40 a 30 cm = 29,9; Scp 20×20 = 1,051; TMR 3×3 a 30 cm = 0,312;
OAR 20 cm a 25 cm = 0,904: todos iguais ao PDF).

| Conteúdo | Situação |
|---|---|
| PDD campo aberto, 26 campos (3×3 a 40×40), 0–30 cm, SSD 100 | extraído (1.586 pontos) |
| TMR campo aberto, 26 campos, 1,5–30 cm | extraído (1.508 pontos) |
| Sc, Sp, Scp (3×3 a 40×40) | extraído. **Atenção: o Sp vem do BJR 25, não de medida; o Scp é Sc × Sp calculado** |
| OAR diagonal 40×40, profundidades 1,5/5/10/20/25 cm | extraído (105 pontos) |
| Cunha motorizada 60°: fator, OAR, PDD e TMR | não usado: cunha fora do escopo |
| Perfis inplane/crossplane | **não existem** no documento |
| Campos < 3×3 cm² | **não existem** |
| Dados de MLC (transmissão, gap dosimétrico, ponta) | **não existem** |
| Calibração absoluta | não consta no documento; **informada em 8/11/2026: 1,000 cGy/UM** (condição de referência a confirmar; o modelo assume dmax, 10×10, SSD 100) |

### 2. Relatório de validação do modelo Monaco — "SynergyP06x", 6 MV (Elekta/IMPAC, 27/2/2018, Monaco 5.11)
- **O relatório é de um Synergy com MLCi2 (40 pares de 10 mm). O Synergy S/N 154828 tem hoje Agility (confirmado em
  8/11/2026)**, então este relatório é anterior à troca do MLC ou de outra máquina; o modelo atual do Monaco para o
  Agility tem outro relatório e outros dados. Nota original: precisamos saber se o Synergy da
  OncoMed foi trocado para Agility depois de 2018 ou se este relatório é de outra máquina.
- Útil como referência: Scp **medidos** a SSD 90 / 10 cm de 2×2 a 40×40 (2×2 = 0,803; 40×40 = 1,168), parâmetros do
  modelo de fonte virtual (σ primário 0,09; fonte de espalhamento; contaminação eletrônica) e de colimação do MLCi2
  (transmissão 0,73%, groove 0,7 mm, LeafGap 5 mm, jaw X 1,01%).
- PDD e perfis (AB e GT a 1,5/5/10/20 cm, 2×2 a 40×40) aparecem só como gráficos. **Os arquivos de varredura
  originais enviados à Elekta para a modelagem são exatamente os dados que faltam ao TPS.**

### 3. Roche et al., "Agility MLC transmission optimization in the Monaco TPS", JACMP 2018;19(5):473–482
Define os campos de teste para transmissão, tongue-and-groove e ponta de lâmina. Vira o protocolo de medida do
requisito SRS-COM-004.

### 4. SecDose 0.8.0
Núcleo copiado para o pacote `tps` (ADR 0001); 36 testes herdados verdes. Os modelos comissionados do SecDose ficam
na pasta de dados da clínica e não vieram no zip.

## Modelo de feixe comissionado a partir da planilha (protótipo)
Gerado por `tools/comissionar.py` com o algoritmo de comissionamento do SecDose (8 min em CPU); arquivos em
`dados/maquinas/oncomed_synergy_154828/6MV/modelo/`. Usa a calibração **informada de 1,000 cGy/UM, assumida em dmax (10×10,
SSD 100)**; a condição de referência ainda precisa ser confirmada. Serve para desenvolver e testar; **não** serve para uso clínico.

Resultado contra os dados usados no ajuste (SSD 100):
- PDD: RMS 0,5–0,8% por campo; máximo 2,9% (3×3, logo após dmax). Dentro de ±1% depois de 3 cm.
- Scp: dentro de 0,7% de 3×3 a 40×40.
- OAR diagonal 40×40: dentro de 1,7%.

**Achado importante — geometria isocêntrica (`tools/validar_isocentrico.py`).** Fora das condições do ajuste, o
modelo erra mais:

| Teste | 5×5 | 10×10 | 20×20 |
|---|---|---|---|
| Dose/UM no isocentro a 10 cm (SSD 90) × cálculo por TMR/Scp | −2,8% | −0,3% | +1,4% |
| Idem a 20 cm (SSD 80) | −4,5% | −1,4% | +2,5% |
| Scp a SSD 90 / 10 cm × medida do relatório Monaco | −1,9% | 0 | +1,3% (2×2: −2,9%) |

As tabelas da RXT são coerentes entre si (TMR recalculado do PDD + Sp concorda em 0,7%) e concordam com o Scp
medido do relatório Monaco em ~0,6%. Portanto, **o erro está no modelo**, não nos dados: o comissionamento ajusta o
Scp só em dmax a SSD 100 e o PDD a SSD 100, e isso não restringe o espalhamento no fantoma em profundidade. O
espectro ficou muito endurecido (k = 12), sinal de que ele compensa outra coisa.

Consequências:
1. Para o TPS: novo requisito SRS-COM-006 (ajustar e validar também em geometria isocêntrica).
2. **Para o SecDose: ele usa o mesmo algoritmo de comissionamento (v1).** O v2 deste repositório corrige o problema e pode ser levado para o SecDose. Vale rodar este teste no modelo
   comissionado da clínica. Campos pequenos em profundidade (SBRT, boosts) e campos grandes são os mais afetados.

## Pendências (em ordem de prioridade)
Resolvidas em 8/11/2026: MLC do Synergy = **Agility**; calibração = **1,000 cGy/UM**; **sem cunha** no escopo.

1. **Arquivos de varredura do comissionamento do Monaco para o Agility** (PDD e perfis em água, todos os campos e
   profundidades), no formato do software do tanque (.mcc, .asc/.opax, .w2CAD, .csv), mais o relatório de validação
   do modelo Monaco do Agility (equivalente ao do MLCi2 de 2018).
2. Condição de referência da calibração de 1,000 cGy/UM (dmax a SSD 100 ou 10 cm, SSD/SAD).
3. Versão do Mosaiq: **2.83** (informada em 13/11/2026). Falta o DICOM Conformance Statement dessa versão.
4. **RTPLANs 3D do Monaco já aceitos pelo Mosaiq** (2–3 planos), exportados como vão para o Mosaiq.
5. Medidas de MLC pelo protocolo de Roche et al. (transmissão, tongue-and-groove, ponta de lâmina).
6. Campos pequenos (1×1, 2×2) com detector adequado e fatores de correção do TRS-483.
7. 10–20 casos 3D anonimizados (CT + RTSTRUCT + RTPLAN + RTDOSE do Monaco).
8. Curva HU→DE atual de cada tomógrafo.
9. Tolerance tables e nomes exatos das máquinas no Mosaiq.

## 22/11/2026 — plano 3D do Monaco (pelve, Precise) e fantomas de placa

**Plano clínico (RTPLAN/RTSTRUCT/RTDOSE do Monaco 6.1.4, Precise, 4 campos 3D com field-in-field).** Convenções
copiadas para a exportação (SRS-EXP-030..036): `TreatmentMachineName = "Precise"`; Latin-1 (`ISO_IR 100`);
ASYMX/ASYMY/MLCX com `SourceToBeamLimitingDeviceDistance` 426/509/377 mm; field-in-field como **um feixe STATIC com
pares de pontos de controle** (step-and-shoot); tabela de tolerância "1 Fótons"; `InstitutionalDepartmentName`
"0~RadioSerra"; Dose Reference TARGET (SITE) + ponto (COORDINATES) com `BeamDose` por feixe; lâminas fechadas
estacionadas em ±2,5 mm. A exportação agora inclui o RTSTRUCT do TPS (estruturas editadas/novas/derivadas).

Comparação TPS × Monaco CC no plano importado (UM do Monaco, mesma geometria):
- Fora do contorno externo o Monaco considera ar; o TPS considerava o CT inteiro (mesa do tomógrafo) → o campo PA
  saía 8,5% baixo. Corrigido: fora do BODY = ar (SRS-ENG-050).
- Depois da correção: gama 3%/3 mm = 95,6%; PTV −1,5%; campos laterais −1 a −2,6% no isocentro (profundidade ~21 cm,
  passando pelas cabeças de fêmur) — **curva HU→DE usada é a do Siemens da Oncomed; o CT é GE** (pendente a curva).
- Dose baixa fora dos campos (corpo −7%, genitália −11%): mesma tendência já vista nos campos grandes em água
  (dose fora do campo baixa) — alvo do comissionamento v3.
- Conferência independente por tabelas nos campos com segmentos de FiF: +8,8% (método de tabela grosseiro para
  segmentos que bloqueiam o ponto; informativo, não bloqueia a aprovação).

**Fantomas de placa (Monaco CC, sobrescrita de densidade eletrônica 0,25 pulmão / 1,6 osso / 1,0 BODY).**
Fator de correção (heterogêneo/água) TPS ÷ Monaco:
- Osso: antes da placa ~0%, depois −1% (TPS atenua um pouco mais). Dentro do osso o Monaco cai ~4% em degrau — dose
  no meio (osso), não na água; o TPS dá dose na água. CONFIRMAR qual grandeza o Monaco reporta.
- Pulmão: depois da placa ±1%; **dentro do pulmão o TPS dá +6 a +7% (3×3, 5×5 e 10×10)**: o Monaco mostra a queda
  por desequilíbrio eletrônico na entrada do pulmão e o TPS quase não mostra. Como o próprio CC do Monaco é uma
  aproximação, **pedido: os mesmos 6 planos em Monte Carlo** (1%, grade 2 mm) para decidir quem está certo antes de
  mexer no núcleo (SRS-ENG-012).
