import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE=Path(__file__).resolve().parents[1]/"client/api_patches/src/util/contactStateBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None,reason="Node unavailable")
def test_explicit_contact_changes_coalesce_flags_and_preserve_cleanup():
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const store=new EventEmitter(),sent=[];
const ctx=vm.createContext({window:{WPP:{whatsapp:{ContactStore:store}},
 __winzappOnContactStateChanged:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
(async()=>{
ctx.installContactStateInPage();ctx.installContactStateInPage();assert.equal(store.listenerCount('change:name'),1);
const m={id:{_serialized:'901@lid'},name:'Book name',isMyContact:true,syncToAddressbook:true,pushname:'Public name'};
store.emit('add',m);await Promise.resolve();assert.equal(sent.length,0);
store.emit('change:name',m);store.emit('change:isMyContact',m);await Promise.resolve();
assert.deepEqual(sent,[{contactId:'901@lid',kind:'address-book',name:'Book name',saved:true,syncedToPhone:true}]);
m.isMyContact=false;store.emit('change:isMyContact',m);m.name='';m.syncToAddressbook=false;
store.emit('change:name',m);await Promise.resolve();
assert.equal(sent.length,2);assert.equal(sent[1].saved,false);assert.equal(sent[1].name,'');
store.emit('change:pushname',m);await Promise.resolve();assert.equal(sent[2].kind,'profile');
for(const row of sent)assert.equal(ctx.contactStatePayload(JSON.stringify(row),'s').session,'s');
for(const bad of [{...sent[0],saved:'false'},{...sent[0],syncedToPhone:null},{...sent[0],contactId:'7@g.us'},
 {...sent[0],name:'',saved:true}])assert.equal(ctx.contactStatePayload(JSON.stringify(bad),'s'),null);
store.emit('change:name',m);ctx.window.__winzappContactStateObserver.dispose();await Promise.resolve();
assert.equal(sent.length,3);assert.equal(store.listenerCount('change:name'),0);assert.equal(store.listenerCount('change:pushname'),0);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result=subprocess.run(["node","-e",script,str(SOURCE)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
