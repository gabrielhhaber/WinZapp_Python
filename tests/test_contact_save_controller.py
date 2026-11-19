"""Run the actual controller and isolated Puppeteer callback against native fakes.

The contact action must receive a full LID Wid, not bare digits; a local saved
flag alone did not make that malformed mutation appear on the user's iPhone.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get(
    "WINZAPP_TEST_TYPESCRIPT", ROOT / "client/api/node_modules/typescript/lib/typescript.js"))


def test_native_save_preserves_lid_domain_and_failure_boundaries():
    node = shutil.which("node")
    if not node or not COMPILER.is_file():
        pytest.skip("Existing Node and TypeScript compiler required; no install")
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),ts=require(process.argv[1]);
const source=fs.readFileSync(process.argv[2],'utf8');
const code=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2020,
 module:ts.ModuleKind.CommonJS}}).outputText;
const ctx=vm.createContext({exports:{}});vm.runInContext(code,ctx);
const PN='5511999999999@c.us', LID='123456789012345@lid';
class Wid {
 constructor(id){[this.user,this.server]=id.split('@')}
 toString(){return this.user+'@'+this.server}
}
async function run(opts={}) {
 const sent=[],legacy=[],reads=[];let evaluations=0;
 const wpp={whatsapp:{Debug:{VERSION:opts.version??'2.3000.1054630338'},
  WidFactory:{createWid:id=>new Wid(id)},
  ApiContact:{getAlternateUserWid:wid=>opts.noMapping?null:new Wid(wid.server==='lid'?PN:LID)},
  functions:{saveContactActionV2:async payload=>{
   sent.push(payload);if(opts.failWrite)throw {code:'private-code',message:'private-detail'};
  }}},contact:{
   getPnLidEntry:async()=>opts.unknownPhone?{}:{phoneNumber:new Wid(PN)},
   save:async (...args)=>{legacy.push(args);return {isMyContact:true,syncToAddressbook:true}},
   get:async id=>{reads.push(id);if(opts.failRead)throw Error('private-detail');
    return {isMyContact:true,syncToAddressbook:opts.sync!==false}}
  }};
 if(opts.unavailable)delete wpp.whatsapp.functions.saveContactActionV2;
 const debug=wpp.whatsapp.Debug;
 if(opts.windowDebugOnly)delete wpp.whatsapp.Debug;
 const pageCtx=vm.createContext({window:{WPP:wpp,Debug:opts.windowDebugOnly?debug:undefined}});
 const page={isClosed:()=>false,evaluate:async(fn,arg)=>{
  evaluations++;return vm.runInContext('('+fn.toString()+')',pageCtx)(arg);
 }};
 const res={status(n){this.http=n;return this},json(body){this.body=body;return this}};
 await ctx.exports.saveContact({body:{phone:opts.id||PN,name:'  Example  ',lastName:' Person ',
  syncAddressBook:opts.sync},client:{page}},res);
 return {res,sent,legacy,reads,evaluations};
}
(async()=>{
 for(const id of [PN,LID])for(const sync of [true,false]){
  const r=await run({id,sync});assert.equal(r.res.http,200);assert.equal(r.sent.length,1);
  assert.equal(r.legacy.length,0);assert.deepEqual(r.reads,[id]);
  const p=r.sent[0];assert(p.lid instanceof Wid);assert.equal(p.lid.toString(),LID);
  // This is what the measured native contact-sync serializer writes.
  const action={lidJid:p.lid.toString(),saveOnPrimaryAddressbook:p.syncToAddressbook};
  assert.equal(action.lidJid.endsWith('@lid'),true);assert.equal(action.saveOnPrimaryAddressbook,sync);
  assert.equal(p.phoneNumber,PN.split('@')[0]);assert.equal(p.firstName,'Example');
  assert.equal(p.lastName,'Person');assert.equal(r.res.body.response.id,id);
  assert.equal(r.res.body.response.isMyContact,true);
 }
 const noLid=await run({noMapping:true});assert.equal(noLid.res.http,200);
 assert.equal(noLid.sent[0].lid,undefined);assert.equal(noLid.sent[0].phoneNumber,PN.split('@')[0]);
 const missing=await run({id:LID,noMapping:true});assert.equal(missing.res.http,400);
 assert.equal(missing.sent.length,0);assert.equal(missing.res.body.code,'contact_lid_without_phone');
 const unknown=await run({id:LID,unknownPhone:true});assert.equal(unknown.sent.length,0);
 assert.equal(unknown.res.body.code,'contact_lid_without_phone');
 for(const opts of [{failWrite:true},{failRead:true}]){
  const r=await run(opts);assert.equal(r.res.http,502);assert.equal(r.sent.length,1);
  assert.equal(r.legacy.length,0);assert.equal(r.res.body.code,'contact_operation_failed');
  assert(!JSON.stringify(r.res.body).includes('private'));
 }
 for(const opts of [{unavailable:true},{version:'unknown'}]){
  const r=await run(opts);assert.equal(r.res.http,503);
  assert.equal(r.sent.length,0);assert.equal(r.legacy.length,0);
 }
 const old=await run({version:'2.3000.1030209353'});
 assert.equal(old.res.http,200);assert.equal(old.sent.length,0);assert.equal(old.legacy.length,1);
 const boundary=await run({version:'2.3000.1030209354'});assert.equal(boundary.sent.length,1);
 assert.equal((await run({windowDebugOnly:true})).sent.length,1);
 for(const version of ['2.3001.1','3.1.1'])assert.equal((await run({version})).sent.length,1);
 for(const id of ['123456789@g.us','not-a-contact']){
  const r=await run({id});assert.equal(r.res.http,400);assert.equal(r.evaluations,0);
 }
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", script, str(COMPILER),
         str(ROOT / "client/api_patches/src/controller/contactSaveController.ts")],
        capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr
