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
| Compensação sem cunha | field-in-field manual; compensador eletrônico (fluência pintada) | field-in-field manual | **field-in-field automático** e **compensador de fluência automático** (referência: dose no próprio alvo; segmentos MLCi; HI 0,20 → 0,12 no caso de separação variável) |
| Segunda conta de UM | aplicativo à parte | aplicativo à parte | **embutida**: TMR/Sc/Sp/OAR com profundidade radiológica, em todo plano |
| Plano 2D sem CT | possível com fantoma/contorno | limitado | fantoma virtual (caixa, elipse, contorno), SAD/SSD, exportação sem CT |
| Integridade do plano aprovado | aprovação com estados | aprovação | aprovação com **hash SHA-256 conferido antes de exportar** |

## O que ainda falta para o 3D ficar completo (backlog da Fase 1/1b)
- Interface 0.2.0 já tem cortes, BEV/DRR, edição de lâminas, isodoses, DVH; faltam régua, edição de jaws com o
  mouse, contorno e registro.
- Pintura manual da fluência do compensador (o automático já existe).
- Pontos de referência com dose acumulada, campos de setup (kV/MV), soma de planos, bolus.
- Relatório em PDF nativo (hoje: HTML imprimível) e teste real do envio ao Mosaiq 2.83.
- Elétrons, se o Precise tiver (a confirmar).
