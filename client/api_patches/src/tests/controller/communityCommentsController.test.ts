import { getComments, sendCommentMessage } from '../../controller/communityCommentsController';

function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}
function request(result: unknown = { ok: true, response: [] }) {
  return { params: { messageId: 'false_123@g.us_PARENT_456@lid' }, body: { text: ' exact reply ' },
    client: { page: { isClosed: () => false, evaluate: jest.fn().mockResolvedValue(result) } } } as any;
}
describe('community announcement comments', () => {
  it('returns an empty read as success and preserves the parent id', async () => {
    const req = request(), res = response();
    await getComments(req, res);
    expect(req.client.page.evaluate).toHaveBeenCalledWith(expect.any(Function), {
      operation: 'read', messageId: req.params.messageId, text: ' exact reply ',
    });
    expect(res.statusCode).toBe(200);
    expect(res.body).toEqual({ status: 'success', response: [] });
  });
  it('confirms only the native OK send and preserves whitespace', async () => {
    const req = request({ ok: true, response: { messageSendResult: 'OK' } }), res = response();
    await sendCommentMessage(req, res);
    expect(req.client.page.evaluate).toHaveBeenCalledTimes(1);
    expect(req.client.page.evaluate.mock.calls[0][1].text).toBe(' exact reply ');
    expect(res.statusCode).toBe(201);
  });
  it.each([null, {}, {messageSendResult: 'ERROR_UNKNOWN'}])('never retries an unconfirmed verdict %p', async verdict => {
    const req = request({ok: true, response: verdict}), res = response();
    await sendCommentMessage(req, res);
    expect(req.client.page.evaluate).toHaveBeenCalledTimes(1);
    expect(res.statusCode).toBe(502);
    expect(res.body.retrySafe).toBe(false);
  });
  it('scrubs native errors and never retries a lost response', async () => {
    const req = request(), res = response();
    req.client.page.evaluate.mockRejectedValue(new Error('private content'));
    await sendCommentMessage(req, res);
    expect(req.client.page.evaluate).toHaveBeenCalledTimes(1);
    expect(res.statusCode).toBe(502);
    expect(JSON.stringify(res.body)).not.toContain('private content');
  });
  it('rejects ordinary groups clearly', async () => {
    const req = request({ok: false, code: 'not_community_announcement'}), res = response();
    await getComments(req, res);
    expect(res.statusCode).toBe(400);
    expect(res.body.code).toBe('not_community_announcement');
  });
  it.each([null, '', ' ', 12])('rejects invalid text %p before evaluating the page', async text => {
    const req = request(), res = response(); req.body.text = text;
    await sendCommentMessage(req, res);
    expect(res.statusCode).toBe(400);
    expect(req.client.page.evaluate).not.toHaveBeenCalled();
  });
  it('handles a closed page before running anything', async () => {
    const req = request(), res = response(); req.client.page.isClosed = () => true;
    await getComments(req, res);
    expect(res.statusCode).toBe(503);
    expect(req.client.page.evaluate).not.toHaveBeenCalled();
  });
});
