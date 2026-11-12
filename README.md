# TPS — sistema de planejamento de radioterapia (nome provisório)

Cálculo primário de dose e planejamento para fótons de aceleradores Elekta, com exportação ao Mosaiq.
Desenvolvimento sob IEC 62304 classe C. **Não é dispositivo liberado para uso clínico.**

## Estado atual (12/11/2026) — Fase 1: Precise + TPS 2D/3D
- **Máquinas:** Elekta Precise (MLCi) da Radioserra, 6 MV, comissionado com o relatório RXT 2019; Synergy (Agility)
  da OncoMed, 6 MV. Comissionamento v2 (PDD multi-campo + saídas em dmax e no isocentro).
- **2D sem CT:** fantoma virtual (caixa, elipse, contorno), campos retangulares, SAD/SSD, normalização no ponto,
  exportação RTPLAN na geometria do equipamento.
- **3D (estilo Eclipse):** ajuste de MLC com ponto de encontro fora/dentro/meio/área, margens X1/X2/Y1/Y2, blindagem
  de órgãos, pares fechados sob o jaw, regra de não-interdigitação do MLCi.
- **Automações (além do Eclipse/Monaco):** ângulo de colimador, pesos dos campos e field-in-field automático
  (substitui a cunha).
- **Segurança:** conferência independente de UM por tabela (TMR/Sc/Sp/OAR) em todo plano; aprovação com hash;
  checagem mecânica de todo campo; RTPLAN conferido e relido.

## Documentos (`docs/`)
| Arquivo | Conteúdo |
|---|---|
| 00-plano-de-desenvolvimento.md | classificação, ciclo de vida em fases, configuração, papéis |
| 01-requisitos-fase-0-1.md | requisitos com situação, módulo e teste (rastreabilidade) |
| 02-arquitetura.md | camadas, fluxo de dados, itens de classe C |
| 03-gestao-de-risco.md | análise preliminar ISO 14971 |
| 04-plano-de-verificacao-e-validacao.md | testes de modelo, heterogeneidade, Mosaiq, validação clínica |
| 05-soup.md | componentes de terceiros |
| 06-dados-recebidos-e-pendencias.md | o que foi recebido, o que o modelo mostrou e o que falta |
| 07-planejamento-3d-comparado.md | recursos 3D × Eclipse × Monaco, e o que falta |
| adr/ | decisões de arquitetura (reuso do SecDose, independência, linguagem) |

## Uso
    python -m pip install -r requirements.txt
    python -m pytest -q
    python tools/exemplo_planejamento_3d.py saida/          # fantoma: plano 4 campos → RTPLAN/RTDOSE + figura
    python tools/comissionar.py dados/maquinas/<maquina>/6MV --cgy-por-um <calibração>
    python tools/comissionar.py dados/maquinas/<maquina>/6MV --cgy-por-um 1.0 --v2   # comissionamento v2
    python tools/validar_isocentrico.py dados/maquinas/<maquina>/6MV
    python tools/exemplo_precise.py saida/          # 2D, 3D e field-in-field no Precise

## Estrutura
    tps/planejamento   modelo do plano, conformação, cálculo/normalização, fluxo 3D        (novo)
    tps/exportacao     RTPLAN, RTDOSE, conferência do IOD                                  (novo)
    tps/engine, physics, dicom_io, imaging, machine, beamdata, compare, checks, commissioning   (herdado)
    dados/maquinas     dados de feixe e modelos por máquina (nunca dados de paciente)
    machines/          kernels, espectros, curvas HU→DE de fábrica
    tools/             comissionamento, validação, exemplo
