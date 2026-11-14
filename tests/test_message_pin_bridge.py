import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/messagePinBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_committed_rows_only_and_native_results_failures_and_descriptors_survive():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let fail=false;const sent=[];
const api={createOrUpdatePinInChat:async function(rows){assert.equal(this,api);if(fail)throw Error('refused');return rows}};
const original=api.createOrUpdatePinInChat;
const ctx=vm.createContext({window:{require:()=>api,WPP:{whatsapp:{MsgKey:{fromString:s=>({remote:s.split('_')[1],id:s.split('_')[2]})}}},
 __winzappOnMessagePinChanged:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
(async()=>{
assert.equal(ctx.installMessagePinInPage(),true);const wrapper=api.createOrUpdatePinInChat;
ctx.installMessagePinInPage();assert.equal(api.createOrUpdatePinInChat,wrapper);
const row={parentMsgKey:'true_7@g.us_M',pinType:1,senderTimestampMs:2000000000000,pinExpiryDuration:86400};
const rows=[row];assert.equal(await api.createOrUpdatePinInChat(rows),rows);
await api.createOrUpdatePinInChat([{...row,pinType:2,pinExpiryDuration:0}]);
assert.deepEqual(sent.map(v=>[v.messageId,v.pinned,v.duration]),[['M',true,86400],['M',false,0]]);
await api.createOrUpdatePinInChat([]);assert.equal(sent.length,2);
for(const bad of [{...row,pinType:0},{...row,senderTimestampMs:null},{...row,pinExpiryDuration:-1}])
 await api.createOrUpdatePinInChat([bad]);assert.equal(sent.length,2);
assert.equal(ctx.messagePinPayload(JSON.stringify(sent[0]),'s').session,'s');
assert.equal(ctx.messagePinPayload(JSON.stringify({...sent[0],pinned:'false'}),'s'),null);
fail=true;await assert.rejects(()=>api.createOrUpdatePinInChat(rows),/refused/);assert.equal(sent.length,2);fail=false;
ctx.window.__winzappOnMessagePinChanged=()=>{throw Error('binding failed')};ctx.installMessagePinInPage();
assert.equal(await api.createOrUpdatePinInChat(rows),rows);
ctx.window.__winzappMessagePinObserver.dispose();assert.equal(api.createOrUpdatePinInChat,original);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    r = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
