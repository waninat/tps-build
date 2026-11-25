# Guia de teste da interface — versão 0.2.0

Primeira versão da tela do TPS, para teste com fantomas e casos anonimizados. **Não usar em paciente.**

## Instalar e abrir (Windows)
1. Instale o Python 3.12 ou 3.13 (python.org), marcando "Add python.exe to PATH".
2. Descompacte o TPS numa pasta sua (ex.: `C:\TPS`).
3. Dê dois cliques em `iniciar.bat`. Na primeira vez ele cria o ambiente, instala as dependências (alguns minutos)
   e gera os casos de teste em `casos_teste\`. Depois abre o navegador em http://127.0.0.1:8770.
4. O primeiro cálculo de dose demora mais (o motor é compilado na primeira execução); os seguintes são rápidos.

Opções: `iniciar.bat --porta 8780`, `iniciar.bat --rede` (aceita outros computadores da rede — só dentro da VPN),
`iniciar.bat --home D:\TPS_dados` (pasta de planos e exportações).

## Roteiro sugerido
### A. Caixa 4 campos (casos_teste\cilindro)
1. **Caso** → cole o caminho da pasta → *Carregar pasta*.
2. **Máquina e prescrição** → Precise, 50 Gy / 25 fx, alvo `PTV_fantoma` → *Criar plano*.
3. **Campos** → ponto de encontro *Área 50%* → *Caixa 4*.
4. **Cálculo** → marque *otimizar pesos* → *Calcular dose*.
5. Confira: isodoses nos três cortes, DVH, tabela de UM com a conferência por tabela (deve ficar em ±1%).
6. Clique num campo na tabela: o BEV mostra lâminas, jaws e as estruturas projetadas; marque *DRR*.
7. Arraste a ponta de uma lâmina no BEV: a mudança é conferida (gap, overtravel, não-interdigitação do MLCi) e a
   dose fica "desatualizada" até recalcular.
8. **Aprovação** → usuário → *Aprovar* → *Exportar RTPLAN*. Os arquivos aparecem como links; o relatório abre em
   *Relatório* (imprimir/salvar PDF pelo navegador).

### B. Compensador sem cunha (casos_teste\separacao_variavel)
Separação AP de 12 cm ("pescoço") a 26 cm ("tórax"), PTV na linha média, medula e vértebra posteriores.
1. Carregar, Precise, 40 Gy / 20 fx, alvo `PTV`, arranjo *AP/PA*, calcular: HI ≈ 0,20 (dose cai ao longo do campo).
2. Marque *compensador de fluência* → *Calcular dose*: HI ≈ 0,12. O BEV mostra os segmentos (barra "segmento").
3. Compare com *field-in-field automático* no mesmo plano (desmarque o compensador): ganho menor.
4. Exportação: *um campo por segmento* (padrão, funciona em qualquer R&V) ou *step-and-shoot* (um campo com
   segmentos — **precisa ser testado no Mosaiq 2.83 antes de qualquer uso**).

### C. Plano 2D sem CT
1. **Caso** → aba *2D (sem CT)* → elipse 34 × 22 cm → *Criar fantoma virtual*.
2. Prescrição 20 Gy / 5 fx, normalização *Ponto* (0, 0, 0) → *Criar plano*.
3. **Campos** → modo *Retangular*, X1 = X2 = 4 cm, Y1 = Y2 = 7,5 cm → *AP/PA* → calcular. A conferência por tabela
   fica em ±1%.
4. O RTPLAN 2D sai sem CT (geometria do equipamento). **Testar no Mosaiq se ele aceita**; se não, eu passo a
   exportar o fantoma como CT.

### D. Com um caso real anonimizado
Exporte do Monaco/CT a série de CT **com o RTSTRUCT** na mesma pasta (HFS). Planos 3D convencionais (mama
tangente, pelve 4 campos, paliativos) servem para comparar UM e DVH com o Monaco.

### E. IMRT e VMAT (versão 0.8)
**Objetivos** (comuns aos dois): tipo *alvo*, *mín*, *máx*, *média*, *DVH*, *serial* (gEUD, expoente k) e
*paralelo* (no máximo v% acima da dose). A coluna **R** marca o objetivo como *restrito* (estilo Monaco: o
otimizador cumpre e busca o melhor alvo possível); sem R ele é ponderado (estilo Eclipse). *Sugerir objetivos*
monta a lista pelas restrições do protocolo.
**Avoidance** (estilo Eclipse): estrutura + *entrada*, *saída* ou *ambos*. **Auto-flash** (estilo Monaco, para
mama): margem fora da pele onde o alvo encosta nela.

IMRT step-and-shoot:
1. Técnica *IMRT* → *Campos equiespaçados* (ex.: 7 campos, início 0°, margem 5 mm).
2. Objetivos → *Otimizar IMRT*. A barra mostra o andamento; no fim aparecem segmentos, UM e as restrições.

VMAT (Precise e Synergy):
1. Técnica *VMAT* → *Arcos*: nº de arcos, início/fim (cheio = 181→179), sentido, colimador por arco
   (ex.: "10, 350") e margem → *Criar arcos* (substituem os campos).
2. *Setor* 40° (padrão), *PC a cada* 2° (ou 1°: mais modulação, cálculo mais lento), iterações 2–5.
3. *Otimizar VMAT*: aparecem UM e tempo de entrega estimado de cada arco. O checklist confere colisão a cada 5°,
   pontos de controle por arco e o tempo.
4. Exportação: um feixe **DYNAMIC** por arco. **Testar no Mosaiq 2.83 antes de qualquer uso clínico.**

**Dose final** (4 · Cálculo): *automático* (VMAT: Monte Carlo; o resto: collapsed cone), *collapsed cone* ou
*Monte Carlo*, com a incerteza-alvo (1% padrão) e *dose na água* (padrão: dose no meio, como o Monaco). Os
otimizadores iteram com o collapsed cone e a dose final é recalculada pelo MC; a primeira vez com um modelo de feixe
faz a calibração do MC (alguns minutos). O aviso mostra a incerteza obtida, as histórias e o tempo; o checklist
marca a incerteza e o comissionamento provisório do MC. Detalhes em 10-monte-carlo.md.

QA de paciente (IMRT e VMAT): *QA de paciente* → fantoma e modo (*ângulos reais* ou *gantry 0*). Saem o RTPLAN de
verificação, o RTDOSE no fantoma e o plano coronal (CSV). No VMAT em *gantry 0* o RTPLAN é o arco real (detector
preso ao gantry) e só o cálculo junta os pontos no gantry 0.

**Pendências a confirmar na máquina/Monaco** (`dados/maquinas/*/maquina.yaml`, marcadas CONFIRMAR): gap mínimo do
MLCi (hoje 5 mm — é o que mais limita o VMAT no Precise), máximo de pontos de controle por arco, UM mínima por
grau, velocidade das lâminas e taxa de dose.

### F. Tela no estilo Monaco (versão 0.9)
* **Menu** (Arquivo, Plano, Ferramentas, Exibir, Ajuda) e **barra de ferramentas**: salvar (Ctrl+S), calcular/otimizar
  (F5), aprovar, exportar, relatórios, CT (janela/nível), isodoses.
* **Painel esquerdo em abas**: *Paciente*, *Prescrição*, *Feixes*, *Otimização*, *Aprovação*. O botão de cálculo e a
  escolha da dose final ficam sempre visíveis no rodapé do painel.
* **Divisórias arrastáveis** entre os painéis e entre as vistas; *arrastar o título* de uma vista sobre outra troca as
  duas; *duplo clique* no título maximiza. Exibir › layouts (axial grande, grade 2×2, só a vista principal) e
  *Restaurar divisórias*. O arranjo fica guardado neste computador.
* **CT**: predefinições (tecidos moles, abdome/pelve, mediastino, pulmão, osso, cérebro, cabeça e pescoço, fígado,
  mama, larga), barras de *contraste* e *brilho*, e botão direito + arrastar na imagem (←→ contraste, ↑↓ brilho),
  nas três vistas.
* **Isodoses** (aba à direita ou ⚙): curvas relativas (% da prescrição ou do máximo) e **absolutas em Gy** (ex.: 40 Gy),
  tracejadas, juntas. Atalho na barra: digite a dose e *+ Gy*.
* **Apagar plano**: *Apagar…* no topo (plano aberto) ou na lista *Abrir plano*. Pede a senha; o arquivo vai para
  `planos/lixeira` da pasta de dados (recuperável) e sai da lista. Plano aprovado precisa ser desaprovado antes.
* **Objetivos agrupados por estrutura**: *+ estrutura…* acrescenta a estrutura uma vez; *+ objetivo* na linha dela
  acrescenta mais objetivos.
* **Protocolo do sítio**: objetivos de partida por sítio (pelve ginecológica, reto, próstata, mama, pulmão, esôfago,
  cabeça e pescoço, encéfalo — RTOG/NRG/QUANTEC, a revisar; arquivos em `dados/objetivos_sitio`, a clínica pode
  substituir em `<pasta de dados>/objetivos_sitio`). *Sugerir pelas restrições* agora refaz a lista (antes devolvia
  a gravada).
* **Assistente do caso**: descreva o caso (sítio, doses, frações, lado, cirurgia). Ele lê a TC e as estruturas (alvos e
  doses pelos nomes, lado, órgãos perto do alvo, sobreposição com o PTV, pele), aponta os órgãos que faltam (e
  contorna por IA, se instalada), propõe os objetivos e, na mama/pulmão, arcos parciais do lado do alvo. São regras
  explicadas, não uma IA generativa.
* **Contorno por IA**: Ferramentas › *Contorno por IA…* instala o TotalSegmentator pelo próprio TPS (CPU ou GPU;
  precisa de internet e, no Windows, do Python 3.10–3.12) ou aponta um já instalado.

## Integração com o Mosaiq 2.83 (pendente de teste)
- Em *Enviar ao Mosaiq*, informe IP, porta e AE Title do destino DICOM do Mosaiq (a TI ou a Elekta informam).
  O envio usa C-STORE; a importação no Mosaiq segue o fluxo normal de RTPLAN.
- Nome da máquina: o RTPLAN usa o `dicom_machine_names` de `dados/maquinas/<máquina>/maquina.yaml`. Para o Precise
  ainda está `CONFIRMAR` → o TPS usa `RADIOSERRA_PRECISE`. **Coloque o nome exato do Mosaiq antes de testar.**
- O que conferir no Mosaiq de teste: máquina, energia, UM de cada campo, gantry/colimador/mesa, jaws, lâminas
  (banco A/B e ordem dos pares), SSD, isocentro, prescrição, estado de aprovação, e para campos com segmentos:
  número de segmentos e UM de cada um.

## O que esta versão ainda não tem
Registro (fusão) de imagens, RM e PET, elétrons, blocos (Mevatron) e cálculo por Monte Carlo. A dose é calculada
pelo collapsed cone; o comissionamento ainda depende dos dados medidos pendentes (doc. 06).

## Como relatar problemas
Anote o que fez, o que esperava e o que aconteceu; mande o print e, se houver, o texto vermelho que aparece embaixo
da tela. O registro de ações fica em *Registro*, no painel da direita.
