"""Run the actual in-page observer and socket owner in Node against fake stores."""
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/messageStarBridge.ts"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")


def test_observer_payloads_reinstallation_and_page_ownership():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const store=new EventEmitter(),sent=[],timers=[];
const context=vm.createContext({window:{WPP:{whatsapp:{MsgStore:store}},
  __winzappOnMessageStarChanged:raw=>sent.push(JSON.parse(raw))},
  setTimeout:fn=>(timers.push(fn),timers.length),clearTimeout:()=>{}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ',''),context);
const install=vm.runInContext('('+context.installMessageStarInPage.toString()+')',context);
const model={id:{id:'M',remote:{_serialized:'7@g.us'}},star:true};
(async()=>{
assert.equal(install(),true);assert.equal(install(),true);
assert.equal(store.listenerCount('change:star'),1);
store.emit('change:star',model);model.star=false;store.emit('change:star',model);
assert.deepEqual(sent,[{chatId:'7@g.us',messageId:'M',starred:true},
  {chatId:'7@g.us',messageId:'M',starred:false}]);
const fresh=[];context.window.__winzappOnMessageStarChanged=raw=>fresh.push(JSON.parse(raw));
install();store.emit('change:star',model);assert.equal(sent.length,2);assert.equal(fresh.length,1);
store.emit('change:star',{id:{id:'M',remote:'status@broadcast'},star:true});
store.emit('change:star',{id:{id:'M',remote:'7@g.us'}});assert.equal(fresh.length,1);
const good=context.messageStarPayload(JSON.stringify(fresh[0]),'s');
assert.equal(JSON.stringify(good),JSON.stringify({session:'s',...fresh[0]}));
for(const raw of [null,'x',JSON.stringify({...fresh[0],starred:'false'}),
 JSON.stringify({...fresh[0],messageId:''}),JSON.stringify({...fresh[0],chatId:'7@newsletter'})])
 assert.equal(context.messageStarPayload(raw,'s'),null);
context.window.__winzappMessageStarObserver.dispose();assert.equal(store.listenerCount('change:star'),0);

const pageEvents={},emitted=[],bindings={};let evaluations=0,exposed=0,installed=true;
const page={exposeFunction:async(n,f)=>{bindings[n]=f;exposed++},
 on:(n,f)=>{(pageEvents[n]??=[]).push(f)},evaluate:async()=>{evaluations++;return installed}};
const io=tag=>({to:room=>({emit:(name,payload)=>emitted.push({tag,room,name,payload})})});
await context.installMessageStarBridge({page,session:'old'},io('old'),{});
await context.installMessageStarBridge({page,session:'new'},io('new'),{});
bindings.__winzappOnMessageStarChanged(JSON.stringify(fresh[0]));
assert.equal(exposed,1);assert.equal(pageEvents.load.length,1);assert.equal(pageEvents.close.length,1);
assert.equal(emitted[0].room,'session:new');assert.equal(emitted[0].payload.session,'new');
assert.equal(emitted[0].tag,'new');assert.equal(emitted[0].name,'message-star-changed');
installed=false;pageEvents.load[0]();await Promise.resolve();await Promise.resolve();
pageEvents.close[0]();const count=evaluations;for(const retry of timers)retry();
assert.equal(evaluations,count);bindings.__winzappOnMessageStarChanged(JSON.stringify(fresh[0]));
assert.equal(emitted.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
