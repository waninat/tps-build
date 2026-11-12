# Planejamento 3D direto: o que o TPS faz, comparado ao Eclipse e ao Monaco

Versão 0.1, 12/11/2026. A comparação reflete o que conhecemos das versões recentes do Eclipse (Varian) e do Monaco
(Elekta) e deve ser conferida com os manuais das versões instaladas. Diretriz do projeto: quando for preciso escolher,
seguir o Eclipse no 3D, e ir além dos dois onde der.

| Recurso | Eclipse | Monaco | TPS (este repositório) |
|---|---|---|---|
| Ajuste do MLC ao alvo | "Fit MLC" com margens e ponto de encontro (fora/dentro/cruzando), posição das lâminas fechadas | ajuste por margem | ponto de encontro **fora, dentro, meio, área (50%) ou fração livre**; margens X1/X2/Y1/Y2 elípticas ou retangulares; lâminas fechadas sob o jaw |
| Critério "área" | — | — | minimiza (área exposta fora do alvo + área do alvo bloqueada) por lâmina |
| Blindagem de órgão | ajuste a estrutura com margem negativa/blindagem | manual | estruturas de blindagem com margem própria, com ou sem prioridade do alvo |
| Restrição mecânica do MLCi (sem interdigitação) | respeita na entrega, conforme o MLC | respeita | resolvida no ajuste por propagação; recua o mínimo e avisa |
| Ângulo de colimador | manual | manual | **otimização automática** (menor erro de conformação) |
| Pesos dos campos | manual | manual | **otimização automática** (homogeneidade do alvo, penalidade em OAR) |
| Compensação sem cunha | field-in-field manual; compensador eletrônico (fluência pintada) | field-in-field manual | **field-in-field automático**: sub-campos a 107/105/103%, pesos otimizados, UM mínima, campo aberto ≥ 60% |
| Segunda conta de UM | aplicativo à parte | aplicativo à parte | **embutida**: TMR/Sc/Sp/OAR com profundidade radiológica, em todo plano |
| Plano 2D sem CT | possível com fantoma/contorno | limitado | fantoma virtual (caixa, elipse, contorno), SAD/SSD, exportação sem CT |
| Integridade do plano aprovado | aprovação com estados | aprovação | aprovação com **hash SHA-256 conferido antes de exportar** |

## O que ainda falta para o 3D ficar completo (backlog da Fase 1/1b)
- Interface: visualizador axial/coronal/sagital, BEV com DRR, edição de lâminas com o mouse, régua, isodoses.
- Compensador eletrônico (fluência pintada, como o do Eclipse) para mama: é a evolução natural do field-in-field e
  precisa de sequenciador step-and-shoot, que a Fase 3 vai trazer.
- Pontos de referência com dose acumulada, campos de setup (kV/MV), soma de planos, bolus.
- Relatório do plano em PDF e envio DICOM ao Mosaiq.
- Elétrons, se o Precise tiver (a confirmar).
