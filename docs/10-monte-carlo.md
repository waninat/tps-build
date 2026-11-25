# 10 — Cálculo de dose por Monte Carlo (SRS-MC)

Situação em 25/11/2026: implementado e integrado; **comissionamento provisório** (ajustado ao modelo CCC, não a
medidas). Não liberado para uso clínico.

## Papel no sistema
* **Dose final do VMAT**: padrão Monte Carlo (incerteza-alvo 1%, dose no meio). As otimizações (IMRT, VMAT,
  field-in-field, proteção de OARs) iteram com o collapsed cone, como no Monaco; ao fim, a dose de cada campo é
  recalculada pelo MC e é ela que normaliza o plano, entra no DVH, no RTDOSE, na conferência de UM e no QA.
* Qualquer plano pode escolher *Dose final: collapsed cone / Monte Carlo* (tela: 4 · Cálculo).
* Recalcular sem mudar os campos reaproveita a dose do MC (cache por conteúdo do campo, modelo, densidades e
  incerteza).

## Física (tps/mc/fisica.py, tps/mc/transporte.py)
* Materiais no estilo XVMC/VMC++: água com a densidade eletrônica η do voxel e fatores por material (ar, pulmão,
  tecido, osso, interpolados em η) para pares (∝ Z), colisão, radiativo e comprimento de radiação. A massa do voxel
  é ρ = η·(ρ/η)_material.
* Fótons: rastreamento de Woodcock; Compton por Klein–Nishina (amostragem de Kahn), pares (divisão uniforme da
  energia), fotoelétrico/Rayleigh absorvidos no local; corte 20 keV.
* Elétrons/pósitrons: história condensada classe II — colisão restrita (Bethe, ICRU 37, efeito de densidade de
  Sternheimer), radiativo (ESTAR), raios δ (Møller, termo 1/T²) acima de 200 keV, bremsstrahlung emitido como
  fóton, espalhamento múltiplo gaussiano (Highland) com dobradiça aleatória, passo ≤ 10% da energia e ≤ 0,6 voxel;
  corte 200 keV; aniquilação do pósitron.
* Gás e ar (ρ < 0,05 g/cm³): dose = energia/massa na vizinhança 3×3×3 (evita picos de massa ~0).
* Dose na água (opcional): D_w = D_m·(S/ρ)_w,m por material (osso ≈ 1,13).

Conferido contra os dados de referência: poder de freamento da água a ≤ 0,5% do ESTAR (0,01–10 MeV); Compton +
pares + resto a ≤ 1% do XCOM; livre caminho médio do Woodcock a ≤ 0,5% de 1/μ; fração média de energia do Compton
(0,34 / 0,53 / 0,64 em 0,5 / 2 / 6 MeV) conforme Klein–Nishina.

## Fonte e calibração (tps/mc/calculo.py)
* A fonte é o **mesmo modelo de feixe do CCC**: para cada segmento ou intervalo de arco, a fluência de energia no
  plano do isocentro (MLC/jaws, transmissões, ponta arredondada, tongue-and-groove, fonte focal borrada,
  extrafocal, horn) define de onde saem os fótons, a partir do ponto focal, com o espectro do modelo.
* O MC tem o **próprio endurecimento do espectro** (o k do CCC é efetivo: vem junto com a escala de espalhamento
  0,78 e compensa aproximações do kernel; no transporte físico penetra demais) e a **própria amplitude da
  contaminação eletrônica** (mesma forma analítica do CCC).
* Provisório (sem medidas): k ajustado pela inclinação do PDD do 10×10 do modelo comissionado de 5 a 25 cm; fator
  absoluto e amplitude da contaminação por mínimos quadrados no PDD do 10×10 (2,5–250 mm, peso maior no dmax).
  Resultado atual: k = 4,5 g/cm², contaminação = 0,80 × a do CCC. Fica em `<home>/mc/calibracao.json` por modelo.
* Com as medidas: `mc_spectrum_hardening_k` no modelo de feixe (ajuste aos PDDs medidos) e a calibração refeita.

## Incerteza
Lotes independentes; a incerteza (1σ) é a média nos voxels de tecido acima de 50% da dose máxima. O número de
histórias cresce até a incerteza pedida (o plano inteiro; cada campo usa a incerteza·√nº de campos). O relatório,
o checklist e a tela mostram a incerteza obtida e se a dose é no meio ou na água.

## Validação até aqui
| Teste | Resultado |
|---|---|
| PDD 10×10 e 3×3 (água, absoluto) × modelo comissionado | ±1,5% de 5 a 200 mm (3×3: −2,8% a 20 cm) |
| Fantoma, VMAT 1 arco | UM +1,6% × CCC; PTV D2/D98 52,2/48,4 × 51,9/48,9 Gy; conferência composta +0,2% |
| Laje de pulmão (η 0,26, 5–15 cm), 10×10 | MC 4–5% abaixo do CCC dentro do pulmão; +3 a +8% nas interfaces — esperado em MC, **a confirmar com medida** |
| Laje de osso (η 1,7) | MC (dose no meio) 5–9% abaixo do CCC — coerente com (S/ρ)_w,osso ≈ 1,11 |

| Pelve real, VMAT 2 arcos (mesmos arcos) | MC × CCC: UM +0,6%; PTV D98 42,4 × 42,7 Gy; reto/bexiga ±0,3 Gy; conferência +2,7% |
| Placas do Monaco (CC do Monaco como referência), descontado o desvio comum de ~2,3% antes da placa | pulmão: MC +0,3 a +1,9% (CCC +5 a +6%); osso: MC ≈ −2,5% (dose no meio) — falta o MC do Monaco e a medida |

Tempo (2 núcleos do servidor de desenvolvimento): ~0,7 milhão de histórias/s; fantoma VMAT a 1% em ~6 min; pelve VMAT
(2 arcos, grade 3 mm, 1,2 bilhão de histórias) em 55 min. Escala com os núcleos: num PC de 8–16 núcleos, ~2–5 min.
Próximo passo de desempenho: GPU (NVIDIA) e reaproveitamento de trajetórias de elétrons (até ~1,7×).

## Limitações conhecidas
* Sem transporte de partículas no cabeçote/MLC (o MLC entra pela fluência do modelo: transmissão, ponta, groove).
* Pares com divisão uniforme da energia; Møller só no termo dominante; espalhamento múltiplo gaussiano.
* Sem redução de variância além do Woodcock; CPU apenas (GPU é o próximo passo de desempenho, se houver NVIDIA).
