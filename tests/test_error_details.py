"""Error reports use simulated widgets; never create desktop windows."""

from types import SimpleNamespace

import pytest

from ui.dialogs import error_details


def test_error_text_keeps_full_diagnostics():
    details = "compiler error\n" * 1000
    assert error_details.error_text("Failed", details) == "Failed\n\n" + details
    assert error_details.error_text("Failed") == "Failed"


def test_node_download_uses_the_same_report(monkeypatch):
    from ui.dialogs import node_download
    events = []
    monkeypatch.setattr(node_download, "show_error_details", lambda *a: events.append(a))
    dialog = SimpleNamespace(_timer=SimpleNamespace(Stop=lambda: events.append("stop")),
                             _i18n=SimpleNamespace(t=lambda key: key),
                             IsModal=lambda: True,
                             EndModal=lambda result: events.append(result))
    node_download.NodeDownloadDialog._finish_error(dialog, "download failed")
    assert events[0] == "stop"
    assert events[1][-1] == "download failed"
    assert events[2] == error_details.wx.ID_CANCEL


def test_field_is_read_only_copyable_and_initially_focused(monkeypatch):
    fields = []
    class Widget:
        def __init__(self, *args, **kwargs):
            self.options = kwargs
            self.position = self.focused = None
        def Add(self, *args):
            pass
        def SetInsertionPoint(self, position):
            self.position = position
        def SetFocus(self):
            self.focused = True
    def text(*args, **kwargs):
        field = Widget(*args, **kwargs)
        fields.append(field)
        return field
    monkeypatch.setattr(error_details.wx, "TextCtrl", text)
    for name in ("BoxSizer", "StaticText", "Button"):
        monkeypatch.setattr(error_details.wx, name, Widget)
    events = []
    dialog = SimpleNamespace(FromDIP=lambda value: value,
                             SetSizerAndFit=lambda s: None,
                             SetEscapeId=lambda ident: events.append(ident),
                             CentreOnParent=lambda: None)
    error_details.ErrorDetailsDialog._build_ui(dialog, SimpleNamespace(t=lambda k: k),
                                              "Failed", "npm details")
    assert fields[0].options["value"] == "Failed\n\nnpm details"
    assert fields[0].options["style"] & error_details.wx.TE_READONLY
    assert fields[0].options["style"] & error_details.wx.TE_MULTILINE
    assert fields[0].position == 0 and fields[0].focused
    assert events == [error_details.wx.ID_CANCEL]


@pytest.mark.parametrize("raises", [False, True])
def test_report_raises_hidden_parent_and_always_destroys(monkeypatch, raises):
    events = []
    def modal():
        events.append("modal")
        if raises:
            raise RuntimeError("closed")
    dialog = SimpleNamespace(ShowModal=modal, Destroy=lambda: events.append("destroy"))
    monkeypatch.setattr(error_details, "ErrorDetailsDialog", lambda *a: dialog)
    monkeypatch.setattr(error_details, "bring_to_front_if_hidden",
                        lambda *a: events.append("foreground"))
    if raises:
        with pytest.raises(RuntimeError):
            error_details.show_error_details(None, None, "Failed", "Error")
    else:
        error_details.show_error_details(None, None, "Failed", "Error")
    assert events == ["foreground", "modal", "destroy"]
