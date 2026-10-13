// Resolve the quote before ANY recipient is sent to. Keep the verdict in the
// page: Puppeteer does not reliably preserve a WPPError's code across a throw.
export const QUOTED_MESSAGE_PREFLIGHT = String.raw`
async function(messageId) {
  try {
    const message = await window.WPP.chat.getMessageById(messageId);
    if (!message) throw new Error('quote lookup returned no verdict');
    return false;
  } catch (error) {
    if (error && error.code === 'msg_not_found') return true;
    throw error;
  }
}`;

export async function replyQuoteIsMissing(client, messageId) {
  return (
    (await client.page.evaluate(
      `(${QUOTED_MESSAGE_PREFLIGHT})(${JSON.stringify(messageId)})`
    )) === true
  );
}

export function missingReplyQuoteResponse(res) {
  // Deliberately 500: older WinZapp clients strip quotes and resend on 4xx.
  // Only clients understanding this complete pre-send verdict may call it a
  // definite refusal. Never add this marker to a catch around a SEND.
  return res.status(500).json({
    status: 'error',
    code: 'quoted_message_not_found',
    stage: 'quote_resolution',
    sendAttempted: false,
  });
}
