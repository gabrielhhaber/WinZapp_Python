"""Deleting a contact, after a confirmation: the contact's details dialog
(ConversationDataDialog) and the Contacts panel (ui/contacts_panel.py) share
it.

A local contact is removed here at once. A contact saved to the phone is
removed from WhatsApp and the phone first, and kept here when that fails,
which is what is still true (tests/test_phone_synced_contact_actions.py).
"""
import wx

from core import phone_contacts


def delete_contact_with_confirmation(mw, parent, i18n, entry, jid, name, finish, set_busy):
    """Ask, then delete the contact *entry* stored under *jid*.

    *finish(jid)* removes it from WinZapp once that is right (the caller
    refreshes what it shows); *set_busy(bool)* marks a phone removal in
    flight, so the caller can refuse a second press meanwhile.
    """
    synced = phone_contacts.is_phone_synced(entry)
    message_key = "delete_contact_phone_confirm_msg" if synced else "delete_contact_local_confirm_msg"
    if wx.MessageBox(
        i18n.t(message_key).format(name=name),
        i18n.t("delete_contact_local").replace("&", ""),
        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        parent,
    ) != wx.YES:
        return
    if not synced:
        finish(jid)
        return

    def removed(ok):
        set_busy(False)
        if not ok:
            text = i18n.t("delete_contact_phone_failed")
            mw.output(text)
            if parent:
                wx.MessageBox(text, i18n.t("app_name"), wx.OK | wx.ICON_WARNING, parent)
            return
        finish(jid)

    set_busy(True)
    mw.output(i18n.t("delete_contact_phone_removing"))
    mw.remove_phone_synced_contact(jid, removed)
