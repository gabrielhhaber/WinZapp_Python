"""The in-page poll runtime (client/api_patches/src/util/pollRuntime.ts), run in
Node against fakes of the WhatsApp Web modules measured over CDP: a poll is a
``poll_creation`` Msg with ``pollOptions``; its votes are the group
``PollVoteCollection.getForParentAddressingModeInsensitive([id])[0]``; a vote
is cast with ``WAWebPollsSendVoteMsgAction.sendVote(msg, Set)``."""

import shutil
import subprocess
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/pollRuntime.ts"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not available")


def _run(body: str):
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const sent = [], raw = [];
const options = [{name:'A', localId:0}, {name:'B', localId:1}, {name:'C', localId:2}];
const poll = {id:'poll-key', type:'poll_creation', pollOptions:options, pollSelectableOptionsCount:1};
const votes = [
  {selectedOptionLocalIds:[0], sender:'ana@lid'}, {selectedOptionLocalIds:[0, 1], sender:{_serialized:'me'}},
  {selectedOptionLocalIds:[], sender:'gone@lid'},
];
const group = {toArray:()=>votes, getVoteFromSenderAddressingModeInsensitive:()=>({selectedOptionLocalIds:[1]})};
const modules = {
  WAWebPollsPollVoteCollection:{PollVoteCollection:{getForParentAddressingModeInsensitive:()=>[group]}},
  WAWebUserPrefsMeUser:{getMeLidUserOrThrow:()=>'me'},
  WAWebPollsSendVoteMsgAction:{sendVote:async (m, s)=>{sent.push([m.id, [...s]]);}},
};
const wpp = {loader:{loadModule:n=>modules[n]},
  chat:{getMessageById:async id=>id==='poll-id'?poll:(id==='other-id'?{id:'x',type:'chat'}:undefined)}};
wpp.chat.sendRawMessage = async (chat, msg, opts) => { raw.push([chat, msg, opts]); return {id: 'new-id'}; };
const ctx = vm.createContext({WPP:wpp, WAPI:{getMessageById: async id => ({id, type: 'poll_creation'})}, self:{crypto:{getRandomValues:a=>a.fill(7)}}});
// Serialize like Puppeteer: only each function's own text reaches the page.
const source = fs.readFileSync(process.argv[1],'utf8').replaceAll('export ','');
vm.runInContext(source, vm.createContext({}));
for (const name of ['readPollVotes', 'readPollVoters', 'writePollVote', 'sendPollWithSettings']) {
  const fn = new vm.Script('(' + (() => { const c = vm.createContext({}); vm.runInContext(source, c); return c[name].toString(); })() + ')').runInContext(ctx);
  ctx[name] = fn;
}
(async()=>{
""" + body + r"""
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script, str(RUNTIME)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_counts_each_option_ignores_cleared_votes_and_marks_mine():
    _run("""
const out = await ctx.readPollVotes({messageIds:['poll-id', 'other-id', 'missing']});
assert.strictEqual(JSON.stringify(Object.keys(out)), '["poll-id"]');
assert.strictEqual(out['poll-id'].voters, 2);
assert.strictEqual(JSON.stringify(out['poll-id'].options.map(o=>[o.name,o.votes,o.mine])),
  '[["A",2,false],["B",1,true],["C",0,false]]');
""")


def test_unavailable_collection_is_an_error_not_zero_votes():
    _run("""
delete modules.WAWebPollsPollVoteCollection;
await assert.rejects(()=>ctx.readPollVotes({messageIds:['poll-id']}), /poll_votes_unavailable/);
""")


def test_a_vote_is_sent_as_a_set_of_local_ids():
    _run("""
assert.strictEqual(JSON.stringify(await ctx.writePollVote({messageId:'poll-id', localIds:[1]})), '{"ok":true}');
assert.strictEqual(JSON.stringify(sent), '[["poll-key",[1]]]');
await ctx.writePollVote({messageId:'poll-id', localIds:[]});
assert.strictEqual(JSON.stringify(sent[1]), '["poll-key",[]]');
""")


def test_invalid_votes_are_refused_before_anything_is_sent():
    _run("""
for (const ids of [[7], [0, 1], ['x']]) {
  const r = await ctx.writePollVote({messageId:'poll-id', localIds:ids});
  assert.strictEqual(r.ok, false); assert.strictEqual(r.error, 'poll_vote_invalid');
}
assert.strictEqual((await ctx.writePollVote({messageId:'other-id', localIds:[0]})).error, 'poll_vote_unavailable');
assert.strictEqual(sent.length, 0);
""")


def test_a_throwing_send_reports_failure_without_native_detail():
    _run("""
modules.WAWebPollsSendVoteMsgAction.sendVote = async () => { throw new Error('secret name'); };
const r = await ctx.writePollVote({messageId:'poll-id', localIds:[0]});
assert.strictEqual(JSON.stringify(r), '{"ok":false,"error":"poll_vote_failed"}');
""")


def test_voters_list_who_voted_what_and_mark_mine():
    _run("""
modules.WAWebContactCollection = {ContactCollection: {get: id => id === 'ana@lid' ? {name: '', pushname: 'Ana P'} : undefined}};
const out = await ctx.readPollVoters({messageId:'poll-id'});
assert.strictEqual(out.ok, true);
assert.strictEqual(JSON.stringify(out.voters),
  '[{"sender":"ana@lid","mine":false,"name":"Ana P","localIds":[0]},{"sender":"me","mine":true,"name":"","localIds":[0,1]}]');
assert.strictEqual(JSON.stringify(out.options.map(o=>o.name)), '["A","B","C"]');
""")


def test_voters_of_a_missing_poll_or_collection_is_an_error_code():
    _run("""
assert.strictEqual(JSON.stringify(await ctx.readPollVoters({messageId:'other-id'})),
  '{"ok":false,"error":"poll_voters_unavailable"}');
delete modules.WAWebPollsPollVoteCollection;
assert.strictEqual((await ctx.readPollVoters({messageId:'poll-id'})).error, 'poll_voters_unavailable');
""")


def test_poll_settings_go_into_the_message_model_wa_web_reads():
    _run("""
const before = Date.now();
const out = await ctx.sendPollWithSettings({chatId:'c@g.us', name:'Q?', choices:['A','B'],
  selectableCount:1, hideVoterNames:true, durationSeconds:3600});
const after = Date.now();
const [chat, msg] = raw[0];
assert.strictEqual(chat, 'c@g.us');
assert.strictEqual(msg.type, 'poll_creation');
assert.strictEqual(msg.pollHideVoterNames, true);
assert.ok(msg.pollEndTime >= before + 3600000 && msg.pollEndTime <= after + 3600000);
assert.strictEqual(msg.pollSelectableOptionsCount, 1);
assert.strictEqual(JSON.stringify(msg.pollOptions), '[{"name":"A","localId":0},{"name":"B","localId":1}]');
assert.strictEqual(msg.pollEncKey.length, 32);
assert.strictEqual(msg.messageSecret.length, 32);
assert.strictEqual(JSON.stringify(out), '{"id":"new-id","type":"poll_creation"}');
""")


def test_only_what_was_asked_is_added_to_the_poll():
    _run("""
await ctx.sendPollWithSettings({chatId:'c@g.us', name:'Q?', choices:['A','B'], selectableCount:0,
  hideVoterNames:false, durationSeconds:0});
const msg = raw[0][1];
assert.ok(!('pollHideVoterNames' in msg) && !('pollEndTime' in msg));
assert.strictEqual(msg.pollSelectableOptionsCount, 0);
""")
