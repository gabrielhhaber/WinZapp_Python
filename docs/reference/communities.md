# Communities

The "Communities" button of the New chat dialog (`ui/dialogs/communities.py`)
lists the account's communities, their admins and the groups of each that the
account has joined, opens a group, and offers the admin actions.

## What is reused and what is WinZapp's own

| | where it comes from |
| --- | --- |
| create, deactivate, link and unlink groups, promote and demote | wppconnect-server's own routes (`create-community`, `deactivate-community`, `add-community-subgroup`, `remove-community-subgroup`, `promote-community-participant`, `demote-community-participant`) over the library's `WPP.community.*`. **Not patched.** |
| the list of communities | `GET /api/:session/communities`, WinZapp's own (`api_patches/src/controller/communityOverviewController.ts`): nothing upstream answers it. |
| who can be promoted | the participants of the community's announcement group, through the existing group info (`get_group_info_recent`): everyone in a community is in that group. |
| replies to announcements | `docs/community-comments.md`. |

## Read over CDP (WhatsApp Web 2.3000.1049387914, wa-js 4.6.1, 39 communities)

- `WPP.community.getAnnouncementGroup()` is broken: it tests
  `groupType === "LINKED_ANNOUNCEMENT_GROUP"`, and `groupType` was undefined
  on all 273 loaded groups. The announcement group is the joined subgroup
  whose metadata has `defaultSubgroup`; `generalSubgroup` marks the general
  one. The route reads those.
- A community's own participant list holds its **admins**, not its members
  (`WPP.group.iAmMember()` is false for a community whose groups the account
  is in). "Am I an admin" is read from that list; unreadable is "no".
- `WPP.community.getParticipants()` failed with `SmaxParsingFailure` on that
  account, so `GET community-participants/:id` is not used at all.
- `unjoinedSubgroups` was empty everywhere (WhatsApp Web fills it lazily), so
  only joined groups are listed.

## An action is believed when a reading shows it

The upstream routes answer the library's raw result: an object, a `success`
flag (`demote` answers 200 with `success: false` when WhatsApp refuses), or
no body at all. `core.communities.run_action()` therefore sends the action
**once**, never retries it, and reads the communities again until the effect
is there:

- `done`: the reading shows it;
- `unconfirmed`: accepted, not visible yet after three readings;
- `failed`: refused, and one reading shows no effect. A refused request is
  still read back once, because creating a community is two requests
  (create, then link the groups) and the second can fail after the first has
  made the community.

## Not verified on a real account

The account probed is an admin of none of its communities, and nothing that
changes an account is ever run over CDP. So the six admin actions were never
exercised live. Known from the library's code, not from a run:

- `promoteParticipants` looks the person up in the community's own
  participant list and throws `group_participant_not_found` when they are not
  in it. On a non-admin account that list holds only admins; whether an
  admin's list holds every member is unknown. If it does not, promotion
  through the upstream route fails and WinZapp says "failed"; the fix would
  be a page-side call of WinZapp's own, written against a real admin session.
- Whether WhatsApp lets any admin deactivate, or only the creator, is left to
  WhatsApp: the button is offered to every admin.

Tests: `tests/test_communities.py` (Python), and
`communityOverviewController.test.ts` (Jest).
