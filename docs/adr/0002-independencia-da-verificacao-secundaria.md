# ADR 0002 — Independência entre o TPS e a verificação secundária

Data: novembro de 2026. Situação: aceita.

## Contexto
A verificação secundária (AAPM TG-219, MPPG 5.b) só detecta erros do TPS se usar algoritmo, modelo de feixe e dados
de entrada independentes. O TPS herda o motor collapsed cone e o modelo de feixe do SecDose (ADR 0001).

## Decisão
1. Enquanto o TPS calcular a dose final com o motor CCC herdado, **o SecDose não é oferecido como verificação
   independente de planos deste TPS**. Para esses planos, a clínica usa outro sistema de verificação ou CQ por
   medida.
2. A Fase 4 troca a dose final do TPS por Monte Carlo (GPU). A partir daí, o par TPS (Monte Carlo) × SecDose
   (collapsed cone, modelo comissionado separadamente) pode ser apresentado como independente quanto ao algoritmo,
   desde que os modelos de feixe sejam comissionados de forma separada e a independência seja demonstrada.
3. Os parâmetros de MLC do TPS vêm de medidas próprias (não da configuração do Monaco), para que um erro de
   configuração do Monaco não se propague.

## Consequências
O argumento comercial "TPS + verificação secundária do mesmo fornecedor" só vale depois da Fase 4, e com
documentação de independência.
