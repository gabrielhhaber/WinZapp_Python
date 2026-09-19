# Como usar as skills do Claude Code no WinZapp

Um tutorial. Se você nunca usou uma skill, comece por aqui.

## Primeiro: dois tipos de skill

Existem skills que **entram sozinhas** e skills que **você chama**.

- **Automáticas** — o agente carrega quando a tarefa bate com a descrição.
  Você não digita nada. Exemplos: mexeu em texto da interface, a
  `i18n-ui-string` entra e ele já sabe dos cinco idiomas; escreveu um teste, a
  `write-test` entra. Todas as cinco skills do projeto são assim, e várias do
  Matt Pocock também (`tdd`, `code-review`, `diagnosing-bugs`, `prototype`,
  `research`, `domain-modeling`, `codebase-design`,
  `resolving-merge-conflicts`, `wizard`).
- **Manuais** — só rodam quando você digita `/nome`. O agente não pode
  chamá-las por conta própria. São as que **decidem** ou **publicam** algo:
  abrir issue, criar tickets, começar uma entrevista. Este documento é sobre
  elas.

Para ver a lista completa dentro do Claude Code, digite `/` e olhe as
descrições.

---

## O caminho de uma feature, do começo ao fim

Cinco comandos, nesta ordem. Os três primeiros na **mesma sessão**, sem
`/compact` nem `/clear` no meio — o spec e os tickets precisam nascer do mesmo
raciocínio da entrevista.

```
/grill-with-docs  →  /to-spec  →  /to-tickets  →  /implement (por ticket)  →  /code-review
```

### 1. `/grill-with-docs` — a entrevista

**O que faz:** entrevista você, em rodadas, sobre a ideia que você trouxe.
Pergunta o que você não colocaria no prompt: o que acontece no caso X, o que o
NVDA deve falar, o que **não** entra, qual comportamento quando está offline.
Ele para quando não sobra pergunta importante.

**Por que existe:** sem isso o agente preenche as lacunas com suposições
próprias — e depois você descobre no PR.

**O que deixa no repo:** termos novos vão para o `CONTEXT.md` (glossário do
projeto) e decisões difíceis de reverter viram um arquivo em `docs/adr/`. É o
que diferencia do `/grill-me`.

**Como usar:**
```
/grill-with-docs quero que a lista de conversas mostre quem está digitando
```
Responda as perguntas. Fatos sobre o código são obrigação dele descobrir;
decisões são suas. Se ele perguntar algo que você não sabe, diga que não sabe
— ele vai investigar ou registrar como pendência.

### 2. `/to-spec` — o documento do combinado

**O que faz:** pega tudo que foi decidido na entrevista e escreve um spec:
problema (do ponto de vista do usuário), solução, o que foi **recusado**, e
onde vão ficar os testes. **Não entrevista de novo.** Publica como issue no
GitHub com a label `ready-for-agent`.

**Por que existe:** congela o combinado. Uma sessão futura (ou outra pessoa)
lê a issue e sabe o que foi decidido e o que foi descartado.

**Como usar:**
```
/to-spec
```
Ele mostra os "seams" (onde pretende testar) e pede confirmação antes de
publicar.

### 3. `/to-tickets` — a divisão em partes

**O que faz:** quebra o spec em tickets pequenos. Cada ticket é uma **fatia
vertical**: tela + lógica + dados + teste + os cinco idiomas, entregável e
testável sozinha. Nunca "camada de dados" num ticket e "tela" em outro. Cada
ticket declara quais outros o bloqueiam. Publica um issue por ticket, em
ordem de dependência.

**Por que existe:** cada entrega pode ser testada de ponta a ponta na hora,
e cada `/implement` começa com contexto limpo lendo só o ticket dele.

**Como usar:**
```
/to-tickets
```
ou apontando para um spec já existente:
```
/to-tickets #240
```

### 4. `/implement` — a construção

**O que faz:** implementa **um** ticket. Escreve o teste antes do código
(`tdd` entra sozinha), roda testes de arquivo durante o trabalho e a suíte
completa no fim, e chama o `code-review` antes de terminar.

**Regras do WinZapp que valem aqui** (estão no `CLAUDE.md`, mas vale repetir):
- "Suíte completa" é `pytest` puro. **Nunca** `--run-wx-gui` — isso abre
  janela na tela de quem está usando leitor de tela.
- Ele **não commita** sem você pedir, mesmo a skill dizendo o contrário.

**Como usar:** uma sessão por ticket. Antes de cada um, `/clear`.
```
/implement #241
```

### 5. `/code-review` — a conferida de fora

**O que faz:** revisa o diff desde um ponto fixo (`main`, um commit, uma
branch) em dois eixos, cada um num sub-agente sem o viés de quem escreveu:
**Standards** (smells de código do livro *Refactoring*, mais os padrões
documentados do repo — aqui, `CLAUDE.md` e `docs/traps/`) e **Spec** (o
código faz o que a issue pediu?).

**Como usar:**
```
/code-review main
```
Esta é automática também — o `/implement` chama sozinho — mas vale chamar à
mão antes de abrir PR.

**Rode o `winzapp-reviewer` depois.** O `/code-review` do Matt olha
estrutura e spec; o nosso agente olha o que quebra *este* repositório (JID,
sync, echo matching, cinco idiomas, leitor de tela, patches). Não são
alternativos:
```
usa o winzapp-reviewer pra revisar minha branch contra origin/main
```

---

## Quando a coisa não começa por uma ideia sua

### `/triage` — issue que chegou de fora

**O que faz:** pega issues que chegaram cruas (relato de bug, sugestão de
usuário) e move por um fluxo: categoriza, verifica se reproduz, pede
informação ao autor se faltar (`needs-info`), e quando está claro escreve um
"brief" que um agente consegue executar (`ready-for-agent`) ou marca que
precisa de humano (`ready-for-human`). Todo comentário que ele posta começa
com *"This was generated by AI during triage."*

**Não use** em tickets que o `/to-tickets` criou — esses já nascem prontos.

**Como usar:**
```
/triage
```
ou uma issue específica:
```
/triage #212
```

### Bug difícil — não precisa de comando

Descreva o bug e a `diagnosing-bugs` entra sozinha. Ela se recusa a teorizar
até ter **um comando que fica vermelho neste bug**, depois corrige com teste
de regressão. Se não dá para reproduzir (o caso da citação de documento),
ela vai dizer isso em vez de chutar.

### `/wayfinder` — trabalho grande demais para uma sessão

**O que faz:** para uma ideia enorme e nebulosa (um sistema novo, uma
reescrita), cria um **mapa** no GitHub — uma issue-mãe com issues-filhas, cada
uma sendo uma **decisão** a tomar, não uma tarefa. Resolve uma por vez até o
caminho ficar claro. Só então entrega para o `/to-spec`.

**Quando usar:** raramente. Se a ideia cabe numa conversa, use
`/grill-with-docs`. Wayfinder é lento e denso de propósito.

---

## Skills de apoio

### `/handoff` — passar o bastão

**O que faz:** resume a conversa atual num arquivo markdown (na pasta
temporária do sistema, não no repo) para outra sessão, outra máquina ou outra
pessoa continuar. Aponta para specs/issues/commits em vez de repetir, e
remove segredos.

**Quando usar:** vai passar o trabalho para o Gabriel ou para o Pedro; vai
continuar amanhã em outra máquina; vai abrir uma sessão separada para um
protótipo e voltar.
```
/handoff o próximo passo é implementar o ticket #243
```

### `/improve-codebase-architecture` — sobrou um tempo

**O que faz:** varre o código (dando peso ao que mudou recentemente),
encontra módulos rasos que valeriam virar módulos profundos, e mostra um
relatório HTML. Você escolhe um e ele entrevista sobre esse.

**No WinZapp:** vai apontar para o `main.py` e o `conversations.py`. Achou o
candidato, a execução é o nosso `extract-from-god-file` (automático) ou o
agente `refactor-extractor`.

### `/wait-what` — não entendi

Digite no meio de qualquer conversa quando a última resposta do agente não
fez sentido. Ele reexplica com contexto, em inglês simples, usando os termos
do `CONTEXT.md`.

### `/to-questionnaire` — a resposta está com outra pessoa

**O que faz:** quando você trava porque quem sabe é outra pessoa (o Gabriel
sabe por que aquela decisão foi tomada; um usuário sabe como o bug aparece),
ele te pergunta só duas coisas — para quem vai e o que você precisa de volta —
e escreve um questionário markdown para essa pessoa preencher.

### `/grill-me` — entrevista sem repositório

Igual ao `/grill-with-docs`, mas não grava nada. Para afiar um plano, um
texto, uma ideia sem código embaixo. **Dentro do repo, use sempre
`/grill-with-docs`.**

### `/ask-matt` — esqueci qual skill usar

Mostra o mapa de todas as skills e em que situação cada uma entra. É o que
gerou este documento.

### `/setup-matt-pocock-skills` — já foi rodado

Configurou tracker (GitHub), labels e layout de docs. Está versionado em
`docs/agents/`. **Não rode de novo** — só se for trocar de tracker.

---

## Resumo de um parágrafo

Ideia nova: `/grill-with-docs`, `/to-spec`, `/to-tickets` numa sessão só;
depois `/implement` por ticket com `/clear` entre eles; `/code-review` e o
`winzapp-reviewer` antes do PR. Issue que chegou: `/triage`. Bug: só
descreva. Trabalho enorme: `/wayfinder`. Vai passar para alguém: `/handoff`.
Tudo o mais entra sozinho.
