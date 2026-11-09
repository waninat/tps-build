# TPS — sistema de planejamento de radioterapia (nome provisório)

Cálculo primário de dose e planejamento para fótons de aceleradores Elekta, com exportação ao Mosaiq.
Desenvolvimento sob IEC 62304 classe C. **Não é dispositivo liberado para uso clínico.**

## Estado atual — Fase 0 concluída, Fase 1 iniciada
- Núcleo de cálculo herdado do SecDose 0.8.0: DICOM, HU→DE, kernels EGSnrc, collapsed cone, comissionamento.
- Planejamento 3D conformacional em fantoma: conformação MLC/jaws no BEV (Agility e MLCi2), dose por UM,
  normalização (média do alvo, isocentro ou ponto), SSD, DVH.
- Plano com estado e aprovação; plano aprovado congelado por hash SHA-256 e conferido antes de exportar.
- Exportação RTPLAN (convenções Elekta) e RTDOSE, com conferência dos atributos obrigatórios. O RTPLAN exportado,
  relido por um leitor independente e recalculado, reproduz a dose do planejamento.
- Modelo 6 MV protótipo comissionado com a planilha da OncoMed (1,000 cGy/UM; condição de referência a confirmar).

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
| adr/ | decisões de arquitetura (reuso do SecDose, independência, linguagem) |

## Uso
    python -m pip install -r requirements.txt
    python -m pytest -q
    python tools/exemplo_planejamento_3d.py saida/          # fantoma: plano 4 campos → RTPLAN/RTDOSE + figura
    python tools/comissionar.py dados/maquinas/<maquina>/6MV --cgy-por-um <calibração>
    python tools/validar_isocentrico.py dados/maquinas/<maquina>/6MV

## Estrutura
    tps/planejamento   modelo do plano, conformação, cálculo/normalização, fluxo 3D        (novo)
    tps/exportacao     RTPLAN, RTDOSE, conferência do IOD                                  (novo)
    tps/engine, physics, dicom_io, imaging, machine, beamdata, compare, checks, commissioning   (herdado)
    dados/maquinas     dados de feixe e modelos por máquina (nunca dados de paciente)
    machines/          kernels, espectros, curvas HU→DE de fábrica
    tools/             comissionamento, validação, exemplo
