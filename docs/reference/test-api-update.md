# Testar manualmente a atualização da API

Este roteiro usa a API e a conta do checkout. A reinstalação é real: baixa a
versão mais recente disponível da WPPConnect e pode interromper a conexão
enquanto instala. O roteiro não altera números de versão nem inicia o aplicativo.

## Instalação e validação

1. Encerre outras instâncias do WinZapp que usem a mesma instalação e termine
   eventuais chamadas.
2. Na raiz do repositório, execute `uv run winzapp` e espere a conta conectar.
3. Em **Ajuda**, escolha **Forçar reinstalação da WPPConnect**.
4. Confirme a reinstalação. Ela ocorre mesmo se a versão oferecida já estiver
   instalada; esse comando chama o mesmo fluxo de atualização do verificador.
5. Aguarde o download, a instalação e o reinício da API. A conclusão só deve
   ser anunciada depois que `/healthz` e `/winzapp/identity` forem validados.
6. Confira se a conta reconecta e se uma mensagem pode ser enviada e recebida.

A instalação é preparada em `api_staging` (ao lado da pasta da API). Durante a troca, a versão
anterior fica em `api_old`. Depois da validação, ela é renomeada para
uma pasta exclusiva e removida em segundo plano após a reconexão (com limite
de 90 segundos de espera).
Se a nova API não iniciar ou não passar na validação, o fluxo tenta restaurar
a anterior. Se a restauração também falhar, as pastas são mantidas para
recuperação. Não as remova manualmente durante o teste.

## Cancelamento

Com o download em segundo plano desativado nas configurações, repita o
comando do menu e cancele pela janela de progresso. A versão anterior deve
continuar disponível, ser reiniciada e reconectar, sem anúncio de atualização
concluída. Não encerre processos pelo Gerenciador de Tarefas para simular isso.

## Segundo plano

Ative a opção de download de atualizações em segundo plano e repita a
reinstalação. A API atual deve continuar funcionando durante a preparação;
a interrupção ocorre na troca e no reinício. Restaure depois a configuração
de sua preferência.

## O que registrar

Se houver falha, guarde `log.log` da execução antes de abrir o aplicativo
novamente: ele é truncado a cada inicialização. Registre também a etapa,
a mensagem exibida e se a conta reconectou. No log, procure os marcadores
`[wpp_update]` e `[api-staging]`.

### Medir o tempo de cada etapa

Reabra o WinZapp para carregar a instrumentação antes da próxima reinstalação.
O `log.log` registra `[api-timing]`, com `step`, `event=start/end` e
`elapsed_s` medido por relógio monotônico. Isso separa download, extração,
dependências, patches, cópia do navegador, build, parada/liberação do perfil,
troca, inicialização da API, validação HTTP, limpeza da versão anterior e
reconexão do WhatsApp. Uma etapa que retorna `false` ou lança uma exceção
também registra sua duração. `returned` indica retorno da função; não
significa que a conta já esteja conectada. O fim de `setup_worker` mede apenas
o instalador, sem incluir reinício e reconexão.

Os subprocessos npm recebem `npm_config_timing=true` e preservam até 50 logs.
O npm grava os detalhes em arquivos `*-timing.json` no diretório
`data/global/npm/cache/_logs` da instalação (`client/data/global/npm/cache/_logs`
no checkout). O cache e os logs ficam isolados do npm pessoal do usuário.
Os comandos internos `build:types` e `build:js`
também geram arquivos próprios, permitindo separar TypeScript e Babel.
Guarde esses arquivos junto ao `log.log` antes de repetir muitas instalações.
As etapas de instalação não registram argumentos de comandos nem tokens.

O `wppconnect.log` registra `[node-startup]` para os principais carregamentos
internos do Node. O cache de compilação fica em `data/global/node-compile-cache`,
fora da pasta da API, e pode ser reaproveitado após reinstalar arquivos de
conteúdo idêntico. Compare a primeira abertura, a segunda e uma reinstalação;
a primeira geração desse cache pode acrescentar custo.

Durante `server_modules`, `[node-load]` detalha o tempo total, CPU do processo
e tempo nas chamadas síncronas `fs.readFileSync`. As dez bibliotecas com maior
tempo próprio aparecem como `module`, `self_s` e `calls`; o tempo dos imports
filhos é descontado para não contar a mesma espera duas vezes. Nomes de
bibliotecas são registrados sem caminhos, argumentos ou dados da conta.
Os hooks são removidos antes de iniciar o servidor. `read_s` é parte do tempo
total, não uma etapa adicional; ele não inclui todas as operações do sistema
de arquivos. O restante inclui resolução, compilação e execução dos módulos,
e não permite atribuir sozinho uma demora ao antivírus. Esta medição também
tem um pequeno custo de instrumentação.

Este roteiro verifica instalação, reconexão e cancelamento. A recuperação
após uma falha da nova API é coberta pelos testes automatizados; uma
reinstalação bem-sucedida não verifica manualmente esse caso.

Não diminua `client/api/package.json` abaixo de
`client/wpp_minimum_version.txt`: isso aciona a verificação obrigatória da
inicialização, que usa outro caminho de instalação. Para testar o fluxo com
backup e validação descrito acima, use o comando do menu.
