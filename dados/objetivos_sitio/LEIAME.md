# Objetivos de otimização por sítio (IMRT/VMAT)

Cada arquivo é um conjunto de objetivos iniciais para os planos modulados de um sítio. **São um ponto de partida
tirado de protocolos publicados (RTOG/NRG, QUANTEC, ESTRO); cada clínica deve revisar e aprovar.** Para mudar ou
acrescentar, copie o arquivo para `<pasta de dados>/objetivos_sitio/` (mesmo `id` substitui o de fábrica).

* `palavras`: termos que o assistente reconhece na descrição do caso (sem acento, minúsculas).
* `orgaos`: chave = nome TG-263; `{ipsi}`/`{contra}` = lado do alvo (L/R). Cada limite usa a notação das
  restrições: `Dmax`/`D0.03cc`/`D<n>cc` → objetivo de máximo; `Dmean` → média; `V<n>Gy` (% do volume) e
  `V<n>Gy_cc` (cm³) → DVH. Na otimização entram com folga (máximo a 95%, média e volume a 90% do limite).
* `essenciais`: órgãos que o assistente pede para contornar (por IA, se instalada) quando faltam.
* `dose_fracao_ref`: fracionamento dos limites; outro fracionamento gera um aviso (não converte).
