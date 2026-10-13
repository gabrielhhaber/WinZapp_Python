# Carts and orders

## Who sees what

Read from WhatsApp Web's own program text (2.3000.1049387914), not assumed:

| | who | where it is decided |
| --- | --- | --- |
| a business's catalog and the cart | any account, in a chat with a business | the chat's contact is a business (`contact.isBusiness`), its profile has a catalog (`profileOptions.commerceExperience`) |
| filling and sending the cart | the same, when the business takes carts | `profileOptions.cartEnabled`. The library does not check it; WinZapp's route does, and neither fills nor sends a cart to a business that turned it off |
| opening an order message | both sides | WhatsApp Web's "View sent cart" / "View received cart" |
| accept, decline, update an order | a WhatsApp Business account, for an order it received | `WAWebMobilePlatforms.isSMB()`, the test behind WhatsApp Web's "Orders" menu |
| create an order for a customer, the cart setting | a WhatsApp Business account | the same |

The client never works these out. `MainWindow.commerce_account()`
(`main_window/commerce.py`) keeps the server's reading (business or not, and
the ids of the chats with a business) and refreshes it in the background;
until a reading arrives, nothing commercial is offered. An order carries its
own `canAnswer` / `canUpdate`.

## Routes

WinZapp's own (`commerceController.ts`), because wppconnect-server has none
for `WPP.cart.*` nor for `WPP.order.accept/decline/update`, and its only order
route passes on a model that does not survive being returned by value:

```
GET  commerce/account                    business, own cart setting, chats with a business
GET  commerce/catalog/:chatId            catalog, cartEnabled, products, the cart
GET  commerce/cart/:chatId
POST commerce/cart/:chatId/set           {productId, quantity}   add or change (WPP.cart.add)
POST commerce/cart/:chatId/remove        {productId}
POST commerce/cart/:chatId/clear
POST commerce/cart/:chatId/submit        {message}               WPP.cart.submit, once
GET  commerce/order/:messageId
POST commerce/order/:messageId/accept
POST commerce/order/:messageId/decline   {note}
POST commerce/order/:messageId/status    {status, note, paymentStatus}
```

Upstream, used as they are: `send-order-message` (a business writes an order;
`WPP.chat.sendChargeMessage`) and `set-cart-enabled`.

Every amount is an integer in thousandths of the currency unit.

## Messages

`order` (a cart someone sent) becomes `orderMessage`; an `interactive`
message whose native flow is one of `order_details`, `order_status`,
`payment_info`, `payment_status` **and** whose button carries a
`reference_id` becomes `orderUpdateMessage`. Any other interactive message
keeps the handling it had. `product` (one item of a catalog) becomes
`productMessage`: its title, description, price and currency, never its `body`,
which is the picture's thumbnail (left to the text fallback it was a row of
base64 or "unsupported message"). All three are listed, previewed, notified and
counted as unread. The token that opens an order is not stored: the order window asks
the server by message id.

## What was run, and what was not

Run over CDP on a real, non-business account, with the page function as it
ships and nothing sent to anyone:

- the account reading (not a business; 60 chats with a business, 8 of them
  with products);
- a catalog; the cart of a business that takes carts: set 2, set 5, read,
  remove, clear (the page's own cart table, left empty);
- the refusal for a business whose cart is off, for a product the catalog no
  longer has, for sending an empty cart; the seller's actions answering
  `not_business`.

What that run showed, and the code relies on: a product's price is a string
of digits in thousandths, a cart item's too, while the cart's total is a
number; `WPP.cart.get()` returned by value carries no items; `WPP.cart.clear()`
throws on an empty cart; `WPP.cart.add()` skips, without a word, a product the
catalog no longer has (answered as `product_unavailable`).

## One real order

With the account owner's leave, one order (a cart with one item) was then sent
to a business of their choosing, read back and deleted for everyone. It
arrived (ack 2) and its fields were the ones `order_payload()` reads:
`itemCount`, `status` 1, `message`, `totalAmount1000` (a number),
`totalCurrencyCode`. Two faults of the library showed, and the route works
around both:

- **`WPP.cart.submit()` throws after sending.** It looks the message up at
  once (`getMessageById`), before the page has it, and throws `msg_not_found`
  for an order that went out. The route no longer takes a throw for "not
  sent": it reads the chat for a new order message of the account's own. A
  throw with nothing new in the chat is answered as unavailable, which the
  window says as "may or may not have been sent".
- **The library cannot read an order on this WhatsApp Web.** It calls the
  order query with positional arguments (`orderId, width, height, token`);
  WhatsApp Web now takes one object and needs `sellerJid` in it. So
  `WPP.order.get()`, `accept()`, `decline()` and `update()` all failed with
  `ServerStatusCodeError` before doing anything. Reading an order calls the
  query in its current form (run on the real order: one product, price and
  quantity as numbers, subtotal and total). The three seller functions are run
  with the library's own reference to the query swapped, for the length of
  the call, for one that translates and adds the seller's id; with that swap
  `WPP.order.get()` read the real order, and the reference was put back.

A second order was then sent, with the same leave, through the page function
as it ships after those two changes: it answered `sent: true` with the id of
the message that was in the chat (about a second after the call), and reading
it back gave the order whole (own order, state 1, the note, the product with
name, price and quantity, subtotal and total, `canAnswer` and `canUpdate`
false). A seller's action on it answered `not_business`. It was deleted for
everyone like the first.

## Cancelling, as WhatsApp Web does it

Read from the order drawer's own program text
(`WAWebCartOrderDetailDrawer.react`, `WAWebBizProductDetailsFlow.react`):

- the **buyer** has no cancel. A sent cart opens read-only ("Your sent
  cart"); taking it back is the ordinary "delete message", which is what the
  test above used (`isRevoked: true`);
- the **seller** cancels: "Decline order", which asks "Decline this order?
  You can include a note with the cancellation message", and later the
  status "Canceled" in "Update order status".

WinZapp has the same and no more: no cancel button for the buyer; "Decline
order" with its note and the status list for a business account.

**Still not run:**

- the seller's side past the order query (it needs a WhatsApp Business
  account and an order received);
- an order update (`interactive`, native flow) as it arrives: the account had
  none. Its fields come from `WAWebOrderStatus` and `WAWebOrderDetails`;

So the code claims no more than it can read back:

- a cart change shows the cart the server answers, refusal or not;
- a cart is sent once; anything but a definite refusal leaves the window
  saying "may or may not have been sent" and offers no second try by itself;
- accept and decline are announced when the order, read again, shows the new
  state; otherwise "sent, not shown yet";
- a status update leaves nothing to read back and is announced as "accepted
  for sending", nothing more;
- the cart setting is announced from the server's reading after the change.

The library's accept, decline and update look for the message in the page's
loaded messages only (`MsgStore.get`); an order far back in a chat answers
`not_an_order` until the chat is opened at that message.

Tests: `tests/test_commerce.py` (Python), `commerceController.test.ts` (Jest).
