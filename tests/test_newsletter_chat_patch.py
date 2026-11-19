"""wa-js patch: a post to a channel finds the channel's chat and its job.

chat.find() looked the chat up again in ChatStore, which never holds a
channel's chat (WhatsApp Web keeps it in the newsletter collection), so every
post to a channel failed with "Chat not found in ChatStore". The find() below
is copied verbatim from the installed wa-js 4.7.2 bundle and run under Node,
unpatched and patched, against synthetic stores. No session, no network.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.wppconnect_newsletter_chat_patch import (
    patch_newsletter_chat_bundle, patch_newsletter_chat_source,
)
from core.wppconnect_wa_js_patch import (
    STATUS_ALREADY, STATUS_APPLIED, STATUS_NO_MATCH, WA_JS_BUNDLE_PARTS,
)

ROOT = Path(__file__).resolve().parents[1]
FIND = (
    't.find=async function(e,t="createChat"){var r;const a=(0,n.assertWid)(e),s=await(0,i.'
    'findOrCreateLatestChat)(a,t);if(!(null===(r=null==s?void 0:s.chat)||void 0===r?void 0:'
    'r.id))throw new Error(`Failed to find or create chat for ${a.toString()}`);const u=o.'
    'ChatStore.get(s.chat.id);if(!u)throw new Error(`Chat not found in ChatStore for '
    '${a.toString()}`);u.id.isGroup()&&await o.GroupMetadataStore.find(u.id);return u};'
)
OPEN_CHAT_BOTTOM = (
    'const c=i.ChatStore.get(u.chat.id);if(!c)throw new Error(`Chat not found in ChatStore '
    'for ${s.toString()}`);'
)

_HARNESS = r"""
const config = CONFIG;
const wid = {
  toString: () => config.jid,
  isGroup: () => config.jid.endsWith('@g.us'),
  isNewsletter: () => config.jid.endsWith('@newsletter'),
};
const chat = {id: wid, name: 'found'};
const n = {assertWid: e => e};
const i = {async findOrCreateLatestChat() { return {chat, created: false}; }};
const o = {
  ChatStore: {get: id => (config.inChatStore ? chat : undefined)},
  NewsletterStore: {get: id => (config.inNewsletterStore ? chat : undefined)},
  GroupMetadataStore: {async find() {}},
};
const t = {};
FIND
t.find(wid).then(
  found => console.log(JSON.stringify({found: found === chat})),
  error => console.log(JSON.stringify({error: error.message.split(' for ')[0]})));
"""


def _run(find, jid, in_chat_store, in_newsletter_store):
    bundled = ROOT / "client/node/node.exe"
    node = str(bundled) if bundled.exists() else shutil.which("node")
    if not node:
        pytest.skip("node not available")
    config = {"jid": jid, "inChatStore": in_chat_store, "inNewsletterStore": in_newsletter_store}
    script = _HARNESS.replace("CONFIG", json.dumps(config)).replace("FIND", find)
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20,
                            check=True)
    return json.loads(result.stdout)


CHANNEL = "120363000000000001@newsletter"


def test_unpatched_find_cannot_post_to_a_channel():
    assert _run(FIND, CHANNEL, False, True) == {"error": "Chat not found in ChatStore"}


def test_patched_find_takes_a_channel_from_the_newsletter_store():
    patched, status = patch_newsletter_chat_source(FIND)
    assert status == STATUS_APPLIED
    assert _run(patched, CHANNEL, False, True) == {"found": True}


@pytest.mark.parametrize("jid", ["10000000001@c.us", "10000000001-1000000000@g.us",
                                 "10000000000001@lid"])
def test_any_other_chat_is_looked_up_as_before(jid):
    patched, _ = patch_newsletter_chat_source(FIND)
    assert _run(patched, jid, True, True) == {"found": True}
    # never taken from the newsletter store, even if it held the id
    assert _run(patched, jid, False, True) == {"error": "Chat not found in ChatStore"}


def test_a_channel_in_neither_store_still_fails():
    patched, _ = patch_newsletter_chat_source(FIND)
    assert _run(patched, CHANNEL, False, False) == {"error": "Chat not found in ChatStore"}


def test_every_lookup_is_patched_once_and_a_repeat_is_a_no_op():
    source = FIND + OPEN_CHAT_BOTTOM
    once, status = patch_newsletter_chat_source(source)
    twice, repeated = patch_newsletter_chat_source(once)
    assert status == STATUS_APPLIED and repeated == STATUS_ALREADY and twice == once
    assert once.count("NewsletterStore.get(") == 2


def test_an_unrecognized_bundle_is_untouched():
    content = FIND.replace("ChatStore.get(s.chat.id)", "ChatStore.find(s.chat.id)")
    assert patch_newsletter_chat_source(content) == (content, STATUS_NO_MATCH)


def test_the_bundle_file_changes_only_in_the_lookup(tmp_path):
    bundle = tmp_path.joinpath(*WA_JS_BUNDLE_PARTS)
    bundle.parent.mkdir(parents=True)
    original = ('"use strict";\r\n' + FIND + '\r\n//# sourceMappingURL=x\r\n').encode()
    bundle.write_bytes(original)
    ok, _note = patch_newsletter_chat_bundle(str(tmp_path))
    assert ok
    patched = bundle.read_bytes()
    assert patched.startswith(b'"use strict";\r\n') and patched.endswith(b'\r\n//# sourceMappingURL=x\r\n')
    ok, _note = patch_newsletter_chat_bundle(str(tmp_path))
    assert ok and bundle.read_bytes() == patched
    assert patch_newsletter_chat_bundle(str(tmp_path / "missing"))[0] is False


# The binding from the installed wa-js 4.7.2 bundle, and a synthetic loader
# that walks the modules in WhatsApp Web's order: the UI action first.
JOB_BINDING = (
    '(0,r(54993).exportModule)(t,{sendNewsletterMessageJob:["sendNewsletterMessageJob",'
    '"sendNewsletterMessage"]},e=>e.sendNewsletterMessageJob||e.sendNewsletterMessage)'
)
_JOB_HARNESS = r"""
const modules = new Map([
  ['WAWebNewsletterSendMsgAction', {sendNewsletterMessage: () => 'ui action'}],
  ['WAWebNewsletterSendMessageJob', {sendNewsletterMessageJob: () => 'job'}],
]);
const t = {};
const r = () => ({exportModule(target, names, predicate) {
  const match = [...modules].find(([name, module]) => predicate(module, name));
  if (!match) throw new Error('Native module not found');
  for (const [publicName, nativeNames] of Object.entries(names)) {
    const found = [].concat(nativeNames).find(n => typeof match[1][n] === 'function');
    target[publicName] = match[1][found];
  }
}});
BINDING;
console.log(JSON.stringify({bound: t.sendNewsletterMessageJob()}));
"""


def _bind(binding):
    bundled = ROOT / "client/node/node.exe"
    node = str(bundled) if bundled.exists() else shutil.which("node")
    if not node:
        pytest.skip("node not available")
    result = subprocess.run([node, "-e", _JOB_HARNESS.replace("BINDING", binding)],
                            capture_output=True, text=True, timeout=20, check=True)
    return json.loads(result.stdout)["bound"]


def test_unpatched_binding_takes_the_ui_send_action():
    assert _bind(JOB_BINDING) == "ui action"


@pytest.mark.parametrize("binding", [JOB_BINDING, JOB_BINDING.replace("e=>e.", "$x=>$x.").replace("||e.", "||$x.")])
def test_patched_binding_takes_the_send_job(binding):
    patched, status = patch_newsletter_chat_source(binding)
    assert status == STATUS_APPLIED and _bind(patched) == "job"
    assert patch_newsletter_chat_source(patched) == (patched, STATUS_ALREADY)


def test_both_fixes_apply_to_one_bundle():
    once, status = patch_newsletter_chat_source(FIND + JOB_BINDING)
    assert status == STATUS_APPLIED
    assert "NewsletterStore.get(" in once and '"WAWebNewsletterSendMessageJob"===name' in once
    assert patch_newsletter_chat_source(once) == (once, STATUS_ALREADY)
