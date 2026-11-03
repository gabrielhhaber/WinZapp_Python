#!/usr/bin/env python3
"""Confere se o guia de uso está completo para o que o changelog anuncia.

Roda como teste (tests/test_user_guide_coverage.py), então barra o PR e a alpha
(o job `test` do alpha-release.yml roda o pytest inteiro). Três conferências:

1. Cada item da versão em andamento (a primeira de client/changelog_pt-BR.txt)
   está registrado em cobertura.json -> "itens". Registrar é dizer: "eu li este
   item e o guia já explica a função". Item novo, ou com o começo reescrito,
   fica sem registro e o teste falha dizendo qual.
2. Os guias gerados em client/data/help_guide são exatamente o que o
   gerar_guia.py produz agora (atalhos, textos do PO, seções).
3. Os 8 idiomas têm as mesmas seções, os mesmos selos "desde" e os mesmos
   subtítulos.

Uso:
  python verificar_guia.py <repo>             confere e lista o que falta
  python verificar_guia.py <repo> --registrar registra os itens atuais
                                              (só depois de atualizar o guia!)
"""
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import gerar_guia  # noqa: E402

COBERTURA = AQUI / 'cobertura.json'
TAMANHO_CHAVE = 80


def chave(item):
    """Identifica um item pelo começo do texto: corrigir um erro de digitação
    no fim não exige registrar de novo; reescrever o item, sim."""
    inicio = re.sub(r'\s+', ' ', item[2:]).strip()[:TAMANHO_CHAVE]
    return hashlib.sha1(inicio.encode('utf-8')).hexdigest()[:12], inicio


def itens_da_versao_atual(repo):
    """Itens ("- ...") da primeira versão do changelog pt-BR."""
    texto = (Path(repo) / 'client' / 'changelog_pt-BR.txt').read_text(encoding='utf-8')
    linhas = texto.split('\n')
    itens, dentro = [], False
    for linha in linhas:
        if re.match(r'^V\d', linha):
            if dentro:
                break
            dentro = True
            continue
        if dentro and linha.startswith('- '):
            itens.append(linha)
    return itens


def itens_sem_registro(repo):
    registrados = json.loads(COBERTURA.read_text(encoding='utf-8')).get('itens', {})
    return [chave(i)[1] for i in itens_da_versao_atual(repo) if chave(i)[0] not in registrados]


def guias_desatualizados(repo):
    """Idiomas cujo guia gerado difere do que está em client/data/help_guide."""
    ruins = []
    with tempfile.TemporaryDirectory() as tmp:
        for loc in gerar_guia.IDIOMAS:
            gerar_guia.gerar(str(repo), loc, pasta_saida=tmp)
            novo = (Path(tmp) / f'{loc}.html').read_bytes().replace(b'\r\n', b'\n')
            atual = Path(repo) / 'client' / 'data' / 'help_guide' / f'{loc}.html'
            # Fim de linha normalizado: no Windows o checkout pode trazer CRLF.
            if not atual.is_file() or atual.read_bytes().replace(b'\r\n', b'\n') != novo:
                ruins.append(loc)
    return ruins


def divergencias_entre_idiomas():
    """Compara cada idioma com o pt-BR: nomes de seção, selos e subtítulos."""
    def perfil(loc):
        pasta = AQUI / 'secoes' / loc
        return {
            a.name: (a.read_text(encoding='utf-8').count('class="desde"'),
                     a.read_text(encoding='utf-8').count('<h3'))
            for a in sorted(pasta.glob('[0-9]*.html'))
        }
    base = perfil('pt-BR')
    achados = []
    for loc in gerar_guia.IDIOMAS[1:]:
        outro = perfil(loc)
        if outro.keys() != base.keys():
            achados.append(f'{loc}: seções diferentes do pt-BR')
            continue
        for nome, valor in base.items():
            if outro[nome] != valor:
                achados.append(f'{loc}/{nome}: selos/subtítulos {outro[nome]} em vez de {valor}')
    return achados


def registrar(repo):
    dados = json.loads(COBERTURA.read_text(encoding='utf-8'))
    dados['itens'] = dict(chave(i) for i in itens_da_versao_atual(repo))
    COBERTURA.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return len(dados['itens'])


def main():
    repo = sys.argv[1]
    if '--registrar' in sys.argv:
        print(f'{registrar(repo)} itens registrados em cobertura.json')
        return
    problemas = []
    problemas += [f'item do changelog sem registro no guia: {i}' for i in itens_sem_registro(repo)]
    problemas += [f'guia desatualizado (rode gerar_guia.py): {l}' for l in guias_desatualizados(repo)]
    problemas += divergencias_entre_idiomas()
    print('\n'.join(problemas) or 'guia completo')
    sys.exit(1 if problemas else 0)


if __name__ == '__main__':
    main()
