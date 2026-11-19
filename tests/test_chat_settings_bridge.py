"""Execute the actual page observer against event-emitting fake native stores."""
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/chatSettingsBridge.ts"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")


def test_setting_observations_validation_and_disposal():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const chats=new EventEmitter(),mutes=new EventEmitter(),sent=[];
const context=vm.createContext({window:{WPP:{whatsapp:{ChatStore:chats,MuteStore:mutes}},
 __winzappOnChatSettingsChanged:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),context);
const install=vm.runInContext('('+context.installChatSettingsInPage.toString()+')',context);
assert.equal(install(),true);install();
assert.equal(chats.listenerCount('change:archive'),1);assert.equal(chats.listenerCount('change:pin'),1);
assert.equal(mutes.listenerCount('change:expiration'),1);
const locks=[];
context.window.__winzappOnChatSettingsChanged=raw=>locks.push(JSON.parse(raw));install();
chats.emit('change:isLocked',{id:'7@g.us',isLocked:true});
chats.emit('change:isLocked',{id:'7@g.us',isLocked:false});
assert.deepEqual(locks.map(v=>[v.setting,v.value]),[['lock',true],['lock',false]]);
assert.equal(context.chatSettingsPayload(JSON.stringify(locks[0]),'s').value,true);
context.window.__winzappOnChatSettingsChanged=raw=>sent.push(JSON.parse(raw));install();
const model={id:{_serialized:'7@g.us'},archive:true,pin:2000000000};
chats.emit('add',model);chats.emit('remove',model);assert.equal(sent.length,0);
chats.emit('change:archive',model);model.archive=false;chats.emit('change:archive',model);
chats.emit('change:pin',model);delete model.pin;chats.emit('change:pin',model);
const mute={id:'7@g.us',expiration:2000000000};
for(const expiry of [2000000000,-1,0]){mute.expiration=expiry;mutes.emit('change:expiration',mute)}
assert.deepEqual(sent.map(v=>[v.setting,v.value]),[
 ['archive',true],['archive',false],['pin',true],['pin',false],['mute',2000000000],['mute',-1],['mute',0]]);
for(const data of sent){assert.equal(data.chatId,'7@g.us');
 assert.equal(context.chatSettingsPayload(JSON.stringify({...data,body:'discard'}),'s').session,'s');
 assert.equal(context.chatSettingsPayload(JSON.stringify({...data,body:'discard'}),'s').body,undefined)}
for(const [setting,value] of [['mute',true],['mute','0'],['mute',-2],['mute',0.5],['pin',1],['archive','false'],['unknown',true]])
 assert.equal(context.chatSettingsPayload(JSON.stringify({chatId:'7@g.us',setting,value}),'s'),null);
for(const raw of ['x','{}','null',JSON.stringify({chatId:'status@broadcast',setting:'mute',value:0})])
 assert.equal(context.chatSettingsPayload(raw,'s'),null);
chats.emit('change:archive',{id:'1@newsletter',archive:true});
chats.emit('change:archive',{id:'7@g.us',archive:'false'});
mutes.emit('change:expiration',{id:'7@g.us',expiration:NaN});assert.equal(sent.length,7);
const fresh=[];context.window.__winzappOnChatSettingsChanged=raw=>fresh.push(JSON.parse(raw));install();
chats.emit('change:archive',model);assert.equal(sent.length,7);assert.equal(fresh.length,1);
context.window.__winzappOnChatSettingsChanged=()=>{throw Error('binding lost')};install();
assert.doesNotThrow(()=>chats.emit('change:archive',model));
const next=new EventEmitter();context.window.WPP.whatsapp.ChatStore=next;install();
assert.equal(chats.listenerCount('change:archive'),0);assert.equal(mutes.listenerCount('change:expiration'),1);
context.window.__winzappChatSettingsObserver.dispose();
assert.equal(next.listenerCount('change:archive'),0);assert.equal(next.listenerCount('change:pin'),0);
assert.equal(mutes.listenerCount('change:expiration'),0);
"""
    result = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
