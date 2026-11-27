# 13 — Versão 0.9.8: pós-processamento de contornos, blindagem e compensador, perfis Monaco/Eclipse, copiloto e fusão deformável

Tudo continua **não clínico**: as funções abaixo precisam de validação da física antes de qualquer uso em paciente.

## 1. Pós-processamento de contornos (SRS-CNT-020)
Os contornos do TotalSegmentator vêm "em degraus" (máscara de 1,5 mm). Agora:
- **Contorno automático** já sai suavizado (padrão 2 mm; dá para escolher 0–4 mm na caixa da IA).
- **Pós-processamento** (aba Estruturas › Contorno): suavizar (3D, mantém o volume), fechar, abrir, preencher
  buracos, remover ilhas pequenas e manter só a maior parte — como as ferramentas do Eclipse. Desfazível.

## 2. Blindagem de órgão no campo 3D
**Problema:** quando o órgão fica no meio de um par de lâminas (alvo dos dois lados), o MLC não fecha o meio do par;
o TPS só avisava e deixava aberto — a blindagem "não funcionava".

**Agora:** o campo é dividido em segmentos step-and-shoot (o segmento 1 abre o trecho de um lado, o 2 o do outro),
cada ponto aberto em exatamente um segmento; o peso do campo é multiplicado pelo número de segmentos para manter a
fluência. Vai para o Mosaiq como um único feixe (como o field-in-field do Monaco). A divisão faz parte da
conformação (não some ao recalcular), e o field-in-field, a proteção de órgãos e o compensador respeitam a abertura
com a blindagem. Aviso quando a não-interdigitação do MLCi obriga dois segmentos a abrirem a mesma área (ponto quente).

Pelve real (caixa 4 campos, Precise), blindando o reto nos laterais com prioridade do alvo: HI 0,098 → 0,098,
reto Dmean 42,6 → 42,0 Gy (o reto fica quase todo atrás do PTV nos laterais).

## 3. Compensador de fluência
O sequenciamento entregava só o **maior trecho** de cada nível em cada par: quando o excesso de fluência aparecia dos
dois lados (bordas do campo, mama), um lado ficava sem compensação. Agora cada nível com dois trechos relevantes
(≥ 1,5 cm e ≥ 50% do principal) vira dois segmentos; segmentos pequenos são fundidos no que mais se sobrepõe (antes:
no vizinho da lista, que podia ser o outro lado do campo).

## 4. Proteção de órgãos (sub-campos automáticos)
Corrigido o erro "campo com menos de 1 UM" e acrescentados sub-campos que bloqueiam o órgão inteiro (inclusive atrás
do alvo). A cobertura não se troca mais pelo órgão: se o HI piora mais de 0,03 ou o D98 cai mais de 1%, a prioridade
dos órgãos cai (½, ¼, 1/10) até voltar à tolerância, com aviso. Pelve: reto Dmean 42,6 → 39,6 Gy, bexiga 45,0 → 41,0
Gy, HI 0,098 → 0,104, D98 94,5% → 93,9%.

## 5. Planejar "como no Monaco" ou "como no Eclipse" (SRS-PLN-120)
Em Otimização › Objetivos, **Planejar como**: TPS, Monaco ou Eclipse.
- **Vocabulário:** Monaco — Target Penalty, Quadratic Overdose/Underdose, Maximum Dose, Overdose/Underdose DVH,
  Serial, Parallel; Eclipse — Upper, Lower, Mean, Upper gEUD, com **prioridade** 0–1000 (100 = peso 1; ≥ 300 num
  órgão = restrição).
- **Padrões:** Monaco: 8 níveis, 2 UM e 2 cm² por segmento; Eclipse: 10 níveis, 3 UM, 3 cm², NTO mais forte e
  fluência mais suave.
- **Exportação:** Monaco — `OBJ_Monaco_*.csv` (funções de custo). Eclipse — `OBJ_Eclipse_*.xml` (modelo de
  objetivos com NTO) e `OBJ_Eclipse_*.csv`; rótulo do plano até 13 caracteres e descrição do feixe pela técnica.
  **O fabricante gravado no DICOM continua sendo o TPS** — a origem do plano não é falseada; o que muda é o arquivo
  chegar no formato e nas convenções do outro sistema, pronto para reotimizar.
- O esquema XML de objetivos do Eclipse muda entre versões: **envie um modelo exportado do seu Eclipse** para
  ajustarmos o formato exato (até lá, a tabela CSV serve de guia).
- Novo tipo de objetivo `dvh_min` (Underdose DVH / Lower com volume): pelo menos V% com a dose.

## 6. Copiloto do plano com o Claude (SRS-PLN-130)
Ferramentas › Copiloto (ou **Ctrl+K**): painel de conversa. O Claude usa as ferramentas do TPS:
- **ler:** plano, dose (D2/D50/D95/D98, V95/100/107, restrições, HI, conferência de UM), DVH, medidas entre
  estruturas, campos e lâminas, objetivos, máquina, protocolos;
- **agir (só no rascunho):** perfil Monaco/Eclipse, objetivos, otimizar IMRT/VMAT, calcular com otimização de
  pesos/field-in-field/compensador/proteção de órgãos, criar/alterar/remover campos, **blindar um órgão num campo**,
  mover lâminas, pós-processar contornos;
- **parecer:** aceitável / pode melhorar / refazer, com pontos e sugestões priorizadas (segunda opinião, não aprovação);
- **máquina nova:** lista os dados necessários (identificação, geometria/MLC, DICOM, energias, dados de feixe), pede
  um bloco por vez e grava um **rascunho** em `<dados>/maquinas_rascunho/` com o que falta marcado CONFIRMAR. O
  rascunho não entra em uso até a física comissionar.

Limites: não aprova, não desaprova, não exporta, não envia e não apaga planos. Antes da primeira alteração de cada
pedido o estado é guardado (**Desfazer**). Cada ação entra no registro da sessão e no histórico do plano; cada
conversa fica em `<dados>/ia/claude/`. Sai da clínica só o resumo anônimo (sem imagens, sem nome/ID).

## 7. Fusão CT–CT rígida e deformável (SRS-IMG-040)
Aba Estruturas › **Fusão com outra TC**: carregar a TC secundária (mesmo paciente, outra posição ou outros
acessórios), **registrar** (rígido 6 graus por correlação multirresolução; deformável = rígido + demons simétrico
em 2 níveis com regularização gaussiana) e ver a fusão em **mistura, xadrez, diferença (HU)** ou só a secundária.
Métricas: correlação e erro médio em HU antes/depois, deslocamento médio/máximo e jacobiano (dobras).
**Propagar** leva as estruturas da secundária para a TC do plano (sufixo `_def`), suavizadas — revise sempre.

Limitações: CT–CT apenas (não RM/PET); sem acumulação de dose deformada ainda; o deformável não sabe o que é osso
(pode "dobrar" osso se a regularização for baixa — use o xadrez).
