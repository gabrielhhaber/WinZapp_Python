import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE=Path(__file__).resolve().parents[1]/"client/api_patches/src/util/profilePictureBridge.ts"


@pytest.mark.skipif(shutil.which("node") is None,reason="Node unavailable")
def test_picture_changes_coalesce_without_exporting_signed_urls_or_inferring_removal():
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const store=new EventEmitter(),sent=[];
const ctx=vm.createContext({window:{WPP:{whatsapp:{ProfilePicThumbStore:store}},
 __winzappOnProfilePictureChanged:raw=>sent.push(JSON.parse(raw))}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','').replace(/^import .*$/mg,''),ctx);
(async()=>{
ctx.installProfilePictureInPage();ctx.installProfilePictureInPage();assert.equal(store.listenerCount('change:tag'),1);
const m={id:{_serialized:'7@g.us'},tag:null,eurl:'https://private-url'};
store.emit('add',m);store.emit('remove',m);await Promise.resolve();assert.equal(sent.length,0);
store.emit('change:tag',m);store.emit('change:eurl',m);await Promise.resolve();
assert.deepEqual(sent,[{chatId:'7@g.us'}]);
assert.deepEqual(JSON.parse(JSON.stringify(ctx.profilePicturePayload(JSON.stringify(sent[0]),'s'))),{session:'s',chatId:'7@g.us'});
assert.equal(ctx.profilePicturePayload('{"chatId":"status@broadcast"}','s'),null);
store.emit('change:tag',m);ctx.window.__winzappProfilePictureObserver.dispose();await Promise.resolve();
assert.equal(sent.length,1);assert.equal(store.listenerCount('change:tag'),0);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    r=subprocess.run(["node","-e",script,str(SOURCE)],capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stderr
