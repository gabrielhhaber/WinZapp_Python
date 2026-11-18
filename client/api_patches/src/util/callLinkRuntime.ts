/*
 * Serialized into WhatsApp's page: call links through WhatsApp Web's own code.
 *
 * Read over CDP on 2026-11-18 and run live on a test link:
 * - WAWebVoipCreateCallLink.createCallLink({callType}) sends <link_create> and
 *   answers https://call.whatsapp.com/<voice|video>/<token>, as the
 *   "New call link" dialog does;
 * - joining is what WAWebVoipLinkPreviewCallLink.handleClickCallLink does when
 *   the user already consented: setPendingCallLink, the VoIP stack's
 *   previewCallLink (<link_query>) and joinCallLink (<link_join>). The first to
 *   join becomes the call's creator and admin (CONNECTED_LONELY until someone
 *   else connects);
 * - a guest who opens the link in a browser always waits in the waiting room
 *   (filter guests_only); the admin lets them in with the stack's
 *   waitingRoomAdmit / waitingRoomDeny, as WAWebVoipParticipantsModal does.
 * A waiting person is named by their saved name or the name they typed, never
 * by number (WAWebWaitingRoomNameFormat falls back to the formatted number).
 */
export function installCallLinkRuntimeInPage(): void {
  const win = window as any;
  const module = (name: string): any => {
    try { return win.require?.(name); } catch (_) { return null; }
  };
  // WAWebParseCallLinkType's two public forms; a token is 22 characters
  // (WAWebVoipLinkPreviewCallLink refuses any other length).
  const LINK = /^https:\/\/(?:call\.whatsapp\.com|web\.whatsapp\.com\/call)\/(video|voice)\/(\w+)(?:\?.*)?$/i;
  const parse = (text: any): any => {
    const found = LINK.exec(typeof text === 'string' ? text.trim() : '');
    if (!found || found[2].length !== 22) throw new Error('A call link is required');
    const kind = found[1].toLowerCase();
    return { isVideo: kind === 'video', token: found[2], link: `https://call.whatsapp.com/${kind}/${found[2]}` };
  };
  const idOf = (call: any): string => (typeof call?.id === 'string' ? call.id : call?.id?._serialized) || '';
  const stateOf = (call: any): any => call?.getState?.() ?? call?.state;
  const TERMINAL = [0, 7, 13, 'NONE', 'ENDED', 'HANDLED_REMOTELY', 'REJECTED', 'FAILED', 'NOT_ANSWERED'];
  const CONNECTED = [4, 5, 6, 9, 11, 'ACCEPT_SENT', 'ACCEPT_RECEIVED', 'ACTIVE', 'REJOINING', 'CONNECTED_LONELY'];
  const collection = (): any => {
    const calls = module('WAWebCallCollection');
    if (!calls) throw new Error('Calls are unavailable in this WhatsApp version');
    return calls;
  };
  const voipStack = async (): Promise<any> => {
    const ensure = module('WAWebEnsureVoipInited')?.ensureVoipInitialized;
    const get = module('WAWebVoipStackInterface')?.getVoipStackInterface;
    if (typeof ensure !== 'function' || typeof get !== 'function') {
      throw new Error('Call links are unavailable in this WhatsApp version');
    }
    await ensure('call');
    const stack = await get();
    if (!stack || stack.type !== 'web') throw new Error('WhatsApp calling is not ready');
    return stack;
  };
  const busy = (calls: any): boolean => {
    const active = calls.activeCall;
    return !!calls.pendingOutgoingCall || !!calls.pendingCallLink
      || (!!active && !TERMINAL.includes(stateOf(active)));
  };
  const ownCall = (callId: any): any => {
    if (typeof callId !== 'string' || !callId || callId.length > 512) throw new Error('A call id is required');
    const active = collection().activeCall;
    if (!active || idOf(active) !== callId || active.isCallLink !== true || TERMINAL.includes(stateOf(active))) {
      throw new Error('This device is not in that call');
    }
    return active;
  };
  const legacy = (wid: any): string => {
    try { return String(wid?.toString?.({ legacy: true }) || ''); } catch (_) { return ''; }
  };
  const person = (wid: any): any => {
    const contacts = module('WAWebContactCollection')?.ContactCollection;
    const contact = contacts?.get?.(wid);
    let guest = false;
    let typed = '';
    try { guest = module('WAWebFrontendContactGetters')?.getIsGuest?.(contact) === true; } catch (_) { /* a contact */ }
    try { typed = module('WAWebContactGetters')?.getNotifyName?.(contact) || ''; } catch (_) { /* no name */ }
    const name = [guest ? '' : contact?.name, typed, contact?.pushname].find(
      value => typeof value === 'string' && value.trim() && !/^\+?[\d\s()-]+$/.test(value) && !/@/.test(value)) || '';
    return { jid: legacy(wid), name, guest };
  };
  const callInfo = async (stack: any): Promise<any> => {
    try {
      const raw = await stack.getCallInfo();
      return raw ? stack.parsers.parseCallInfo(raw) : null;
    } catch (_) { return null; }
  };
  win.__winzappCallLinkRuntime = {
    parse: (text: any): any => parse(text),
    create: async (isVideo: boolean): Promise<any> => {
      const create = module('WAWebVoipCreateCallLink')?.createCallLink;
      if (typeof create !== 'function') throw new Error('Call links are unavailable in this WhatsApp version');
      const made = parse(await create({ callType: isVideo === true ? 'video' : 'audio' }));
      return { link: made.link, isVideo: made.isVideo };
    },
    check: (payload: any): any => {
      const link = parse(payload?.link);
      if (busy(collection())) throw new Error('Another call is already active');
      return link;
    },
    mayStopAudio: (): boolean => {
      const calls = module('WAWebCallCollection');
      return !!calls && !calls.pendingOutgoingCall && !calls.activeCall;
    },
    join: async (payload: any): Promise<any> => {
      const { token, isVideo, link } = parse(payload?.link);
      const calls = collection();
      if (busy(calls)) throw new Error('Another call is already active');
      const stack = await voipStack();
      if (busy(calls)) throw new Error('Another call is already active');
      const username = module('WAWebFrontendContactGetters')?.getMyUsername?.();
      const pending = { isVideo };
      calls.setPendingCallLink?.(pending);
      const release = (): void => {
        if (calls.pendingCallLink === pending) calls.setPendingCallLink?.(null);
      };
      try {
        // the camera starts off, as Answer without video (videoMuted)
        const status = await stack.previewCallLink(token, isVideo, 0, username, isVideo);
        if (typeof status === 'number' && status !== 0) throw new Error('WhatsApp did not open the call link');
        await stack.joinCallLink();
      } catch (error) {
        release();
        throw error;
      }
      for (let attempt = 0; attempt < 150; attempt += 1) {
        const call = calls.activeCall;
        const state = stateOf(call);
        if (call?.isCallLink === true && idOf(call)) {
          if (CONNECTED.includes(state) || call.isInWaitingRoom === true) {
            release();
            const info = await callInfo(stack);
            return { id: idOf(call), link, isVideo, isCallLink: true, peerJid: '', state, outgoing: false,
              waiting: info?.isInWaitingRoom === true || call.isInWaitingRoom === true,
              admin: info?.isWaitingRoomAdmin === true || call.isWaitingRoomAdmin === true };
          }
          if (TERMINAL.includes(state)) break;
        }
        await new Promise((resolve) => win.setTimeout(resolve, 100));
      }
      release();
      // A join answered after this wait would leave the user in a call with
      // no window: end the link call this attempt opened.
      const late = calls.activeCall;
      if (late?.isCallLink === true && !TERMINAL.includes(stateOf(late))) {
        try { await stack.endCall(module('WAWebVoipSignalingEnums')?.EndCallReason?.Unknown ?? 0, true); } catch (_) { /* gone */ }
      }
      throw new Error('WhatsApp did not join the call link');
    },
    waitingRoom: async (callId: string): Promise<any> => {
      const call = ownCall(callId);
      const users = Array.isArray(call.waitingRoomUsers) ? call.waitingRoomUsers.slice(0, 256) : [];
      return { id: callId, admin: call.isWaitingRoomAdmin === true, enabled: call.isWaitingRoomEnabled === true,
        waiting: call.isInWaitingRoom === true,
        people: users.map(person).filter((row: any) => /^\d+@(?:lid|c\.us|s\.whatsapp\.net)$/.test(row.jid)) };
    },
    admit: async (payload: any): Promise<any> => {
      const call = ownCall(payload?.callId);
      const jid = typeof payload?.jid === 'string' ? payload.jid : '';
      const allow = payload?.admit !== false;
      const wid = (Array.isArray(call.waitingRoomUsers) ? call.waitingRoomUsers : []).find(
        (user: any) => legacy(user) === jid);
      if (!jid || !wid) throw new Error('That person is not waiting to enter');
      if (call.isWaitingRoomAdmin !== true) throw new Error('Only the call admin decides who enters');
      const stack = await voipStack();
      const status = allow ? await stack.waitingRoomAdmit(jid) : await stack.waitingRoomDeny(jid);
      if (typeof status === 'number' && status !== 0) throw new Error('WhatsApp did not take the decision');
      return { id: payload.callId, admitted: allow };
    },
  };
}
