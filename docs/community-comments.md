# Respostas aos avisos de comunidades

Leitura e envio de respostas nativas aos avisos, pela interface acessível do
WinZapp. A integração provisória usa patches enquanto o suporte público
proposto em WA-JS, WPPConnect e WPPConnect Server não estiver publicado.

## Testar no WinZapp

Depois de preparar a API com `uv run setup-api`, feche o WinZapp e
abra novamente a partir deste checkout (`uv run winzapp`). O processo antigo
não incorpora mudanças de Python nem novas rotas de Node.

1. Abra o grupo de avisos de uma comunidade e selecione um aviso, inclusive
   uma mensagem de voz.
2. Abra seu menu de contexto e escolha **Respostas ao aviso da comunidade**.
3. Navegue pela lista com as setas. Cada resposta mostra autor, horário e texto. Nomes salvos têm prioridade;
   nomes de perfil são usados quando necessário. Respostas próprias respeitam
   a configuração "Como se referir a mim?".
   **Atualizar respostas** consulta novamente o histórico sincronizado.
4. Escreva no campo de resposta e use **Enviar resposta** ou **Ctrl+Enter**.
   **Enter** sozinho continua inserindo uma linha no campo.
5. Ao navegar pelos avisos, a contagem conhecida de respostas é anunciada no
   final, depois do conteúdo da mensagem. Abrir ou atualizar as respostas também atualiza essa contagem.
6. **Escape** fecha a janela. Atualizações preservam a resposta selecionada.

Chats antigos podem não ter a identificação de comunidade no cache. Nesse
caso, a opção também pode aparecer em um grupo comum: a API verifica o grupo
real e informa que não é um aviso de comunidade antes de habilitar o envio.

## Limites e contrato

- A leitura usa o histórico sincronizado no dispositivo vinculado. Respostas
  existentes apenas no telefone podem não aparecer. Não marca respostas lidas.
- Respostas apagadas e ainda criptografadas têm rótulos próprios, sem texto
  antigo. Identificadores internos e material de criptografia não são exibidos.
- Um comentário é enviado pelo remetente nativo de comentários. Não entra na
  fila de mensagens normais. Apenas `messageSendResult: "OK"` confirma o envio.
- Falha ou timeout mantém o texto e bloqueia outra tentativa até uma atualização
  explícita. Confira a lista antes de tentar outra vez: o envio pode ter ocorrido.
- A janela usa atualização manual; ainda não consome os eventos de comentários
  adicionados ao WPPConnect upstream.

## Integração provisória da API

`client/api_patches/src/util/communityCommentsRuntime.ts` usa primeiro a API
pública nova do WA-JS, quando disponível. Para o par homologado atual, usa os
módulos nativos observados no WhatsApp Web. O componente de comentários é
carregado sem abrir uma janela do navegador. As versões npm homologadas
permanecem iguais. O adaptador pode ser removido após a adoção da versão upstream.

As rotas autenticadas usam GET e POST em
`/api/:session/message-comments/:messageId`; o POST recebe `{text: "..."}`.
O adaptador e o controlador são restaurados pelos mesmos caminhos de setup e
build usados nos demais patches. Não edite `client/api/` manualmente.

## Validação realizada

- Testes de contratos, segurança dos dados, falhas de envio,
  callbacks de janela encerrada e seleção preservada, usando widgets simulados.
- Traduções completas nos sete idiomas cadastrados; MO e mapa de chaves compilados.
- Teste manual com NVDA, incluindo envio real pelo WinZapp. A consulta posterior
  encontrou sete respostas, incluindo a enviada pelo usuário, identificada como própria.

As verificações automatizadas não enviaram respostas reais. O teste manual usou o adaptador
nativo provisório do WinZapp; a cadeia completa dos pacotes upstream ainda
precisa de validação.

Falhas de leitura e envio registram apenas códigos estáticos em `log.log`, com
o prefixo `[community_comments]`. Não registram texto, autores ou identificadores.
