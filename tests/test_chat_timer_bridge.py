import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/chatTimerBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_batched_timer_changes_payload_validation_and_disposal():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const store=new EventEmitter(),groups=new EventEmitter(),sent=[];
const ctx=vm.createContext({window:{WPP:{whatsapp:{ChatStore:store,GroupMetadataStore:groups}},
 __winzappOnChatTimerChanged:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
(async()=>{
ctx.installChatTimerInPage();ctx.installChatTimerInPage();
assert.equal(store.listenerCount('change:ephemeralDuration'),1);
const m={id:{_serialized:'7@g.us'},ephemeralDuration:86400,ephemeralSettingTimestamp:100};
store.emit('add',m);await Promise.resolve();assert.equal(sent.length,0);
store.emit('change:ephemeralDuration',m);m.ephemeralSettingTimestamp=200;
store.emit('change:ephemeralSettingTimestamp',m);await Promise.resolve();
assert.deepEqual(sent,[{chatId:'7@g.us',duration:86400,settingTimestamp:200}]);
m.ephemeralDuration=0;store.emit('change:ephemeralDuration',m);await Promise.resolve();
assert.equal(sent[1].duration,0);
const metadata={id:{_serialized:'7@g.us'},ephemeralDuration:604800};
groups.emit('change:ephemeralDuration',metadata);await Promise.resolve();
metadata.ephemeralDuration=0;groups.emit('change:ephemeralDuration',metadata);await Promise.resolve();
assert.deepEqual(sent.slice(2),[{chatId:'7@g.us',duration:604800,settingTimestamp:0},
 {chatId:'7@g.us',duration:0,settingTimestamp:0}]);
assert.equal(ctx.chatTimerPayload(JSON.stringify(sent[1]),'s').session,'s');
for(const v of [{...sent[0],duration:true},{...sent[0],duration:-1},{...sent[0],settingTimestamp:null},
 {...sent[0],chatId:'status@broadcast'}])assert.equal(ctx.chatTimerPayload(JSON.stringify(v),'s'),null);
store.emit('change:ephemeralDuration',m);ctx.window.__winzappChatTimerObserver.dispose();
await Promise.resolve();assert.equal(sent.length,4);
assert.equal(store.listenerCount('change:ephemeralDuration'),0);
assert.equal(store.listenerCount('change:ephemeralSettingTimestamp'),0);
assert.equal(groups.listenerCount('change:ephemeralDuration'),0);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    r = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
