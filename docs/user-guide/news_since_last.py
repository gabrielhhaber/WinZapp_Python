#!/usr/bin/env python3
"""Mostra o que mudou no WinZapp desde a alpha que o guia cobre.

Uso: python news_since_last.py <pasta-do-repositorio-winzapp>

Saída: (1) alpha mais recente, (2) commits/PRs novos, (3) diff do changelog
pt-BR (a fonte principal de funcionalidades), (4) atalhos novos/removidos/
alterados no catálogo. Não altera nada: é o roteiro para atualizar o guia.
"""
import json, re, subprocess, sys
from pathlib import Path

repo = Path(sys.argv[1])
cob = json.loads((Path(__file__).parent / 'coverage.json').read_text(encoding='utf-8'))

def git(*args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout

git('fetch', '--tags', '--quiet', 'origin')
tags = [t for t in git('tag', '--list', 'v2.*alpha').split() if re.match(r'^v\d+\.\d+\.\d+\.\d+alpha$', t)]
tags.sort(key=lambda t: [int(x) for x in re.findall(r'\d+', t)])
ultima = tags[-1]
print(f'Guia cobre: {cob["tag"]}   |   alpha mais recente: {ultima}')
if ultima == cob['tag']:
    print('Nada novo: o guia já está em dia.'); sys.exit(0)

print('\n=== Commits novos (com número do PR) ===')
print(git('log', '--oneline', f'{cob["tag"]}..{ultima}'))

print('=== Mudanças no changelog pt-BR (funcionalidades novas) ===')
print(git('diff', '--unified=0', cob['tag'], ultima, '--', 'client/changelog_pt-BR.txt'))

def atalhos(ref):
    txt = git('show', f'{ref}:client/core/shortcut_catalog.py')
    return {m[0]: f'{m[1]} mod={m[2]} key={m[3]}' for m in re.findall(
        r"Shortcut\('([^']+)',\s*'[^']*',\s*'[^']*',\s*'([^']*)',\s*(\d+),\s*([^,]+?),", txt)}

antes, depois = atalhos(cob['tag']), atalhos(ultima)
print('=== Atalhos ===')
for k in sorted(depois.keys() - antes.keys()): print('NOVO     ', k, depois[k])
for k in sorted(antes.keys() - depois.keys()): print('REMOVIDO ', k, antes[k])
for k in sorted(antes.keys() & depois.keys()):
    if antes[k] != depois[k]: print('ALTERADO ', k, antes[k], '->', depois[k])
print('\nDepois de atualizar as seções: edite coverage.json, acrescente a linha em sections/<idioma>/99-*.html e rode generate_guide.py.')
