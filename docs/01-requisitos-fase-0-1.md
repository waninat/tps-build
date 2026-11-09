# Especificação de requisitos de software — Fases 0 e 1

Documento controlado. Versão 0.1 (rascunho), novembro de 2026.

Escopo da Fase 1: **planejamento 3D conformacional de ponta a ponta**, fótons 6 MV, aceleradores Elekta
(Agility e MLCi/MLCi2), paciente em decúbito dorsal cabeça primeiro (HFS), exportação ao Mosaiq.

Situação de cada requisito: **I** = implementado com teste automático · **H** = herdado do SecDose (teste herdado) ·
**P** = pendente · **M** = verificação manual (medida, Mosaiq, máquina).

## 1. Entrada de dados
| ID | Requisito | Sit. | Módulo | Verificação |
|---|---|---|---|---|
| SRS-IO-001 | Ler série de CT DICOM (inclusive sem preâmbulo), espaçamento uniforme, posição do paciente | H | dicom_io, imaging/ct | tests/test_engine.py, caso real |
| SRS-IO-002 | Ler RTSTRUCT e rasterizar estruturas na grade de dose (XOR de contornos do mesmo plano) | H | dicom_io/rtstruct | tests/test_rtstruct.py |
| SRS-IO-003 | Recusar CT com orientação diferente de axial ou posição diferente de HFS, com mensagem clara | P | imaging/ct | — |
| SRS-IO-004 | Conferir que CT e RTSTRUCT têm o mesmo FrameOfReferenceUID e o mesmo paciente | P | planejamento/fluxo | — |
| SRS-IMG-001 | Converter HU → densidade eletrônica pela curva do tomógrafo (CSV versionado) | H | imaging/hu_ed | test_hu_ed_curve_oncomed |
| SRS-IMG-002 | Sobrescrita de densidade por estrutura (ex.: contraste, prótese) | P | — | — |

## 2. Máquina e modelo de feixe
| ID | Requisito | Sit. | Módulo | Verificação |
|---|---|---|---|---|
| SRS-MCH-001 | Cadastro da máquina em YAML: nome DICOM (igual ao do Mosaiq), MLC, jaws, limites, energias | H | machine/model | tests/test_rtplan.py |
| SRS-MCH-002 | Geometria de colimação a partir do cadastro: limites das lâminas, gap mínimo, overtravel, jaws X | I | planejamento/conformacao | test_conformacao_* |
| SRS-MCH-003 | Presets Agility (80 pares × 5 mm, sem jaws X) e MLCi2 (40 × 10 mm, jaws X, gap 5 mm) | I | planejamento/conformacao | test_conformacao_mlci2_gap_minimo_e_jaw_x |
| SRS-COM-001 | Comissionamento automático contra PDD, Scp, OAR (e perfis, se houver) | H | commissioning | relatório de comissionamento |
| SRS-COM-002 | Só modelo comissionado calcula plano de paciente; modelo de demonstração só com permissão explícita | I | planejamento/calculo | test_modelo_nao_comissionado_recusado |
| SRS-COM-003 | O plano registra energia, hash e situação do modelo de feixe usado | I | planejamento/fluxo, modelo | test_fluxo_3d_* |
| SRS-COM-004 | Modelo de MLC (transmissão, tongue-and-groove, ponta arredondada) ajustado a **medidas próprias** | P | engine/beam_model | campos do artigo Roche et al. 2018 |
| SRS-COM-005 | Fatores e PDD de cunha motorizada (60°) no modelo | P (1b) | — | dados W60 do relatório RXT |
| SRS-COM-006 | Comissionamento ajusta e valida também a geometria isocêntrica: Scp a SSD 90 / 10 cm medido e dose/UM no isocentro × TMR, dentro de 1% (≥ 4×4) e 2% (< 4×4). **Hoje falha: −2,8% a +2,5%** | P | commissioning, tools/validar_isocentrico.py | docs/06 |

## 3. Planejamento
| ID | Requisito | Sit. | Módulo | Verificação |
|---|---|---|---|---|
| SRS-PLN-001 | Modelo de plano: campos, prescrição, máquina, referências ao CT/RTSTRUCT | I | planejamento/modelo | test_fluxo_3d_* |
| SRS-PLN-002 | Prescrição validada: dose total 0–100 Gy, 1–60 frações, ≤ 25 Gy/fx, isodose 50–110% | I | planejamento/modelo | test_prescricao_invalida_e_rotulo_longo |
| SRS-PLN-003 | Plano aprovado fica bloqueado para edição; desaprovar exige motivo e fica no histórico | I | planejamento/modelo | test_desaprovar_exige_motivo_e_registra |
| SRS-PLN-004 | Hash SHA-256 do conteúdo gravado na aprovação e conferido antes de exportar | I | planejamento/modelo | test_alteracao_apos_aprovacao_bloqueia_exportacao |
| SRS-PLN-005 | Aprovação exige: campos com abertura e UM, rótulo ≤ 16 caracteres, modelo identificado | I | planejamento/modelo | test_fluxo_3d_* |
| SRS-PLN-006 | Aprovação em dois níveis (físico e médico), com usuário autenticado | P (2) | — | — |
| SRS-PLN-010 | Conformar lâminas e jaws ao alvo + margem na visão do feixe (método "fora") | I | planejamento/conformacao | test_conformacao_cubo_centrado_agility |
| SRS-PLN-011 | Pares sem alvo fechados com o gap mínimo da máquina | I | planejamento/conformacao | test_conformacao_* |
| SRS-PLN-012 | Recusar alvo + margem maior que o campo máximo ou lâmina além do overtravel | I | planejamento/conformacao | test_alvo_maior_que_o_campo_maximo_recusado |
| SRS-PLN-013 | Conformação correta para alvo fora do eixo e para gantry 0/90/180/270 | I | planejamento/conformacao | test_conformacao_alvo_fora_do_eixo_segue_o_alvo |
| SRS-PLN-014 | Edição manual de lâminas e jaws, com checagem de limites mecânicos | P (2) | — | — |
| SRS-PLN-015 | Rotação de colimador e mesa conferida contra o Monaco com campos assimétricos (herança SRS-ENG-002) | P | engine/geometry | casos Monaco |
| SRS-PLN-020 | Grade de dose a partir do CT, voxel configurável (padrão 3 mm) | I | planejamento/calculo | test_fluxo_3d_* |
| SRS-PLN-021 | Dose por UM de cada campo pelo motor CCC herdado | I/H | planejamento/calculo, engine | test_fluxo_3d_*, tests/test_engine.py |
| SRS-PLN-022 | Normalização por média do alvo, isocentro ou ponto, com isodose de prescrição | I | planejamento/calculo | test_normalizacao_linear_com_pesos |
| SRS-PLN-023 | UM arredondadas a 0,1 e dose final recalculada com as UM arredondadas | I | planejamento/calculo | test_fluxo_3d_* |
| SRS-PLN-024 | Recusar campo com menos de 1 UM e pesos ≤ 0 | I | planejamento/calculo | revisão de código |
| SRS-PLN-025 | SSD calculada no eixo central a partir do CT | I | planejamento/calculo | test_fluxo_3d_* (900–1000 mm no fantoma) |
| SRS-PLN-026 | DVH cumulativo e métricas (D95, D98, D2, Dmean, Dmax) por estrutura | H | dicom_io/rtstruct | test_fluxo_3d_* |
| SRS-PLN-027 | Cálculo independente das UM por tabela (TMR/Scp, método clássico) como checagem interna em todo plano 3D | P | — | dados TMR/Scp já extraídos |

## 4. Exportação
| ID | Requisito | Sit. | Módulo | Verificação |
|---|---|---|---|---|
| SRS-EXP-001 | Só plano aprovado e íntegro é exportado para tratamento | I | exportacao/rtplan | test_fluxo_3d_* |
| SRS-EXP-002 | RTPLAN no IOD RT Plan: paciente/estudo do CT, mesmo FrameOfReference, RTSTRUCT referenciado | I | exportacao/rtplan | test_fluxo_3d_* |
| SRS-EXP-003 | Dispositivos Elekta: ASYMY + MLCX (Agility); ASYMX + ASYMY + MLCX (MLCi) | I | exportacao/rtplan | releitura em test_fluxo_3d_* |
| SRS-EXP-004 | UM no FractionGroup com 0,1 de resolução; 2 pontos de controle por campo estático | I | exportacao/rtplan | idem |
| SRS-EXP-005 | ApprovalStatus, ReviewDate/Time e ReviewerName preenchidos a partir da aprovação | I | exportacao/rtplan | conferir_rtplan |
| SRS-EXP-010 | Conferência automática dos atributos tipo 1/2 e da consistência interna do RTPLAN | I | exportacao/validacao_iod | test_fluxo_3d_* |
| SRS-EXP-011 | O RTPLAN exportado, relido por leitor independente (SecDose) e recalculado, reproduz a dose do planejamento (< 0,1%) | I | — | test_fluxo_3d_normaliza_exporta_e_releitura_reproduz_a_dose |
| SRS-EXP-012 | Validação com dciodvfy (DICOM3TOOLS) no CI | P | — | — |
| SRS-EXP-020 | Compatibilidade com o Mosaiq da clínica: conformance statement, comparação atributo a atributo com RTPLAN do Monaco aceito, importação no Mosaiq de teste | P/M | — | protocolo de integração |
| SRS-EXP-021 | Envio DICOM (C-STORE) ao Mosaiq | P | — | Mosaiq de teste |
| SRS-EXP-030 | RTDOSE (soma do plano, Gy, 32 bits) na mesma referência, ligado ao RTPLAN | I | exportacao/rtdose | test_fluxo_3d_* |
| SRS-EXP-031 | Recusar dose negativa, não finita ou que estoure 32 bits | I | exportacao/rtdose | revisão de código |
| SRS-EXP-040 | Relatório do plano em PDF (campos, UM, SSD, prescrição, DVH, isodoses, aprovação) | P | — | — |

## 5. Fluxo
| ID | Requisito | Sit. | Módulo | Verificação |
|---|---|---|---|---|
| SRS-WF-101 | Fluxo único: CT + RTSTRUCT + prescrição + ângulos → plano normalizado com DVH | I | planejamento/fluxo | test_fluxo_3d_* |
| SRS-WF-102 | Isocentro padrão no centróide do alvo, arredondado a 0,1 mm, ou informado | I | planejamento/fluxo | test_fluxo_3d_* |
| SRS-WF-103 | Trilha de auditoria persistente (quem, quando, o quê) em banco de dados | P (2) | — | — |

## 6. Limitações herdadas do motor (viram requisitos de fases seguintes)
- Kernel não inclinado e kernel poli-energético único (sem endurecimento com a profundidade).
- Contaminação eletrônica sem dependência explícita do tamanho de campo (superfície com erro de −10% a +3%).
- Fonte extrafocal ajustada só aos Scp; superestima a dose perto do campo sem perfis medidos (+7–9% em OAR
  adjacente no caso de mama).
- Sem tongue-and-groove nem interleaf leakage; transmissão média única do MLC.
- Dados atuais sem perfis de penumbra e sem campos < 3×3 cm²; o Sp vem do BJR 25, não de medida.
- Só HFS.

Nenhuma dessas limitações impede o desenvolvimento, mas **todas** precisam estar resolvidas ou quantificadas na
validação antes do primeiro uso clínico, mesmo só em 3D.

## 7. Critérios de aceitação da Fase 1 (resumo; detalhes em `docs/04`)
1. Todos os requisitos I e H com teste verde no CI.
2. Testes em água e heterogêneo (IAEA TRS-430 / TECDOC-1583) dentro das tolerâncias.
3. Plano exportado aceito pelo Mosaiq de teste sem edição manual, e entregue no simulador com medida pontual dentro
   de 3% e gama 3%/3 mm ≥ 95%.
4. Cálculo independente de UM (SRS-PLN-027) concordando com o TPS dentro de 3% em todos os casos de teste.
