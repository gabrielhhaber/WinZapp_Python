#!/usr/bin/env python3
"""Monta client/data/help_guide/<idioma>.html a partir de template.html, i18n/ e secoes/<idioma>/*.html.

Uso: python gerar_guia.py <pasta-do-repositorio-winzapp> [idioma ...]
Sem idiomas, gera todos. Idiomas: pt-BR, pt-PT, en-US, es-ES, pl, tr-TR, ro, ar, it.

1. Regenera as tabelas de atalhos do catálogo do repositório, com o PO do idioma.
2. Numera e junta as seções (ordem dos nomes de arquivo).
3. Escreve o HTML final com a alpha coberta (cobertura.json).
"""
import json, re, subprocess, sys
from pathlib import Path

AQUI = Path(__file__).parent
IDIOMAS = ['pt-BR', 'pt-PT', 'en-US', 'es-ES', 'pl', 'tr-TR', 'ro', 'ar', 'it']

def gerar(repo, loc, pasta_saida=None):
    t = json.loads((AQUI / 'i18n' / f'{loc}.json').read_text(encoding='utf-8'))
    cob = json.loads((AQUI / 'cobertura.json').read_text(encoding='utf-8'))
    pasta = AQUI / 'secoes' / loc
    gerado = pasta / '.atalhos-gerados.html'
    subprocess.run([sys.executable, str(AQUI / 'gerar_atalhos.py'), repo, loc, str(gerado)], check=True)
    nav, toc, secoes = [], [], []
    for n, arq in enumerate(sorted(pasta.glob('[0-9]*.html')), 1):
        texto = arq.read_text(encoding='utf-8')
        m = re.match(r'<!--nav: (.+?)-->\n', texto)
        if not m:
            sys.exit(f'{loc}/{arq.name}: falta a linha <!--nav: ...-->')
        titulo, corpo = m.group(1).strip(), texto[m.end():]
        corpo = corpo.replace('<!--ATALHOS_GERADOS-->', gerado.read_text(encoding='utf-8'))
        sid = re.sub(r'^\d+-', '', arq.stem)
        emoji, sem_emoji = titulo.split(' ', 1)
        nav.append(f'    <a href="#{sid}" class="nav-link">{n}. {sem_emoji}</a>')
        toc.append(f'        <li><a href="#{sid}">{sem_emoji}</a></li>')
        secoes.append(f'  <section id="{sid}" class="wiki-section" aria-labelledby="h-{sid}">\n'
                      f'    <h2 id="h-{sid}">{emoji} {n}. {sem_emoji}</h2>\n{corpo}\n  </section>\n')
    gerado.unlink()
    campos = {'LANG': t['lang'], 'DIR': t.get('dir', 'ltr'), 'SUBTITLE': t['subtitle'], 'SKIP': t['skip'], 'MENU': t['menu'],
              'MENU_OPEN_LABEL': t['menu_open_label'], 'CLOSE': t['close'], 'NAV_LABEL': t['nav_label'],
              'SEARCH': t['search'], 'SEARCH_LABEL': t['search_label'], 'INTRO': t['intro'],
              'COVERAGE': t['coverage'].replace('{ALPHA}', cob['alpha']).replace('{DATA}', cob['data']),
              'TOC_LABEL': t['toc_label'], 'TOC_TITLE': t['toc_title'],
              'NAV': '\n'.join(nav), 'TOC': '\n'.join(toc), 'SECOES': '\n'.join(secoes)}
    html = (AQUI / 'template.html').read_text(encoding='utf-8')
    for k, v in campos.items():
        html = html.replace('{{' + k + '}}', v)
    sobra = re.findall(r'\{\{\w+\}\}', html)
    if sobra:
        sys.exit(f'{loc}: marcadores sem valor: {sobra}')
    pasta_saida = Path(pasta_saida) if pasta_saida else Path(repo) / 'client' / 'data' / 'help_guide'
    pasta_saida.mkdir(parents=True, exist_ok=True)
    saida = pasta_saida / f'{loc}.html'
    saida.write_text(html, encoding='utf-8')
    print(f'{loc}: {len(secoes)} seções, {len(html)//1024} KB -> {saida.name} (cobre até a alpha {cob["alpha"]})')

def main():
    repo = sys.argv[1]
    alvo = sys.argv[2:] or IDIOMAS
    for loc in alvo:
        if (AQUI / 'secoes' / loc).is_dir():
            gerar(repo, loc)
        else:
            print(f'{loc}: sem pasta secoes/{loc}, ignorado')

if __name__ == '__main__':
    main()
