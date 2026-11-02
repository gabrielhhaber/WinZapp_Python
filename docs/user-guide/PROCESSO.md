# Guia de uso do WinZapp — como manter atualizado

Regra do projeto: **a cada alpha lançada, as funcionalidades novas entram no guia.**

## Como a regra é cobrada (automático)

Toda alteração na `main` vira uma alpha, e o pytest roda antes de cada alpha e em cada PR. O teste `tests/test_user_guide_coverage.py` falha quando:

1. a versão em andamento de `client/changelog_pt-BR.txt` tem um item que não está em `cobertura.json` (`itens`). Isso significa: o guia ainda não explica essa função;
2. os guias de `client/data/help_guide/` não são exatamente o que o `gerar_guia.py` produz agora;
3. algum idioma tem seções, selos `desde` ou subtítulos diferentes do pt-BR.

Para destravar: escreva a função nos 8 idiomas (roteiro abaixo), rode `python gerar_guia.py <clone>` e depois `python verificar_guia.py <clone> --registrar`. Registrar é a declaração de que o guia cobre os itens atuais; só faça depois de atualizar o texto. Um PR sem item novo no changelog (só documentação, refatoração) não precisa de nada.

## O que é cada arquivo

| Arquivo | Função |
|---|---|
| `client/data/help_guide/<idioma>.html` | O guia pronto de cada idioma (um arquivo só, acessível, com busca e modo escuro). É o que o menu Ajuda > Guia de uso abre. É gerado; não edite à mão. O `.gitignore` do projeto ignora `data/`: um idioma novo precisa de `git add -f`. |
| `secoes/<idioma>/NN-nome.html` | O conteúdo, uma seção por arquivo (a ordem dos números define a ordem no guia). **É aqui que se edita.** Idiomas: pt-BR (fonte), pt-PT, en-US, es-ES, pl, tr-TR, ro, ar. |
| `i18n/<idioma>.json` | Textos da página (título, busca, menu, títulos dos grupos de atalhos) e nomes das teclas. |
| `ferramentas/` | `swap.py` troca rótulos entre aspas pelos do PO do idioma; `examples.py` e `verify.py` conferem os exemplos entre aspas contra o PO; `gloss.json`/`prefix.json` são glossários. |
| `template.html` | Visual e script do guia (CSS, busca, menu). Raramente muda. |
| `cobertura.json` | Até que alpha o guia está completo (`alpha`, `tag`, `commit`, `data`). |
| `gerar_guia.py` | Junta tudo e escreve `client/data/help_guide/<idioma>.html` (`python gerar_guia.py <clone> [idioma ...]`). |
| `gerar_atalhos.py` | Gera as tabelas de atalhos direto do catálogo do WinZapp (`client/core/shortcut_catalog.py` + PO do idioma). Chamado pelo `gerar_guia.py`. |
| `verificar_guia.py` | Confere guia x changelog x idiomas. É chamado pelo teste `tests/test_user_guide_coverage.py`; `--registrar` registra os itens atuais. |
| `novidades_desde_a_ultima.py` | Mostra o que mudou desde a alpha coberta: commits/PRs, diff do changelog e atalhos novos/removidos/alterados. |

## Roteiro a cada alpha (ou a cada lote de alphas)

1. Atualize o clone do WinZapp (`git fetch --tags origin` e `git checkout origin/main`).
2. Rode `python novidades_desde_a_ultima.py <clone>`. A saída traz:
   - os commits novos com o número do PR;
   - o diff de `client/changelog_pt-BR.txt` (a melhor lista de funcionalidades novas, porque o projeto exige uma entrada por PR em todos os idiomas);
   - atalhos novos, removidos ou alterados.
3. Para cada funcionalidade nova do changelog:
   - em `secoes/pt-BR/`, ache a seção certa e escreva como se usa (onde fica, qual atalho, o que o leitor de tela diz);
   - acrescente `<span class="desde">alpha 4412</span>` no título ou no começo do item, com o número da alpha em que ela chegou;
   - se não houver seção que sirva, crie `secoes/NN-nome.html` (começa com `<!--nav: 🔔 Título-->`).
4. Atalhos: **nada a fazer à mão**; o `gerar_guia.py` regenera as tabelas. Só atualize as tabelas escritas à mão nas seções (conversas, mensagens, voz, ligações) se um atalho citado nelas mudou.
5. Mudou ou foi removida uma funcionalidade? Ajuste o texto da seção para descrever **só como é agora** (sem "antes era assim").
6. **Traduza o mesmo trecho para os outros 7 idiomas** (pt-PT, en-US, es-ES, pl, tr-TR, ro, ar), nos mesmos arquivos `secoes/<idioma>/` e na mesma ordem, com o mesmo selo `desde`. Escreva os rótulos da interface entre aspas retas, em inglês (ou como `⟦rótulo⟧`), e rode `python ferramentas/swap.py <idioma> apply`: ele troca cada rótulo pelo texto real do PO daquele idioma. Depois `python ferramentas/examples.py <idioma>` e `python ferramentas/verify.py <idioma>`; o que ficar "sem correspondência" deve ser só exemplo de fala do leitor de tela, não rótulo.
7. Registro: rode `python verificar_guia.py <clone> --registrar` e acrescente uma linha em `secoes/<idioma>/99-registro.html` **em cada idioma** (alpha + o que entrou) e atualize `cobertura.json`.
8. Rode `python gerar_guia.py <clone>` (todos os idiomas) e abra os HTML: confira a busca, o menu lateral e, com leitor de tela, as seções alteradas.

## Regras de conteúdo

- Só funcionalidades que funcionam. Nada de "não funciona ainda" nem de bug conhecido.
- Nunca descreva como era antes de uma mudança.
- Nomes de botões, menus e opções exatamente como aparecem no programa, no idioma do guia (conferir no PO: `translations/<idioma>/LC_MESSAGES/winzapp.po`; o `swap.py` faz isso). Termos que o PO de cada idioma já usa têm prioridade sobre tradução própria (`docs/reference/i18n-terminology.md`).
- Atalhos sempre os de fábrica; o texto lembra que dá para trocar nas Configurações.
- O changelog entra só com mudanças estável-para-estável; para o guia vale o contrário: tudo o que existe na alpha entra, inclusive o que só existe em alpha.

## Verificação antes de publicar

- `python gerar_guia.py <clone>` roda sem erro e informa a alpha coberta nos 8 idiomas.
- Todos os idiomas têm as mesmas seções e o mesmo número de selos `desde`.
- Nenhum `{{...}}` sobrou no HTML; todos os links `#...` do menu levam a uma seção.
- Os atalhos citados no texto conferem com a tabela gerada.

## Árabe

O guia árabe é completo e segue o mesmo processo dos outros idiomas, com três cuidados:

- O `dir` vem de `i18n/ar.json`; o `template.html` usa propriedades lógicas (`margin-inline-start` etc.), então uma regra nova de layout não deve usar `left`/`right`.
- As letras de `Alt+letra` de botões vêm do `&` do PO árabe (por exemplo `Alt+ع` em «عرض الأصوات»), não do texto em inglês. Atalhos fixos do catálogo não mudam.
- O PO escreve WhatsApp e Windows em árabe (`واتساب`, `ويندوز`); WinZapp fica sempre em inglês. Rótulos vão entre `«»`.

`docs/reference/arabic-localization.md` traz os termos adotados e o estado da revisão.
