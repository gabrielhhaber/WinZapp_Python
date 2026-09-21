import os
import shutil
import subprocess
import sys

import setup_api as canonical_setup


def _sync_canonical_patches(api_dir: str, api_patches_dir: str) -> int:
    """Copy only WinZapp-owned source/root patches into client/api.

    package.json is intentionally not copied wholesale: setup_api.py merges
    only WinZapp-owned dependencies into the upstream manifest.
    """
    rel_paths = canonical_setup.CUSTOM_ROOT_FILES + canonical_setup.CUSTOM_SRC_FILES
    synced = 0
    for rel_path in rel_paths:
        src_path = os.path.join(api_patches_dir, rel_path)
        if not os.path.isfile(src_path):
            print(f"[WARNING] Canonical API patch missing: {rel_path}")
            continue
        dest_path = os.path.join(api_dir, rel_path)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(src_path, dest_path)
        synced += 1
        print(f"[INFO] Synced custom patch: {rel_path}")
    return synced


def _verify_critical_call_patch(api_dir: str) -> None:
    """Fail if dist/callController.js is older than the patched source."""
    source_path = os.path.join(api_dir, "src", "controller", "callController.ts")
    compiled_path = os.path.join(api_dir, "dist", "controller", "callController.js")

    if not os.path.isfile(source_path):
        raise RuntimeError("Patched src/controller/callController.ts is missing")
    if not os.path.isfile(compiled_path):
        raise RuntimeError("Compiled dist/controller/callController.js is missing")

    with open(source_path, encoding="utf-8", errors="replace") as fh:
        source = fh.read()
    with open(compiled_path, encoding="utf-8", errors="replace") as fh:
        compiled = fh.read()

    # These strings used to be checked only when the current source contained
    # them, so a refactor could rename the routing without breaking the build.
    # That clause is how this guard silently stopped guarding anything: it was
    # still naming the markers of the group-call routing this controller no
    # longer has
    # ("native-group-chat", "native-group-wids", "WhatsApp Web group calling
    # gate is disabled"), none of which appear in the source any more, so
    # `missing` was unconditionally empty and a stale dist/ passed. Markers
    # here must be verified to exist in the CURRENT source -- the test below
    # does exactly that, rather than only checking the literals are present in
    # this file, which is what it used to do.
    markers = (
        "forgetIncomingCall",
        "__winzappForgetIncomingCall",
        "runNativeVoipAction",
    )
    absent_from_source = [marker for marker in markers if marker not in source]
    if absent_from_source:
        raise RuntimeError(
            "_verify_critical_call_patch markers no longer exist in "
            "callController.ts, so this guard would never fire: "
            + ", ".join(absent_from_source)
        )
    missing = [marker for marker in markers if marker not in compiled]
    if missing:
        raise RuntimeError(
            "Compiled callController.js is stale; missing current source markers: "
            + ", ".join(missing)
        )


def _apply_node_modules_patches(api_dir: str) -> None:
    custom_decrypt = os.path.join(api_dir, "decrypt.js")
    decrypt_target = os.path.join(
        api_dir,
        "node_modules",
        "@wppconnect-team",
        "wppconnect",
        "dist",
        "api",
        "helpers",
        "decrypt.js",
    )
    if os.path.isfile(custom_decrypt) and os.path.isdir(os.path.dirname(decrypt_target)):
        shutil.copy2(custom_decrypt, decrypt_target)
        print("[OK] Copied decrypt.js patch to node_modules.")

    for patcher in (
        canonical_setup._patch_wppconnect_host_layer,
        canonical_setup._patch_wppconnect_status_layer,
        canonical_setup._patch_wppconnect_sender_layer,
        canonical_setup._patch_wppconnect_welcome_layer,
    ):
        try:
            patcher(api_dir)
        except Exception as exc:
            print(f"[WARNING] Runtime node_modules patch failed: {exc}")


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    api_dir = os.path.join(base_dir, "client", "api")
    api_patches_dir = os.path.join(base_dir, "client", "api_patches")

    if not os.path.isdir(api_dir):
        print("[ERROR] client/api does not exist. Run setup_api.py first.")
        sys.exit(1)
    if not os.path.isdir(api_patches_dir):
        print("[ERROR] client/api_patches does not exist; refusing to build a vanilla/stale API.")
        sys.exit(1)

    print("[INFO] Syncing canonical WinZapp API patches...")
    synced_count = _sync_canonical_patches(api_dir, api_patches_dir)
    print(f"[OK] Synced {synced_count} canonical patch file(s).")

    # Keep upstream package metadata/version intact and merge only dependencies
    # WinZapp deliberately owns.
    canonical_setup._recover_upstream_package_json()
    canonical_setup._merge_package_json_dependencies()
    _apply_node_modules_patches(api_dir)

    node_exe = None
    npm_cli = None
    if sys.platform == "win32":
        portable_node = os.path.join(base_dir, "client", "node", "node.exe")
        portable_npm = os.path.join(
            base_dir, "client", "node", "node_modules", "npm", "bin", "npm-cli.js"
        )
        if os.path.isfile(portable_node) and os.path.isfile(portable_npm):
            node_exe = portable_node
            npm_cli = portable_npm
            print(f"[INFO] Using portable Node: {node_exe}")

    print("[INFO] Running build inside client/api...")
    try:
        if node_exe and npm_cli:
            cmd = [node_exe, npm_cli, "run", "build"]
            env = dict(os.environ)
            node_dir = os.path.dirname(node_exe)
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
            subprocess.run(cmd, cwd=api_dir, env=env, check=True)
        else:
            npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
            subprocess.run(
                [npm_cmd, "run", "build"],
                cwd=api_dir,
                shell=sys.platform == "win32",
                check=True,
            )

        _verify_critical_call_patch(api_dir)
        print("[OK] WPPConnect Server built successfully with current WinZapp patches.")
        print("[IMPORTANT] If the API is already running, restart its Node process before testing.")
    except Exception as exc:
        print(f"[ERROR] Failed to build API: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
