# 16 — Passagem do trabalho: melhorar o VMAT

Situação em 28/11/2026, versão 0.9.11. Software não clínico, em desenvolvimento.

## Onde pegar o código
- **Repositório:** GitHub `waninat/tps-build`.
- **Ramo de trabalho:** `dev`. O `main` é igual ao `dev` no commit `9679d65`, que é o que gerou a 0.9.11.
- **Sugestão:** criar um ramo `vmat` a partir do `dev`. Juntar ao `dev` só quando estiver melhor que o atual nos casos de referência.
- **Push no `main` dispara o build do instalador Windows e a release.** Faça isso só quando o físico pedir: `git checkout main && git merge --ff-only dev && git push build main`.
- **Requisitos:** Python 3.11+ e `pip install -r requirements.txt`.
- **Testes:** `python -m pytest -q`. São 191 testes, uns 18 min; `tests/test_vmat.py` tem 10 testes, uns 6–7 min.
- **App local:** `python tps_app.py --porta 8770 --home <pasta>`.

## Regras do projeto
- **Textos para o usuário:** sempre em português. Não citar Claude, Anthropic nem TotalSegmentator; usar "IA" e "segmentação automática".
- **Commits:** um commit por mudança, com push no ramo de trabalho.
- **Compilar e publicar** só quando o físico pedir.
- **Máquinas:** Elekta Precise com MLCi, que não interdigita, tem overtravel de 125 mm e gap mínimo de 5 mm (`dados/maquinas/radioserra_precise`). Elekta Synergy com Agility (`dados/maquinas/oncomed_synergy_154828`). Os limites dinâmicos estão no `maquina.yaml`, seção `vmat` (ver docs/11).

## Estado atual
- **3D:** muito bom.
  - Inclui tangentes de mama, field-in-field, compensador por segmentos e proteção de órgãos.
- **IMRT:** razoável.
  - Fluência por campo e sequenciamento.
  - O mesmo custo e os mesmos objetivos que o VMAT usa.
- **VMAT:** fraco. Planos ruins, piores que o 3D e o IMRT do mesmo caso, muitas vezes não tratáveis.

### Como o VMAT funciona hoje (`tps/planejamento/vmat.py`, `otimizar_vmat`)
1. **Fluência por setor:**
   - O arco é dividido em setores de 40°, e cada setor vira um campo no meio do setor, conformado ao alvo.
   - As fluências são otimizadas juntas, com o custo do IMRT (`tps/planejamento/imrt.py`): objetivos, tecido normal, "evitar" e auto-flash.
2. **Sequenciamento em arco:**
   - A fluência de cada setor vira uma janela deslizante unidirecional, distribuída pelos pontos de controle.
   - O sentido alterna de um setor para o outro, e a UM por intervalo é constante no setor.
   - Funções: `sequenciar_dinamico`, `sequenciar_setor`.
3. **Otimização direta dos pontos de controle (`otimizar_pontos`, `ModeloIntervalos`):**
   - Dose por intervalo pelo CCC em grade grossa.
   - UM por intervalo, dentro dos limites de UM/grau.
   - Posições das lâminas num modelo rápido, com o percurso por grau penalizado, gap mínimo e sem interdigitação no MLCi.
   - Repete com a dose recalculada.
4. **Dose final** na grade do plano. Por padrão a dose final é por Monte Carlo, o que leva dezenas de minutos por plano neste ambiente.

### Onde o VMAT entra
- `tps/app/sessao.py`:
  - `campos_vmat(n_arcos, inicio, fim, sentido, alvo, margem_mm, colimadores)` cria os arcos;
  - `calcular({"vmat": {"objetivos": [...], "perfil": ...}})` chama `_otimizar_vmat`;
  - `_calcular` é onde o VMAT é despachado.
- Na interface, o VMAT fica na aba Campos: arcos e objetivos.
- O **plano automático** (`tps/app/automacao.py`, `_fazer_plano` e `_com_tentativas`) faz 3D e VMAT lado a lado, com até 3 tentativas cada:
  - Objetivos iniciais: vêm do assistente (IA ou regras).
  - Tentativas seguintes: reforçam os objetivos que falharam (`_objetivos_mais_agressivos`).
  - Nota de cada tentativa: `_nota`.
  - Arcos: 2 (3 na última tentativa). Na mama são arcos parciais (esquerda 295°→135°, direita 225°→65°).
- Comparar com o IMRT do mesmo caso e dos mesmos objetivos: `calcular({"imrt": ...})` depois de `campos_imrt(n)`.

## Problemas conhecidos do VMAT
1. **Qualidade da dose:**
   - cobertura e homogeneidade piores que IMRT e 3D;
   - pontos quentes;
   - órgãos de risco acima do que o IMRT consegue com os mesmos objetivos.
2. **Entregabilidade:**
   - planos "não tratáveis" (o físico relatou);
   - no teste de mama + FSC, a otimização deixou lâminas além do overtravel.
     - Em 0.9.11, `reparar` (vmat.py) força o limite **depois** da otimização, numa correção grosseira.
     - O otimizador não conhece o limite, então a dose pode mudar.
   - Conferir aberturas pequenas ou recortadas, velocidade da lâmina × gantry, UM/grau e UM mínima por cm de lâmina (`um_por_cm_lamina_min`, ainda não usada).
3. **Taxa de dose do Precise:** discreta, em 7 faixas. A modelagem existe nos dados (`taxas_dose_discretas_um_min`), mas o otimizador não usa.
4. **Perda entre as etapas:** a perda da fluência ideal (etapa 1) para o sequenciamento (etapa 2) e para os pontos de controle (etapa 3) ainda não foi medida.
5. **Diferença entre a dose otimizada e a dose final:**
   - a otimizada vem do CCC em grade grossa e do modelo rápido;
   - a final é CCC na grade fina ou Monte Carlo.
6. **Tempo:** com Monte Carlo no fim, um plano leva mais de 30 min. Durante o desenvolvimento, use o collapsed cone e deixe o Monte Carlo para a verificação.
7. **Modelo de MLC:** transmissão de 1%, groove 0 e ponta 0 são valores de partida, não medidos (docs/11).

## Como sugiro atacar
1. **Casos de referência:**
   - Casos que existem:
     - CT real de pelve (o físico tem; plano 3D do Monaco disponível);
     - fantomas (`tps.fantoma`);
     - mama sintética com FSC.
   - Pedir ao físico os planos VMAT do Monaco (RTPLAN, RTDOSE e objetivos) com os QA.
2. **Métrica:**
   - Script de benchmark que roda IMRT e VMAT com os mesmos objetivos e reporta V95, D98, D2, HI, CI, Paddick (`tps/planejamento/indices.py`) e as restrições.
   - Entregabilidade: `verificar_mecanica`, `tempo_entrega`, UM total, a menor abertura e a dose otimizada × dose final.
   - Critério inicial: o VMAT chegar perto do IMRT.
3. **Medir onde a qualidade se perde** (etapa 1 → 2 → 3) antes de reescrever.
4. **Restrições mecânicas dentro da otimização:** overtravel, gap, interdigitação, velocidades, UM/grau e as faixas de taxa de dose.
5. **Manter o que funciona:** os testes do `test_vmat.py`, a interface (arcos, objetivos, pontos de controle no BEV) e o formato do RTPLAN exportado.
