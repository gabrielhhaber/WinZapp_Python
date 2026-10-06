/**
 * WinZapp: save / remove a contact that WhatsApp syncs to the phone's address
 * book. WA-JS already has WPP.contact.save / WPP.contact.remove (the same
 * action WhatsApp Web's own "add contact" runs); neither wppconnect nor the
 * server exposes them, so this only forwards to the page.
 */
import type { Request, Response } from 'express';

type SaveCommand = {
  id: string;
  name: string;
  lastName: string;
  syncAddressBook: boolean;
};

const KNOWN_CODES = [
  'contact_not_available',
  'contact_invalid',
  'contact_name_required',
  'contact_lid_without_phone',
  'contact_not_found',
  'number_is_not_your_contact',
];

// The only two id forms a contact has. Anything else (a group, a broadcast, a
// newsletter, free text) never reaches the page.
const WID = /^\d{7,20}@(c\.us|lid)$/;

/**
 * The contact id out of req.body.phone, or '' when it is not one.
 *
 * save-contact runs behind statusConnection, which replaces the field with an
 * array holding the id WhatsApp resolved for the number (['<id>@c.us']: this
 * is what settles the Brazilian 9th digit). remove-contact does not, and gets
 * what the client sent: bare digits, or a whole '<digits>@lid'.
 */
export function toWid(phone: unknown): string {
  const first = Array.isArray(phone) ? phone[0] : phone;
  const raw = String(first ?? '').trim();
  const wid = raw.includes('@') ? raw : `${raw.replace(/\D/g, '')}@c.us`;
  return WID.test(wid) ? wid : '';
}

function fail(res: Response, error: any) {
  // No arbitrary exception text: native errors can contain names and numbers.
  const code = String(error?.code || '');
  const safe = KNOWN_CODES.includes(code) ? code : 'contact_operation_failed';
  const status = safe === 'contact_not_available' ? 503
    : safe === 'contact_operation_failed' ? 502 : 400;
  return res.status(status).json({ status: 'error', code: safe });
}

function livePage(req: Request, res: Response) {
  const page = (req.client as any)?.page;
  if (!page || page.isClosed()) {
    fail(res, { code: 'contact_not_available' });
    return null;
  }
  return page;
}

export async function saveContact(req: Request, res: Response) {
  // Only our fields enter the page. No function name or arbitrary JS payload.
  const { phone, name, lastName, syncAddressBook } = req.body || {};
  const id = toWid(phone);
  const first = String(name ?? '').trim().slice(0, 100);
  if (!id) return fail(res, { code: 'contact_invalid' });
  if (!first) return fail(res, { code: 'contact_name_required' });
  const page = livePage(req, res);
  if (!page) return;
  const command: SaveCommand = {
    id,
    name: first,
    lastName: String(lastName ?? '').trim().slice(0, 100),
    // Synced to the phone unless the caller says otherwise.
    syncAddressBook: syncAddressBook !== false,
  };
  try {
    // Errors cross page.evaluate as a message only, so the page answers with
    // the WPPError code instead of throwing it.
    const result = await page.evaluate(async (cmd: SaveCommand) => {
      const WPP = (window as any).WPP;
      if (!WPP?.contact?.save) return { code: 'contact_not_available' };
      try {
        if (cmd.id.endsWith('@lid')) {
          // WPP.contact.save reads the phone side of an @lid without a guard
          // and throws a TypeError when WhatsApp Web does not know it yet.
          // That would surface as "operation failed, try again", which never
          // works: say what it is, so the client can point to the local tab.
          let entry: any = null;
          try {
            entry = await WPP.contact.getPnLidEntry(cmd.id);
          } catch (error) {
            entry = null;
          }
          if (!entry?.phoneNumber) return { code: 'contact_lid_without_phone' };
        }
        const contact = await WPP.contact.save(cmd.id, cmd.name, {
          lastName: cmd.lastName,
          syncAddressBook: cmd.syncAddressBook,
        });
        return {
          isMyContact: !!contact?.isMyContact,
          syncToAddressbook: !!contact?.syncToAddressbook,
        };
      } catch (error: any) {
        return { code: String(error?.code || '') };
      }
    }, command);
    if (result?.code !== undefined) return fail(res, result);
    // The id the contact was saved under, so the client files it under the
    // same one instead of under whatever digits the user typed.
    return res.status(200).json({
      status: 'success',
      response: {
        id,
        isMyContact: !!result?.isMyContact,
        syncToAddressbook: !!result?.syncToAddressbook,
      },
    });
  } catch (error: any) {
    return fail(res, error);
  }
}

export async function removeContact(req: Request, res: Response) {
  const id = toWid(req.body?.phone);
  if (!id) return fail(res, { code: 'contact_invalid' });
  const page = livePage(req, res);
  if (!page) return;
  try {
    const result = await page.evaluate(async (contactId: string) => {
      const WPP = (window as any).WPP;
      if (!WPP?.contact?.remove) return { code: 'contact_not_available' };
      try {
        await WPP.contact.remove(contactId);
        return {};
      } catch (error: any) {
        // WPPError carries its own code (contact_not_found, number_is_not_your_contact).
        return { code: String(error?.code || '') };
      }
    }, id);
    if (result?.code !== undefined) return fail(res, result);
    return res.status(200).json({ status: 'success', response: { message: 'Contact removed' } });
  } catch (error: any) {
    return fail(res, error);
  }
}
