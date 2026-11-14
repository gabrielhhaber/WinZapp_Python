import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/chatDeletedBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_only_accepted_explicit_deletions_have_notices_and_native_behavior_survives():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let fail=false;const sent=[];
const api=Object.create({applyMutations:async function(rows){assert.equal(this,api);if(fail)throw Error('failed');return rows.map(r=>({actionState:r.state||'Success'}))}});
const original=api.applyMutations;
const ctx=vm.createContext({window:{require:()=>({modulesMap:{WAWebDeleteChatSync:{defaultExport:api}}}),
 __winzappOnChatDeleted:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
(async()=>{
ctx.installChatDeletedInPage();const wrapper=api.applyMutations;ctx.installChatDeletedInPage();assert.equal(api.applyMutations,wrapper);
const row={operation:'set',indexParts:['deleteChat','7@g.us','0'],value:{deleteChatAction:{messageRange:{lastMessageTimestamp:2000000000}}}};
const results=await api.applyMutations([row,{...row,state:'Failed'},{...row,state:'Orphan'},{...row,operation:'remove'},
 {...row,indexParts:['clearChat','7@g.us','0']},{...row,value:{}}]);
assert.equal(results.length,6);assert.deepEqual(sent,[{chatId:'7@g.us',until:2000000000}]);
assert.equal(ctx.chatDeletedPayload(JSON.stringify(sent[0]),'s').session,'s');
for(const value of [0,true,-1])assert.equal(ctx.chatDeletedPayload(JSON.stringify({...sent[0],until:value}),'s'),null);
fail=true;await assert.rejects(()=>api.applyMutations([row]),/failed/);assert.equal(sent.length,1);fail=false;
ctx.window.__winzappOnChatDeleted=()=>{throw Error('binding failed')};ctx.installChatDeletedInPage();
assert.equal((await api.applyMutations([row])).length,1);
ctx.window.__winzappChatDeletedObserver.dispose();assert.equal(api.applyMutations,original);
assert.equal(Object.hasOwn(api,'applyMutations'),false);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    r = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
