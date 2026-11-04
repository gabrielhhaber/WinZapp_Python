/* Serialized into WhatsApp's page. Use its own join path, never an offer. */
export function installGroupCallRuntimeInPage(): void {
  const win = window as any;
  const module = (name: string): any => {
    try { return win.require?.(name); } catch (_) { return null; }
  };
  const serialized = (value: any): string =>
    typeof value === 'string' ? value : String(value?._serialized || '');
  const sameChat = (left: string, right: string): boolean =>
    left.replace(/@c\.us$/, '@s.whatsapp.net') === right.replace(/@c\.us$/, '@s.whatsapp.net');
  const callStore = (): any => {
    const calls = module('WAWebVoipOngoingCallCollection')?.WAWebVoipOngoingCallCollection;
    if (typeof calls?.getByChat !== 'function' || typeof calls?.getByCallId !== 'function') {
      throw new Error('Ongoing group calls are not available in this WhatsApp version');
    }
    return calls;
  };
  const context = (chatJid: string, groupOnly = false): any => {
    const pattern = groupOnly ? /^\d+(?:-\d+)?@g\.us$/ : /^\d+(?:-\d+)?@(?:g\.us|lid|s\.whatsapp\.net|c\.us)$/;
    if (typeof chatJid !== 'string' || !pattern.test(chatJid)) throw new Error('A group chat is required');
    const createWid = module('WAWebWidFactory')?.createWid;
    const chats = module('WAWebChatCollection')?.ChatCollection;
    const calls = callStore();
    if (typeof createWid !== 'function' || typeof chats?.get !== 'function') throw new Error('Chat lookup unavailable');
    const chat = chats.get(createWid(chatJid));
    if (!chat || !sameChat(serialized(chat.id), chatJid) || (groupOnly && !chat.groupMetadata)) {
      throw new Error('The group chat is not available');
    }
    return { chat, calls };
  };
  const describe = (call: any, chatJid: string): any => {
    if (!call || !sameChat(serialized(call.id?.remote), chatJid)
        || typeof call.id?.id !== 'string' || !call.id.id || !call.callCreator) return null;
    // The native collection holds multiparty calls, including calls whose
    // log belongs to a person's chat rather than a WhatsApp group.
    if (!chatJid.endsWith('@g.us') && module('WAWebMsgGetters')?.getIsGroupCall?.(call) !== true) return null;
    return { id: call.id.id, chatJid, isVideo: call.isVideoCall === true };
  };
  const refresh = async (calls: any): Promise<any> => {
    await calls.waitUntilLoaded?.();
    if (module('WAWebVoipInitEventEmitter')?.VoipInitEventEmitter?.getIsVoipInited?.() !== true) {
      throw new Error('WhatsApp calling is not ready');
    }
    const check = module('WAWebVoipCheckOngoingCalls')?.checkOngoingCalls;
    if (typeof check !== 'function') throw new Error('Ongoing call checks are unavailable');
    await check();
  };
  const validate = (payload: any): any => {
    const { callId, isVideo } = payload;
    const chatJid = payload.chatJid || payload.groupJid;
    const { chat, calls } = context(chatJid);
    const collection = module('WAWebCallCollection');
    const active = collection?.activeCall;
    const state = active?.getState?.() ?? active?.state;
    if (collection?.pendingOutgoingCall || (active && ![0, 7, 13, 'NONE', 'ENDED',
        'HANDLED_REMOTELY', 'REJECTED', 'FAILED', 'NOT_ANSWERED'].includes(state))) {
      throw new Error('Another call is already active');
    }
    const current = typeof callId === 'string' && callId
      ? describe(calls.getByCallId(callId), chatJid) : null;
    if (!current || current.isVideo !== isVideo) {
      throw new Error('The selected group call is no longer available');
    }
    return { chat, collection, current };
  };
  win.__winzappGroupCallRuntime = {
    ongoing: async (groupJid: string): Promise<any> => {
      const { chat, calls } = context(groupJid, true);
      await refresh(calls);
      const call = describe(calls.getByChat(chat), groupJid);
      return call ? { id: call.id, groupJid, isVideo: call.isVideo } : null;
    },
    list: async (): Promise<any> => {
      const calls = callStore();
      await refresh(calls);
      if (typeof calls.getModelsArray !== 'function') throw new Error('Ongoing call list unavailable');
      const result = new Map();
      for (const record of calls.getModelsArray().slice(0, 500)) {
        const chatJid = serialized(record?.id?.remote);
        try {
          context(chatJid);
          const call = describe(record, chatJid);
          if (call) result.set(call.id, call);
        } catch (_) { /* Unknown/unavailable chats cannot be joined. */ }
      }
      return Array.from(result.values());
    },
    participants: async (callId: string): Promise<any> => {
      if (typeof callId !== 'string' || !callId || callId.length > 512) throw new Error('A call id is required');
      const calls = callStore();
      await refresh(calls);
      const active = module('WAWebCallCollection')?.activeCall;
      const activeId = typeof active?.id === 'string' ? active.id : active?.id?._serialized;
      const state = active?.getState?.() ?? active?.state;
      const current = activeId === callId && [6, 9, 11, 'ACTIVE', 'REJOINING', 'CONNECTED_LONELY'].includes(state);
      const record = calls.getByCallId(callId);
      const result = new Map();
      const add = (wid: any): void => {
        let normalized = wid;
        try { normalized = module('WAWebLidMigrationUtils')?.toPn?.(wid) || wid; } catch (_) { /* Keep the LID. */ }
        const jid = serialized(normalized).replace(/@c\.us$/, '@s.whatsapp.net');
        if (!/^\d+@(?:lid|s\.whatsapp\.net)$/.test(jid)) return;
        let isSelf = false;
        try { isSelf = module('WAWebUserPrefsMeUser')?.isMeAccount?.(wid) === true; } catch (_) { /* Unknown identity. */ }
        const contacts = module('WAWebContactCollection')?.ContactCollection;
        const contact = contacts?.get?.(wid) || contacts?.get?.(normalized);
        const name = [contact?.name, contact?.pushname, contact?.pushName].find(
          value => typeof value === 'string' && value.trim() && !/^\d+@/.test(value)) || '';
        result.set(isSelf ? 'self' : jid, { jid, name, isSelf });
      };
      if (record?.callCreator) {
        // The same filter as WhatsApp's Calls-tab participant face pile.
        // Invited, ringing and departed people must never appear as connected.
        const connected = module('WAWebVoipWaCallEnums')?.CallParticipantState?.Connected;
        const participants = module('WAWebMsgGetters')?.getCallParticipants?.(record) ?? record.callParticipants;
        if (connected == null || !Array.isArray(participants) || participants.length > 1024) {
          throw new Error('Connected participants are unavailable');
        }
        for (const person of participants) {
          if (person?.outcome === connected) add(person.participant);
        }
      } else if (current && active?.isGroup === false && [6, 9, 'ACTIVE', 'REJOINING'].includes(state)) {
        add(active.peerJid);
      } else if (current) {
        throw new Error('Connected group participants are unavailable');
      } else {
        return { id: callId, available: false, participants: [] };
      }
      if (current) {
        try { add(module('WAWebUserPrefsMeUser')?.getMeLidUserOrThrow?.()); } catch (_) { /* No self id yet. */ }
      }
      return { id: callId, available: true, participants: Array.from(result.values()) };
    },
    invite: async (payload: any): Promise<any> => {
      // WhatsApp's own "Add participant" (WAWebVoipNewGroupCallContactPicker)
      // calls WAWebVoipStartCall.inviteToCall(wid) once per person picked; the
      // same call rings someone on the list who has not joined
      // (WAWebVoipActionRingParticipant). Only into the call this device is in.
      const { callId } = payload;
      const people = payload.participants;
      if (typeof callId !== 'string' || !callId || callId.length > 512) throw new Error('A call id is required');
      if (!Array.isArray(people) || people.length === 0 || people.length > 31) {
        throw new Error('Between 1 and 31 participants are required');
      }
      const active = module('WAWebCallCollection')?.activeCall;
      const activeId = typeof active?.id === 'string' ? active.id : active?.id?._serialized;
      const state = active?.getState?.() ?? active?.state;
      if (activeId !== callId || ![6, 9, 11, 'ACTIVE', 'REJOINING', 'CONNECTED_LONELY'].includes(state)) {
        throw new Error('This device is not in that call');
      }
      const invite = module('WAWebVoipStartCall')?.inviteToCall;
      const createWid = module('WAWebWidFactory')?.createWid;
      if (typeof invite !== 'function' || typeof createWid !== 'function') {
        throw new Error('Adding participants is unavailable in this WhatsApp version');
      }
      const results = [];
      for (const jid of people) {
        const address = typeof jid === 'string' ? jid.replace(/@s\.whatsapp\.net$/, '@c.us') : '';
        if (!/^\d+@(?:lid|c\.us)$/.test(address)) {
          results.push({ jid, status: 'failed', error: 'invalid address' });
          continue;
        }
        try {
          await invite(createWid(address));
          results.push({ jid, status: 'invited' });
        } catch (error) {
          results.push({ jid, status: 'failed', error: String((error as any)?.message || error).slice(0, 200) });
        }
      }
      return { id: callId, results };
    },
    check: (payload: any): any => validate(payload).current,
    mayStopAudio: (callId: string): boolean => {
      const collection = module('WAWebCallCollection');
      const active = collection?.activeCall;
      const id = typeof active?.id === 'string' ? active.id : active?.id?._serialized;
      return !!collection && !collection.pendingOutgoingCall && (!active || id === callId);
    },
    join: async (payload: any): Promise<any> => {
      const { callId } = payload;
      const { chat, collection, current } = validate(payload);
      const join = module('WAWebVoipStartCall')?.joinOngoingWAWebVoipGroupCallPN;
      const entryPoint = module('WAWebWamEnumLobbyEntryPointType')?.LOBBY_ENTRY_POINT_TYPE?.CALL_LOG;
      if (typeof join !== 'function' || typeof entryPoint !== 'number') {
        throw new Error('Joining ongoing calls is unavailable');
      }
      // Same arguments as WhatsApp's Calls-tab Join button. joinAndAccept
      // bypasses the hidden lobby; this desktop button is the user's consent.
      await join({ callId, chat, isVideo: current.isVideo, isDeviceSwitch: false,
        joinAndAccept: true, lobbyEntryPoint: entryPoint });
      // A native join can resolve without joining (ended call, blocked device).
      // Require THIS call to reach the live store and never retry with an offer.
      // THIS call still dialling gets 15 s instead of 5: a join reported failed
      // that connects afterwards leaves the user in a call with no window.
      for (let attempt = 0; attempt < 150; attempt += 1) {
        const call = collection?.activeCall;
        const id = typeof call?.id === 'string' ? call.id : call?.id?._serialized;
        const state = call?.getState?.() ?? call?.state;
        if (id === callId && [4, 5, 6, 9, 11, 'ACCEPT_SENT', 'ACCEPT_RECEIVED',
            'ACTIVE', 'REJOINING', 'CONNECTED_LONELY'].includes(state)) {
          return { ...current, peerJid: current.chatJid, state, outgoing: false };
        }
        if (id === callId && [0, 7, 13, 'ENDED', 'FAILED', 'REJECTED'].includes(state)) break;
        const dialling = id === callId && [1, 10, 12, 14, 'CALLING', 'LINK', 'PRE_CALLING', 'CALL_B_STARTING'].includes(state);
        if (attempt >= 49 && !dialling) break;
        await new Promise((resolve) => win.setTimeout(resolve, 100));
      }
      throw new Error('WhatsApp did not join the selected group call');
    },
  };
}
