"""Only explicit successful deletions become notices, never memory eviction."""
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/messageDeleteBridge.ts"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")


def test_sync_local_deletions_and_failures_preserve_native_behavior():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const sent=[],applied=[],store=new EventEmitter();let fail=false,nativeDone=false;
const cached=new Set(),timers=new Map();let timerId=0;
store.get=serialized=>cached.has(String(serialized).split('_')[2])?{}:undefined;
const handler=Object.create({applyMutations:async function(batch,extra){
 assert.equal(this,handler);applied.push([batch,extra]);if(fail)throw Error('refused');
 nativeDone=true;return batch.map(m=>({actionState:m.state||'Success'}))}});
const chat={deleteMessage:async function(result){assert.equal(this,chat);if(fail)throw Error('refused');return result}};
const nativeSync=handler.applyMutations,nativeLocal=chat.deleteMessage;
const nativeGetter=()=>nativeLocal;
Object.defineProperty(chat,'deleteMessage',{get:nativeGetter,enumerable:true,configurable:true});
const parser={fromString:s=>{const p=s.split('_');return {remote:p[1],id:p[2]}}};
const ctx=vm.createContext({setTimeout:fn=>{timers.set(++timerId,fn);return timerId},
 clearTimeout:id=>timers.delete(id),window:{require:()=>({modulesMap:{WAWebDeleteMessageForMeSync:{defaultExport:handler}}}),
 WPP:{chat,whatsapp:{MsgStore:store,MsgKey:parser,ChatStore:{get:()=>({msgs:{get:key=>cached.has(key.id)?{}:undefined}})}}},
 __winzappOnMessageDeletedForMe:raw=>{assert(nativeDone);sent.push(JSON.parse(raw))}}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
const install=vm.runInContext('('+ctx.installMessageDeleteInPage.toString()+')',ctx);
const mutation=(mid,state='Success',operation='set')=>({operation,state,
 indexParts:['deleteMessageForMe','7@g.us',mid,'0','901@lid']});
(async()=>{
assert.equal(install(),true);const wrapper=handler.applyMutations;install();assert.equal(handler.applyMutations,wrapper);
const batch=[mutation('OK'),mutation('FAILED','Failed'),mutation('ORPHAN','Orphan'),
 mutation('REMOVE','Success','remove'),{...mutation('WRONG'),indexParts:['clearChat','7@g.us']}];
const results=await handler.applyMutations(batch,{same:1});assert.equal(applied.length,1);assert.equal(results.length,5);
assert.deepEqual(sent,[{chatId:'7@g.us',messageId:'OK'}]);
store.emit('remove',{id:{id:'EVICTED',remote:'7@g.us'}});assert.equal(sent.length,1);
const local={id:'true_7@g.us_LOCAL',isDeleted:true,isRevoked:false};
assert.equal(await chat.deleteMessage(local),local);assert.equal(sent[1].messageId,'LOCAL');
await chat.deleteMessage({id:'true_7@g.us_REVOKED',isDeleted:true,isRevoked:true});
await chat.deleteMessage({id:'true_7@g.us_KEPT',isDeleted:false});assert.equal(sent.length,2);
cached.add('DELAYED');
const delayed={id:'true_7@g.us_DELAYED',isDeleted:false,isRevoked:false,sendMsgResult:'OK'};
await chat.deleteMessage(delayed);assert.equal(sent.length,2);assert.equal(timers.size,1);
cached.delete('DELAYED');const pending=[...timers.values()];timers.clear();pending.forEach(fn=>fn());
assert.equal(sent.length,3);assert.equal(sent[2].messageId,'DELAYED');
cached.add('STILLHERE');
await chat.deleteMessage({...delayed,id:'true_7@g.us_STILLHERE'});
for(let i=0;i<25;i++){const jobs=[...timers.values()];timers.clear();jobs.forEach(fn=>fn())}
assert.equal(sent.length,3);assert.equal(timers.size,0);
fail=true;await assert.rejects(()=>handler.applyMutations([mutation('FAILED')]),/refused/);
await assert.rejects(()=>chat.deleteMessage(local),/refused/);assert.equal(sent.length,3);fail=false;
const fresh=[];ctx.window.__winzappOnMessageDeletedForMe=raw=>fresh.push(JSON.parse(raw));install();
await chat.deleteMessage(local);assert.equal(fresh.length,1);assert.equal(sent.length,3);
ctx.window.__winzappOnMessageDeletedForMe=()=>{throw Error('binding failed')};install();
assert.equal(await chat.deleteMessage(local),local);
assert.equal(ctx.messageDeletedPayload(JSON.stringify(fresh[0]),'s').session,'s');
for(const raw of ['x','{}','{"chatId":"7@newsletter","messageId":"M"}'])
 assert.equal(ctx.messageDeletedPayload(raw,'s'),null);
await chat.deleteMessage({...delayed,id:'true_7@g.us_STILLHERE'});
assert.equal(timers.size,1);
ctx.window.__winzappMessageDeleteObserver.dispose();
assert.equal(timers.size,0);
assert.equal(handler.applyMutations,nativeSync);assert.equal(chat.deleteMessage,nativeLocal);
assert.equal(Object.hasOwn(handler,'applyMutations'),false);
assert.equal(Object.getOwnPropertyDescriptor(chat,'deleteMessage').get,nativeGetter);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
