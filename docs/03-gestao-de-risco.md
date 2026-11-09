# Gestão de risco — análise preliminar (ISO 14971:2019)

Versão 0.1 (rascunho), novembro de 2026. Escopo: Fase 1 (3D conformacional).

Severidade (S): 5 catastrófica (morte/lesão permanente), 4 crítica, 3 séria, 2 menor, 1 desprezível.
Probabilidade (P) estimada antes e depois do controle: 5 frequente … 1 improvável. Aceitável: S×P ≤ 6 e nenhum
item S = 5 com P > 1 sem controle duplo.

| ID | Perigo / situação | Causa possível | S | P₀ | Controle (requisito) | P₁ |
|---|---|---|---|---|---|---|
| R-01 | Dose calculada errada em todo o volume | Modelo de feixe mal comissionado; calibração absoluta errada; modelo ajustado só a SSD 100 (erro de −2,8% a +2,5% em geometria isocêntrica já observado, docs/06) | 5 | 3 | SRS-COM-001/002/006; validação TRS-430; checagem independente de UM (SRS-PLN-027); CQ por medida na Fase 1 | 1 |
| R-02 | UM errada em um campo | Erro na normalização, arredondamento, peso | 5 | 2 | SRS-PLN-022/023/024; recálculo do RTPLAN relido (SRS-EXP-011); SRS-PLN-027 | 1 |
| R-03 | Dose errada em heterogeneidade (pulmão, osso) | Limitações do CCC; curva HU→DE errada | 4 | 3 | Validação em fantoma heterogêneo; curva por tomógrafo versionada; Monte Carlo (Fase 4) | 2 |
| R-04 | Campo entregue em posição/orientação errada | Sentido de colimador/mesa; HFS × outras posições; convenção A/B das lâminas | 5 | 3 | SRS-IO-003 (recusar ≠ HFS); SRS-PLN-013/015; teste de campos assimétricos medidos com filme | 1 |
| R-05 | Lâminas fecham sobre o alvo | Erro de projeção BEV, margem não aplicada | 4 | 2 | SRS-PLN-010/013; revisão visual do BEV obrigatória antes da aprovação (Fase 2) | 1 |
| R-06 | Plano entregue diferente do aprovado | Alteração após aprovação; exportação de rascunho | 5 | 2 | SRS-PLN-003/004; SRS-EXP-001 | 1 |
| R-07 | Plano associado ao paciente errado | CT/RTSTRUCT de pacientes diferentes; UIDs trocados | 5 | 2 | SRS-IO-004; paciente/estudo herdados do CT; conferência no Mosaiq | 1 |
| R-08 | Plano calculado para uma máquina e tratado em outra | TreatmentMachineName diferente; modelo de outra máquina | 5 | 2 | Nome DICOM do cadastro igual ao do Mosaiq; hash do modelo no plano (SRS-COM-003); Mosaiq recusa nome desconhecido | 1 |
| R-09 | Mosaiq interpreta o RTPLAN diferente do TPS | Atributo ausente/diferente do que o Mosaiq espera | 5 | 3 | SRS-EXP-010/012/020; protocolo de integração com o Mosaiq de teste antes de qualquer paciente | 1 |
| R-10 | Prescrição digitada errada | Erro humano (Gy × cGy, frações) | 4 | 3 | SRS-PLN-002 (faixas); dupla aprovação (SRS-PLN-006); exibição clara de dose/fração e total | 2 |
| R-11 | Estrutura sumida ou deformada na rasterização | CT mais fino que a grade (bug corrigido no SecDose 0.7.3) | 3 | 2 | SRS-IO-002 com teste; DVH mostra volume (cc) de cada estrutura | 1 |
| R-12 | Dose fora do campo subestimada/superestimada (OAR) | Fonte extrafocal sem perfis; transmissão de MLC | 3 | 3 | Perfis medidos no comissionamento (pendente); SRS-COM-004 | 2 |
| R-13 | Uso de modelo de demonstração em paciente | Configuração errada | 5 | 2 | SRS-COM-002 (recusa); marca no plano | 1 |
| R-14 | Perda/corrupção de planos aprovados | Falha de disco, banco | 3 | 2 | Banco com backup (Fase 2), hash do plano | 1 |
| R-15 | Acesso indevido / alteração maliciosa | Rede, senhas | 4 | 2 | Fase 2: usuários, papéis, auditoria, IEC 81001-5-1 | 1 |

Riscos residuais e análise risco-benefício: a fazer no fim da Fase 1, com os resultados da validação.
