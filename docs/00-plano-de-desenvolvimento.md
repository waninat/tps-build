# Plano de desenvolvimento de software (IEC 62304, seção 5.1)

Documento controlado. Versão 0.1 (rascunho), novembro de 2026. Responsável: Nadeem Ahmad Tahir.

## 1. Produto
Sistema de planejamento de radioterapia (TPS) com cálculo primário de dose para feixes de fótons de aceleradores
lineares, com exportação do plano ao sistema de registro e verificação (Mosaiq). Nome comercial ainda não definido;
neste repositório o produto se chama **TPS** e o pacote Python, `tps`.

Uso pretendido (rascunho, a ser revisado com o consultor regulatório): *software destinado a físicos médicos e
dosimetristas para criar, calcular, avaliar e aprovar planos de tratamento de radioterapia externa com fótons,
sob prescrição e aprovação de radio-oncologista, para entrega em aceleradores lineares comissionados no sistema.*

## 2. Classificações
| Item | Classificação | Observação |
|---|---|---|
| Segurança de software (IEC 62304) | **Classe C** | Um erro pode levar a dose errada no paciente: lesão grave ou morte. Todo o software do produto segue os requisitos da classe C, inclusive verificação de unidades e testes de integração. |
| Risco sanitário (ANVISA, RDC 751/2022) | Provavelmente **classe III** | Confirmar com consultor. SaMD segue também a RDC 657/2022. |
| Boas práticas de fabricação | RDC 665/2022 + ISO 13485 | Sistema de qualidade da empresa, antes do registro. |
| Norma de produto | **IEC 62083:2025 (3ª edição)** | Requisitos de segurança específicos de TPS. Comprar a norma e mapear cláusula a cláusula (tarefa da Fase 0). |
| Gestão de risco | ISO 14971:2019 | `docs/03-gestao-de-risco.md`. |
| Usabilidade | IEC 62366-1 | Começa na Fase 2 (interface). |
| Cibersegurança | IEC 81001-5-1 | Começa na Fase 2 (servidor, rede, DICOM). |

## 3. Ciclo de vida
Desenvolvimento incremental em fases. Cada fase entrega software que funciona de ponta a ponta e fecha com
verificação e validação documentadas:

| Fase | Entrega | Critério de saída |
|---|---|---|
| 0 | Fundação: repositório, processo, requisitos, risco, motor herdado do SecDose | Este conjunto de documentos aprovado; testes automáticos rodando no CI |
| 1 | **3D conformacional ponta a ponta**: CT → contornos → campos conformados → dose → UM → aprovação → RTPLAN/RTDOSE → Mosaiq → entrega em simulador | Testes TRS-430/TECDOC-1583 em água e heterogêneo; plano aceito pelo Mosaiq de teste e entregue no simulador dentro de 3% / 3 mm |
| 1b | Campos assimétricos com mesa e colimador girados, múltiplos isocentros, soma de planos | Idem, com casos de mama e pelve |
| 2 | Interface do usuário, banco de pacientes, contorno manual, registro rígido | Avaliação de usabilidade formativa |
| 3 | IMRT step-and-shoot (otimização de fluência + sequenciamento / DMPO) | TG-119 + CQ paciente-específico |
| 4 | VMAT; Monte Carlo GPU para dose final | TG-119, MPPG 5.b, comparação com Monaco |
| 5 | Auto-segmentação, funções biológicas, registro deformável | — |

## 4. Gerência de configuração
- Git (repositório privado no GitHub). Ramo `main` protegido: só entra por pull request com revisão e CI verde.
- Versão semântica. Cada versão liberada tem tag, notas de versão e o relatório de testes anexado.
- Dados de máquina (`dados/maquinas/`) versionados junto com o código; dados de paciente **nunca** entram no
  repositório (`.gitignore` e revisão).
- Itens SOUP listados com versão em `docs/05-soup.md`; dependências fixadas para cada versão liberada.

## 5. Requisitos, rastreabilidade e verificação
- Cada requisito tem identificador (`SRS-...`) em `docs/01-requisitos-fase-0-1.md`, o módulo que o implementa e o
  teste que o verifica. O identificador aparece na docstring do módulo.
- Cada risco em `docs/03-gestao-de-risco.md` aponta para o requisito que o controla.
- Teste automático (pytest) para tudo o que é verificável sem máquina; o resto é verificação manual com protocolo
  e registro (medidas, Mosaiq, entrega).

## 6. Gestão de problemas
Toda falha encontrada (teste, uso, medida) vira um *issue* com: descrição, versão afetada, avaliação de impacto em
segurança, correção, verificação e, se houver risco ao paciente, registro no arquivo de gestão de risco.

## 7. Papéis (mínimo para registro)
| Papel | Quem | Situação |
|---|---|---|
| Responsável técnico / físico médico | Nadeem | definido |
| Desenvolvimento | Nadeem + Claude + 1–2 desenvolvedores | contratar |
| Qualidade e assuntos regulatórios | consultor externo | contratar |
| Consultor clínico (radio-oncologista) | a definir | — |
| Revisor independente de física | físico que não escreveu o código | a definir |

## 8. Relação com o SecDose
O motor de cálculo do TPS nasce como cópia do núcleo do SecDose 0.8.0 (ver `docs/adr/0001`). Isso cria um problema
de independência: **o SecDose não pode ser usado como verificação secundária "independente" de planos feitos neste
TPS enquanto os dois compartilharem o mesmo algoritmo e modelo de feixe** (ver `docs/adr/0002`).
