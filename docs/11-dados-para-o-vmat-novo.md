# 11 — Dados para o VMAT novo (entrega ao trabalho do VMAT/GPU)

Situação em 26/11/2026. O que já existe, de onde veio, e o que falta.

## Limites dinâmicos (maquina.yaml › vmat)
| Item | Precise (MLCi) | Synergy (Agility) | Fonte |
|---|---|---|---|
| Velocidade de lâmina | 2,0 cm/s | 3,5 cm/s (6,5 cm/s com o guia dinâmico) | Kantz et al. (Agility × MLCi) |
| Velocidade de gantry | 6,0 °/s | 6,0 °/s | Dobler 2011; alguns modelos usam 5,5 |
| Taxa de dose | **discreta**: 7 faixas, cada uma metade da anterior (500 → 7,8 UM/min, ±25%) | contínua (CVDR) se licenciada; senão as mesmas faixas | Dobler 2011; Kantz et al. |
| UM por grau | 0,1 – 20 | 0,1 – 20 | Dobler 2011 |
| UM mínima por cm de lâmina | 0,3 | — | Dobler 2011 |
| Pontos de controle por arco | 360 (a confirmar) | 360 (a confirmar) | — |

`taxas_dose_discretas_um_min`, `taxa_dose_continua` e `um_por_cm_lamina_min` foram acrescentados, mas o otimizador
atual ainda não os usa: modelar as faixas discretas do Precise faz parte do VMAT novo.

A confirmar pela física: a versão do controle do Precise (Desktop 7 ou Integrity, o que decide se a taxa é
discreta) e a licença de CVDR do Synergy.

## MLC (modelo de feixe)
O modelo comissionado usa transmissão de 1%, groove 0 e deslocamento da ponta 0. Esses são valores de partida, não
medidas. Para comparar: o modelo Monaco de 2018 do MLCi2 tem transmissão de 0,73%, groove de 0,7 mm e LeafGap de
5 mm (docs/06). O ajuste deve ser feito contra os QA de paciente dos VMAT do Monaco, a serem enviados com os planos.

## Já disponível
* PDD, TMR, Scp e OAR diagonal das duas máquinas (relatórios RXT 2019), com o modelo CCC v2 validado (docs/06).
* Placas de pulmão e osso **calculadas pelo Monaco** (não são medidas) e o plano 3D de pelve do Monaco.

## Falta (a ser enviado)
* Planos VMAT do Monaco (RTPLAN + RTDOSE + objetivos) com os respectivos QA (gama e arquivo de medida).
* Registros de entrega da máquina (log files), se disponíveis.
* Perfis medidos em água (os relatórios trazem só a OAR diagonal do 40×40): validam a penumbra e a ponta de lâmina.

## Decisões
* GPU: código genérico NVIDIA/CUDA, com volta automática para a CPU quando não houver placa. O resultado da GPU deve
  bater com o da CPU (teste de equivalência), e um script de benchmark fica disponível para validar em cada cliente.
* Metas clínicas: formato genérico; cada clínica cadastra as suas (dados/objetivos_sitio e a futura lista de metas
  com prioridade).
