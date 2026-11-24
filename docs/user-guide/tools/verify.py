from pathlib import Path
import re,sys,glob
sys.path.insert(0,'.')
from generate_shortcuts import carregar_po
from pathlib import Path
loc=sys.argv[1]
po=carregar_po(Path(f'../../translations/{loc}/LC_MESSAGES/winzapp.po'))
norm=lambda x:re.sub(r'[&]|\.{3}|…','',x).strip().rstrip('.:!?').strip().lower()
vals={norm(v) for v in po.values()}
vals|={norm(v.split(':',1)[-1]) for v in po.values()}
q=set()
for f in glob.glob(f'sections/{loc}/*.html'):
    t=re.sub(r'<[^>]+>',' ',open(f,encoding='utf-8').read())
    t=t.replace('&gt;','>').replace('&lt;','<')
    for m in re.findall(r'[“"„«]([^"”“»]{3,130})[”"”»]',t): q.add(norm(m))
allv=' || '.join(vals)
miss=sorted(x for x in q if x not in vals and x not in allv)
print(loc,len(q),'frases entre aspas;',len(miss),'sem correspondência no PO:')
for x in miss: print('  -',x)
