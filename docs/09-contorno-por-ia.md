# Contorno automático (BODY, limiar de HU e IA)

| Ferramenta | Como funciona | Onde |
|---|---|---|
| BODY | limiar −350 HU por corte, maior componente, buracos preenchidos | Estruturas › Contorno automático |
| Pulmões (E/D) | ar < −400 HU dentro do corpo, 3 maiores componentes; traqueia (componente < 7 cm² na linha média) retirada; lados pela linha média do corpo (HFS) | idem |
| Ossos | > 200 HU, medular preenchida por corte | idem |
| Órgãos por IA | TotalSegmentator (Wasserthal et al., Radiol AI 2023, Apache-2.0), modelo `total --fast` só com as classes pedidas, resultado em RTSTRUCT lido pelo TPS | idem |

Todo contorno automático é **rascunho**: o físico/médico revisa e corrige com as ferramentas de contorno.

## Instalar a IA no computador do TPS

1. Instale o Python 3.12 (python.org, marcar "Add python.exe to PATH").
2. Menu Iniciar › TPS › **Instalar contorno por IA (TotalSegmentator)** (pede administrador). Com placa NVIDIA, rode o
   script com `-GPU`. Ocupa ~2 GB (CPU) ou ~6 GB (GPU) em `C:\ProgramData\TPS\ia\venv`; precisa de internet uma vez.
3. Reabra o TPS. Outra instalação pode ser apontada em `C:\ProgramData\TPS\ia\config.yaml` (`executavel: ...`) ou
   na variável `TPS_TOTALSEG`.

Tempo típico na CPU (modo rápido, 3 mm): 1–3 min por CT. Limitações do modelo `total`: não há classe "reto" (use o
contorno manual ou o cólon como ponto de partida); "cabeça femoral" = 7 cm superiores do fêmur segmentado.
