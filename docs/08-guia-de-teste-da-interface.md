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

## Integração com o Mosaiq 2.83 (pendente de teste)
- Em *Enviar ao Mosaiq*, informe IP, porta e AE Title do destino DICOM do Mosaiq (a TI ou a Elekta informam).
  O envio usa C-STORE; a importação no Mosaiq segue o fluxo normal de RTPLAN.
- Nome da máquina: o RTPLAN usa o `dicom_machine_names` de `dados/maquinas/<máquina>/maquina.yaml`. Para o Precise
  ainda está `CONFIRMAR` → o TPS usa `RADIOSERRA_PRECISE`. **Coloque o nome exato do Mosaiq antes de testar.**
- O que conferir no Mosaiq de teste: máquina, energia, UM de cada campo, gantry/colimador/mesa, jaws, lâminas
  (banco A/B e ordem dos pares), SSD, isocentro, prescrição, estado de aprovação, e para campos com segmentos:
  número de segmentos e UM de cada um.

## O que esta versão ainda não tem
Contorno (as estruturas vêm do RTSTRUCT), registro de imagens, edição de jaws com o mouse, pontos de referência,
soma de planos, campos de setup, bolus, elétrons, usuários com senha e auditoria em banco. A dose é calculada pelo
collapsed cone com o modelo v2 do Precise; perfis medidos ainda não entraram no comissionamento.

## Como relatar problemas
Anote o que fez, o que esperava e o que aconteceu; mande o print e, se houver, o texto vermelho que aparece embaixo
da tela. O registro de ações fica em *Registro*, no painel da direita.
