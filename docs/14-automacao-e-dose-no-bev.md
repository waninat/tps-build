# 14 — Fluxo automático (TC → órgãos → aprovação dos alvos → plano) e dose no BEV

Não clínico. Tudo desligado por padrão.

## Dose em colorwash no BEV
No BEV, ao lado do DRR: **dose: máx. no raio** (a dose máxima ao longo de cada raio dentro do corpo — mostra atrás
de qual lâmina está o ponto quente, para fechá-la ali) ou **dose: plano do iso** (dose no plano perpendicular ao
feixe pelo isocentro). "A partir de %" esconde o que está abaixo (ex.: 105% mostra só os pontos quentes) e dá para
restringir a uma estrutura (ex.: só os pontos quentes dentro do PTV, ou a dose máxima num órgão).

## Fluxo automático (Ferramentas › Automação)
1. **Recepção:** uma pasta de entrada (onde o tomógrafo/PACS/Mosaiq grava) e/ou o **receptor DICOM do TPS**
   (porta e AE configuráveis; o tomógrafo envia direto). Quando a série termina de chegar, vira um **caso** em
   `<dados>/casos/` (os originais não mudam).
2. **Contorno:** BODY por limiar; o sítio é identificado pela descrição do exame ou pela anatomia (pulmão no corte,
   tamanho da secção, sexo); órgãos do sítio por IA (TotalSegmentator, se instalado), suavizados. Sai um
   `RS_auto_*.dcm` no caso.
3. **Alvos:** o médico desenha os PTVs com a prescrição no nome — `PTVPelve45@25`, `PTV_Mama_50@25`, `PTV70@35`
   (dose ≥ 100 = cGy). Pode ser no TPS ou em outro sistema (o RTSTRUCT que chega na pasta entra no caso).
4. **Gatilho:** a **aprovação das estruturas** (aba Estruturas › *Aprovar estruturas…*, com senha de médico ou
   físico; ou o RTSTRUCT recebido já aprovado) — ou, se preferir, só a presença de PTVs com prescrição.
5. **Plano:** um por grupo de PTVs com o mesmo número de frações (boost integrado num plano só); o **nome do plano é o
   nome do PTV** de maior dose. Técnica por sítio (padrão VMAT; mama e encéfalo em 3D pelo modelo do sítio), objetivos
   pelo Claude (ou pelas regras), cálculo, plano salvo e, se ligado, **parecer do Claude** (só avalia).
6. **Físico:** Arquivo › *Casos recebidos* mostra cada caso, o estado, os PTVs, os planos (técnica, HI, parecer) e abre
   o plano para avaliar. Nada é aprovado nem exportado automaticamente.

Também: abrir uma TC sem RTSTRUCT agora funciona (BODY automático); com vários RTSTRUCT na pasta vale o aprovado
mais recente (senão o mais recente).

Limitações: a identificação do sítio pela anatomia é simples (descrição do exame tem prioridade); sem a IA
instalada sai só o BODY (e pulmões no tórax); um plano VMAT com Monte Carlo pode levar bastante tempo e roda em
segundo plano no mesmo computador.
