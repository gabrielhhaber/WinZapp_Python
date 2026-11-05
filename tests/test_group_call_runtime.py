"""Execute the real serialized group-call helper against native-shaped stores."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
const assert = require('node:assert/strict');
const group = '120363000001@g.us';
const wid = { _serialized: group };
const chat = { id: wid, groupMetadata: {} };
let record, joined, active, pending, supported, autoConnect, checks, extra, polls, invited,
  hangUpAfterFirstInvite;
const person = '555000001@lid';
const personChat = { id: { _serialized: person } };
const pnChat = { id: { _serialized: '555000002@c.us' } };
const calls = {
  waitUntilLoaded: async () => {},
  getByChat: c => c === chat ? record : null,
  getByCallId: id => record?.id.id === id ? record : null,
  getModelsArray: () => [record, ...extra].filter(Boolean),
};
const modules = {
  WAWebWidFactory: { createWid: id => ({ _serialized: id.replace('@s.whatsapp.net', '@c.us') }) },
  WAWebChatCollection: { ChatCollection: { get: id => id._serialized === group ? chat :
    id._serialized.endsWith('@g.us') ? { id, groupMetadata: {} } :
    id._serialized === person ? personChat : pnChat } },
  WAWebMsgGetters: { getIsGroupCall: call => call.isGroupCall === true,
    getCallParticipants: call => call.callParticipants },
  WAWebVoipWaCallEnums: { CallParticipantState: { Connected: 71 } },
  WAWebUserPrefsMeUser: { isMeAccount: wid => wid?._serialized === '555000099@lid',
    getMeLidUserOrThrow: () => ({ _serialized: '555000099@lid' }) },
  WAWebContactCollection: { ContactCollection: { get: wid => ({ name: wid?._serialized === person ? 'Synthetic Person' : '' }) } },
  WAWebVoipOngoingCallCollection: { WAWebVoipOngoingCallCollection: calls },
  WAWebVoipCheckOngoingCalls: { checkOngoingCalls: async () => { checks++; } },
  WAWebVoipInitEventEmitter: { VoipInitEventEmitter: { getIsVoipInited: () => true } },
  WAWebCallCollection: { get activeCall() { return active; }, get pendingOutgoingCall() { return pending; } },
  WAWebVoipStartCall: { inviteToCall: async w => {
    if (w._serialized === '555000666@lid') throw new Error('blocked 555000666');
    invited.push(w._serialized);
    if (hangUpAfterFirstInvite) active = null;
  }, joinOngoingWAWebVoipGroupCallPN: async p => {
    joined.push(p);
    if (autoConnect) active = typeof autoConnect === 'function' ? autoConnect(p) : { id: p.callId, getState: () => 6 };
  } },
  WAWebWamEnumLobbyEntryPointType: { LOBBY_ENTRY_POINT_TYPE: { CALL_LOG: 7 } },
};
global.window = { require: name => supported ? modules[name] : undefined, setTimeout: fn => fn() };
function fresh() {
  record = { id: { id: 'call-1', remote: wid }, callCreator: {}, isVideoCall: false, isGroupCall: true };
  joined = []; active = null; pending = null; supported = true; autoConnect = true; checks = 0;
  invited = []; hangUpAfterFirstInvite = false;
  extra = [];
  installGroupCallRuntimeInPage();
  return window.__winzappGroupCallRuntime;
}
(async () => {
  let api = fresh();
  assert.deepEqual(await api.ongoing(group), { id: 'call-1', groupJid: group, isVideo: false });
  assert.equal(checks, 1);
  record = null;
  assert.equal(await api.ongoing(group), null);

  api = fresh(); record.id.remote = { _serialized: '120363000002@g.us' };
  assert.equal(await api.ongoing(group), null);
  api = fresh(); record.callCreator = null;
  assert.equal(await api.ongoing(group), null);
  api = fresh(); supported = false;
  await assert.rejects(api.ongoing(group), /not available/);
  supported = true;
  await assert.rejects(api.ongoing('123@lid'), /group chat/);

  api = fresh(); record.isVideoCall = true;
  const result = await api.join({ groupJid: group, callId: 'call-1', isVideo: true });
  assert.equal(result.id, 'call-1');
  assert.equal(joined.length, 1);
  assert.deepEqual(joined[0], { callId: 'call-1', chat, isVideo: true,
    isDeviceSwitch: false, joinAndAccept: true, lobbyEntryPoint: 7 });

  for (const payload of [
    { groupJid: group, callId: 'old-call', isVideo: false },
    { groupJid: group, callId: '', isVideo: false },
    { groupJid: group, callId: 'call-1', isVideo: true },
    { groupJid: '120363000002@g.us', callId: 'call-1', isVideo: false },
  ]) {
    api = fresh();
    await assert.rejects(api.join(payload), /no longer available/);
    assert.equal(joined.length, 0);
  }
  api = fresh(); active = { id: 'other-call', getState: () => 6 };
  assert.equal(api.mayStopAudio('call-1'), false);
  await assert.rejects(api.join({ groupJid: group, callId: 'call-1', isVideo: false }), /already active/);
  assert.equal(joined.length, 0);
  api = fresh(); pending = {};
  assert.equal(api.mayStopAudio('call-1'), false);
  await assert.rejects(api.join({ groupJid: group, callId: 'call-1', isVideo: false }), /already active/);
  assert.equal(joined.length, 0);
  api = fresh(); autoConnect = false;
  await assert.rejects(api.join({ groupJid: group, callId: 'call-1', isVideo: false }), /did not join/);
  assert.equal(joined.length, 1, 'never retry or fall back to a new call');
  polls = 0; api = fresh(); autoConnect = p => ({ id: p.callId, getState: () => (++polls < 120 ? 12 : 6) });
  assert.equal((await api.join({ groupJid: group, callId: 'call-1', isVideo: false })).state, 6,
    'a join still dialling is waited for past the 5 s bound');
  polls = 0; api = fresh(); autoConnect = p => ({ id: p.callId, getState: () => (++polls < 120 ? 14 : 6) });
  assert.equal((await api.join({ groupJid: group, callId: 'call-1', isVideo: false })).state, 6,
    'CALL_B_STARTING counts as dialling, as ensure-ended does');
  polls = 0; api = fresh(); autoConnect = p => ({ id: p.callId, getState: () => { polls += 1; return 1; } });
  await assert.rejects(api.join({ groupJid: group, callId: 'call-1', isVideo: false }), /did not join/);
  assert.equal(polls, 150, 'a join that keeps dialling is still bounded');
  polls = 0; api = fresh(); autoConnect = p => ({ id: 'other-call', getState: () => { polls += 1; return 1; } });
  await assert.rejects(api.join({ groupJid: group, callId: 'call-1', isVideo: false }), /did not join/);
  assert.equal(polls, 50, 'another call dialling never extends the wait');

  api = fresh();
  extra = [
    { id: { id: 'multi', remote: personChat.id }, callCreator: {}, isVideoCall: true, isGroupCall: true },
    { id: { id: 'ordinary', remote: personChat.id }, callCreator: {}, isGroupCall: false },
    { id: { id: 'ended', remote: wid }, callCreator: null, isGroupCall: true },
    record,
  ];
  assert.deepEqual(await api.list(), [
    { id: 'call-1', chatJid: group, isVideo: false },
    { id: 'multi', chatJid: person, isVideo: true },
  ]);
  assert.equal(checks, 1, 'one native refresh for the entire list');
  api = fresh(); record.id.remote = personChat.id;
  const joinedMulti = await api.join({ chatJid: person, callId: 'call-1', isVideo: false });
  assert.equal(joinedMulti.peerJid, person);
  assert.equal(joined[0].chat, personChat);
  assert.equal(joined[0].joinAndAccept, true);
  api = fresh(); record.id.remote = personChat.id; record.isGroupCall = false;
  await assert.rejects(api.join({ chatJid: person, callId: 'call-1', isVideo: false }), /no longer available/);
  assert.equal(joined.length, 0);
  api = fresh(); record.id.remote = pnChat.id;
  await api.join({ chatJid: '555000002@s.whatsapp.net', callId: 'call-1', isVideo: false });
  assert.equal(joined[0].chat, pnChat, 'canonical phone ids still address the same native chat');
  api = fresh();
  record.callParticipants = [
    { participant: personChat.id, outcome: 71 },
    { participant: personChat.id, outcome: 71 },
    { participant: pnChat.id, outcome: 20 },
    { participant: { _serialized: '555000003@lid' }, outcome: 'Connected' },
  ];
  let roster = await api.participants('call-1');
  assert.equal(roster.available, true);
  assert.deepEqual(roster.participants, [{ jid: person, name: 'Synthetic Person', isSelf: false }]);
  active = { id: 'call-1', getState: () => 6, isGroup: true };
  roster = await api.participants('call-1');
  assert.equal(roster.participants.length, 2, 'the connected local user is included once');
  record = null;
  await assert.rejects(api.participants('call-1'), /unavailable/, 'never substitute group membership for a roster');
  active = { id: 'individual', peerJid: personChat.id, isGroup: false, getState: () => 6 };
  assert.equal((await api.participants('individual')).participants.length, 2);
  assert.equal((await api.participants('ended')).available, false);
  active = null;
  assert.equal((await api.participants('individual')).available, false);
  assert.equal(joined.length, 0, 'roster reads never join or acquire audio');
  // Add participant: WhatsApp's own inviteToCall, once per person, only into this device's call.
  api = fresh(); active = { id: 'call-1', getState: () => 6, isGroup: true };
  const added = await api.invite({ callId: 'call-1',
    participants: ['555000004@s.whatsapp.net', '555000005@lid', '555000666@lid', 'nobody'] });
  assert.deepEqual(invited, ['555000004@c.us', '555000005@lid']);
  assert.deepEqual(added.results.map(r => r.status), ['invited', 'invited', 'failed', 'failed']);
  assert.equal(added.results[2].error, 'refused', 'a fixed word, never WhatsApp\'s text');
  // a hangup halfway through the list sends nobody else anywhere
  api = fresh(); active = { id: 'call-1', getState: () => 6, isGroup: true };
  hangUpAfterFirstInvite = true;
  const cut = await api.invite({ callId: 'call-1', participants: ['555000004@lid', '555000005@lid', '555000007@lid'] });
  assert.deepEqual(invited, ['555000004@lid']);
  assert.deepEqual(cut.results.map(r => r.status), ['invited', 'failed', 'failed']);
  assert.equal(cut.results[1].error, 'call ended');
  api = fresh(); active = { id: 'call-1', getState: () => 1 };
  await assert.rejects(api.invite({ callId: 'call-1', participants: [person] }), /not in that call/);
  api = fresh(); active = { id: 'other-call', getState: () => 6 };
  await assert.rejects(api.invite({ callId: 'call-1', participants: [person] }), /not in that call/);
  api = fresh(); active = { id: 'call-1', getState: () => 6 };
  await assert.rejects(api.invite({ callId: 'call-1', participants: [] }), /participants are required/);
  await assert.rejects(api.invite({ callId: 'call-1', participants: Array(32).fill(person) }),
    /participants are required/);
  assert.equal(invited.length, 0, 'a refused request invites nobody');
  assert.equal(joined.length, 0, 'inviting never joins');
  console.log(JSON.stringify({ passed: true }));
})().catch(e => { console.error(e); process.exitCode = 1; });
'''


def test_native_group_call_discovery_and_join(tmp_path):
    node = ROOT / "client/node/node.exe"
    node = str(node) if node.exists() else shutil.which("node")
    if not node:
        pytest.skip("Node not available")
    source = (ROOT / "client/api_patches/src/util/groupCallRuntime.ts").read_text(encoding="utf-8")
    source = source.replace("export function ", "function ")
    for annotation in (" as any", ": Promise<any>", ": any", ": string", ": void", ": boolean"):
        source = source.replace(annotation, "")
    script = tmp_path / "group-calls.cjs"
    script.write_text(source + "\n" + HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"]
