# Skills do WinZapp

Referência do ferramental de IA disponível para quem trabalha neste repositório.
São **três grupos com procedências diferentes**, e a distinção importa: só o
primeiro vem junto com o `git clone`.

---

## Como usar

### Skills: ninguém invoca, elas aparecem

Uma skill **carrega sozinha** quando a tarefa bate com a descrição dela. Mexeu
em `client/languages/`, a `i18n-ui-string` entra em cena e o agente já sabe dos
cinco arquivos, do `&&` e dos placeholders — você não precisa lembrar de nada.
Para forçar, digite `/nome-da-skill`.

### Agentes: você chama pelo nome

```
usa o winzapp-reviewer pra revisar meu diff contra origin/main
```

Descrever a tarefa sem citar o nome também costuma funcionar (a escolha sai do
campo `description` de cada agente), mas **falar o nome é o caminho
confiável**.

Três coisas que mudam como se usa:

1. **O agente não vê a sua conversa.** Ele nasce com contexto zero — só o
   repositório e o que você escrever no pedido. É isso que torna o revisor
   útil: ele não sabe o que você já argumentou, então não concorda por
   inércia. Em troca, **o contexto é você quem passa**: "revisa a branch X
   contra o commit Y" funciona, "revisa aquilo que a gente falou" não.
2. **Agente não chama agente.** O `winzapp-implementer` não consegue invocar o
   revisor — não tem essa ferramenta. Quem encadeia é você.
3. **Carregam no início da sessão.** Depois de puxar um merge que adiciona ou
   altera um agente, feche e reabra o Claude Code.

### Os dois juntos

- **Skill = conhecimento.** Como se faz X neste repositório.
- **Agente = trabalhador.** Uma cabeça separada, com contexto próprio, que
  carrega as skills da área antes de escrever.

Os três agentes daqui têm a ferramenta `Skill` e são instruídos a carregar as
skills relevantes antes de tocar em qualquer coisa.

### Regras e traps: carregam quando você mexe no arquivo

O `CLAUDE.md` é carregado inteiro em toda sessão, então guarda só o que vale
para qualquer tarefa. A história por trás de cada regra — o incidente medido,
as hipóteses descartadas, os trechos de log — fica em `docs/traps/` (e o
material de referência em `docs/reference/`). Dois mecanismos levam o agente
até lá sem custar tokens em toda sessão:

- **`.claude/rules/<area>.md`** — cada um tem `paths:` no frontmatter e só
  entra no contexto quando o agente toca num arquivo que bate no glob. O corpo
  é a regra em três linhas e o ponteiro para o trap completo.
- **A tabela no fim do `CLAUDE.md`** — "antes de mexer em X, leia o trap
  daquela área". O agente lê com `Read` quando a tarefa pede.

Escrevendo um post-mortem novo: vai para `docs/traps/`, ganha uma regra em
`.claude/rules/` com os `paths` que ele protege, e uma linha na tabela. Não
volta para o `CLAUDE.md`. `tests/test_claude_md_matches_reality.py` confere
que todo caminho citado nesses arquivos existe.

### O fluxo de uma feature nova

```
1. grill-with-docs      entrevista até o plano parar de ter buraco (e gera ADR)
2. to-spec              vira a conversa em spec
3. winzapp-implementer  implementa, com as skills da área carregadas
4. winzapp-reviewer     revisa o diff, com contexto zero
5. PR                   CI roda a suíte + 1 aprovação humana
```

O passo 1 é o que parece estranho e é o mais valioso para quem está começando:
`grill-with-docs` **se recusa a gerar código** e fica perguntando até o plano
ficar de pé. É o antídoto para "pedi uma tela de configurações e recebi 400
linhas com a premissa errada".

Nada disso é obrigatório. Para um ajuste de uma linha, pular direto para o
passo 3 é o certo; a cerimônia é proporcional ao tamanho do que se está
decidindo.

---

## 1. Skills do projeto — versionadas, todo mundo tem

Vivem em `.claude/skills/<nome>/SKILL.md` e entram no repositório como qualquer
outro arquivo. Quem clona, tem. São carregadas automaticamente e acionadas pelo
próprio agente quando a tarefa se encaixa na descrição — não é preciso invocar
à mão.

| skill | quando ela vale |
| --- | --- |
| `i18n-ui-string` | Qualquer texto que o usuário lê ou o leitor de tela fala. `I18n.t()` é `translations.get(key, key)`, sem fallback por chave: uma chave faltando vira o nome cru dela na tela, lido em voz alta. Cobre os cinco locales, `&` como mnemônico do wx (um `&` literal se escreve `&&`) e placeholders `{}`. |
| `write-test` | Teste novo ou estendido. `MainWindow` é `wx.Frame` e `ConversationsPanel` é `wx.Panel`: nenhum dos dois instanciável sem `wx.App`, então ou a lógica sai para nível de módulo, ou o método real é ligado a um stub. Também as fixtures do `conftest.py` e o `asyncio_mode=auto` (teste async é só `async def`, sem decorator). |
| `wppconnect-patch` | Conserto do lado Node. São três mecanismos de patch diferentes, cada um com suas listas; escolher o errado é silencioso, e a mudança some no próximo `setup_api.py`. Nunca editar `client/api/` direto. |
| `accessible-ui` | Qualquer mexida em `client/ui/`, `status_panel.py` ou no wx do `main.py`. Controles padrão do wx, toda fala por `speak_output`, `Freeze`/`Thaw` com `try/finally` em volta de mutação de lista, e o limite de 511 caracteres do SysListView32. |

As quatro estão escritas em inglês, para casar com o `CLAUDE.md` e o `AGENTS.md`
— são documentos do mesmo tipo, dirigidos ao agente.

### Escrevendo uma skill nova

O critério que usamos para decidir o que vira skill: **procedimento repetível**
vira skill; **julgamento caso a caso** não. Normalização de JID, por exemplo, é
a maior fonte de bug do projeto e mesmo assim não é skill — não existe passo a
passo, existe um conjunto de invariantes a respeitar. Isso é material para
agente revisor, não para skill.

---

## 2. `mattpocock-skills` — declarado pelo repositório

Os arquivos não vêm no clone, mas a **declaração** vem: `.claude/settings.json`
registra o marketplace e marca o plugin como habilitado, então quem clona
recebe a instalação sem precisar rodar nada à mão.

```json
"enabledPlugins": { "mattpocock-skills@claude-plugins-official": true },
"extraKnownMarketplaces": { "claude-plugins-official": { ... } }
```

As duas chaves andam juntas de propósito: sem o `extraKnownMarketplaces`, um
clone que ainda não conheça o marketplace oficial encontraria uma referência a
um plugin que não sabe de onde vem.

Vem do marketplace oficial da Anthropic, pinado num commit de
`github.com/mattpocock/skills`. Traz 25 skills registradas e custa ~1,6k tokens *always-on* em toda
sessão. O cache do plugin carrega 35 arquivos; os 10 excedentes
(`setup-pre-commit`, `git-guardrails-claude-code`, `migrate-to-shoehorn`,
as de writing…) não são expostos por ele e não contam nesse custo.

Os arquivos em si ficam fora do repositório, em
`~/.claude/plugins/cache/claude-plugins-official/mattpocock-skills/<versão>/`,
e atualizam com `claude plugin update`. É de propósito que não sejam copiados
para `.claude/skills/`: essa pasta é das skills que o projeto escreve e
versiona, e uma cópia local das do plugin viraria um fork que envelhece calado
— o mesmo motivo pelo qual o `AGENTS.md` não duplica o `CLAUDE.md` e pelo qual
nunca se edita `client/api/` direto.

**Grelha** — bloqueiam a geração de código e entrevistam você primeiro:
`grill-me` (afia plano ou design), `grill-with-docs` (o mesmo, produzindo ADRs e
glossário no caminho), `grilling` (dispara por qualquer gatilho "grill"),
`wait-what` ("para, essa mensagem não colou: repitch").

**Spec-driven development** — o fluxo completo:
`to-spec` (vira a conversa em spec e publica no issue tracker),
`to-tickets` (quebra em tickets tracer-bullet com as dependências declaradas),
`to-questionnaire` (decisão que você não sabe responder vira questionário para
outra pessoa), `implement` (executa a partir da spec ou dos tickets),
`wayfinder` (trabalho grande demais para uma sessão, como mapa de tickets de
decisão), `triage` (máquina de estados para issues e PRs externos),
`handoff` (compacta a conversa para outro agente continuar).

**Engenharia:** `tdd`, `code-review`, `diagnosing-bugs`, `codebase-design`,
`improve-codebase-architecture`, `domain-modeling`, `prototype`, `research`,
`resolving-merge-conflicts`, `wizard`, `teach`, `ask-matt` (roteador entre as
outras) e `writing-for-agents` (escrever skill, `AGENTS.md` ou `CLAUDE.md`).

### O setup já rodou — e o fluxo que ele habilita

`setup-matt-pocock-skills` foi executada em 17/09/2026 e o resultado está
versionado: `docs/agents/issue-tracker.md` (GitHub Issues, via `gh`),
`docs/agents/triage-labels.md` (as cinco labels padrão, já criadas no repo) e
`docs/agents/domain.md` (single-context: `CONTEXT.md` + `docs/adr/` na raiz,
criados sob demanda pelo `/domain-modeling`). O bloco `## Agent skills` no fim
do `CLAUDE.md` resume isso e acrescenta três limites do WinZapp: "suíte
completa" é `pytest` sem `--run-wx-gui`; `/implement` não commita sem pedido;
os "standards" que o `/code-review` procura são `CLAUDE.md` + `docs/traps/` +
as skills daqui. Não é preciso rodar o setup de novo — só para trocar de
tracker.

Tutorial passo a passo, com o que cada skill manual faz e quando chamar:
`docs/agents/como-usar-as-skills.md`.

O caminho de uma feature, do jeito que o `/ask-matt` descreve, aplicado aqui:

```
/grill-with-docs   entrevista; grava termos em CONTEXT.md e decisões em docs/adr/
      │            (precisa ver rodando? /prototype, com /handoff nos dois sentidos)
/to-spec           congela o combinado numa issue do GitHub
/to-tickets        quebra em tickets verticais (tela + lógica + dados + teste + 5 locales)
/implement         um ticket por sessão, /clear entre eles; roda /tdd por dentro
/code-review       dois eixos: smells de Fowler + aderência à spec
```

Tudo até `/to-tickets` numa sessão só, sem `/compact`. Dois pontos de entrada
fora desse caminho: `/triage` para issue que chega crua (relato de usuário,
sugestão) e `/diagnosing-bugs` para bug que resiste ao primeiro olhar — ele
exige um comando que fique vermelho *neste* bug antes de teorizar.

**Onde o `winzapp-reviewer` entra:** o `/code-review` do Matt cobre estrutura
e spec; o nosso cobre o que quebra *este* repositório (JID, sync gate, echo
matching, cinco locales, leitor de tela, mecanismo de patch). Não são
alternativos — rode os dois antes de abrir PR, o do Matt primeiro.

`/improve-codebase-architecture` acha candidatos a extração; a execução é o
`extract-from-god-file` daqui. `/handoff` é como se passa contexto entre
sessões e entre pessoas sem reexplicar.

---

## 3. Skills do Claude Code — por conta, não por repositório

O que cada um tem aqui varia com a conta e os plugins habilitados. Por categoria:

- **Arquivos:** `docx`, `pdf`, `pptx`, `xlsx`
- **Visual:** `design`, `dataviz`, `artifact-design`, `artifact-diagramming`, `artifact-capabilities`
- **Engenharia (`engineering:*`):** `architecture` (ADR), `code-review`, `debug`, `deploy-checklist`, `documentation`, `incident-response`, `standup`, `system-design`, `tech-debt`, `testing-strategy`
- **Revisão:** `/code-review`, `/simplify`, `/security-review`
- **Configuração:** `update-config`, `keybindings-help`, `fewer-permission-prompts`, `schedule`, `loop`, `init`, `run`, `claude-api`
- **Skills e memória:** `find-skills`, `skill-creator`, `consolidate-memory`, `import-memory`

---

## Três coisas chamadas `code-review`

Vale saber antes de alguém pedir "roda o code-review" e receber outra coisa:

| qual | o que faz |
| --- | --- |
| `/code-review` (Claude Code) | Revisa o diff atual ou um PR, com níveis de esforço. `ultra` dispara revisão multi-agente na nuvem. |
| `engineering:code-review` | Playbook genérico de revisão — segurança, performance, correção. |
| `code-review` (mattpocock) | Revisa as mudanças desde um ponto fixo contra a **documentação do próprio repositório**, além da correção. |

---

## O que ainda não temos

Sem CI em `pull_request`: os testes só rodam **depois** do merge, no build alpha.
Enquanto isso, a aprovação humana é o único portão antes do merge — e é por isso
que rodar `pytest` local antes de abrir PR não é opcional.
