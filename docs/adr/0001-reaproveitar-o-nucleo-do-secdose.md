# ADR 0001 — Reaproveitar o núcleo de cálculo do SecDose

Data: novembro de 2026. Situação: aceita.

## Contexto
O SecDose 0.8.0 já tem leitura DICOM (CT, RTSTRUCT, RTPLAN, RTDOSE), conversão HU→DE, kernels EGSnrc, motor
collapsed cone (CPU/numba), modelo de fonte e fluência, comissionamento automático contra PDD/Scp/OAR e índice
gama, com ~90 testes e comparação clínica contra o Monaco (caso de mama VMAT, gama 3%/2 mm = 97,6%).

## Decisão
Copiar (não importar como dependência) os módulos `physics`, `engine`, `dicom_io`, `imaging`, `machine`,
`beamdata`, `compare`, `checks`, `commissioning`, `pipeline`, `complexity` e `fantoma` para o pacote `tps`, com os
testes correspondentes. Servidor web, licença, logs e relatório do SecDose ficam de fora.

## Consequências
- A Fase 1 começa com um motor de dose que funciona e foi testado.
- Os dois produtos evoluem separadamente. Correção de física num deles precisa ser avaliada no outro (registro
  cruzado de *issues*).
- As limitações conhecidas do SecDose passam a ser limitações do TPS (ver `docs/01`, seção 4) e viram requisitos
  das próximas fases.
- O motor herdado é SOUP-interno: foi desenvolvido sem o processo da classe C. Antes do uso clínico, cada módulo
  herdado precisa de revisão de código, testes unitários completos e registro no plano de verificação.
