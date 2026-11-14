import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE=Path(__file__).resolve().parents[1]/"client/api_patches/src/util/privacyEventsBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None,reason="Node unavailable")
def test_watched_pref_writes_preserve_sync_async_returns_and_other_keys():
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const sent=[],settings={readReceipts:'all',online:'all'};let fail=false;
class Prefs {
 applySyncSet(key,value){if(key==='hashed')Object.assign(settings,value);return 'sync-result'}
 async set(key,value){if(fail)throw Error('refused');if(key==='privacy')Object.assign(settings,value);return value}
 async bulkSetItemsToIndexedDB(rows){for(const row of rows)if(row.key==='privacy')Object.assign(settings,row.value);return rows}
}
const prefs=new Prefs(),originals=[prefs.set,prefs.applySyncSet,prefs.bulkSetItemsToIndexedDB];
const modules={WAWebUserPrefsIndexedDBStorage:{userPrefsIdb:prefs},WAWebUserPrefsKeys:{HASHED_KEYS:{USER_PRIVACY_SETTINGS:'privacy'}},
 WAWebUserPrefsStorageUtils:{hashUserPrefKey:key=>'hashed'}};
const ctx=vm.createContext({window:{require:name=>modules[name],WPP:{whatsapp:{functions:{getUserPrivacySettings:()=>settings}}},
 __winzappOnPrivacySettingsChanged:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
(async()=>{
assert.equal(ctx.installPrivacyEventsInPage(),true);const installed=prefs.set;ctx.installPrivacyEventsInPage();assert.equal(prefs.set,installed);
const unrelated={foo:1};assert.equal(await prefs.set('other',unrelated),unrelated);assert.equal(sent.length,0);
assert.equal(prefs.applySyncSet('hashed',{readReceipts:'none'}),'sync-result');
prefs.applySyncSet('hashed',{online:'match_last_seen'});await Promise.resolve();assert.equal(sent.length,1);
assert.deepEqual(sent[0].settings,{readReceipts:'none',online:'match_last_seen'});
const value={readReceipts:'all'};assert.equal(await prefs.set('privacy',value),value);await Promise.resolve();assert.equal(sent.length,2);
fail=true;await assert.rejects(()=>prefs.set('privacy',value),/refused/);assert.equal(sent.length,2);fail=false;
const rows=[{key:'privacy',value:{about:'contacts'}}];assert.equal(await prefs.bulkSetItemsToIndexedDB(rows),rows);await Promise.resolve();
assert.equal(sent[2].settings.about,'contacts');
const payload=ctx.privacyEventPayload(JSON.stringify({settings:{about:'none',online:'none',privateKey:'secret'}}),'s');
assert.equal(JSON.stringify(payload),JSON.stringify({session:'s',settings:{about:'none'}}));
assert.equal(ctx.privacyEventPayload('{"settings":{"about":false}}','s'),null);
prefs.applySyncSet('hashed',{about:'none'});ctx.window.__winzappPrivacyEventsObserver.dispose();await Promise.resolve();
assert.equal(sent.length,3);assert.equal(prefs.set,originals[0]);assert.equal(prefs.applySyncSet,originals[1]);
assert.equal(Object.hasOwn(prefs,'set'),false);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    r=subprocess.run(["node","-e",script,str(SOURCE)],capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stderr
