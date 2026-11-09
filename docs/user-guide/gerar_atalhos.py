#!/usr/bin/env python3
"""Gera as tabelas de atalhos do guia a partir do catálogo do WinZapp.

Uso: python gerar_atalhos.py <pasta-do-repositorio-winzapp> <idioma> [saida.html]
  idioma: pt-BR, pt-PT, en-US, es-ES, pl, tr-TR, ro, ar ou it

Lê client/core/shortcut_catalog.py e o PO pt-BR do próprio repositório, então
as tabelas nunca ficam desatualizadas em relação ao código.
"""
import ast, collections, html, json, re, sys
from pathlib import Path

ALT, CTRL, SHIFT = 1, 2, 4
TECLAS = {32: 'Space', 13: 'Enter', 27: 'Esc', 127: 'Delete', 9: 'Tab', 44: ',', 46: '.',
          312: 'End', 313: 'Home', 314: 'Left', 315: 'Up', 316: 'Right', 317: 'Down',
          366: 'PageUp', 367: 'PageDown'}
TECLAS.update({340 + i: f'F{i + 1}' for i in range(12)})

GRUPOS = ['main', 'navigation', 'device', 'chats', 'chat_selection', 'archived', 'locked', 'search',
          'messages', 'message_list', 'composer', 'links', 'mention', 'autocomplete', 'media', 'emoji',
          'ai_result', 'status', 'status_list', 'status_reply', 'calls', 'call', 'incoming',
          'channels', 'channel_people']

# Textos do próprio programa que descrevem a ação errada; corrigidos aqui.
CORRECOES = {  # id do atalho -> chave do PO com o texto certo
    'chats.ID_ARCHIVE_LIST': 'archive_chat',
}

def carregar_po(po_path):
    texto = po_path.read_text(encoding='utf-8')
    saida, ctx, mid, mstr, cur = {}, None, None, None, None
    for linha in texto.splitlines() + ['']:
        if linha.startswith('msgctxt '):
            if ctx is not None and mstr is not None:
                saida[ctx] = mstr
            ctx, mstr, cur = ast.literal_eval(linha[8:]), None, 'ctx'
        elif linha.startswith('msgid '):
            cur = 'id'
        elif linha.startswith('msgstr '):
            mstr, cur = ast.literal_eval(linha[7:]), 'str'
        elif linha.startswith('"') and cur == 'str':
            mstr += ast.literal_eval(linha)
    if ctx is not None and mstr is not None:
        saida[ctx] = mstr
    return saida

def nome_tecla(mod, key, nomes):
    partes = []
    if mod & CTRL: partes.append('Ctrl')
    if mod & ALT: partes.append('Alt')
    if mod & SHIFT: partes.append('Shift')
    if isinstance(key, int) and (65 <= key <= 90 or 48 <= key <= 57):
        partes.append(chr(key))
    else:
        partes.append(nomes.get(TECLAS.get(key, ''), TECLAS.get(key, f'#{key}')))
    return '+'.join(partes)

def juntar_numerados(linhas, number_range='{first} a {last}'):
    """Junta famílias numeradas (Ctrl+1..9 etc.) numa linha só."""
    familias, resultado, usados = {}, [], set()
    for i, (tecla, acao) in enumerate(linhas):
        m = re.match(r'^(.*) \((\d)\)$', acao)
        if m and tecla[-1].isdigit():
            familias.setdefault(m.group(1), []).append((i, tecla, int(m.group(2))))
    for base, itens in familias.items():
        if len(itens) < 2:
            continue
        digitos = sorted(int(t[-1]) for _, t, _ in itens)
        prefixo = itens[0][1][:-1]
        faixa = (number_range.format(first=digitos[0], last=digitos[-1]) if digitos == list(range(digitos[0], digitos[-1] + 1))
                 else ', '.join(map(str, digitos)))
        usados |= {i for i, _, _ in itens}
        resultado.append((itens[0][0], prefixo + faixa, base))
    for i, (tecla, acao) in enumerate(linhas):
        if i not in usados:
            resultado.append((i, tecla, acao))
    return [(t, a) for _, t, a in sorted(resultado)]

def main():
    repo, locale = Path(sys.argv[1]), sys.argv[2]
    destino = Path(sys.argv[3]) if len(sys.argv) > 3 else Path('secoes/zz-atalhos-gerados.html')
    t = json.loads((Path(__file__).parent / 'i18n' / f'{locale}.json').read_text(encoding='utf-8'))
    sys.path.insert(0, str(repo / 'client'))
    from core.shortcut_catalog import SHORTCUTS
    from core.keyboard_shortcuts import default_binding, action_text
    po = carregar_po(repo / 'translations' / locale / 'LC_MESSAGES' / 'winzapp.po')
    tr = lambda k: po.get(k, k)
    por_escopo = collections.defaultdict(list)
    sem_tecla = []
    for s in SHORTCUTS:
        b = default_binding(s, tr)
        acao = (tr(CORRECOES[s.id]).replace('&', '') if s.id in CORRECOES
                else action_text(s, tr)).rstrip(' :')
        if b is None:
            sem_tecla.append((s, acao)); continue
        por_escopo[s.scope].append((nome_tecla(*b, t['keys']), acao))
    for escopo, linhas in list(por_escopo.items()):
        por_escopo[escopo] = juntar_numerados(linhas, t.get('number_range', '{first} a {last}'))
    up = lambda x: x[:1].upper() + x[1:]
    out = []
    ordem = GRUPOS + [e for e in por_escopo if e not in GRUPOS]
    for escopo in ordem:
        linhas = por_escopo.get(escopo)
        if not linhas:
            continue
        titulo = t['groups'].get(escopo, escopo)
        out.append(f'<h3>{html.escape(titulo)}</h3>\n<table class="field-table">\n<tr><th>{html.escape(t["col_shortcut"])}</th><th>{html.escape(t["col_action"])}</th></tr>')
        vistos = set()
        for tecla, acao in linhas:
            if (tecla, acao) in vistos: continue
            vistos.add((tecla, acao))
            out.append(f'<tr><td class="field-name">{html.escape(tecla)}</td><td>{html.escape(up(acao))}</td></tr>')
        out.append('</table>')
    if sem_tecla:
        out.append(f'<h3>{html.escape(t["nokey_title"])}</h3>\n<p>{html.escape(t["nokey_intro"])}</p>\n<table class="field-table">\n<tr><th>{html.escape(t["col_command"])}</th></tr>')
        vistos = set()
        for s, acao in sem_tecla:
            if acao in vistos: continue
            vistos.add(acao)
            out.append(f'<tr><td>{html.escape(up(acao))}</td></tr>')
        out.append('</table>')
    destino.write_text('\n'.join(out), encoding='utf-8')
    print(f'{locale}: {len(SHORTCUTS)} atalhos no catálogo -> {destino}')

if __name__ == '__main__':
    main()
