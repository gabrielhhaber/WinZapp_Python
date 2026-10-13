import type { Request, Response } from 'express';

const validChat = (value: unknown): value is string =>
  typeof value === 'string' && /^\d+@(c\.us|lid)$/.test(value);
const validProduct = (value: unknown): value is string =>
  typeof value === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(value);
// "true_<chat>_<id>" or "false_<chat>_<id>", a message of a one-to-one chat.
const validMessage = (value: unknown): value is string =>
  typeof value === 'string' && /^(true|false)_\d+@(c\.us|lid)_[A-Za-z0-9]{8,64}$/.test(value);

// WhatsApp's own order statuses (WAWebOrderStatus) and payment statuses.
export const ORDER_STATUSES = [
  'pending', 'processing', 'partially_shipped', 'shipped', 'completed', 'canceled',
  'payment_requested', 'preparing_to_ship', 'delivered', 'confirmed', 'delayed', 'failed',
  'out_for_delivery', 'refunded',
];
export const PAYMENT_STATUSES = ['pending', 'captured', 'failed', 'canceled'];

// Carts and orders.
//
// Routes of WinZapp's own: wppconnect-server has none for WPP.cart.* nor for
// WPP.order.accept/decline/update, and its only order route passes on a model
// that does not survive being returned by value. The library functions are
// called as they are.
//
// Who may do what follows WhatsApp Web's own program text (2.3000.1049387914):
//
// - The cart is the buyer's: any account, in a chat with a business that has
//   a catalog. Whether the business takes carts is its own setting
//   (profileOptions.cartEnabled); the library does not look at it, so this
//   does, and neither fills nor sends a cart to a business that turned it off.
// - An order message opens for both sides ("View sent cart" / "View received
//   cart").
// - Accepting, declining and updating an order are the seller's: only a
//   WhatsApp Business account (WAWebMobilePlatforms.isSMB()), and only for an
//   order it received.
//
// Run over CDP on a real, non-business account, with nothing sent to anyone:
// the catalog of a business, and add / update / remove on its cart, which
// only change the page's own cart table (emptied again afterwards). What that
// showed and this relies on: a product's price is a string of digits in
// thousandths; a cart item's price too, while the cart's total is a number;
// WPP.cart.get() returned by value carries no items, so they are read here;
// WPP.cart.clear() throws on an empty cart. The seller's side could not be
// run (it needs a business account and a real order): it follows the
// library's and WhatsApp Web's program text, and says so in its answers by
// never claiming more than "not refused".
//
// Runs inside the page: self-contained, no closure over anything here.
export async function commerceInPage(request: any): Promise<any> {
  const page = globalThis as any;
  const WPP = page.WPP;
  const W = WPP?.whatsapp;
  if (!W?.WidFactory || !W?.ChatStore) return { error: 'unavailable' };
  const text = (value: unknown) => (typeof value === 'string' ? value : '');
  const amount = (value: unknown) => {
    const number = typeof value === 'number' ? value
      : typeof value === 'string' && /^\d+$/.test(value) ? parseInt(value, 10) : NaN;
    return Number.isFinite(number) && number >= 0 ? number : null;
  };
  const businessAccount = () => {
    try { return page.require('WAWebMobilePlatforms').isSMB() === true; } catch (_) { return false; }
  };
  // The chat's own id, in the form WhatsApp keys its catalog and cart by: a
  // chat known under its @lid is not found under the number, and the reverse.
  const seller = (chatId: string) => {
    const wid = W.WidFactory.createWid(chatId);
    if (W.ChatStore.get(wid)) return wid;
    try {
      const other = wid.isLid?.() ? W.functions?.getPhoneNumber?.(wid) : W.functions?.getCurrentLid?.(wid);
      if (other && W.ChatStore.get(other)) return other;
    } catch (_) {
      // Only one form known: use the one given.
    }
    return wid;
  };
  const sellerProfile = async (wid: any) => {
    const profile = await W.BusinessProfileStore.fetchBizProfile(wid);
    let catalog = false;
    try { catalog = page.require('WAWebBusinessProfileUtils').hasCatalog(profile) === true; } catch (_) { catalog = false; }
    return { catalog, cartEnabled: profile?.profileOptions?.cartEnabled === true };
  };
  const cartView = (id: string) => {
    const cart = W.CartStore.findCart(id);
    const items: unknown[] = [];
    for (const item of cart?.cartItemCollection?.getModelsArray?.() ?? []) {
      const quantity = amount(item?.quantity);
      if (!text(String(item?.id ?? '')) || !quantity) continue;
      items.push({ id: String(item.id), name: text(item.name), quantity,
                   price: amount(item.priceAmount1000), currency: text(item.currency) });
    }
    return { items, itemCount: amount(cart?.itemCount) ?? 0, total: amount(cart?.total),
             currency: text(cart?.currency), message: text(cart?.message) };
  };
  const orderMessage = async (messageId: string) => {
    const message = W.MsgStore.get(messageId) ?? await WPP.chat.getMessageById(messageId).catch(() => null);
    return message && message.type === 'order' && message.orderId && message.token ? message : null;
  };

  switch (request.action) {
    case 'account': {
      const business = businessAccount();
      let cartEnabled: boolean | null = null;
      if (business) {
        try {
          const me = W.UserPrefs?.getMaybeMePnUser?.() ?? WPP.conn.getMyUserId();
          cartEnabled = (await sellerProfile(me)).cartEnabled;
        } catch (_) {
          cartEnabled = null;
        }
      }
      return { business, cartEnabled };
    }
    case 'catalog': {
      const wid = seller(request.chatId);
      const profile = await sellerProfile(wid);
      const products: unknown[] = [];
      if (profile.catalog) {
        for (const product of (await WPP.catalog.getProducts(wid.toString(), 100)) ?? []) {
          const id = text(product?.id);
          if (!id || product?.is_hidden === true) continue;
          products.push({ id, name: text(product.name), description: text(product.description),
                          price: amount(product.price), currency: text(product.currency),
                          available: product.availability === 'in stock' });
        }
      }
      return { ...profile, products, cart: cartView(wid.toString()) };
    }
    case 'cart':
      return { cart: cartView(seller(request.chatId).toString()) };
    case 'cartSet': {
      const wid = seller(request.chatId);
      if (!(await sellerProfile(wid)).cartEnabled) return { error: 'cart_disabled' };
      const id = wid.toString();
      await WPP.cart.add(id, [{ id: request.productId, qnt: request.quantity }]);
      const cart: any = cartView(id);
      // The library skips, without a word, a product the catalog no longer has.
      const item = cart.items.find((entry: any) => entry.id === request.productId);
      if (!item || item.quantity !== request.quantity) return { error: 'product_unavailable', cart };
      return { cart };
    }
    case 'cartRemove': {
      const id = seller(request.chatId).toString();
      await WPP.cart.remove(id, request.productId);
      return { cart: cartView(id) };
    }
    case 'cartClear': {
      const id = seller(request.chatId).toString();
      // WPP.cart.clear() throws on a cart that is empty already.
      if ((cartView(id) as any).items.length) await WPP.cart.clear(id);
      return { cart: cartView(id) };
    }
    case 'cartSubmit': {
      const wid = seller(request.chatId);
      if (!(await sellerProfile(wid)).cartEnabled) return { error: 'cart_disabled' };
      const id = wid.toString();
      if (!(cartView(id) as any).items.length) return { error: 'cart_empty' };
      const sent = await WPP.cart.submit(id, request.message || undefined);
      return { sent: true, id: text(sent?.id?._serialized ?? sent?.id), cart: cartView(id) };
    }
    case 'order': {
      const message = await orderMessage(request.messageId);
      if (!message) return { error: 'not_an_order' };
      const details = await W.functions.queryOrder(message.orderId, 80, 80, message.token);
      const products: unknown[] = [];
      for (const product of details?.products ?? []) {
        products.push({ id: String(product?.id ?? ''), name: text(product?.name), price: amount(product?.price),
                        currency: text(product?.currency), quantity: amount(product?.quantity) ?? 0 });
      }
      const mine = message.id?.fromMe === true;
      return { order: {
        mine, status: [1, 2, 3].includes(message.status) ? message.status : null,
        note: text(message.message), products,
        subtotal: amount(details?.subtotal), total: amount(details?.total), currency: text(details?.currency),
        createdAt: amount(details?.createdAt),
        // What this account may do with it, decided where the facts are.
        canAnswer: businessAccount() && !mine && message.status !== 2 && message.status !== 3,
        canUpdate: businessAccount() && !mine && (message.status === 2 || message.status === 3),
      } };
    }
    case 'orderAccept':
    case 'orderDecline':
    case 'orderUpdate': {
      if (!businessAccount()) return { error: 'not_business' };
      // The library only looks in the page's loaded messages.
      const message = W.MsgStore.get(request.messageId);
      if (!message || message.type !== 'order') return { error: 'not_an_order' };
      if (message.id?.fromMe === true) return { error: 'own_order' };
      if (request.action === 'orderAccept') await WPP.order.accept({ msgId: request.messageId });
      else if (request.action === 'orderDecline') {
        await WPP.order.decline({ msgId: request.messageId, declineNote: request.note || undefined });
      } else {
        await WPP.order.update({ msgId: request.messageId, orderStatus: request.status,
                                 orderNote: request.note || undefined,
                                 paymentStatus: request.paymentStatus || undefined });
      }
      const after = W.MsgStore.get(request.messageId);
      return { accepted: true, status: [1, 2, 3].includes(after?.status) ? after.status : null };
    }
    default:
      return { error: 'unknown_action' };
  }
}

const STATUS_OF: Record<string, number> = {
  invalid_request: 400, not_business: 403, own_order: 403, not_an_order: 404,
  cart_disabled: 409, product_unavailable: 409, cart_empty: 409,
};

async function run(req: Request, res: Response, request: Record<string, unknown> | null) {
  if (!request) return res.status(400).json({ status: 'error', code: 'invalid_request' });
  try {
    const page: any = (req.client as any)?.page;
    if (!page || page.isClosed?.()) throw new Error('commerce_unavailable');
    const answer = await page.evaluate(commerceInPage, request);
    if (!answer || typeof answer !== 'object') throw new Error('commerce_unavailable');
    if (typeof answer.error === 'string') {
      const status = STATUS_OF[answer.error];
      if (!status) throw new Error('commerce_unavailable');
      // A refusal still carries the cart when there is one to show.
      return res.status(status).json({ status: 'error', code: answer.error,
                                       ...(answer.cart ? { response: { cart: answer.cart } } : {}) });
    }
    return res.status(200).json({ status: 'success', response: answer });
  } catch (_) {
    // Never the native error: it can carry a chat id or a product name.
    return res.status(503).json({ status: 'error', code: 'commerce_unavailable' });
  }
}

const quantity = (value: unknown) =>
  typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 99 ? value : null;
const note = (value: unknown) =>
  value === undefined || value === null || value === '' ? ''
    : typeof value === 'string' && value.length <= 1024 ? value : null;

export const account = (req: Request, res: Response) => run(req, res, { action: 'account' });

export const catalog = (req: Request, res: Response) =>
  run(req, res, validChat(req.params.chatId) ? { action: 'catalog', chatId: req.params.chatId } : null);

export const cart = (req: Request, res: Response) =>
  run(req, res, validChat(req.params.chatId) ? { action: 'cart', chatId: req.params.chatId } : null);

export function cartSet(req: Request, res: Response) {
  const amount = quantity(req.body?.quantity);
  const valid = validChat(req.params.chatId) && validProduct(req.body?.productId) && amount !== null;
  return run(req, res, valid ? { action: 'cartSet', chatId: req.params.chatId,
                                 productId: req.body.productId, quantity: amount } : null);
}

export function cartRemove(req: Request, res: Response) {
  const valid = validChat(req.params.chatId) && validProduct(req.body?.productId);
  return run(req, res, valid ? { action: 'cartRemove', chatId: req.params.chatId,
                                 productId: req.body.productId } : null);
}

export const cartClear = (req: Request, res: Response) =>
  run(req, res, validChat(req.params.chatId) ? { action: 'cartClear', chatId: req.params.chatId } : null);

export function cartSubmit(req: Request, res: Response) {
  const message = note(req.body?.message);
  const valid = validChat(req.params.chatId) && message !== null;
  return run(req, res, valid ? { action: 'cartSubmit', chatId: req.params.chatId, message } : null);
}

export const order = (req: Request, res: Response) =>
  run(req, res, validMessage(req.params.messageId) ? { action: 'order', messageId: req.params.messageId } : null);

export const orderAccept = (req: Request, res: Response) =>
  run(req, res, validMessage(req.params.messageId)
    ? { action: 'orderAccept', messageId: req.params.messageId } : null);

export function orderDecline(req: Request, res: Response) {
  const text = note(req.body?.note);
  const valid = validMessage(req.params.messageId) && text !== null;
  return run(req, res, valid ? { action: 'orderDecline', messageId: req.params.messageId, note: text } : null);
}

export function orderUpdate(req: Request, res: Response) {
  const text = note(req.body?.note);
  const payment = req.body?.paymentStatus;
  const valid = validMessage(req.params.messageId) && text !== null
    && ORDER_STATUSES.includes(req.body?.status)
    && (payment === undefined || payment === null || PAYMENT_STATUSES.includes(payment));
  return run(req, res, valid ? { action: 'orderUpdate', messageId: req.params.messageId, status: req.body.status,
                                 note: text, paymentStatus: payment || '' } : null);
}
