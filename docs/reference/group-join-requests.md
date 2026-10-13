# Requests to join a group

In a group with "approve new participants" on, people who use the invitation
link (or are added by a participant, or come through the community) wait for
an admin. The group data dialog shows an admin the "Pending join requests"
button (`ui/dialogs/group_membership_requests.py`), which lists them and
approves or rejects the selected one.

## What is reused and what is WinZapp's own

The library has the three functions (`getGroupMembershipRequests`,
`approveGroupMembershipRequest`, `rejectGroupMembershipRequest`);
wppconnect-server has no route for them. So the routes are WinZapp's own
(`groupMembershipRequestsController.ts`) and call those functions as they are:

```
GET  /api/:session/group-membership-requests/:groupId
POST /api/:session/group-membership-requests/:groupId/approve   {participants: [...]}
POST /api/:session/group-membership-requests/:groupId/reject    {participants: [...]}
```

One addition when reading. `WPP.group.getMembershipRequests()` only reads the
page's local table (`WAWebApiMembershipApprovalRequestStore`), which is as
fresh as the last time WhatsApp Web asked the server. The route asks first,
with the job WhatsApp Web's interface uses
(`WAWebGroupGetMembershipApprovalRequestsJob.queryAndUpdateGroupMembershipApprovalRequests`),
which also removes from the table the requests that are no longer pending.
The answer carries `refreshed`; when the server could not be asked the list
is still shown, announced as possibly out of date.

For each requester the route adds what the page knows and the client may
not, since a requester is not a member: the number behind a `@lid` and the
profile or saved name.

## What was run, and what was not

Run over CDP (WhatsApp Web 2.3000.1049387914, wa-js 4.6.1): the reading, for
a group the account administers (asked of the server, empty list) and for one
it does not (the server refuses with status 403, answered as `not_admin`);
the `@lid` to number step; the three values of the request method
(`InviteLink`, `LinkedGroupJoin`, `NonAdminAdd`).

**Not run: a real request.** None of the account's groups had one pending,
and a request needs a second account. So the shape of an entry
(`id`, `addedBy`, `requestMethod`, `t`) and the approve and reject calls come
from the library's typing and WhatsApp Web's program text. That is why:

- everything read from an entry is checked before it leaves the page, and
  again in `core.group_membership_requests.parse_requests()`;
- the answer of approve and reject is not interpreted. `settle()` sends it
  once and reads the list again: the answer is believed when the person has
  left a list the server confirmed (`refreshed`). A list that could not be
  refreshed proves nothing.

Tests: `tests/test_group_membership_requests.py` (Python),
`groupMembershipRequestsController.test.ts` (Jest).
