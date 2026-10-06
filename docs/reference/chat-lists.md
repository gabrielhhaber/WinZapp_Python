# WhatsApp custom chat lists

The Conversations tab offers a native list choice, Refresh lists and Manage
lists. Refresh is explicit: startup, history synchronization and message events
do not fetch lists. A selected custom list combines with the existing
All/Unread/Groups/Individual tabs and search. Archived conversations retain the
existing behavior: the main panel includes them only while searching.

The manager reads, creates, renames and deletes custom lists and adds/removes
selected conversations. Deletion asks for confirmation with No selected by
default; it deletes the list, not its chats or messages. Canceling a name or
member prompt sends nothing. A name accepts up to 100 UTF-16 code units, and a
membership request accepts at most 500 addresses.

## Linked-device contract

The implementation targets the repository's pinned WA-JS **4.6.1**:

- [lists.list](https://github.com/wppconnect-team/wa-js/blob/v4.6.1/src/lists/functions/list.ts)
  returns CUSTOM labels (type 5), with identifiers and names but no members.
- Membership comes from each locally available ChatStore model's `labels`
  and serialized chat ID. The adapter compares exact list IDs; it does not use
  `chat.list({withLabels: ...})`, which also resolves names.
- [create](https://github.com/wppconnect-team/wa-js/blob/v4.6.1/src/lists/functions/create.ts),
  rename, remove, addChats and removeChats are called through WPP.lists.
  Editing is enabled only when the account exposes labelsEditingEnabled and
  all five mutators. Accounts without this capability can still read lists.
- The native Favorites feature has no verified separate contract in this
  pinned version. No favorite identifier is guessed and no predefined or
  Business label is exposed as an editable custom list.

The authenticated GET/POST `/api/:session/custom-lists` routes require both
verifyToken and statusConnection. Code lives under `client/api_patches/` and
is restored by both API setup paths. The page function is self-contained so
Puppeteer can serialize it; do not introduce references to module helpers
inside it. Error responses expose only fixed codes, never native error text.

## Identity, membership and lifetime

Snapshots live only in memory and are scoped to account, self JID, server,
port, token and reset generation. Token replacement, F5 and logout invalidate
them. Late replies from an earlier context cannot restore state. There is one
bounded background job at a time, with a 20-second mutation timeout and a
15-second verification read timeout. No additional history/sync failure gate,
database table or plaintext list cache is introduced.

The member picker includes only locally cached main/archived conversations
outside the vault. Locked chats are excluded even if the vault is unlocked.
Unselected, locked and otherwise unavailable members are preserved; there is
no replacement of a list's complete membership. PN/LID matching uses explicit
identity mappings, never guessed digits. The backend additionally requires
every mutation target to exist in the linked device's ChatStore.

One explicit mutation sends one POST followed by one GET. Success requires a
matching acknowledgement and the requested state in the read snapshot.
Timeouts, partial results and ambiguous outcomes are reported as unconfirmed;
the client never retries a write automatically. A failed read preserves the
cached filter but disables editing until a successful refresh. Fresh reads
are device snapshots, not a guarantee that every phone conversation is loaded
or that server-side propagation has completed.

## Verification

The four `tests/test_whatsapp_chat_lists*.py` modules use pure functions,
recording stubs and a Node VM with synthetic stores. No wx.App, window,
browser, real account or network connection is created. Locale checks cover
all seven translations. Live phone behavior and NVDA announcements require
separate acceptance; headless tests do not claim those outcomes.
