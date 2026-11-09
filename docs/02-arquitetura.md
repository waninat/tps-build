# Arquitetura de software (IEC 62304, seção 5.3)

Versão 0.1 (rascunho), novembro de 2026.

## Camadas
```
┌──────────────────────────────────────────────────────────────────────────┐
│ Interface (Fase 2): visualizador, contorno, campos, DVH, aprovação       │
├──────────────────────────────────────────────────────────────────────────┤
│ Serviço (Fase 2): banco de pacientes/planos, auditoria, usuários, fila   │
├──────────────────────────────────────────────────────────────────────────┤
│ Planejamento  tps/planejamento                                            │
│   modelo.py      Plano, Campo, Prescrição; estado, aprovação, hash        │
│   conformacao.py BEV, conformação MLC/jaws, limites mecânicos             │
│   calculo.py     grade, dose por UM, normalização, SSD                    │
│   fluxo.py       CT + RTSTRUCT → plano 3D normalizado                     │
│   (Fase 3+)      otimizador, sequenciador, VMAT                           │
├──────────────────────────────────────────────────────────────────────────┤
│ Exportação  tps/exportacao: RTPLAN, RTDOSE, conferência do IOD,           │
│             (P) envio C-STORE ao Mosaiq, relatório PDF                   │
├──────────────────────────────────────────────────────────────────────────┤
│ Núcleo de cálculo (herdado do SecDose)                                    │
│   physics   NIST, espectros, kernels EGSnrc, cones colapsados             │
│   engine    geometria IEC 61217, fluência, TERMA, superposição CCC        │
│   commissioning  ajuste do modelo aos dados medidos                       │
│   (Fase 4)  Monte Carlo GPU para dose final                               │
├──────────────────────────────────────────────────────────────────────────┤
│ E/S  dicom_io (CT, RTSTRUCT, RTPLAN, RTDOSE), imaging (HU→DE), machine,   │
│      beamdata (CSV e extração de relatório PDF)                          │
└──────────────────────────────────────────────────────────────────────────┘
```

Regras:
1. Dependências só de cima para baixo. O núcleo de cálculo não conhece planos, usuários nem DICOM.
2. O `Plano` não conhece DICOM; a exportação converte.
3. Funções de cálculo são puras sobre arrays (entrada → saída), para poderem ser portadas para GPU e testadas
   contra a versão CPU.
4. Todo valor com unidade carrega a unidade no nome (`_mm`, `_gy`, `_cm2`) ou na docstring.

## Fluxo de dados da Fase 1
```
CT (DICOM) ──► CTVolume ──► HU→DE ──► GradeDose (η)
RTSTRUCT  ──► Structure ──► máscaras na grade
                                   │
prescrição + ângulos ──► Campo ──► conformar (BEV) ──► jaws/lâminas
                                   │
modelo de feixe comissionado ─────► dose por UM (CCC) por campo
                                   │
                                   ▼
                          normalização → UM (0,1) → dose final → DVH
                                   │
                         aprovação (hash) ──► RTPLAN + RTDOSE ──► Mosaiq
```

## Segregação para a classe C (IEC 62304, 5.3.5)
Itens que podem causar dano direto e recebem o maior rigor (revisão independente, cobertura de testes, análise de
risco por item):
- `engine/*`, `physics/*`, `commissioning` (dose);
- `planejamento/calculo.py` (UM);
- `planejamento/conformacao.py` e `engine/geometry.py` (geometria);
- `exportacao/rtplan.py` (o que a máquina entrega).

## Decisões em aberto
| Tema | Opções | Quando decidir |
|---|---|---|
| Interface | Qt desktop × web cliente-servidor | início da Fase 2 |
| Banco de dados | PostgreSQL (como o QualiRT) × SQLite (como o SecDose) | início da Fase 2 |
| Motor GPU | CUDA C++ × numba-cuda/CuPy | Fase 3 |
| Monte Carlo | próprio (base XVMC/DPM) × biblioteca existente com licença comercial | Fase 3 (estudo de licenças) |
| Auto-segmentação | nnU-Net treinado com dados próprios (LGPD, comitê de ética) | Fase 5 |
