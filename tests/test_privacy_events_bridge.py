import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE=Path(__file__).resolve().parents[1]/"client/api_patches/src/util/privacyEventsBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None,reason="Node unavailable")
def test_watched_pref_writes_preserve_sync_async_returns_and_other_keys():
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const sent=[],settings={readReceipts:'all',online:'all'};let fail=false,archiveV2=true,unarchive=false;
let statusSetting='contact',statusIds=[];
class Prefs {
 applySyncSet(key,value){if(key==='hashed')Object.assign(settings,value);return 'sync-result'}
 async set(key,value){if(fail)throw Error('refused');if(key==='privacy')Object.assign(settings,value);return value}
 async bulkSetItemsToIndexedDB(rows){for(const row of rows)if(row.key==='privacy')Object.assign(settings,row.value);return rows}
}
const archivePrefs={
 getArchiveV2EnabledSetting:()=>archiveV2,
 getUnarchiveChatsSetting:()=>unarchive,
 async setArchiveV2EnabledSetting(value){archiveV2=value;return value},
 async setUnarchiveChatsSetting(value){unarchive=value;return value},
};
const archiveOriginals=[archivePrefs.setArchiveV2EnabledSetting,archivePrefs.setUnarchiveChatsSetting];
const prefs=new Prefs(),originals=[prefs.set,prefs.applySyncSet,prefs.bulkSetItemsToIndexedDB];
const statusApi={getStatusPrivacySetting:()=>statusSetting,getStatusAllowList:()=>statusIds,getStatusDenyList:()=>statusIds,
 async setStatusPrivacyConfig(value){statusSetting=value.setting;statusIds=value.list;await prefs.bulkSetItemsToIndexedDB([{key:'status',value:value.setting}]);return true}};
const functions={...statusApi,getUserPrivacySettings:()=>settings};
const modules={WAWebUserPrefsIndexedDBStorage:{userPrefsIdb:prefs},WAWebUserPrefsKeys:{HASHED_KEYS:{USER_PRIVACY_SETTINGS:'privacy'},BACKEND_ONLY_KEYS:{STATUS_PRIVACY_SETTING:'status',STATUS_ALLOW_LIST:'allow',STATUS_DENY_LIST:'deny',ARCHIVE_V2_ENABLED_SETTING:'archive',UNARCHIVE_CHATS_SETTING:'unarchive'}},
 WAWebUserPrefsStorageUtils:{hashUserPrefKey:key=>'hashed'},WAWebUserPrefsMultiDevice:archivePrefs};
const ctx=vm.createContext({window:{require:name=>modules[name],WPP:{whatsapp:{functions}},
 __winzappOnPrivacySettingsChanged:raw=>sent.push(JSON.parse(raw))},setTimeout,clearTimeout});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
(async()=>{
assert.equal(ctx.installPrivacyEventsInPage(),true);const installed=prefs.set;ctx.installPrivacyEventsInPage();assert.equal(prefs.set,installed);
const unrelated={foo:1};assert.equal(await prefs.set('other',unrelated),unrelated);assert.equal(sent.length,0);
assert.equal(prefs.applySyncSet('hashed',{readReceipts:'none'}),'sync-result');
prefs.applySyncSet('hashed',{online:'match_last_seen'});await flush();assert.equal(sent.length,1);
assert.deepEqual(sent[0].settings,{readReceipts:'none',online:'match_last_seen'});
const value={readReceipts:'all'};assert.equal(await prefs.set('privacy',value),value);await flush();assert.equal(sent.length,2);
fail=true;await assert.rejects(()=>prefs.set('privacy',value),/refused/);assert.equal(sent.length,2);fail=false;
const rows=[{key:'privacy',value:{about:'contacts'}}];assert.equal(await prefs.bulkSetItemsToIndexedDB(rows),rows);await flush();
assert.equal(sent[2].settings.about,'contacts');
await archivePrefs.setArchiveV2EnabledSetting(false);await archivePrefs.setUnarchiveChatsSetting(true);
await prefs.set('unarchive',true);
await flush();assert.equal(sent[3].keepArchived,false);
await functions.setStatusPrivacyConfig({setting:'deny-list',list:['123456@s.whatsapp.net']});
await flush();assert.deepEqual(sent[4].status,{setting:'deny-list',list:['123456@s.whatsapp.net']});
const payload=ctx.privacyEventPayload(JSON.stringify({settings:{about:'none',online:'none',privateKey:'secret'}}),'s');
assert.equal(JSON.stringify(payload),JSON.stringify({session:'s',settings:{about:'none'}}));
assert.equal(JSON.stringify(ctx.privacyEventPayload('{"keepArchived":false}','s')),
 JSON.stringify({session:'s',settings:{},keepArchived:false}));
assert.deepEqual(JSON.parse(JSON.stringify(ctx.privacyEventPayload(JSON.stringify({status:{setting:'deny-list',list:['123456@s.whatsapp.net']}}),'s'))),
 {session:'s',settings:{},status:{setting:'deny-list',list:['123456@c.us']}});
assert.equal(ctx.privacyEventPayload(JSON.stringify({status:{setting:'allow-list',list:['group@g.us']}}),'s'),null);
assert.equal(ctx.privacyEventPayload('{"settings":{"about":false}}','s'),null);
let release;
functions.getStatusPrivacySetting=()=>new Promise(resolve=>{release=resolve});
prefs.applySyncSet('hashed',{about:'none'});await flush();
assert.equal(typeof release,'function');
functions.getStatusPrivacySetting=()=>'contact';
prefs.applySyncSet('hashed',{about:'all'});await flush();
assert.equal(sent.length,6);assert.equal(sent[5].settings.about,'all');
release('contact');await flush();assert.equal(sent.length,6);
functions.getStatusPrivacySetting=()=>new Promise(resolve=>{release=resolve});
prefs.applySyncSet('hashed',{about:'none'});await flush();
ctx.window.__winzappPrivacyEventsObserver.dispose();release('contact');await flush();
assert.equal(sent.length,6);assert.equal(prefs.set,originals[0]);assert.equal(prefs.applySyncSet,originals[1]);
assert.equal(archivePrefs.setArchiveV2EnabledSetting,archiveOriginals[0]);
assert.equal(archivePrefs.setUnarchiveChatsSetting,archiveOriginals[1]);
assert.equal(Object.hasOwn(prefs,'set'),false);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    r=subprocess.run(["node","-e",script,str(SOURCE)],capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stderr
