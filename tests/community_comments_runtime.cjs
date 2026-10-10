const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');
const ts = require('../client/api/node_modules/typescript');
const source = ts.transpileModule(fs.readFileSync(path.resolve(__dirname,
  '../client/api_patches/src/util/communityCommentsRuntime.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
}).outputText;
function load(wpp) {
  const context = {exports: {}, WPP:wpp, setTimeout, clearTimeout};
  vm.runInNewContext(source, context);
  return arg => Promise.resolve(context.exports.communityComments(arg));
}
const key = value => ({toString:()=>value, remote:{toString:()=> '123@g.us'}});
const parent = {id:key('parent')};
const chat = {groupMetadata:{defaultSubgroup:true}};
const row = (id,t,type='comment')=>({id:key(id), parentMsgKey:key('parent'),
  author:key('456@lid'), body:'private text', t, type, read:false, messageSecret:'secret'});
(async()=>{
 let reads=0, sends=0;
 const nativeRows=[row('b',2),row('a',1,'revoked')];
 const table={bulkGetByParentMsgKey:async keys=>{reads++;assert.equal(keys[0],parent.id);return nativeRows;}};
 const wpp={whatsapp:{ContactStore:{get:()=>({pushname:'Profile name',isMe:true,secret:'private'})}},chat:{getMessageById:async()=>parent,get:()=>chat},loader:{loadModule:name=>
  name==='WAWebAddonCommentTableMode'?{commentTableMode:table}:undefined}};
 let api=load(wpp);
 let result=await api({operation:'read',messageId:'parent'});
 assert.equal(result.ok,true);assert.equal(reads,1);
 assert.deepEqual(Array.from(result.response,r=>r.id),['a','b']);
 assert.equal(result.response[0].body,undefined);
 assert.equal('messageSecret' in result.response[1],false);
 assert.equal(nativeRows[0].read,false);
 assert.equal(result.response[1].author,'456@lid');
 assert.equal(result.response[1].authorName,'Profile name');
 assert.equal(result.response[1].fromMe,true);
 assert.equal('secret' in result.response[1],false);
 chat.groupMetadata.defaultSubgroup=false;
 result=await api({operation:'send',messageId:'parent',text:'text'});
 assert.equal(result.code,'not_community_announcement');assert.equal(reads,1);
 chat.groupMetadata.defaultSubgroup=true;
 for(const text of ['', ' ', null, 12]){
  result=await api({operation:'send',messageId:'parent',text});
  assert.equal(result.code,'invalid_comment_request');
 }
 let loaded=false, boots=0;
 const sender={sendCommentMessage:async(m,text)=>{sends++;assert.equal(m,parent);
  assert.equal(text,' exact text ');return {messageSendResult:'ERROR_UNKNOWN'};}};
 wpp.loader.loadModule=name=>name==='WAWebSendCommentMessageAction'?(loaded?sender:undefined):
  name==='Bootloader'?{__debug:{componentMap:new Map([['WAWebCommentsModal.react',{}]])},
    loadModules:(names,cb)=>{boots++;loaded=true;cb();}}:undefined;
 api=load(wpp);
 result=await api({operation:'send',messageId:'parent',text:' exact text '});
 assert.equal(boots,1);assert.equal(sends,1);assert.equal(result.response.messageSendResult,'ERROR_UNKNOWN');
 sender.sendCommentMessage=async()=>{sends++;throw Error('private token text');};
 result=await api({operation:'send',messageId:'parent',text:'text'});
 assert.equal(result.code,'comment_unconfirmed');assert.equal(sends,2);
 assert.equal(JSON.stringify(result).includes('private token'),false);
 wpp.chat.getComments=async()=>{reads++;return [];};
 wpp.chat.sendCommentMessage=async()=>({messageSendResult:'OK'});
 result=await api({operation:'read',messageId:'parent'});
 assert.equal(result.ok,true);assert.equal(result.response.length,0);
 result=await api({operation:'send',messageId:'parent',text:'text'});
 assert.equal(result.response.messageSendResult,'OK');assert.equal(sends,2);
 assert.equal((await load(undefined)({operation:'read',messageId:'parent'})).code,'comments_unavailable');
 console.log('Community comments runtime: native read, optional public API, lazy sender, group validation and no retry passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
