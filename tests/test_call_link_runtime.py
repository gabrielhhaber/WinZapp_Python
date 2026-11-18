"""Execute the real serialized call-link helper against native-shaped modules."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
const assert = require('node:assert/strict');
const TOKEN = 'AbCdEfGhIjKlMnOpQrStUv';
const VOICE = 'https://call.whatsapp.com/voice/' + TOKEN;
let active, pendingOut, pendingLink, created, previews, joins, decided, ended, previewStatus, joinState,
  info, guestName, savedName;
const wid = (s) => ({ toString: (o) => s, _serialized: s });
const calls = {
  get activeCall() { return active; },
  get pendingOutgoingCall() { return pendingOut; },
  get pendingCallLink() { return pendingLink; },
  setPendingCallLink: (v) => { pendingLink = v; },
};
const stack = {
  type: 'web',
  previewCallLink: async (...args) => { previews.push(args); return previewStatus; },
  joinCallLink: async () => {
    joins += 1;
    if (joinState !== null) active = { id: 'LINKCALL', isCallLink: true, getState: () => joinState,
      isWaitingRoomAdmin: true, isWaitingRoomEnabled: true, waitingRoomUsers: [] };
  },
  getCallInfo: async () => JSON.stringify(info),
  parsers: { parseCallInfo: (raw) => JSON.parse(raw) },
  waitingRoomAdmit: async (jid) => { decided.push(['admit', jid]); return 0; },
  waitingRoomDeny: async (jid) => { decided.push(['deny', jid]); return 0; },
  endCall: async () => { ended += 1; active = null; },
};
const contacts = { get: (w) => w && { name: w._serialized === '2@lid' ? savedName : undefined,
  guest: w._serialized === '1@lid', notify: w._serialized === '1@lid' ? guestName : '' } };
const modules = {
  WAWebCallCollection: calls,
  WAWebEnsureVoipInited: { ensureVoipInitialized: async () => {} },
  WAWebVoipStackInterface: { getVoipStackInterface: async () => stack },
  WAWebVoipCreateCallLink: { createCallLink: async (o) => { created.push(o.callType);
    return 'https://call.whatsapp.com/' + (o.callType === 'video' ? 'video/' : 'voice/') + TOKEN; } },
  WAWebContactCollection: { ContactCollection: contacts },
  WAWebFrontendContactGetters: { getIsGuest: (c) => c?.guest === true, getMyUsername: () => null },
  WAWebContactGetters: { getNotifyName: (c) => c?.notify || null },
  WAWebVoipSignalingEnums: { EndCallReason: { Unknown: 0 } },
};
global.window = { require: (name) => modules[name], setTimeout: (fn) => fn() };
function fresh() {
  active = null; pendingOut = null; pendingLink = null; created = []; previews = []; joins = 0;
  decided = []; ended = 0; previewStatus = 0; joinState = 11; guestName = 'Pedro'; savedName = 'Ana';
  info = { isInWaitingRoom: false, isWaitingRoomAdmin: true };
  installCallLinkRuntimeInPage();
  return window.__winzappCallLinkRuntime;
}
(async () => {
  let api = fresh();
  assert.deepEqual(await api.create(true), { link: 'https://call.whatsapp.com/video/' + TOKEN, isVideo: true });
  assert.deepEqual(await api.create(false), { link: VOICE, isVideo: false });
  assert.deepEqual(created, ['video', 'audio']);

  for (const wrong of ['', null, 'https://call.whatsapp.com/voice/short', 'https://evil.example/voice/' + TOKEN,
                       'http://call.whatsapp.com/voice/' + TOKEN]) {
    assert.throws(() => api.check({ link: wrong }), /call link is required/);
  }
  assert.equal(api.check({ link: 'https://web.whatsapp.com/call/VIDEO/' + TOKEN + '?a=1' }).isVideo, true);

  // the join WhatsApp Web does after consent: preview then join, camera off
  api = fresh();
  const joined = await api.join({ link: VOICE });
  assert.deepEqual(joined, { id: 'LINKCALL', link: VOICE, isVideo: false, isCallLink: true, peerJid: '',
    state: 11, outgoing: false, waiting: false, admin: true });
  assert.deepEqual(previews, [[TOKEN, false, 0, null, false]]);
  assert.equal(joins, 1);
  assert.equal(pendingLink, null);
  api = fresh();
  await api.join({ link: 'https://call.whatsapp.com/video/' + TOKEN });
  assert.equal(previews[0][1], true);
  assert.equal(previews[0][4], true);

  // another call, a pending outgoing call or link, refuses before any stanza
  for (const setup of [() => { active = { id: 'X', getState: () => 6 }; }, () => { pendingOut = {}; },
                       () => { pendingLink = { isVideo: false }; }]) {
    api = fresh(); setup();
    await assert.rejects(api.join({ link: VOICE }), /Another call/);
    assert.equal(previews.length, 0);
  }
  api = fresh(); active = { id: 'OLD', getState: () => 13 };
  await api.join({ link: VOICE });

  // a refused preview never joins and frees the pending link
  api = fresh(); previewStatus = 3;
  await assert.rejects(api.join({ link: VOICE }), /did not open/);
  assert.equal(joins, 0);
  assert.equal(pendingLink, null);

  // a join that never connects is ended, not left behind
  api = fresh(); joinState = 1;
  await assert.rejects(api.join({ link: VOICE }), /did not join/);
  assert.equal(ended, 1);
  api = fresh(); joinState = null;
  await assert.rejects(api.join({ link: VOICE }), /did not join/);
  assert.equal(ended, 0);

  // waiting in someone else's room counts as joined, and says so
  api = fresh(); joinState = 10; info = { isInWaitingRoom: true, isWaitingRoomAdmin: false };
  stack.joinCallLink = async () => { joins += 1; active = { id: 'LINKCALL', isCallLink: true,
    getState: () => 10, isInWaitingRoom: true }; };
  assert.equal((await api.join({ link: VOICE })).waiting, true);
  delete stack.joinCallLink;
  stack.joinCallLink = async () => { joins += 1; active = { id: 'LINKCALL', isCallLink: true,
    getState: () => joinState, isWaitingRoomAdmin: true, isWaitingRoomEnabled: true, waitingRoomUsers: [] }; };

  // the admin's waiting room: names, never numbers
  api = fresh();
  await api.join({ link: VOICE });
  active.waitingRoomUsers = [wid('1@lid'), wid('2@lid'), wid('3@lid'), wid('bogus')];
  guestName = '5511 9999-0000';
  let room = await api.waitingRoom('LINKCALL');
  assert.deepEqual(room.people, [{ jid: '1@lid', name: '', guest: true }, { jid: '2@lid', name: 'Ana', guest: false },
    { jid: '3@lid', name: '', guest: false }]);
  guestName = 'Pedro';
  room = await api.waitingRoom('LINKCALL');
  assert.equal(room.people[0].name, 'Pedro');
  assert.equal(room.admin, true);
  await assert.rejects(api.waitingRoom('OTHER'), /not in that call/);

  assert.deepEqual(await api.admit({ callId: 'LINKCALL', jid: '1@lid' }), { id: 'LINKCALL', admitted: true });
  await api.admit({ callId: 'LINKCALL', jid: '2@lid', admit: false });
  assert.deepEqual(decided, [['admit', '1@lid'], ['deny', '2@lid']]);
  await assert.rejects(api.admit({ callId: 'LINKCALL', jid: '9@lid' }), /not waiting/);
  active.isWaitingRoomAdmin = false;
  await assert.rejects(api.admit({ callId: 'LINKCALL', jid: '1@lid' }), /admin/);
  assert.equal(api.mayStopAudio(), false);
  active = null;
  assert.equal(api.mayStopAudio(), true);
  console.log(JSON.stringify({ passed: true }));
})().catch((error) => { console.error(error); process.exit(1); });
'''


def test_call_links_through_whatsapp_webs_own_code(tmp_path):
    node = ROOT / "client/node/node.exe"
    node = str(node) if node.exists() else shutil.which("node")
    if not node:
        pytest.skip("Node not available")
    source = (ROOT / "client/api_patches/src/util/callLinkRuntime.ts").read_text(encoding="utf-8")
    source = source.replace("export function ", "function ")
    for annotation in (" as any", ": Promise<any>", ": any", ": string", ": void", ": boolean"):
        source = source.replace(annotation, "")
    script = tmp_path / "call-links.cjs"
    script.write_text(source + "\n" + HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"]
