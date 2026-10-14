# Communities

Alt+8 from anywhere in the window, or the "Communities" button of the New
chat dialog, opens `ui/dialogs/communities.py`, which lists the account's communities, their admins and the groups of each that the
account has joined, opens a group, and offers the admin actions.

## What is reused and what is WinZapp's own

| | where it comes from |
| --- | --- |
| create, deactivate, link and unlink groups, demote | wppconnect-server's own routes (`create-community`, `deactivate-community`, `add-community-subgroup`, `remove-community-subgroup`, `demote-community-participant`) over the library's `WPP.community.*`. **Not patched.** |
| the list of communities | `GET /api/:session/communities`, WinZapp's own (`communityOverviewController.ts`): nothing upstream answers it. |
| promote | `POST /api/:session/community-admin/:communityId/promote`, WinZapp's own (`communityAdminController.ts`): the library's cannot do it, see below. |
| who can be promoted | the participants of the community's announcement group, through the existing group info (`get_group_info_recent`): everyone in a community is in that group. |
| replies to announcements | `docs/community-comments.md`. |

## Run on a real account

2026-10-13, WhatsApp Web 2.3000.1049387914, wa-js 4.6.1, over CDP with the
maintainer's leave: two throwaway communities, one throwaway group and one
member borrowed from a group of the account, all removed afterwards. Every
action below was called as the route calls it and read back with the route's
own reader.

| action | what the library answered | when the reader showed it |
| --- | --- | --- |
| create, no group | **threw** `expected at least 1 children, but found 0`, after the community existed | about 500 ms later |
| create, one group | `{wid, subGroups: {linkedGroupJids, failedGroups}}` | at once |
| link a group | `{linkedGroupJids, failedGroups}` | at once |
| link a group created 1.5 s earlier | the same success answer | **never: WhatsApp had not linked it** |
| unlink | `{unlinkedGroupJids, failedGroups}` | at once |
| promote (library) | **threw** `group_participant_not_found` | never |
| promote (WinZapp's route) | status 207, code `"200"` | at once |
| demote | `{participants, success: true}` | at once |
| deactivate | `{parent_group_jid}` | at once; its groups unlinked, the announcement group left behind as a chat |

What that settled:

- **The library's `create` breaks with no group.** It always sends the link
  request, and WhatsApp refuses an empty one; WhatsApp Web's own interface
  only links when a group was chosen. The upstream route then answers 500
  for a community that exists. Nothing is patched for it: the reading finds
  the community.
- **The library's `promoteParticipants` cannot promote.** It looks the person
  up in the community's own participant list, which holds the admins only,
  also on an admin's account (a member added through the announcement group
  was in that group's list and in `getParticipants()`, not in the
  community's). WinZapp's route calls what WhatsApp Web's interface calls,
  `WAWebGroupModifyParticipantsJob.promoteCommunityParticipants(wid, [wid],
  isLidAddressingMode)`. Demotion works through the library because an admin
  is in that list.
- **A success answer is not the effect.** The group linked a second after
  its creation is why `core.read_back.run_action()` exists.

Also read there, on the account's other 39 communities:

- `WPP.community.getAnnouncementGroup()` is broken: it tests
  `groupType === "LINKED_ANNOUNCEMENT_GROUP"`, and `groupType` was undefined
  on all 273 loaded groups. The announcement group is the joined subgroup
  whose metadata has `defaultSubgroup` (it carries the community's own name);
  `generalSubgroup` marks the general one. The route reads those.
- "Am I an admin" is read from the community's participant list; unreadable
  is "no".
- `WPP.community.getParticipants()` works for an admin and failed with
  `SmaxParsingFailure` on a community the account does not administer, so
  `GET community-participants/:id` is not used.
- `unjoinedSubgroups` was empty everywhere (WhatsApp Web fills it lazily), so
  only joined groups are listed.

## An action is believed when a reading shows it

`core.read_back.run_action()` sends the action **once**, never retries it,
and reads the communities up to three times, 1.5 s apart:

- `done`: a reading shows the effect, whatever the action answered;
- `unconfirmed`: accepted, not visible after the last reading;
- `failed`: refused, and no reading shows the effect.

A refused action is read back as patiently as an accepted one, because of the
first row of the table.

## Left to WhatsApp

- Whether any admin may deactivate, or only the creator: the button is
  offered to every admin (the test account was the creator).
- Which groups can be linked: every named group of the account that is in no
  community is offered; WhatsApp refuses the ones it does not accept.

Tests: `tests/test_communities.py` (Python), and
`communityOverviewController.test.ts`, `communityAdminController.test.ts`
(Jest).
