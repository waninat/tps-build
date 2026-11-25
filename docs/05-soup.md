# Itens SOUP (software de origem desconhecida) — IEC 62304, 5.3.3 e 7.1

| Item | Uso | Licença | Riscos a avaliar |
|---|---|---|---|
| Python 3.12+ | linguagem | PSF | — |
| NumPy | arrays | BSD | precisão numérica |
| SciPy | interpolação, filtros, otimização | BSD | `map_coordinates`, `gaussian_filter` em limites |
| numba | compilação dos laços de cálculo | BSD | diferenças com `fastmath`; versão fixada |
| pydicom | leitura/escrita DICOM | MIT | conformidade do que é gravado |
| anthropic (SDK) + API do Claude | assistente de objetivos (opcional): proposta a partir do resumo anônimo | MIT (SDK); serviço da Anthropic sob contrato | resposta errada ou inventada (mitigação: validação por regras + revisão do planejador); dados enviados (só resumo anônimo, registrado em ia/claude); indisponibilidade (cai nas regras) |
| matplotlib (Path) | ponto-no-polígono na rasterização | PSF-like | contornos degenerados |
| PyYAML | cadastro de máquinas e modelos | MIT | — |
| pdfplumber | extração de tabelas de relatórios | MIT | erro de leitura → sempre conferir por amostragem |
| EGSnrc/EDKnrc | **só para gerar** os kernels (não distribuído) | AGPL-3.0 | kernels versionados com hash |
| Dados NIST (XCOM) e espectros de Mohan 1985 | física | dados publicados | citação |
| SecDose 0.8.0 (núcleo) | motor herdado | próprio | SOUP-interno: ver ADR 0001 |

Antes da primeira versão liberada: fixar versões, registrar *known issues* de cada item e avaliar licenças com
assessoria jurídica (em especial qualquer código de terceiros no Monte Carlo).
