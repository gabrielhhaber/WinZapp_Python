// The phone number WhatsApp Web holds for an @lid, read inside the page for
// checkNumberStatus (the typed number's answer) and getAllContacts (saved @lid
// contacts). Plain JavaScript and self-contained on purpose: page.evaluate()
// sends each function's source to the page, and tests/test_lid_number_page.py
// runs them under Node with a fake WPP.

export function pageNumberOfLid(lid) {
  // @ts-ignore: WPP is a global of WhatsApp Web's page
  const wpp = window.WPP?.whatsapp;
  const pn = wpp?.ApiContact?.getPhoneNumber?.(wpp?.WidFactory?.createWid?.(lid));
  const text = typeof pn === 'string' ? pn : pn?._serialized;
  return typeof text === 'string' && /^\d+@c\.us$/.test(text) ? text : '';
}

export function pageNumbersOfLids(ids) {
  // @ts-ignore: WPP is a global of WhatsApp Web's page
  const store = window.WPP?.whatsapp?.ContactStore;
  const found = {};
  for (const id of ids) {
    const pn = store?.get?.(id)?.phoneNumber;
    const text = typeof pn === 'string' ? pn : pn?._serialized;
    if (typeof text === 'string' && /^\d+@c\.us$/.test(text)) found[id] = text;
  }
  return found;
}
