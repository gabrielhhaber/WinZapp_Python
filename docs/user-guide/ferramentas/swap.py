from pathlib import Path
import json,re,sys,glob
AQUI=Path(__file__).resolve().parent.parent; sys.path.insert(0,str(AQUI))
from gerar_atalhos import carregar_po
from pathlib import Path
loc=sys.argv[1]; apply=len(sys.argv)>2 and sys.argv[2]=='apply'
R=AQUI.parent.parent/'translations'
en=carregar_po(R/'en-US/LC_MESSAGES/winzapp.po'); lo=carregar_po(R/f'{loc}/LC_MESSAGES/winzapp.po')
norm=lambda x:re.sub(r'[&]|\.{3}|…','',x).strip().rstrip('.:!?').strip().lower()
allm={}; exact={}
for k,v in en.items():
    if k in lo and lo[k].strip():
        allm.setdefault(norm(v),lo[k]); exact.setdefault(v.replace('&','').strip().lower(),lo[k])
        if ' | ' in v and ' | ' in lo[k]: allm.setdefault(norm(v.split(' | ',1)[1]),lo[k].split(' | ',1)[1])
g=json.load(open(str(AQUI/'ferramentas/gloss.json'))); pre=json.load(open(str(AQUI/'ferramentas/prefix.json')))
m={norm(v['en-US']):v[loc] for v in g.values() if loc in v}
pm=[(norm(v['en-US']),v[loc].split(' (')[0].rstrip('.')) for v in pre.values() if loc in v]
clean=lambda s:s.replace('&','')
def lookup(inner):
    k=norm(inner)
    dots='...' if inner.rstrip().endswith('...') else ''
    dots='' if inner.rstrip().endswith('...') and False else dots
    ex=exact.get(inner.replace('&','').strip().lower())
    if ex: return clean(ex)
    if k in m: return m[k] if m[k].endswith('...') else m[k]+dots
    if k in allm:
        r=clean(allm[k]); return r if r.endswith('...') or r.endswith('…') else r+dots
    if len(k)>=15:
        for e,l in pm:
            if e.startswith(k): return l
    return None
miss=[]
def sw_marker(mm):
    r=lookup(mm.group(1))
    if r is None: miss.append(mm.group(1)); return mm.group(0)
    return r
def sw_quote(mm):
    r=lookup(mm.group(1))
    return '"'+r+'"' if r else mm.group(0)
for f in sorted(glob.glob(str(AQUI/'secoes'/loc/'*.html'))):
    t=open(f,encoding='utf-8').read()
    parts=re.split(r'(<[^>]+>)',t)
    for i,p in enumerate(parts):
        if p.startswith('<'): continue
        p=re.sub(r'⟦([^⟧]+)⟧',sw_marker,p)
        parts[i]=re.sub(r'"([^"<>]{3,200})"',sw_quote,p)
    n=''.join(parts)
    if apply and n!=t: open(f,'w',encoding='utf-8').write(n)
print('marcadores sem tradução:',sorted(set(miss)))
