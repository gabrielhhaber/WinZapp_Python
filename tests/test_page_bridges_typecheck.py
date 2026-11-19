"""Type-check the page bridges with the server's own strict tsconfig.

The bridge tests run these .ts files as plain JavaScript (no annotations), so
they pass even when `tsc` -- the first step of `npm run build` -- rejects the
file. `{handler: null}` infers the type `null` under `strict`, and every later
assignment of a function then fails the build (PR #804: the macOS build broke
while every test passed). This compiles each bridge built on the shared page
helper with the options client/api/tsconfig.json uses.

Needs an existing client/api (Node + TypeScript); CI's build jobs run the real
`tsc`, so the test shards skip it.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
UTIL = ROOT / "client/api_patches/src/util"
COMPILER = Path(os.environ.get(
    "WINZAPP_TEST_TYPESCRIPT", ROOT / "client/api/node_modules/typescript/lib/typescript.js"))
# .../client/api/node_modules/typescript/lib/typescript.js -> .../client/api
API_DIR = COMPILER.parents[3]
HELPER = "messageStatePageBridge"


def page_bridges():
    """The shared helper plus every bridge that installs through it."""
    found = [UTIL / f"{HELPER}.ts"]
    for path in sorted(UTIL.glob("*.ts")):
        if path.stem != HELPER and f"from './{HELPER}'" in path.read_text(encoding="utf-8"):
            found.append(path)
    return found


def test_page_bridge_discovery_finds_the_helper_users():
    names = {path.stem for path in page_bridges()}
    assert {HELPER, "messageStarBridge", "privacyEventsBridge"} <= names


def test_page_bridges_compile_under_the_server_strict_tsconfig():
    node = shutil.which("node")
    if not node or not COMPILER.is_file() or not (API_DIR / "tsconfig.json").is_file():
        pytest.skip("Existing Node, TypeScript and client/api/tsconfig.json required; no install")
    script = r"""
const ts=require(process.argv[1]),path=require('path');
const apiDir=process.argv[2],files=JSON.parse(process.argv[3]);
const configPath=path.join(apiDir,'tsconfig.json');
const read=ts.readConfigFile(configPath,ts.sys.readFile);
if(read.error)throw Error(ts.flattenDiagnosticMessageText(read.error.messageText,'\n'));
// configPath anchors @types lookup in client/api, not next to the patch files.
const parsed=ts.parseJsonConfigFileContent(read.config,ts.sys,apiDir,undefined,configPath);
// A production install may lack dev-only type libraries (jest): keep those present.
const fs=require('fs'),types=(parsed.options.types||[]).filter(name=>
 fs.existsSync(path.join(apiDir,'node_modules/@types',name)));
const options={...parsed.options,types,noEmit:true,declaration:false,emitDeclarationOnly:false};
const program=ts.createProgram(files,options);
const errors=ts.getPreEmitDiagnostics(program).filter(d=>d.category===ts.DiagnosticCategory.Error);
for(const d of errors){
 const where=d.file?path.basename(d.file.fileName)+':'+(d.file.getLineAndCharacterOfPosition(d.start).line+1):'';
 console.log(where+' TS'+d.code+' '+ts.flattenDiagnosticMessageText(d.messageText,' '));
}
process.exit(errors.length?1:0);
"""
    files = [str(path) for path in page_bridges()]
    result = subprocess.run([node, "-e", script, str(COMPILER), str(API_DIR), json.dumps(files)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
