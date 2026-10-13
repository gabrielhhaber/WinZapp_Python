import type { Request, Response } from 'express';

// What the client needs to show the account's communities: each community
// (the "parent group"), its admins as the page knows them, and the groups of
// it the account has joined, with the announcement group told apart.
//
// A route of WinZapp's own because nothing upstream answers this. The library
// exposes the community's *actions* (create, deactivate, link and unlink
// groups, promote and demote: routes that already exist and are used as they
// are), and WPP.community.getSubgroups() for one community, but no list of
// communities; and WPP.community.getAnnouncementGroup() looks for
// groupType === "LINKED_ANNOUNCEMENT_GROUP", a field WhatsApp Web no longer
// fills (read over CDP on 2.3000.1049387914: undefined on all 273 groups, so
// the function answered undefined for every one of 39 communities). The
// announcement group is the subgroup whose metadata has defaultSubgroup, which
// is also what the community-comments runtime keys on.
//
// Runs inside the page: self-contained, no closure over anything here.
export function readCommunities(): unknown {
  const store = (globalThis as any).WPP?.whatsapp?.GroupMetadataStore;
  if (!store || typeof store.getModelsArray !== 'function') return null;
  const text = (value: unknown) => (typeof value === 'string' ? value : '');
  const serialized = (wid: any) =>
    typeof wid === 'string' ? wid : text(wid?._serialized) || (wid ? String(wid) : '');
  const communities: unknown[] = [];
  for (const parent of store.getModelsArray()) {
    if (!parent || parent.isParentGroup !== true) continue;
    const id = serialized(parent.id);
    if (!/^[0-9-]+@g\.us$/.test(id)) continue;
    const admins: unknown[] = [];
    let iAmAdmin = false;
    let iAmSuperAdmin = false;
    try {
      // A community's own participant list holds its admins, not its members.
      for (const participant of parent.participants?.getModelsArray?.() ?? []) {
        if (participant?.isAdmin !== true && participant?.isSuperAdmin !== true) continue;
        const participantId = serialized(participant.id);
        if (participantId) admins.push({ id: participantId, superAdmin: participant.isSuperAdmin === true });
      }
      iAmAdmin = parent.participants?.iAmAdmin?.() === true;
      iAmSuperAdmin = parent.participants?.iAmSuperAdmin?.() === true;
    } catch (_) {
      // Unknown is "not an admin": an admin action is never offered on a guess.
    }
    const subgroups: unknown[] = [];
    for (const wid of Array.isArray(parent.joinedSubgroups) ? parent.joinedSubgroups : []) {
      const subgroupId = serialized(wid);
      if (!/^[0-9-]+@g\.us$/.test(subgroupId)) continue;
      const meta = store.get(subgroupId);
      subgroups.push({
        id: subgroupId,
        subject: text(meta?.subject),
        announcement: meta?.defaultSubgroup === true,
        general: meta?.generalSubgroup === true,
      });
    }
    communities.push({
      id,
      subject: text(parent.subject),
      description: text(parent.desc),
      closed: parent.isParentGroupClosed === true,
      iAmAdmin,
      iAmSuperAdmin,
      admins,
      subgroups,
    });
  }
  return communities;
}

export async function listCommunities(req: Request, res: Response) {
  try {
    const page: any = (req.client as any)?.page;
    if (!page || page.isClosed?.()) throw new Error('communities_unavailable');
    const communities = await page.evaluate(readCommunities);
    if (!Array.isArray(communities)) throw new Error('communities_unavailable');
    return res.status(200).json({ status: 'success', response: communities });
  } catch (_) {
    // Never the native error: it can carry group names and ids.
    return res.status(503).json({ status: 'error', code: 'communities_unavailable' });
  }
}
