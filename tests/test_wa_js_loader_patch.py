"""wa-js patch: the Meta loader stops re-requiring modules a page never defines.

On WhatsApp Web's login-only page (2.3000.1055857453 and later, 2026-11-24)
the stores wa-js binds are predeclared and never defined, so every lookup
throws; wa-js retried each miss with a full walk of the module registry every
second, the page saturated and no QR code ever came. The loader below is
copied verbatim from the installed wa-js 4.8.2 bundle and run under Node,
unpatched and patched, against a synthetic registry. No session, no network.
"""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.wppconnect_wa_js_loader_patch import (
    MARKER, patch_meta_loader_bundle, patch_meta_loader_source,
)
from core.wppconnect_wa_js_patch import (
    STATUS_ALREADY, STATUS_APPLIED, STATUS_NO_MATCH, WA_JS_BUNDLE_PARTS,
)

ROOT = Path(__file__).resolve().parents[1]

LOADER = (
    't.moduleRequire=function(t){try{return e.ErrorGuard.skipGuardGlobal(!0),e.importNamespace(t)}'
    'catch(e){}return null},Object.defineProperty(t.moduleRequire,"m",{get:()=>function(){var e;'
    'const r=(0,t.__debug)().modulesMap,n=Object.keys(r);if(d&&n.length===f)return d;const o={};'
    'for(const t of n)/^(?:use)?WA/.test(t)&&!(0,a.isMetaModuleBlacklisted)(t)&&(o[t]=null===(e=r[t])'
    '||void 0===e?void 0:e.factory);return d=o,f=n.length,o}()})'
)
SEARCH = (
    'function C(e,r=!1,n){var o;const i=b.get(e);if(i)return i;if(null===i&&h.get(e)===Object.keys('
    't.moduleRequire.m).length&&("meta"!==t.loaderType||Date.now()-(null!==(o=_.get(e))&&void 0!==o'
    '?o:0)<1e3))return null;const a=Object.keys(t.moduleRequire.m),s=r?a.slice().reverse():a;let l;'
    'if(n){const e=S(n);if(e.length){const t=new Set(e);l=r?e.slice().reverse().concat(s.filter(e=>'
    '!t.has(e))):e.concat(s.filter(e=>!t.has(e)))}else l=s}else l=s;const c=setTimeout(()=>{u('
    '`Searching for: ${e.toString()}`)},500);for(const r of l)if("webpack"!==t.loaderType||!P(r))'
    'try{const n=(0,t.moduleRequire)(r);if("meta"===t.loaderType&&null==n)continue;if("webpack"!=='
    't.loaderType&&M(r,n))continue;if(e(n,r))return u(`Module found: ${r} - ${e.toString()}`),'
    'clearTimeout(c),b.set(e,r),r}catch(e){continue}const d=Object.keys(t.fallbackModules);for(const '
    'r of d)try{const n=t.fallbackModules[r];if(e(n,r))return u(`Fallback Module found: ${r} - '
    '${e.toString()}`),clearTimeout(c),b.set(e,r),r}catch(e){continue}return clearTimeout(c),u('
    '`Module not found: ${e.toString()}`),b.set(e,null),h.set(e,a.length),_.set(e,Date.now()),null}'
)
SOURCE = LOADER + ";" + SEARCH

# A registry like the login page's: the store wa-js wants is predeclared and
# throws, and so do fifty others. getLoadedResourceCount() is the epoch. The
# scenario runs after the loader and returns what the test asserts on.
_HARNESS = r"""
const config = CONFIG;
let clock = 1000000;
Date.now = () => clock;
let calls = 0, epoch = 1;
const defined = new Set();
const modulesMap = {WAWebTarget: {factory() {}}};
for (let i = 0; i < 50; i++) modulesMap['WAWebMissing' + i] = {factory() {}};
const bootloader = {getLoadedResourceCount: () => epoch};
const e = {
  ErrorGuard: {skipGuardGlobal() {}},
  importNamespace(id) {
    calls++;
    if (defined.has(id)) return {[id]: true};
    throw new Error('Requiring module "' + id + '" with unresolved dependencies');
  },
  require(name) {
    if (name === 'Bootloader' && config.bootloader === 'own') return bootloader;
    if (name === 'Bootloader' && config.bootloader === 'default') return {default: bootloader};
    throw new Error('Requiring unknown module "' + name + '"');
  },
};
const t = {loaderType: 'meta', fallbackModules: {}, __debug: () => ({modulesMap})};
const a = {isMetaModuleBlacklisted: () => false};
let d = null, f = -1;
const b = new Map(), h = new Map(), _ = new Map();
const u = () => {}, P = () => false, M = () => false, S = () => [];
SOURCE;
const wanted = (m) => Boolean(m && m.WAWebTarget);
const out = (() => { SCENARIO })();
console.log(JSON.stringify(out));
"""

# Ten lookups 1.5 s apart while the store is missing, then a resource loads
# and defines it.
MISSING_THEN_LOADED = """
for (let i = 0; i < 10; i++) { clock += 1500; C(wanted); }
const whileMissing = calls;
epoch++; defined.add('WAWebTarget'); clock += 10;
return {whileMissing, found: C(wanted)};
"""


def _run(source, bootloader="own", scenario=MISSING_THEN_LOADED):
    bundled = ROOT / "client/node/node.exe"
    node = str(bundled) if bundled.exists() else shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = (_HARNESS.replace("CONFIG", json.dumps({"bootloader": bootloader}))
              .replace("SOURCE;", source + ";").replace("SCENARIO", scenario))
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20,
                            check=True)
    return json.loads(result.stdout)


def _patched():
    patched, status = patch_meta_loader_source(SOURCE)
    assert status == STATUS_APPLIED
    return patched


def test_unpatched_loader_rewalks_the_registry_every_second():
    """Ten lookups 1.5 s apart: 51 failing requires each, 510 in all."""
    result = _run(SOURCE)
    assert result["whileMissing"] == 510


@pytest.mark.parametrize("bootloader", ["own", "default"])
def test_patched_loader_walks_once_until_a_resource_loads(bootloader):
    """Bootloader exported as itself or as `default`, as wa-js accepts."""
    result = _run(_patched(), bootloader=bootloader)
    assert result["whileMissing"] == 51
    # a newly loaded resource defines the store: found on the next lookup
    assert result["found"] == "WAWebTarget"


def test_without_the_bootloader_epoch_the_loader_behaves_as_upstream():
    assert _run(_patched(), bootloader="none") == _run(SOURCE, bootloader="none")


def test_a_definition_outside_a_resource_is_seen_within_thirty_seconds():
    """The cap that bounds the one trade-off: nothing new loaded, the store
    becomes resolvable anyway. Upstream sees it within a second."""
    # times are counted from the failed lookup
    scenario = """
    clock += 1500; C(wanted);
    defined.add('WAWebTarget');
    clock += 29000; const lookup29 = C(wanted), require29 = t.moduleRequire('WAWebTarget');
    clock += 2000; const require31 = t.moduleRequire('WAWebTarget');
    return {lookup29, require29, found31: C(wanted), require31};
    """
    result = _run(_patched(), scenario=scenario)
    assert result == {"lookup29": None, "require29": None, "found31": "WAWebTarget",
                      "require31": {"WAWebTarget": True}}
    upstream = _run(SOURCE, scenario=scenario)
    assert upstream["lookup29"] == "WAWebTarget"


def test_new_module_names_end_a_cached_miss_as_upstream():
    """A name registered without a new resource (inline __d) is searched
    again within a second, like upstream's key-count check."""
    scenario = """
    clock += 1500; C(wanted);
    modulesMap.WAWebNew = {factory() {}}; defined.add('WAWebNew');
    clock += 1500;
    return {found: C((m) => Boolean(m && m.WAWebNew))};
    """
    assert _run(_patched(), scenario=scenario) == {"found": "WAWebNew"}


def test_a_repeat_is_a_no_op():
    once, status = patch_meta_loader_source(SOURCE)
    assert status == STATUS_APPLIED and MARKER in once
    assert patch_meta_loader_source(once) == (once, STATUS_ALREADY)


def test_renamed_temporaries_still_match():
    """The minifier renames temporaries between builds."""
    renamed = (SOURCE.replace("b.get(e)", "q.get(e)").replace("b.set(e,", "q.set(e,")
               .replace("const r=(0,t.__debug)().modulesMap,n=Object.keys(r);",
                        "const z=(0,t.__debug)().modulesMap,n=Object.keys(z);")
               .replace("null===(e=r[t])", "null===(e=z[t])"))
    patched, status = patch_meta_loader_source(renamed)
    assert status == STATUS_APPLIED and "q.set(e,null)" in patched


@pytest.mark.parametrize("hunk, changed", [
    ("e.importNamespace(t)", "e.importDefault(t)"),
    ("if(d&&n.length===f)return d;", "if(d&&n.length>=f)return d;"),
    ("<1e3))return null;", "<5e3))return null;"),
    ("_.set(e,Date.now()),null}", "_.set(e,performance.now()),null}"),
])
def test_a_bundle_missing_any_hunk_is_untouched(hunk, changed):
    """The four hunks depend on each other: one that moved upstream leaves
    the whole loader as it is."""
    content = SOURCE.replace(hunk, changed)
    assert content != SOURCE
    assert patch_meta_loader_source(content) == (content, STATUS_NO_MATCH)


def test_the_bundle_file_changes_only_in_the_loader(tmp_path):
    bundle = tmp_path.joinpath(*WA_JS_BUNDLE_PARTS)
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(('"use strict";\r\n' + SOURCE + '\r\n//# sourceMappingURL=x\r\n').encode())
    ok, _note = patch_meta_loader_bundle(str(tmp_path))
    assert ok
    patched = bundle.read_bytes()
    assert patched.startswith(b'"use strict";\r\n') and patched.endswith(b'\r\n//# sourceMappingURL=x\r\n')
    ok, _note = patch_meta_loader_bundle(str(tmp_path))
    assert ok and bundle.read_bytes() == patched
    assert patch_meta_loader_bundle(str(tmp_path / "missing"))[0] is False


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def loader_bundle(tmp_path):
    bundle = tmp_path.joinpath(*WA_JS_BUNDLE_PARTS)
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(('"use strict";\r\n' + SOURCE + ';\r\n//# sourceMappingURL=x\r\n').encode())
    return tmp_path, bundle


class TestMetaLoaderCallSites:
    """The per-launch re-apply is how the fix reaches existing installs; the
    runtime pin test only covers setup_api and skips without client/api."""

    def test_setup_api(self, loader_bundle):
        api, bundle = loader_bundle
        assert _load("setup_api")._patch_meta_loader_bundle(str(api))
        assert patch_meta_loader_source(bundle.read_text())[1] == STATUS_ALREADY

    def test_api_setup_on_startup(self, loader_bundle):
        from ui.dialogs.api_setup import ApiSetupDialog
        api, bundle = loader_bundle
        ApiSetupDialog._apply_node_modules_patches(str(api))
        assert patch_meta_loader_source(bundle.read_text())[1] == STATUS_ALREADY

    def test_build_api(self, loader_bundle):
        api, bundle = loader_bundle
        _load("build_api")._apply_node_modules_patches(str(api))
        assert patch_meta_loader_source(bundle.read_text())[1] == STATUS_ALREADY
