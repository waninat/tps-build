# Dados recebidos e pendências

Atualizado em 8/11/2026.

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
| Cunha motorizada 60°: fator, OAR, PDD e TMR | **não extraído** (fica para a Fase 1b; o extrator lê só as tabelas de campo aberto) |
| Perfis inplane/crossplane | **não existem** no documento |
| Campos < 3×3 cm² | **não existem** |
| Dados de MLC (transmissão, gap dosimétrico, ponta) | **não existem** |
| Calibração absoluta (cGy/UM em condição de referência) | **não consta** |

### 2. Relatório de validação do modelo Monaco — "SynergyP06x", 6 MV (Elekta/IMPAC, 27/2/2018, Monaco 5.11)
- **O relatório é de um Synergy com MLCi2 (40 pares de 10 mm), não Agility.** Precisamos saber se o Synergy da
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
`dados/maquinas/oncomed_synergy_154828/6MV/modelo/`. Usa calibração **provisória de 1,000 cGy/UM em dmax (10×10,
SSD 100)** até recebermos a calibração real. Serve para desenvolver e testar; **não** serve para uso clínico.

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
2. **Para o SecDose: o mesmo algoritmo de comissionamento está em produção.** Vale rodar este teste no modelo
   comissionado da clínica. Campos pequenos em profundidade (SBRT, boosts) e campos grandes são os mais afetados.

## Pendências (em ordem de prioridade)
1. **Qual MLC o Synergy S/N 154828 tem hoje** (Agility ou MLCi2) e a qual máquina pertence o relatório do Monaco.
2. **Arquivos de varredura originais** do comissionamento do Monaco (PDD e perfis, todos os campos e profundidades),
   das duas máquinas e de todas as energias, no formato do software de varredura (.mcc, .asc, .w2CAD, .csv).
3. **Calibração absoluta** de cada máquina e energia (cGy/UM e condição de referência, TRS-398).
4. **Versão do Mosaiq** e o DICOM Conformance Statement dessa versão.
5. **RTPLANs do Monaco já aceitos pelo Mosaiq**: 2–3 planos 3D (um com cunha motorizada), exportados como vão para o
   Mosaiq. São o gabarito da exportação.
6. Medidas de MLC pelo protocolo de Roche et al. (transmissão, tongue-and-groove, ponta de lâmina).
7. Campos pequenos (1×1, 2×2) com detector adequado e fatores de correção do TRS-483 (dá para aproveitar a tese).
8. 10–20 casos 3D anonimizados (CT + RTSTRUCT + RTPLAN + RTDOSE do Monaco).
9. Curva HU→DE atual de cada tomógrafo (a do SecDose é `siemens_spirit_oncomed`).
10. Tolerance tables cadastradas no Mosaiq e nomes exatos das máquinas no Mosaiq.
