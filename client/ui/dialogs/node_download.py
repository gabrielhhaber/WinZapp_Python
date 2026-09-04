"""
node_download.py — WinZapp automatic portable Node.js download dialog.

Shown when client/node/node.exe is absent.  Downloads the Windows x64
portable Node.js distribution from nodejs.org and extracts it into
client/node/ so the bundled WPPConnect Server can run.

The user never needs to install Node.js manually.
"""

import hashlib
import io
import logging
import os
import shutil
import sys
import tempfile
import threading
import zipfile

import requests
import wx

from app_paths import resource_path
from node_download_config import (
    NODE_FILENAME,
    NODE_SHASUMS_URL,
    NODE_TOP_DIR,
    NODE_URL,
    NODE_VERSION,
)

log = logging.getLogger(__name__)

# Node 18.20.4 (this constant's value until 2026-08-13) is EOL and, more
# concretely, too old for real dependencies wppconnect-server pulls in:
# `sharp` (image/sticker conversion, required eagerly by
# @wppconnect-team/wppconnect's select-sticker helper) dropped Node 18
# support going from sharp 0.34.5 (wppconnect-server 2.10.1) to 0.35.x
# (wppconnect-server 2.10.4), now declaring `engines.node: ">=20.9.0"`.
# `npm install` itself still exits 0 on Node 18 (npm's engine check is a
# warning, not a hard failure — confirmed by testing both), so nothing
# visibly fails during setup; sharp instead throws
# "Could not load the sharp module using the win32-x64 runtime" the moment
# something actually tries to use it (sending/receiving a sticker). This is
# exactly what broke for users whose only Node was the one this dialog
# downloads (client/node/node.exe already bundled by the installer is newer
# and unaffected) after updating WPPConnect Server 2.10.1 -> 2.10.4 through
# the in-app updater. Picking a current LTS here rather than pinning to
# wppconnect-server's own exact `engines.node` value on purpose — this only
# needs to clear the real floor (sharp's >=20.9.0), not chase whatever exact
# string wppconnect-server happens to declare on its next release.
_NODE_VERSION = NODE_VERSION
_NODE_FILENAME = NODE_FILENAME
_NODE_URL = NODE_URL
# nodejs.org publishes a checksum manifest for every release — verify the
# download against it before ever extracting/running anything from it,
# rather than trusting an unauthenticated HTTP download outright.
_NODE_SHASUMS_URL = NODE_SHASUMS_URL

_TOP_DIR = NODE_TOP_DIR


class NodeDownloadDialog(wx.Dialog):
    """Progress dialog for downloading + extracting portable Node.js.

    Modal result:
      wx.ID_OK     — Node.js is ready; caller may continue
      wx.ID_CANCEL — user cancelled or an error occurred; caller should exit
    """

    _PULSE_MS = 80

    def __init__(self, parent):
        self._i18n = parent.i18n
        title = self._i18n.t("node_download_dialog_title")
        style = wx.DEFAULT_DIALOG_STYLE & ~wx.CLOSE_BOX
        super().__init__(parent, title=title, style=style)

        self._cancelled = False

        self._build_ui()

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_pulse, self._timer)
        self.Bind(wx.EVT_CLOSE, self._on_cancel)

        t = threading.Thread(target=self._run_download, daemon=True)
        t.start()

        self._timer.Start(self._PULSE_MS)

    def _build_ui(self):
        self._status_lbl = wx.StaticText(
            self,
            label=self._i18n.t("node_download_status_label"),
        )

        self._gauge = wx.Gauge(self, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)

        cancel_btn = wx.Button(self, wx.ID_CANCEL, label=self._i18n.t("node_download_cancel"))
        cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._status_lbl, 0, wx.ALL | wx.EXPAND, 12)
        sizer.Add(self._gauge, 0, wx.ALL | wx.EXPAND, 12)
        sizer.Add(cancel_btn, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)

        self.SetSizer(sizer)
        sizer.Fit(self)
        self.SetMinSize((520, -1))
        self.Centre()

    def _set_status(self, text: str):
        wx.CallAfter(self._status_lbl.SetLabel, text)
        wx.CallAfter(self.Layout)

    def _on_pulse(self, _event):
        self._gauge.Pulse()

    def _on_cancel(self, _event=None):
        if self._cancelled:
            return
        self._cancelled = True
        self._timer.Stop()
        self.EndModal(wx.ID_CANCEL)

    def _finish_success(self):
        self._timer.Stop()
        self.EndModal(wx.ID_OK)

    def _finish_error(self, details: str = ""):
        self._timer.Stop()
        msg = self._i18n.t("node_download_error_generic")
        if details:
            msg = f"{msg}\n\n{details}"
        wx.MessageBox(msg, self._i18n.t("node_download_error_title"), wx.OK | wx.ICON_ERROR, self)
        self.EndModal(wx.ID_CANCEL)

    def _download_zip(self, url: str, dest_path: str) -> bool:
        try:
            response = requests.get(url, stream=True, timeout=(30, 300))
            response.raise_for_status()
        except requests.RequestException as exc:
            if not self._cancelled:
                self._finish_error(str(exc))
            return False

        total = int(response.headers.get("content-length", 0))
        downloaded = 0
        chunk_size = 512 * 1024

        try:
            with open(dest_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if self._cancelled:
                        return False
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    mb_down = downloaded / (1024 * 1024)
                    if total:
                        mb_total = total / (1024 * 1024)
                        self._set_status(
                            self._i18n.t("node_download_downloading").format(
                                downloaded=f"{mb_down:.1f}", total=f"{mb_total:.1f}"
                            )
                        )
                    else:
                        self._set_status(
                            self._i18n.t("node_download_downloading_no_total").format(downloaded=f"{mb_down:.1f}")
                        )
        except Exception as exc:
            if not self._cancelled:
                self._finish_error(str(exc))
            return False

        return not self._cancelled

    def _verify_checksum(self, zip_path: str) -> bool:
        self._set_status(self._i18n.t("node_download_verifying"))
        try:
            resp = requests.get(_NODE_SHASUMS_URL, timeout=15)
            resp.raise_for_status()
        except Exception as exc:
            if not self._cancelled:
                self._finish_error(
                    self._i18n.t("node_download_error_checksum_fetch").format(details=exc)
                )
            return False

        expected = ""
        for line in resp.text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].strip().lstrip("*") == _NODE_FILENAME:
                expected = parts[0].strip().lower()
                break

        if not expected:
            if not self._cancelled:
                self._finish_error(
                    self._i18n.t("node_download_error_checksum_missing").format(filename=_NODE_FILENAME)
                )
            return False

        sha256 = hashlib.sha256()
        with open(zip_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        actual = sha256.hexdigest().lower()

        if actual != expected:
            if not self._cancelled:
                self._finish_error(
                    self._i18n.t("node_download_error_checksum_mismatch").format(expected=expected, actual=actual)
                )
            return False

        log.info("Node.js download checksum verified: %s", actual)
        return True

    def _extract_node(self, zip_path: str, node_dir: str) -> bool:
        self._set_status(self._i18n.t("node_download_extracting"))
        parent_dir = os.path.dirname(node_dir)
        staging_dir = tempfile.mkdtemp(prefix=".node-staging-", dir=parent_dir)
        backup_dir = node_dir + ".previous"
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    if self._cancelled:
                        return False

                    rel = member.filename
                    if rel.startswith(_TOP_DIR + "/"):
                        rel = rel[len(_TOP_DIR) + 1:]
                    else:
                        continue
                    if not rel:
                        continue

                    rel_os = rel.replace("/", os.sep)
                    dest = os.path.join(staging_dir, rel_os)

                    if member.is_dir() or rel.endswith("/"):
                        os.makedirs(dest, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with zf.open(member) as src_fh, open(dest, "wb") as dst_fh:
                            shutil.copyfileobj(src_fh, dst_fh)
            if not os.path.isfile(os.path.join(staging_dir, "node.exe")):
                raise RuntimeError("Downloaded Node.js archive has no node.exe")

            # Never overlay a new npm tree on an old one: stale transitive
            # packages can make `npm install` crash even though node.exe has
            # the expected version. Swap the fully extracted runtime instead.
            if os.path.isdir(backup_dir):
                shutil.rmtree(backup_dir)
            if os.path.isdir(node_dir):
                os.replace(node_dir, backup_dir)
            os.replace(staging_dir, node_dir)
            if os.path.isdir(backup_dir):
                shutil.rmtree(backup_dir)
        except Exception as exc:
            if not os.path.isdir(node_dir) and os.path.isdir(backup_dir):
                os.replace(backup_dir, node_dir)
            if not self._cancelled:
                self._finish_error(self._i18n.t("node_download_error_extract").format(details=exc))
            return False
        finally:
            if os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)

        return not self._cancelled

    def _run_download(self):
        node_dir = resource_path("node")
        os.makedirs(node_dir, exist_ok=True)

        tmp_zip = tempfile.mktemp(suffix=".zip", prefix="winzapp_node_")
        try:
            ok = self._download_zip(_NODE_URL, tmp_zip)
            if not ok:
                return

            if self._cancelled:
                return

            ok = self._verify_checksum(tmp_zip)
            if not ok:
                return

            if self._cancelled:
                return

            ok = self._extract_node(tmp_zip, node_dir)
            if not ok:
                return

            node_exe = os.path.join(node_dir, "node.exe")
            if not os.path.isfile(node_exe):
                if not self._cancelled:
                    self._finish_error(self._i18n.t("node_download_error_missing_exe"))
                return

            if not self._cancelled:
                wx.CallAfter(self._finish_success)

        finally:
            try:
                os.remove(tmp_zip)
            except Exception:
                pass
