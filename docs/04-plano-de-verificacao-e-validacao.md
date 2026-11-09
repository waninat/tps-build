# Plano de verificação e validação — Fase 1

Versão 0.1 (rascunho), novembro de 2026.

Referências: IAEA TRS-430 (CQ de TPS), IAEA TECDOC-1583 (testes de comissionamento de TPS), AAPM TG-53, AAPM MPPG 5.b
(2023, comissionamento de algoritmos de dose), AAPM TG-219 (cálculo independente), IEC 62083:2025.

## 1. Verificação automática (CI, a cada commit)
- Testes unitários e de integração (`pytest`), incluindo o caso ponta a ponta em fantoma sintético.
- Conferência do RTPLAN (`exportacao/validacao_iod.py`); a acrescentar: `dciodvfy`.
- Gate: nenhum *merge* com teste vermelho.

## 2. Verificação do modelo de feixe (água)
| Teste | Configuração | Tolerância (MPPG 5.b / TRS-430) |
|---|---|---|
| PDD | 3×3 a 40×40, SSD 100 | 1% após dmax; 2 mm no buildup |
| Perfis | 3×3 a 40×40, 5 profundidades, inplane/crossplane | 2% no platô; 2 mm na penumbra; 3% fora do campo |
| Scp | 3×3 a 40×40 (e < 3×3 com detector adequado) | 1% (≥ 4×4); 2% (< 4×4) |
| OAR diagonal | 40×40, 5 profundidades | 2% |
| Campos retangulares/assimétricos | 5×20, 20×5, meio-campo | 2% / 2 mm |
| SSD estendida | SSD 80–120 | 2% |
| MLC (Roche et al. 2018) | transmissão, tongue-and-groove, ponta de lâmina, campos de teste do artigo | 2% na transmissão; 1 mm na ponta |

## 3. Heterogeneidade e geometria (TECDOC-1583)
- Fantoma CIRS 002LFC (ou equivalente) com câmara: 8 casos do TECDOC-1583 (campos abertos, oblíquos, bandejas,
  heterogeneidades); os casos com cunha do TECDOC ficam de fora (sem cunha no escopo).
- Campos com colimador e mesa girados, medidos com filme: confirma SRS-PLN-015 e R-04.

## 4. Comparação com o Monaco
- 20 casos 3D anonimizados (mama, pelve, paliativos, SNC) replanejados no TPS com os mesmos campos: diferença de UM
  e DVH; gama 3%/2 mm da dose 3D.
- Nota: a diferença com o Monaco é informativa, não critério de aceitação; o critério é a medida.

## 5. Integração com o Mosaiq (protocolo a escrever com a TI da clínica)
1. Comparar atributo a atributo um RTPLAN do TPS com um RTPLAN 3D do Monaco já aceito pelo Mosaiq.
2. Importar no Mosaiq de teste (paciente fictício). Conferir: nome da máquina, energia, UM, ângulos, jaws, lâminas
   (A/B e ordem dos pares), SSD, isocentro, prescrição, aprovação.
3. Entregar no acelerador em modo de serviço/QA, com fantoma. Conferir log (.trf) contra o plano.
4. Medir: ponto com câmara + plano com ArcCHECK/Delta4/Octavius; 3%/3 mm ≥ 95%.

## 6. Validação clínica (antes do primeiro paciente)
Ensaio de uso com físicos e dosimetristas da clínica, em casos reais (sem tratar com o plano do TPS), com o plano do
Monaco como referência, até a aceitação formal. Requisitos de investigação clínica: definir com o consultor
regulatório.
