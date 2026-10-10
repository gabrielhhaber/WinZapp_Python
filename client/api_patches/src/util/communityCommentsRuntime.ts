// Temporary compatibility adapter until WA-JS's public comments API is released.
// Self-contained: Puppeteer serializes it, and Node tests execute the same function.
export async function communityComments({ operation, messageId, text }) {
  const wpp = globalThis['WPP'];
  if (typeof messageId !== 'string' || !messageId.trim()
      || !['read', 'send'].includes(operation)) {
    return { ok: false, code: 'invalid_comment_request' };
  }
  if (operation === 'send' && (typeof text !== 'string' || !text.trim())) {
    return { ok: false, code: 'invalid_comment_request' };
  }
  try {
    if (typeof wpp?.chat?.getMessageById !== 'function'
        || typeof wpp?.chat?.get !== 'function') {
      return { ok: false, code: 'comments_unavailable' };
    }
    const parent = await wpp.chat.getMessageById(messageId);
    const chat = wpp.chat.get(parent?.id?.remote);
    if (!chat?.groupMetadata?.defaultSubgroup) {
      return { ok: false, code: 'not_community_announcement' };
    }
    function withAuthor(row) {
      const contact = row.author ? wpp.whatsapp?.ContactStore?.get?.(row.author) : undefined;
      const name = [contact?.name, contact?.pushname].find(value => typeof value === 'string' && value.trim());
      return {
        ...row,
        authorName: typeof row.authorName === 'string' && row.authorName.trim() ? row.authorName : name?.trim(),
        fromMe: row.fromMe === true || row.id?.fromMe === true
          || (typeof row.id === 'string' && row.id.startsWith('true_')) || contact?.isMe === true,
      };
    }
    if (operation === 'read') {
      if (typeof wpp.chat.getComments === 'function') {
        return { ok: true, response: (await wpp.chat.getComments(messageId)).map(withAuthor) };
      }
      const table = wpp.loader?.loadModule?.('WAWebAddonCommentTableMode')?.commentTableMode;
      if (typeof table?.bulkGetByParentMsgKey !== 'function') {
        return { ok: false, code: 'comments_unavailable' };
      }
      const rows = await table.bulkGetByParentMsgKey([parent.id]);
      if (!Array.isArray(rows)) return { ok: false, code: 'comments_unavailable' };
      const response = rows.map(row => ({
        id: row.id.toString(), fromMe: row.id.fromMe === true, parentMsgId: row.parentMsgKey.toString(),
        chatId: row.parentMsgKey.remote.toString(), author: row.author?.toString(),
        body: row.type === 'comment' ? row.body : undefined,
        timestamp: row.t, type: row.type, ack: row.ack, read: row.read,
        protocolMessageId: row.protocolMessageKey?.toString(),
      })).map(withAuthor).sort((a, b) => a.timestamp - b.timestamp || a.id.localeCompare(b.id));
      return { ok: true, response };
    }
    if (typeof wpp.chat.sendCommentMessage === 'function') {
      return { ok: true, response: await wpp.chat.sendCommentMessage(messageId, text) };
    }
    const load = wpp.loader?.loadModule;
    if (typeof load !== 'function') return { ok: false, code: 'comments_unavailable' };
    let sender = load('WAWebSendCommentMessageAction');
    if (typeof sender?.sendCommentMessage !== 'function') {
      // The released WA-JS lazy table does not yet know this component.
      // Bootloading registers the sender without showing the comments modal.
      const module = load('Bootloader');
      const bootloader = module?.default || module;
      const map = bootloader?.__debug?.componentMap;
      if (typeof bootloader?.loadModules !== 'function' || typeof map?.keys !== 'function') {
        return { ok: false, code: 'comments_unavailable' };
      }
      const candidates = [...map.keys()].filter(name => /CommentsModal/.test(name)).slice(0, 4);
      for (const name of candidates) {
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('comments_unavailable')), 8000);
          try {
            bootloader.loadModules([name], () => { clearTimeout(timer); resolve(); }, 'WinZapp');
          } catch {
            clearTimeout(timer);
            reject(new Error('comments_unavailable'));
          }
        });
        sender = load('WAWebSendCommentMessageAction');
        if (typeof sender?.sendCommentMessage === 'function') break;
      }
    }
    if (typeof sender?.sendCommentMessage !== 'function') {
      return { ok: false, code: 'comments_unavailable' };
    }
    // Exactly one native send. Its verdict is preserved; the controller checks OK.
    return { ok: true, response: await sender.sendCommentMessage(parent, text) };
  } catch {
    // Native exceptions can contain names, identifiers and private message text.
    return { ok: false, code: operation === 'send' ? 'comment_unconfirmed' : 'comments_unavailable' };
  }
}
