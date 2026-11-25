"""wa-js patch: the Meta module loader stops saturating WhatsApp Web's login page.

Since WhatsApp Web 2.3000.1055857453 (measured 2026-11-24, wa-js 4.8.2), a
browser that is not logged in can be sent to `/login/`: an A/B experiment
(`WAWebLoggedOutSeparationGating`) answers `/` with a 302 there, and the
document it serves (~185 KB, `WAWebLoggedOutBootstrap`) registers only what
the QR screen needs. ChatCollection, MsgCollection, ContactCollection,
SocketModel, StreamModel and about 240 of their dependencies are predeclared
by name but never defined on that page, so `importNamespace()` throws
"Requiring module ... with unresolved dependencies" for every one of them.

wa-js's Meta loader was written for the full document, where they resolve.
On the login page it turns into a busy loop that never ends:

- every getter for a missing store (`ChatStore`, `Stream`, `Socket`, ...)
  calls `searchId()`, which walks all ~6,700 modules calling
  `moduleRequire()` -> `importNamespace()` on each, and the failing ones are
  the expensive ones (WhatsApp's resolver walks their dependency graph and
  throws);
- a miss is trusted for one second only (`SEARCH_MISS_TTL`), and the
  `moduleRequire.m` getter runs `Object.keys()` over the whole registry on
  every access, so the same full walk repeats every second for every getter
  wa-js, WPPConnect's wapi and WinZapp's bridges touch.

CPU profile of the page over CDP: 100% busy, 0% idle, 52% in wa-js
(searchId/moduleRequire) and 47% in WhatsApp's own `require`. The QR screen
took 47 s to draw, `Debug.VERSION` (wa-js's `conn.main_init`) 72 s, a
`Runtime.evaluate` timed out after 180 s, and WPPConnect closed the session
at its 60 s `WPP.isReady` deadline: no QR code at all.

Fix: a failed `importNamespace()` is not retried until something new can have
been defined. Modules only arrive in resources the Bootloader loads, so its
`getLoadedResourceCount()` is the epoch: a failure, a search miss and the
registry's key list are reused while that count is unchanged (30 s, 30 s and
1 s at most). New module names still end a cached miss within a second, as
upstream's own key-count check does. The one cost: a module that was already
predeclared and gets defined outside a Bootloader resource is seen up to 30 s
late instead of 1 s; the full document and the move to it after a link load
resources, so the count moves there. With it, measured in an isolated
WPPConnect on the same builds: QR in
6-10 s on 2.3000.1055857453 and on 2.3000.1056039834, unchanged on
2.3000.1055837054 (full document). Without `getLoadedResourceCount` the
loader behaves exactly as upstream.

The minified bundle renames temporaries between builds, so every hunk is
matched by structure with back-references, as the other wa-js patches are.
The four hunks depend on each other and are applied together or not at all.
"""
import os
import re

from core.wppconnect_wa_js_patch import (
    STATUS_ALREADY, STATUS_APPLIED, STATUS_NO_MATCH, WA_JS_BUNDLE_PARTS,
)

#: Present only in a patched bundle.
MARKER = "wzRequire.wzEpoch=wzEpoch"

_ID = r"[\w$]+"

# t.moduleRequire=function(t){try{return e.ErrorGuard.skipGuardGlobal(!0),e.importNamespace(t)}catch(e){}return null}
_REQUIRE = re.compile(
    rf"(?P<loader>{_ID})\.moduleRequire=function\((?P<id>{_ID})\)\{{try\{{return "
    rf"(?P<g>{_ID})\.ErrorGuard\.skipGuardGlobal\(!0\),(?P=g)\.importNamespace\((?P=id)\)\}}"
    rf"catch\({_ID}\)\{{\}}return null\}}"
)

# get:()=>function(){var e;const r=(0,t.__debug)().modulesMap,n=Object.keys(r);if(d&&n.length===f)return d;
_KEYS = re.compile(
    rf"get:\(\)=>function\(\)\{{var (?P<var>{_ID});const (?P<map>{_ID})=\(0,(?P<loader>{_ID})\.__debug\)\(\)"
    rf"\.modulesMap,(?P<keys>{_ID})=Object\.keys\((?P=map)\);"
    rf"if\((?P<cache>{_ID})&&(?P=keys)\.length===(?P<count>{_ID})\)return (?P=cache);"
)

# if(null===i&&h.get(e)===Object.keys(t.moduleRequire.m).length&&("meta"!==t.loaderType||Date.now()-(null!==(o=_.get(e))&&void 0!==o?o:0)<1e3))return null;
_MISS_CHECK = re.compile(
    rf"(?P<check>if\(null===(?P<cached>{_ID})&&(?P<counts>{_ID})\.get\((?P<cond>{_ID})\)===Object\.keys\("
    rf"(?P<loader>{_ID})\.moduleRequire\.m\)\.length&&\(\"meta\"!==(?P=loader)\.loaderType\|\|Date\.now\(\)-"
    rf"\(null!==\((?P<tmp>{_ID})=(?P<times>{_ID})\.get\((?P=cond)\)\)&&void 0!==(?P=tmp)\?(?P=tmp):0\)<1e3\)\)"
    rf"return null;)"
)

# b.set(e,null),h.set(e,a.length),_.set(e,Date.now()),null}
_MISS_SET = re.compile(
    rf"(?P<head>(?P<ids>{_ID})\.set\((?P<cond>{_ID}),null\),(?P<counts>{_ID})\.set\((?P=cond),{_ID}\.length\),"
    rf"(?P<times>{_ID})\.set\((?P=cond),Date\.now\(\)\)),null\}}"
)


def _require(m):
    loader, g = m.group("loader"), m.group("g")
    # wzEpoch: Bootloader's loaded-resource count, -1 when unknown. Bootloader
    # may export itself or a `default` (wa-js's own getBootloader() accepts
    # both); a failed lookup is retried at most once a second.
    # wzCount: the key count of moduleRequire.m, recomputed only when the
    # getter hands out a new object, so searchId keeps upstream's "new module
    # names appeared -> search again" without an Object.keys per lookup.
    return (
        f"{loader}.moduleRequire=function(){{let wzBoot=null,wzBootTried=0,wzCountOf=null,wzCountN=-1;"
        f"const wzFail=new Map,"
        f"wzEpoch=()=>{{try{{if(!wzBoot){{if(Date.now()-wzBootTried<1e3)return-1;wzBootTried=Date.now();"
        f"const wzMod={g}.require(\"Bootloader\");"
        f"wzBoot=wzMod&&\"function\"==typeof wzMod.getLoadedResourceCount?wzMod:wzMod&&wzMod.default}}"
        f"return wzBoot&&\"function\"==typeof wzBoot.getLoadedResourceCount?wzBoot.getLoadedResourceCount():-1}}"
        f"catch(wzErr){{return-1}}}},"
        f"wzRequire=function(wzId){{const wzNow=wzEpoch(),wzLast=wzFail.get(wzId);"
        f"if(-1!==wzNow&&wzLast&&wzLast.epoch===wzNow&&Date.now()-wzLast.at<3e4)return null;"
        f"try{{return {g}.ErrorGuard.skipGuardGlobal(!0),{g}.importNamespace(wzId)}}"
        f"catch(wzErr){{-1!==wzNow&&wzFail.set(wzId,{{epoch:wzNow,at:Date.now()}})}}return null}};"
        f"return {MARKER},wzRequire.wzMiss=new Map,wzRequire.wzKeys=null,"
        f"wzRequire.wzCount=()=>{{const wzM=wzRequire.m;"
        f"return wzM!==wzCountOf&&(wzCountOf=wzM,wzCountN=Object.keys(wzM).length),wzCountN}},wzRequire}}()"
    )


def _keys(m):
    req = f"{m.group('loader')}.moduleRequire"
    cache = m.group("cache")
    return (
        f"get:()=>function(){{var {m.group('var')};"
        f"const wzNow={req}.wzEpoch?{req}.wzEpoch():-1;"
        f"if({cache}&&-1!==wzNow&&{req}.wzKeys&&{req}.wzKeys.epoch===wzNow&&Date.now()-{req}.wzKeys.at<1e3)"
        f"return {cache};"
        f"const {m.group('map')}=(0,{m.group('loader')}.__debug)().modulesMap,"
        f"{m.group('keys')}=Object.keys({m.group('map')});"
        f"if(-1!==wzNow&&({req}.wzKeys={{epoch:wzNow,at:Date.now()}}),"
        f"{cache}&&{m.group('keys')}.length==={m.group('count')})return {cache};"
    )


def _miss_check(m):
    req = f"{m.group('loader')}.moduleRequire"
    cond = m.group("cond")
    return (
        f"if(null==={m.group('cached')}&&\"meta\"==={m.group('loader')}.loaderType&&{req}.wzEpoch"
        f"&&{req}.wzMiss.get({cond})==={req}.wzEpoch()&&Date.now()-({m.group('times')}.get({cond})||0)<3e4"
        f"&&{m.group('counts')}.get({cond})==={req}.wzCount())"
        f"return null;" + m.group("check")
    )


def patch_meta_loader_source(content: str) -> tuple[str, str]:
    """(*content* with the four loader hunks, status). All four must match
    exactly once, or nothing is changed (STATUS_NO_MATCH); a bundle carrying
    MARKER is STATUS_ALREADY."""
    if MARKER in content:
        return content, STATUS_ALREADY
    matches = [rx.findall(content) for rx in (_REQUIRE, _KEYS, _MISS_CHECK, _MISS_SET)]
    if any(len(found) != 1 for found in matches):
        return content, STATUS_NO_MATCH
    check = _MISS_CHECK.search(content)
    req = f"{check.group('loader')}.moduleRequire"
    patched = _REQUIRE.sub(_require, content, count=1)
    patched = _KEYS.sub(_keys, patched, count=1)
    patched = _MISS_CHECK.sub(_miss_check, patched, count=1)
    # An unknown epoch (-1) is never recorded, so the miss check above cannot
    # match it and the loader keeps upstream's one-second retry.
    patched = _MISS_SET.sub(
        lambda m: (f"{m.group('head')},{req}.wzEpoch&&-1!=={req}.wzEpoch()"
                   f"&&{req}.wzMiss.set({m.group('cond')},{req}.wzEpoch()),null}}"),
        patched, count=1)
    return patched, STATUS_APPLIED


def patch_meta_loader_bundle(api_dir: str) -> tuple[bool, str]:
    """Patch the wa-js bundle under *api_dir* (the outer client/api)."""
    path = os.path.join(api_dir, *WA_JS_BUNDLE_PARTS)
    if not os.path.isfile(path):
        return False, "wppconnect-wa.js not found — skipping the Meta loader patch."
    with open(path, encoding="utf-8", newline="") as stream:
        content = stream.read()
    patched, status = patch_meta_loader_source(content)
    if status == STATUS_APPLIED:
        with open(path, "w", encoding="utf-8", newline="") as stream:
            stream.write(patched)
        return True, ("Patched wppconnect-wa.js — the Meta loader no longer re-requires "
                      "modules WhatsApp Web's login page never defines.")
    if status == STATUS_ALREADY:
        return True, "wppconnect-wa.js Meta loader patch already applied."
    return False, ("wppconnect-wa.js: the Meta loader (moduleRequire, its module map and "
                   "searchId's miss cache) did not match the expected upstream source — skipping.")
