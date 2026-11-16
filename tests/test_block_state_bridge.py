import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE=Path(__file__).resolve().parents[1]/"client/api_patches/src/util/blockStateBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None,reason="Node unavailable")
def test_explicit_block_flags_reset_invalidation_and_no_eviction_unblocks():
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const contacts=new EventEmitter(),blocks=new EventEmitter(),sent=[];
const ctx=vm.createContext({window:{WPP:{whatsapp:{ContactStore:contacts,BlocklistStore:blocks}},
 __winzappOnBlockStateChanged:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
ctx.installBlockStateInPage();ctx.installBlockStateInPage();assert.equal(contacts.listenerCount('change:isContactBlocked'),1);
const model={id:{_serialized:'901@lid'},isContactBlocked:true};
blocks.emit('remove',model);blocks.emit('add',model);contacts.emit('add',model);assert.equal(sent.length,0);
contacts.emit('change:isContactBlocked',model);model.isContactBlocked=false;contacts.emit('change:isContactBlocked',model);
blocks.emit('reset');assert.deepEqual(sent,[{kind:'contact',contactId:'901@lid',blocked:true},
 {kind:'contact',contactId:'901@lid',blocked:false},{kind:'refresh'}]);
for(const data of sent)assert.equal(ctx.blockStatePayload(JSON.stringify(data),'s').session,'s');
assert.equal(ctx.blockStatePayload(JSON.stringify({...sent[0],blocked:'false'}),'s'),null);
assert.equal(ctx.blockStatePayload(JSON.stringify({...sent[0],contactId:'7@g.us'}),'s'),null);
ctx.window.__winzappBlockStateObserver.dispose();assert.equal(contacts.listenerCount('change:isContactBlocked'),0);
assert.equal(blocks.listenerCount('reset'),0);
"""
    r=subprocess.run(["node","-e",script,str(SOURCE)],capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stderr
