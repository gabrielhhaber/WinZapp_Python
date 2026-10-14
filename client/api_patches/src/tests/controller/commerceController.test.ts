import * as commerce from '../../controller/commerceController';

function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}
const SELLER = '5511900000001@c.us';
const ORDER = 'false_5511900000001@c.us_3EB0AAAAAAAAAAAAAAAA';
const collection = (items: any[]) => ({ getModelsArray: () => items, get length() { return items.length; } });

// A page with one business chat, its catalog and a cart, as the real page
// showed them: prices are strings of digits in thousandths, a cart's total is
// a number, and the library's own cart functions change the cart in place.
function fakePage(options: { smb?: boolean; cartEnabled?: boolean; catalog?: boolean; products?: any[];
                             messages?: Record<string, any>; knownAs?: string; chats?: any[];
                             submit?: 'throws after sending' | 'throws without sending' } = {}) {
  const items: any[] = [];
  const chatMessages: any[] = [];
  const SENT = 'true_5511900000001@c.us_3EB0BBBBBBBBBBBBBBBB';
  const cart = { cartItemCollection: collection(items), get itemCount() { return items.reduce((n, i) => n + i.quantity, 0); },
                 get total() { return items.length ? items.reduce((n, i) => n + i.quantity * parseInt(i.priceAmount1000, 10), 0) : null; },
                 get currency() { return items.length ? 'BRL' : null; }, message: undefined };
  const products = options.products ?? [
    { id: '100', name: 'Bread', description: 'Fresh', price: '7500', currency: 'BRL', availability: 'in stock' },
    { id: '200', name: 'Hidden', price: '1000', currency: 'BRL', availability: 'in stock', is_hidden: true },
    { id: '300', name: 'Cake', price: '45000', currency: 'BRL', availability: 'out of stock' },
  ];
  const known = options.knownAs ?? SELLER;
  const wid = (id: string) => ({ id, toString: () => id, isLid: () => id.endsWith('@lid') });
  const WPP: any = {
    conn: { getMyUserId: () => wid('5511999999999@c.us') },
    chat: { getMessageById: jest.fn(async (id: string) => options.messages?.[id] ?? null) },
    catalog: { getProducts: jest.fn(async () => products) },
    cart: {
      add: jest.fn(async (_chat: string, list: any[]) => {
        for (const entry of list) {
          const product = products.find((p) => p.id === entry.id);
          if (!product) continue;  // the library skips an unknown product silently
          const found = items.find((i) => i.id === entry.id);
          if (found) found.quantity = entry.qnt;
          else items.push({ id: product.id, name: product.name, quantity: entry.qnt, priceAmount1000: product.price, currency: 'BRL' });
        }
      }),
      remove: jest.fn(async (_chat: string, id: string) => { const at = items.findIndex((i) => i.id === id); if (at >= 0) items.splice(at, 1); }),
      clear: jest.fn(async () => { if (!items.length) throw new Error('cart_not_have_products'); items.length = 0; }),
      submit: jest.fn(async () => {
        if (options.submit === 'throws without sending') throw new Error('offline');
        chatMessages.push({ type: 'order', id: { fromMe: true, _serialized: SENT } });
        items.length = 0;
        // What the real library did after an order that went out.
        if (options.submit === 'throws after sending') throw new Error(`Message ${SENT} not found`);
        return { id: { _serialized: SENT } };
      }),
    },
    order: { accept: jest.fn(async () => undefined), decline: jest.fn(async () => undefined), update: jest.fn(async () => undefined) },
    whatsapp: {
      WidFactory: { createWid: wid },
      ChatStore: { get: (value: any) => (value.toString() === known
                     ? { id: value, msgs: collection(chatMessages) } : undefined),
                   getModelsArray: () => options.chats ?? [] },
      CartStore: { findCart: () => cart },
      MsgStore: { get: (id: string) => options.messages?.[id] },
      UserPrefs: { getMaybeMePnUser: () => wid('5511999999999@c.us') },
      BusinessProfileStore: { fetchBizProfile: jest.fn(async () => ({
        hasCatalog: options.catalog !== false, profileOptions: { cartEnabled: options.cartEnabled !== false } })) },
      functions: {
        getCurrentLid: () => wid('777@lid'), getPhoneNumber: () => wid(SELLER),
        // WhatsApp Web's current form: one object, with the seller's id.
        queryOrder: jest.fn(async (request: any) => {
          if (!request || typeof request !== 'object' || !request.sellerJid) throw new Error('ServerStatusCodeError');
          return { createdAt: 1790000000, currency: 'BRL', subtotal: 15000, tax: null, total: 15000,
                   products: [{ id: '100', name: 'Bread', price: 7500, currency: 'BRL', quantity: 2 }] };
        }),
      },
    },
  };
  const modules: Record<string, any> = {
    WAWebMobilePlatforms: { isSMB: () => options.smb === true },
    WAWebBusinessProfileUtils: { hasCatalog: (profile: any) => profile.hasCatalog },
  };
  return { WPP, require: (name: string) => { if (!modules[name]) throw new Error('module not found'); return modules[name]; } };
}

async function inPage(page: ReturnType<typeof fakePage>, request: Record<string, unknown>) {
  const target = globalThis as any;
  const previous = { WPP: target.WPP, require: target.require };
  target.WPP = page.WPP;
  target.require = page.require;
  try { return await commerce.commerceInPage(request); } finally { target.WPP = previous.WPP; target.require = previous.require; }
}

describe('who the account is', () => {
  test('a consumer account is told so, and its own cart setting is not read', async () => {
    const page = fakePage();
    expect(await inPage(page, { action: 'account' })).toEqual({ business: false, cartEnabled: null, sellers: [] });
    expect(page.WPP.whatsapp.BusinessProfileStore.fetchBizProfile).not.toHaveBeenCalled();
  });

  test('a business account also learns whether its catalog takes carts', async () => {
    expect(await inPage(fakePage({ smb: true, cartEnabled: false }), { action: 'account' }))
      .toEqual({ business: true, cartEnabled: false, sellers: [] });
  });

  test('the chats with a business are listed by id, and nothing else is', async () => {
    const chats = [
      { id: { _serialized: SELLER }, contact: { isBusiness: true } },
      { id: { _serialized: '777@lid' }, contact: { isBusiness: true } },
      { id: { _serialized: '5511900000009@c.us' }, contact: { isBusiness: false } },
      { id: { _serialized: '111-0@g.us' }, isGroup: true, contact: { isBusiness: true } },
      { id: { _serialized: 'status@broadcast' }, contact: { isBusiness: true } },
      { id: { _serialized: '5511900000008@c.us' } },
    ];
    expect((await inPage(fakePage({ chats }), { action: 'account' })).sellers).toEqual([SELLER, '777@lid']);
  });

  test('a WhatsApp Web without the platform module is not a business', async () => {
    const page = fakePage({ smb: true });
    page.require = () => { throw new Error('module not found'); };
    expect((await inPage(page, { action: 'account' })).business).toBe(false);
  });
});

describe('the buyer: catalog and cart', () => {
  test('a catalog lists visible products with numbers for prices, and the cart', async () => {
    const answer = await inPage(fakePage(), { action: 'catalog', chatId: SELLER });
    expect(answer).toMatchObject({ catalog: true, cartEnabled: true });
    expect(answer.products).toEqual([
      { id: '100', name: 'Bread', description: 'Fresh', price: 7500, currency: 'BRL', available: true },
      { id: '300', name: 'Cake', description: '', price: 45000, currency: 'BRL', available: false },
    ]);
    expect(answer.cart).toEqual({ items: [], itemCount: 0, total: null, currency: '', message: '' });
  });

  test('a business without a catalog is not asked for products', async () => {
    const page = fakePage({ catalog: false });
    expect((await inPage(page, { action: 'catalog', chatId: SELLER })).products).toEqual([]);
    expect(page.WPP.catalog.getProducts).not.toHaveBeenCalled();
  });

  test('a chat known under its @lid is used under that id, whatever form was asked for', async () => {
    const page = fakePage({ knownAs: '777@lid' });
    await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 2 });
    expect(page.WPP.cart.add).toHaveBeenCalledWith('777@lid', [{ id: '100', qnt: 2 }]);
  });

  test('setting a quantity adds the product or changes it, and answers the cart read from its items', async () => {
    const page = fakePage();
    const first = await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 2 });
    expect(first.cart).toEqual({ items: [{ id: '100', name: 'Bread', quantity: 2, price: 7500, currency: 'BRL' }],
                                 itemCount: 2, total: 15000, currency: 'BRL', message: '' });
    const second = await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 5 });
    expect(second.cart.items[0].quantity).toBe(5);
    expect((await inPage(page, { action: 'cart', chatId: SELLER })).cart.itemCount).toBe(5);
  });

  test('a product the catalog no longer has is said, not skipped in silence', async () => {
    const answer = await inPage(fakePage(), { action: 'cartSet', chatId: SELLER, productId: '999', quantity: 1 });
    expect(answer.error).toBe('product_unavailable');
  });

  test('a business that turned its cart off gets no cart and no order', async () => {
    const page = fakePage({ cartEnabled: false });
    expect(await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 1 }))
      .toEqual({ error: 'cart_disabled' });
    expect(await inPage(page, { action: 'cartSubmit', chatId: SELLER, message: '' })).toEqual({ error: 'cart_disabled' });
    expect(page.WPP.cart.add).not.toHaveBeenCalled();
    expect(page.WPP.cart.submit).not.toHaveBeenCalled();
  });

  test('removing and clearing answer the cart; clearing an empty cart does not call the library', async () => {
    const page = fakePage();
    await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 2 });
    expect((await inPage(page, { action: 'cartRemove', chatId: SELLER, productId: '100' })).cart.items).toEqual([]);
    expect((await inPage(page, { action: 'cartClear', chatId: SELLER })).cart.items).toEqual([]);
    expect(page.WPP.cart.clear).not.toHaveBeenCalled();  // it throws on an empty cart
    await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 1 });
    await inPage(page, { action: 'cartClear', chatId: SELLER });
    expect(page.WPP.cart.clear).toHaveBeenCalledTimes(1);
  });

  test('an empty cart is never sent; a filled one is sent once, with the optional message', async () => {
    const page = fakePage();
    expect(await inPage(page, { action: 'cartSubmit', chatId: SELLER, message: 'hi' })).toEqual({ error: 'cart_empty' });
    await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 2 });
    const sent = await inPage(page, { action: 'cartSubmit', chatId: SELLER, message: '' });
    expect(sent).toMatchObject({ sent: true, id: 'true_5511900000001@c.us_3EB0BBBBBBBBBBBBBBBB' });
    expect(page.WPP.cart.submit).toHaveBeenCalledTimes(1);
    expect(page.WPP.cart.submit).toHaveBeenCalledWith(SELLER, undefined);
  });

  test('an order the library throws on after sending is still an order sent', async () => {
    const page = fakePage({ submit: 'throws after sending' });
    await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 1 });
    const sent = await inPage(page, { action: 'cartSubmit', chatId: SELLER, message: 'hi' });
    expect(sent).toMatchObject({ sent: true, id: 'true_5511900000001@c.us_3EB0BBBBBBBBBBBBBBBB' });
    expect(page.WPP.cart.submit).toHaveBeenCalledTimes(1);  // never sent twice
  });

  test('a throw with no new order in the chat is unknown, not "sent" and not "not sent"', async () => {
    const page = fakePage({ submit: 'throws without sending' });
    await inPage(page, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 1 });
    await expect(inPage(page, { action: 'cartSubmit', chatId: SELLER, message: '',
                                readBackAttempts: 3, readBackPause: 0 })).rejects.toThrow('order_not_seen');
    expect(page.WPP.cart.submit).toHaveBeenCalledTimes(1);
  });
});

describe('orders', () => {
  const received = (status = 1) => ({ type: 'order', orderId: 'O1', token: 'T1', sellerJid: '5511999999999@c.us',
                                      status, message: 'please', id: { fromMe: false } });

  test('an order opens for both sides, with its products and totals', async () => {
    const page = fakePage({ messages: { [ORDER]: received() } });
    const { order } = await inPage(page, { action: 'order', messageId: ORDER });
    expect(order).toEqual({ mine: false, status: 1, note: 'please', subtotal: 15000, total: 15000, currency: 'BRL',
      createdAt: 1790000000, canAnswer: false, canUpdate: false,
      products: [{ id: '100', name: 'Bread', price: 7500, currency: 'BRL', quantity: 2 }] });
    expect(page.WPP.whatsapp.functions.queryOrder).toHaveBeenCalledWith(
      { orderId: 'O1', token: 'T1', sellerJid: '5511999999999@c.us', width: 80, height: 80 });
  });

  test('the library\'s own order functions get a query that speaks the current form, and get theirs back', async () => {
    const page = fakePage({ smb: true, messages: { [ORDER]: received() } });
    const functions = page.WPP.whatsapp.functions;
    const original = functions.queryOrder;
    // The library asks the old way, as the real one does.
    page.WPP.order.accept = jest.fn(async () => { await functions.queryOrder('O1', 80, 80, 'T1'); });
    await expect(functions.queryOrder('O1', 80, 80, 'T1')).rejects.toThrow();  // as it is: broken
    expect(await inPage(page, { action: 'orderAccept', messageId: ORDER })).toMatchObject({ accepted: true });
    expect(original).toHaveBeenLastCalledWith(
      { orderId: 'O1', token: 'T1', sellerJid: '5511999999999@c.us', width: 80, height: 80 });
    expect(functions.queryOrder).toBe(original);
    // Put back even when the library fails.
    page.WPP.order.decline = jest.fn(async () => { throw new Error('refused'); });
    await expect(inPage(page, { action: 'orderDecline', messageId: ORDER, note: '' })).rejects.toThrow('refused');
    expect(functions.queryOrder).toBe(original);
  });

  test('a message that is not an order, or lacks its token, is not one', async () => {
    const page = fakePage({ messages: { [ORDER]: { type: 'chat' }, other: { type: 'order', orderId: 'O1' } } });
    expect(await inPage(page, { action: 'order', messageId: ORDER })).toEqual({ error: 'not_an_order' });
    expect(await inPage(page, { action: 'order', messageId: 'other' })).toEqual({ error: 'not_an_order' });
    expect(await inPage(page, { action: 'order', messageId: 'missing' })).toEqual({ error: 'not_an_order' });
  });

  test('what may be done with an order is decided where the facts are', async () => {
    const answerable = fakePage({ smb: true, messages: { [ORDER]: received(1) } });
    expect((await inPage(answerable, { action: 'order', messageId: ORDER })).order)
      .toMatchObject({ canAnswer: true, canUpdate: false });
    const accepted = fakePage({ smb: true, messages: { [ORDER]: received(2) } });
    expect((await inPage(accepted, { action: 'order', messageId: ORDER })).order)
      .toMatchObject({ canAnswer: false, canUpdate: true });
    const sentByMe = fakePage({ smb: true, messages: { [ORDER]: { ...received(1), id: { fromMe: true } } } });
    expect((await inPage(sentByMe, { action: 'order', messageId: ORDER })).order)
      .toMatchObject({ mine: true, canAnswer: false, canUpdate: false });
  });

  test.each(['orderAccept', 'orderDecline', 'orderUpdate'])('%s is a business account\'s alone', async (action) => {
    const page = fakePage({ messages: { [ORDER]: received() } });
    expect(await inPage(page, { action, messageId: ORDER, status: 'shipped' })).toEqual({ error: 'not_business' });
    for (const call of [page.WPP.order.accept, page.WPP.order.decline, page.WPP.order.update]) {
      expect(call).not.toHaveBeenCalled();
    }
  });

  test('a business answers an order it received, never one it sent or a message that is not one', async () => {
    const mine = fakePage({ smb: true, messages: { [ORDER]: { ...received(), id: { fromMe: true } } } });
    expect(await inPage(mine, { action: 'orderAccept', messageId: ORDER })).toEqual({ error: 'own_order' });
    expect(await inPage(fakePage({ smb: true }), { action: 'orderDecline', messageId: ORDER }))
      .toEqual({ error: 'not_an_order' });
    const page = fakePage({ smb: true, messages: { [ORDER]: received() } });
    expect(await inPage(page, { action: 'orderAccept', messageId: ORDER })).toEqual({ accepted: true, status: 1 });
    expect(page.WPP.order.accept).toHaveBeenCalledWith({ msgId: ORDER });
    await inPage(page, { action: 'orderDecline', messageId: ORDER, note: 'Sold out' });
    expect(page.WPP.order.decline).toHaveBeenCalledWith({ msgId: ORDER, declineNote: 'Sold out' });
    await inPage(page, { action: 'orderUpdate', messageId: ORDER, status: 'shipped', note: '', paymentStatus: 'captured' });
    expect(page.WPP.order.update).toHaveBeenCalledWith(
      { msgId: ORDER, orderStatus: 'shipped', orderNote: undefined, paymentStatus: 'captured' });
  });
});

describe('the routes', () => {
  function request(evaluate: jest.Mock, params: Record<string, string> = {}, body: unknown = {}): any {
    return { params, body, client: { page: { isClosed: () => false, evaluate } } };
  }

  test('a page answer is wrapped; a refusal has its own status and keeps the cart', async () => {
    const ok = response();
    const evaluate = jest.fn().mockResolvedValue({ cart: { items: [] } });
    await commerce.cart(request(evaluate, { chatId: SELLER }), ok);
    expect(ok.statusCode).toBe(200);
    expect(ok.body).toEqual({ status: 'success', response: { cart: { items: [] } } });
    expect(evaluate).toHaveBeenCalledWith(commerce.commerceInPage, { action: 'cart', chatId: SELLER });
    for (const [code, status] of [['cart_disabled', 409], ['product_unavailable', 409], ['cart_empty', 409],
                                  ['not_business', 403], ['own_order', 403], ['not_an_order', 404]] as const) {
      const res = response();
      await commerce.cart(request(jest.fn().mockResolvedValue({ error: code, cart: { items: [1] } }), { chatId: SELLER }), res);
      expect(res.statusCode).toBe(status);
      expect(res.body).toEqual({ status: 'error', code, response: { cart: { items: [1] } } });
    }
  });

  test('an unavailable page, an unknown code and a native failure all answer the same, with no native text', async () => {
    for (const evaluate of [jest.fn().mockResolvedValue({ error: 'unavailable' }), jest.fn().mockResolvedValue(null),
                            jest.fn().mockRejectedValue(new Error('5511900000001@c.us Bread'))]) {
      const res = response();
      await commerce.catalog(request(evaluate, { chatId: SELLER }), res);
      expect(res.statusCode).toBe(503);
      expect(res.body).toEqual({ status: 'error', code: 'commerce_unavailable' });
    }
  });

  test.each([
    ['a group as the seller', commerce.catalog, { chatId: '111-0@g.us' }, {}],
    ['a quantity of zero', commerce.cartSet, { chatId: SELLER }, { productId: '100', quantity: 0 }],
    ['a quantity over 99', commerce.cartSet, { chatId: SELLER }, { productId: '100', quantity: 100 }],
    ['a quantity that is not a whole number', commerce.cartSet, { chatId: SELLER }, { productId: '100', quantity: '2' }],
    ['a product id with a path in it', commerce.cartRemove, { chatId: SELLER }, { productId: '../1' }],
    ['a message that is not text', commerce.cartSubmit, { chatId: SELLER }, { message: { text: 'x' } }],
    ['a message over 1024 characters', commerce.cartSubmit, { chatId: SELLER }, { message: 'x'.repeat(1025) }],
    ['a group message as the order', commerce.order, { messageId: 'false_111-0@g.us_3EB0AAAAAAAAAAAAAAAA' }, {}],
    ['a status WhatsApp does not have', commerce.orderUpdate, { messageId: ORDER }, { status: 'teleported' }],
    ['a payment status WhatsApp does not have', commerce.orderUpdate, { messageId: ORDER }, { status: 'shipped', paymentStatus: 'free' }],
    ['a note that is not text', commerce.orderDecline, { messageId: ORDER }, { note: 7 }],
  ])('%s never reaches the page', async (_name, handler, params, body) => {
    const evaluate = jest.fn();
    const res = response();
    await handler(request(evaluate, params as any, body), res);
    expect(res.statusCode).toBe(400);
    expect(evaluate).not.toHaveBeenCalled();
  });

  test('each route sends its own action with what it validated', async () => {
    const cases: Array<[any, Record<string, string>, unknown, Record<string, unknown>]> = [
      [commerce.account, {}, {}, { action: 'account' }],
      [commerce.cartSet, { chatId: SELLER }, { productId: '100', quantity: 3 }, { action: 'cartSet', chatId: SELLER, productId: '100', quantity: 3 }],
      [commerce.cartRemove, { chatId: SELLER }, { productId: '100' }, { action: 'cartRemove', chatId: SELLER, productId: '100' }],
      [commerce.cartClear, { chatId: SELLER }, {}, { action: 'cartClear', chatId: SELLER }],
      [commerce.cartSubmit, { chatId: SELLER }, { message: 'Thanks' }, { action: 'cartSubmit', chatId: SELLER, message: 'Thanks' }],
      [commerce.cartSubmit, { chatId: SELLER }, {}, { action: 'cartSubmit', chatId: SELLER, message: '' }],
      [commerce.order, { messageId: ORDER }, {}, { action: 'order', messageId: ORDER }],
      [commerce.orderAccept, { messageId: ORDER }, {}, { action: 'orderAccept', messageId: ORDER }],
      [commerce.orderDecline, { messageId: ORDER }, { note: 'No' }, { action: 'orderDecline', messageId: ORDER, note: 'No' }],
      [commerce.orderUpdate, { messageId: ORDER }, { status: 'delivered' }, { action: 'orderUpdate', messageId: ORDER, status: 'delivered', note: '', paymentStatus: '' }],
    ];
    for (const [handler, params, body, expected] of cases) {
      const evaluate = jest.fn().mockResolvedValue({ ok: true });
      await handler(request(evaluate, params, body), response());
      expect(evaluate).toHaveBeenCalledWith(commerce.commerceInPage, expected);
    }
  });

  test('the statuses offered are WhatsApp\'s own', () => {
    expect(commerce.ORDER_STATUSES).toHaveLength(14);
    expect(commerce.PAYMENT_STATUSES).toEqual(['pending', 'captured', 'failed', 'canceled']);
  });
});
