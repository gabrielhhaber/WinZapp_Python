from pathlib import Path
import sys,re,glob
AQUI=Path(__file__).resolve().parent.parent; sys.path.insert(0,str(AQUI))
from generate_shortcuts import carregar_po
from pathlib import Path
loc=sys.argv[1]
po=carregar_po(AQUI.parent.parent/'translations'/loc/'LC_MESSAGES/winzapp.po')
f=lambda k,**kw: po[k].format(**kw)
rep={
 '"35% (13.8 MB of 39.4 MB)"': '"'+f('downloading_progress_size',pct=35,done='13,8 MB' if loc in('es-ES','pl','tr-TR','ro','cs') else '13.8 MB',total='39,4 MB' if loc in('es-ES','pl','tr-TR','ro','cs') else '39.4 MB')+'"',
 '"Downloading media: 30 of 120 processed (8 downloaded)"': '"'+f('sync_media_progress',done=30,total=120,downloaded=8)+'"',
 '"Syncing chats: 12 of 80"': '"'+f('sync_chats_progress',done=12,total=80)+'"',
 '"message 3 of 12"': '"'+f('bulk_transcription_progress',index=3,total=12).rstrip('.')+'"',
 '("..., duration: 0:12, transcription: ...")': '("..., '+po['duration']+': 0:12, '+po['transcription_row_label']+': ...")',
}
n=0
for p in glob.glob(str(AQUI/'sections'/loc/'*.html')):
    t=open(p,encoding='utf-8').read();o=t
    for a,b in rep.items(): t=t.replace(a,b)
    if t!=o: open(p,'w',encoding='utf-8').write(t);n+=1
print(loc,'arquivos alterados',n)
