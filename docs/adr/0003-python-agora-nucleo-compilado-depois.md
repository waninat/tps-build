# ADR 0003 — Python agora, núcleo compilado (C++/CUDA) depois

Data: novembro de 2026. Situação: aceita.

## Contexto
O código herdado é Python + numba. Um TPS comercial precisa de cálculo rápido (otimização IMRT/VMAT recalcula a dose
centenas de vezes) e de distribuição protegida.

## Decisão
- Fases 0–2: Python 3.12+, numba para os laços de cálculo. A arquitetura separa o núcleo de cálculo (funções puras
  sobre arrays) do resto, para que ele possa ser portado sem mexer nas camadas de cima.
- Fases 3–4: porte do motor e do otimizador para C++/CUDA (ou CuPy/numba-cuda como passo intermediário), com os
  testes Python atuais como referência: a versão GPU tem de reproduzir a versão CPU dentro de tolerância definida.
- Interface: decisão adiada para a Fase 2 (desktop Qt × web cliente-servidor). Preferência atual: cliente-servidor
  (cálculo num servidor com GPU, estações leves), coerente com o SecDose e o QualiRT.
