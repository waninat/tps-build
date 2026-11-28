# 15 — Visualizador (zoom, régua, HU, 3D), estruturas aprovadas, posicionamento e planos automáticos por técnica

Não clínico.

## Cortes (axial, coronal, sagital)
- **Botão direito sem arrastar** abre o menu da vista. Ele tem:
  - zoom +/−, restaurar, centralizar aqui e mover (pan);
  - maximizar a vista, cruz aqui, ir para o isocentro;
  - régua, cursor de HU, HU e densidade do ponto;
  - isocentro do plano aqui, referência (marcas/BB) aqui.
- Botão direito arrastando continua sendo janela/nível.
- **Ctrl+roda** dá zoom em volta do mouse; **Ctrl+arrastar** move a imagem (pan). Na barra, ⤢ restaura o zoom.
- **Régua (📏):** clique e arraste num corte. Mostra a distância em mm. Esc apaga.
- **Cursor HU:** mostra HU, densidade eletrônica e dose (Gy e %) sob o mouse, mais a posição em cm.
- **Campos nos 3 cortes** (marcador *campos* na barra):
  - bordas da fonte aos cantos da abertura;
  - se o feixe atravessa o plano do corte (ex.: AP no coronal), a secção do feixe naquela profundidade;
  - um losango na entrada do eixo central na pele.
- **Estruturas sólidas:** na barra, *Estr.: contorno / sólido*, com transparência de 0 a 100%. Vale para os cortes e para o BEV; o BODY fica só delineado.

## Estruturas
- **⌖** leva às 3 vistas no centro (centroide) da estrutura. **ⓘ** mostra volume, extensão, HU médio ± dp e densidade eletrônica média.
- **Densidade:**
  - *forçar*: a estrutura toda com o valor;
  - *preencher*: só onde o CT é menos denso (ar/gás), como "fill" no Monaco. O resto mantém o CT.
- **Nova estrutura por margem:**
  - *margem simétrica* (um valor para os 6 lados);
  - *recortar da pele*: tira o que sai do BODY, com uma distância opcional da pele em mm.
- **Aprovadas:**
  - Com as estruturas aprovadas (🔒), não dá para apagar nem alterar nenhuma delas.
  - *Desaprovar…* pede a senha e o motivo. Fica registrado quem desaprovou, quando e por quê, e o RTSTRUCT aprovado é guardado no caso.
  - No topo, o botão "✔ Estruturas aprovadas" também desaprova.

## Plano
- **Renomear:** escreva o novo nome no Rótulo e clique em ✎.
- **Isocentro:**
  - na cruz;
  - no centro de qualquer estrutura;
  - em x/y/z digitados;
  - com arredondamento opcional (ex.: 5 mm).
  - Os campos conformados são refeitos no novo isocentro.
- **Referência (marcas/BB):**
  - *referência = cruz*, ou *achar marcadores* (objetos > 1500 HU pequenos na pele).
  - Os deslocamentos da referência ao isocentro aparecem em cm com o sentido (lateral, vertical, longitudinal) e vão para o relatório.
- **Relatório do plano:** traz, por padrão:
  - prescrição e fracionamento;
  - UM por campo e total;
  - tempo de feixe por campo e total;
  - início e fim e sentido dos arcos;
  - SSD;
  - os 3 cortes no centro do PTV;
  - DVH;
  - índices;
  - posicionamento;
  - quem aprovou as estruturas e o plano, e quando.
- **Aba Avaliação › índices:**
  - HI (ICRU 83);
  - CI (RTOG);
  - Paddick;
  - GI;
  - R50%;
  - D2cm;
  - V95/V100;
  - D95/D98/D2/D50.

## BEV
- **Arrastar lâmina** pegando em qualquer ponto dela. Uma posição proibida (overtravel, fim de curso, gap) não move: o aviso diz o motivo.
- **Cortes:** linhas tracejadas na altura dos cortes axial (amarelo), coronal (azul) e sagital (rosa) mostrados.
- **Botão direito › Vista 3D:**
  - estruturas em superfície (BODY translúcido);
  - campos (pirâmide da fonte, abertura no plano do isocentro) e isocentro;
  - girar arrastando; mover com Shift ou o botão direito; zoom com a roda;
  - vistas Ant/Post/Dir/Esq/Sup.
  - A janela se arrasta e muda de tamanho.

## Copiloto
Arraste pelo título e mude o tamanho pelo canto. A posição e o tamanho ficam guardados.

## BODY automático
Os "riscos" vinham do colchão ou da mesa, que encostam no paciente em alguns cortes e ficam soltos nos outros. Agora, em cada corte, saem as ilhas com menos de 8 mm de espessura.

## Plano automático: 3D e VMAT, até 3 tentativas
- **Um plano de cada técnica marcada** (padrão 3D e VMAT, com nomes `<PTV>_3D` e `<PTV>_VM`). Os nomes respeitam o limite de 16 caracteres. O físico escolhe entre eles em *Casos recebidos*.
- **Tentativas** (até 3):
  - A 1ª é simples: modelo do sítio, ou objetivos da IA/regras.
  - As seguintes corrigem o que faltou:
    - cobertura: margem maior no 3D; DVH mínimo e peso maior no alvo no VMAT;
    - ponto quente: field-in-field com mais níveis e ajuste fino; máximo no alvo;
    - órgãos acima do limite: objetivo a 90% e depois 80% do limite, com peso ×2 e ×3.
  - Fica a tentativa de menor penalidade, que soma:
    - V95 < 95% em cada alvo;
    - Dmax > 107%;
    - limites e objetivos que falharam.
  - O caso mostra a nota de cada tentativa.
- **Mama:**
  - 3D por tangentes com ângulo automático, flash e filtro;
  - VMAT com arcos parciais pelo lado da mama;
  - **PTV de FSC/axila** (nome com FSC, SCF, supra, axila…) no mesmo grupo:
    - junção mono-isocêntrica: isocentro na junção, tangentes com Y2 fechado e FSC com Y1 fechado;
    - campo **AP** direto e, se a cobertura ficar baixa, **AP + PA**;
    - os pesos levam a dose média da FSC à prescrição dela.
- **Sítio pelo nome do exame:**
  - CP, C.P., cabeça e pescoço;
  - crânio, SNC;
  - pelve, PEL;
  - mama, MD, ME;
  - tórax, mediastino;
  - próstata.
- **Segmentação na GPU ou na CPU:**
  - Ferramentas › Segmentação automática › *Rodar na*: automático, GPU (NVIDIA/CUDA, com a versão GPU instalada) ou CPU.
  - O cálculo de dose é sempre na CPU, com todos os núcleos.
