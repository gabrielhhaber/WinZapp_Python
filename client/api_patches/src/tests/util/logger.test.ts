import { redactPhone } from '../../util/logger';

describe('redactPhone', () => {
  it.each([
    '5511999998888@s.whatsapp.net',
    '5511999998888@c.us',
    '68904344899801@lid',
    '120363067944453158@g.us', // group id: 18 digits, longer than a phone/LID
  ])('masks the digits of %s', (jid) => {
    const [digits, suffix] = jid.split('@');
    const result = redactPhone(`GET /get-messages/${jid}?count=200`);
    expect(result).not.toContain(digits);
    expect(result.endsWith(`@${suffix}?count=200`)).toBe(true);
    expect(result).toMatch(/GET \/get-messages\/~[0-9a-f]{8}@/);
  });

  it('masks a bare phone number with no JID suffix', () => {
    const result = redactPhone('GET /last-seen/5521970076785 -> 200 in 42ms');
    expect(result).not.toContain('5521970076785');
  });

  it('masks the trailing JID in a status message id', () => {
    const msgId =
      'false_status@broadcast_4A86450069E7268788E7_100742836789440@lid';
    const result = redactPhone(
      `[status-reaction] begin msgId=${msgId} action=set`
    );
    expect(result).not.toContain('100742836789440');
    expect(result.endsWith('@lid action=set')).toBe(true);
  });

  it('produces a stable tag for the same number', () => {
    expect(redactPhone('5511999998888@c.us')).toBe(
      redactPhone('5511999998888@c.us')
    );
  });

  it('produces different tags for different numbers', () => {
    expect(redactPhone('5511999998888@c.us')).not.toBe(
      redactPhone('5511888887777@c.us')
    );
  });

  it('leaves short numbers alone', () => {
    expect(redactPhone('count=200 in 43ms')).toBe('count=200 in 43ms');
  });

  it('does not mistake a hex request id for a phone number', () => {
    const rid = '6f9ac9e02d9c4030b60e1c5022137a2c';
    expect(redactPhone(`rid=${rid}`)).toBe(`rid=${rid}`);
  });

  it('is idempotent', () => {
    const once = redactPhone('5511999998888@c.us');
    expect(redactPhone(once)).toBe(once);
  });
});
