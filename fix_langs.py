
import json, glob
for path in glob.glob('client/languages/*.json'):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('\
', '')
        content = content.replace('
', '')
        data = json.loads(content)
        
    data['play_voice_message'] = 'Reproduzir mensagem de voz'
    
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

