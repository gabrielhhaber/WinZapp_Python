import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/groupMetadataBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_metadata_partial_events_no_hydration_and_model_owned_member_listeners():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const group=new EventEmitter(),store=new EventEmitter(),sent=[];
Object.assign(group,{id:{_serialized:'7@g.us'},desc:'text',announce:true,restrict:false,
 participants:{getModelsArray:()=>[{id:{_serialized:'901@lid'},isAdmin:true,isSuperAdmin:false}]}});
store.getModelsArray=()=>[group];
const ctx=vm.createContext({window:{WPP:{whatsapp:{GroupMetadataStore:store}},
 __winzappOnGroupMetadataChanged:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
assert.equal(ctx.installGroupMetadataInPage(),true);ctx.installGroupMetadataInPage();
assert.equal(sent.length,0);assert.equal(group.listenerCount('change:participants'),1);
store.emit('change:desc',group);group.desc=undefined;store.emit('change:desc',group);
store.emit('change:announce',group);store.emit('change:restrict',group);group.emit('change:participants');
assert.deepEqual(sent.slice(0,4),[{id:'7@g.us',desc:'text'},{id:'7@g.us',desc:''},
 {id:'7@g.us',announce:true},{id:'7@g.us',restrict:false}]);
assert.equal(sent[4].participants[0].id,'901@lid');assert.equal(sent[4].participantsComplete,true);
for(const row of sent)assert.equal(ctx.groupMetadataPayload(JSON.stringify(row),'s').session,'s');
assert.equal(ctx.groupMetadataPayload(JSON.stringify({id:'7@g.us',announce:'false'}),'s'),null);
assert.equal(ctx.groupMetadataPayload(JSON.stringify({id:'7@g.us',participants:[],participantsComplete:true}),'s'),null);
store.emit('remove',group);assert.equal(group.listenerCount('change:participants'),0);
store.emit('add',group);assert.equal(sent.length,5);assert.equal(group.listenerCount('change:participants'),1);
ctx.window.__winzappGroupMetadataObserver.dispose();assert.equal(group.listenerCount('change:participants'),0);
assert.equal(store.listenerCount('change:desc'),0);assert.equal(store.listenerCount('add'),0);
"""
    r = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
