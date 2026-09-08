from ui.conversations import ConversationsPanel


class _ListBoxLike:
    def __init__(self):
        self.refreshed = False

    def SetItemText(self, index, text):
        pass

    def Refresh(self):
        self.refreshed = True


def test_upload_progress_refreshes_listbox_compatible_control():
    panel = ConversationsPanel.__new__(ConversationsPanel)
    panel._media_upload_progress = {}
    panel._upload_stages_seen = {}
    panel._media_transfer_started = set()
    panel._sorted_messages = [{"_local_id": "upload-1", "_local_pending": True}]
    panel.messages_list = _ListBoxLike()
    panel._render_message_line = lambda msg: "rendered"
    panel._update_media_transfer_gauge = lambda progress: None

    ConversationsPanel.update_media_upload_progress(panel, "upload-1", 0.5)

    assert panel.messages_list.refreshed is True
