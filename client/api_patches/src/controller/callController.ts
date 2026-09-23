/*
 * WinZapp call-control endpoints.
 *
 * WhatsApp Web owns signaling and encryption inside Chromium. WinZapp owns the
 * accessible desktop UI and the Python microphone/speaker pipeline. These
 * endpoints expose the WA-JS call functions WPPConnect Server does not expose
 * yet, while activating the injected media bridge before a call asks for audio.
 */
import { Request, Response } from 'express';

import {
  ensureCallMediaBridge,
  setCallMediaBridgeActive,
  warmCallVoipRuntime,
} from '../util/callMediaBridge';

type CallActionPayload = {
  callId?: string;
  to?: string;
  isVideo?: boolean;
};

function getWhatsappPage(req: Request): any {
  const client = req.client as any;
  const page = client?.waPage || client?.page;
  if (!page || typeof page.evaluate !== 'function') {
    throw new Error('WhatsApp page is not available for call control');
  }
  return page;
}

async function evaluateWppCall(req: Request, action: string, payload: CallActionPayload = {}) {
  const page = getWhatsappPage(req);
  const logger = (req as any).logger;
  const session = String((req.client as any)?.session || 'unknown');
  // Session startup warms the lazy VoIP bundle in the background. Await it in
  // Node, before entering the browser context, so the browser callback never
  // tries to resolve a Node-side helper name.
  await warmCallVoipRuntime(req.client, logger);

  let result: any;
  try {
    result = await page.evaluate(
    async ({ action, payload }) => {
      const win = window as any;
      if (!win.WPP?.call) throw new Error('WPP.call is not available');

      const serializeId = (value: any): string => {
        if (!value) return '';
        if (typeof value === 'string') return value;
        return String(value._serialized || value.id || value.toString?.() || value || '');
      };

      const callIdOf = (call: any): string => serializeId(call?.id);
      const peerJidOf = (call: any): string => serializeId(call?.peerJid || call?.sender || call?.from);
      const getCallStore = () => {
        try {
          const module = win.require?.('WAWebCallCollection');
          const nativeStore =
            module?.activeCall !== undefined ? module : module?.get?.() || module;
          if (nativeStore) return nativeStore;
        } catch (_) {}
        return win.WPP?.whatsapp?.CallStore || win.Store?.Call || null;
      };
      const sameCallId = (call: any, wanted: string): boolean => {
        if (!call || !wanted) return false;
        return callIdOf(call) === wanted || serializeId(call?.id?._serialized) === wanted;
      };

      const callStateOf = (call: any): string => {
        // getState() legitimately returns numeric 0 for the terminal/NONE
        // state. Preserve that value when inspecting call lifecycle.
        const rawValue =
          call?.getState?.() ?? call?.state ?? call?.get?.('state') ?? '';
        const raw = String(rawValue);
        const numericStates: Record<string, string> = {
          '0': 'NONE',
          '1': 'CALLING',
          '2': 'PREACCEPT_RECEIVED',
          '3': 'INCOMING_RING',
          '4': 'ACCEPT_SENT',
          '5': 'ACCEPT_RECEIVED',
          '6': 'ACTIVE',
          '7': 'HANDLED_REMOTELY',
          '8': 'INCOMING_RING',
          '9': 'REJOINING',
          '10': 'LINK',
          '11': 'CONNECTED_LONELY',
          '12': 'PRE_CALLING',
          '13': 'ENDED',
          '14': 'CALL_B_STARTING',
        };
        return numericStates[raw] || raw;
      };

      const isIncomingCall = (call: any): boolean => {
        const state = callStateOf(call);
        return (
          state === 'INCOMING_RING' ||
          state === 'ReceivedCall' ||
          state === 'ReceivedCallWithoutOffer' ||
          call?.isIncoming === true ||
          call?.direction === 'incoming'
        );
      };

      const isOutgoingOrLiveCall = (call: any): boolean => {
        const state = callStateOf(call);
        return (
          state === 'CALLING' ||
          state === 'PRE_CALLING' ||
          state === 'ACCEPT_SENT' ||
          state === 'ACCEPT_RECEIVED' ||
          state === 'ACTIVE' ||
          state === 'CALL_B_STARTING' ||
          call?.outgoing === true ||
          call?.isOutgoing === true ||
          call?.direction === 'outgoing'
        );
      };

      const getModels = (store: any): any[] => {
        try {
          const models = store?.getModelsArray?.();
          if (Array.isArray(models)) return models;
        } catch (_) {}
        if (Array.isArray(store?.models)) return store.models;
        if (Array.isArray(store?._models)) return store._models;
        return [];
      };

      const findCall = (wanted = ''): any => {
        const store = getCallStore();
        const active = store?.activeCall;
        if (active && (!wanted || sameCallId(active, wanted))) return active;

        if (wanted && typeof store?.get === 'function') {
          try {
            const direct = store.get(wanted);
            if (direct) return direct;
          } catch (_) {}
        }

        const models = getModels(store);
        if (wanted) {
          const exact = models.find((call) => sameCallId(call, wanted));
          if (exact) return exact;
        }
        return (
          models.find((call) => isIncomingCall(call) || isOutgoingOrLiveCall(call)) ||
          null
        );
      };

      const summarizeCall = (call: any) => ({
        id: callIdOf(call),
        peerJid: peerJidOf(call),
        state: callStateOf(call),
        isVideo: !!call?.isVideo,
        endReason: String(call?.get?.('endReason') ?? call?.endReason ?? '').slice(0, 120),
        error: String(call?.get?.('error') ?? call?.error ?? '').slice(0, 120),
        outgoing: !!call?.outgoing,
      });

      const forgetIncomingCall = (callId: string) => {
        if (!callId) return;
        try {
          win.__winzappForgetIncomingCall?.(callId);
        } catch (_) {}
      };

      const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

      const getNativeVoipStack = async (): Promise<any> => {
        const getter =
          win.WPP?.whatsapp?.functions?.getVoipStackInterface ||
          win.WPP?.whatsapp?.getVoipStackInterface;
        if (typeof getter !== 'function') return null;
        return getter();
      };

      const isVoipInitialized = (): boolean | undefined => {
        const conn =
          win.WPP?.whatsapp?.ConnStore || win.Store?.Conn || win.WPP?.whatsapp?.Conn;
        if (!conn || typeof conn.isVoipInitialized !== 'boolean') return undefined;
        return conn.isVoipInitialized;
      };

      const ensureVoipRuntimeReady = async (): Promise<any> => {
        // WA-JS' enableCallInterface flips the calling AB props, but it marks
        // itself enabled before its best-effort backend init. If that first
        // init races the lazy VoIP bundle, later calls never retry it. Retry
        // the actual backend initialization here before every call action.
        const enable = win.WPP?.call?.enableCallInterface;
        if (typeof enable === 'function') await enable();

        const functions = win.WPP?.whatsapp?.functions || {};
        const requireBackend =
          functions.requireVoipJsBackend ||
          win.WPP?.whatsapp?.requireVoipJsBackend;

        let lastError: any = null;
        for (let attempt = 0; attempt < 8; attempt += 1) {
          try {
            if (typeof requireBackend === 'function') {
              const backend = await requireBackend();
              const init =
                backend?.WAWebVoipInit?.initWAWebVoip ||
                backend?.initWAWebVoip;
              if (typeof init === 'function') {
                const initModule = backend?.WAWebVoipInit || backend;
                await init.call(initModule, 'winzapp_call_action');
                const emitter = initModule?.VoipInitEventEmitter;
                if (
                  emitter?.getIsVoipInited?.() !== true &&
                  emitter?.getDidVoipInitError?.() === true &&
                  typeof initModule?.retryWAWebVoipInitAfterFailure === 'function'
                ) {
                  await initModule.retryWAWebVoipInitAfterFailure();
                }
                if (emitter?.getIsVoipInited?.() === false) {
                  throw new Error('WhatsApp VoIP initializer completed without becoming ready');
                }
              }
            }

            const stack = await getNativeVoipStack();
            if (stack && isVoipInitialized() !== false) return stack;
            lastError = stack
              ? new Error('WhatsApp VoIP connection initialization is pending')
              : new Error('VoIP stack interface is not available');
          } catch (error) {
            lastError = error;
          }
          await delay(180 * (attempt + 1));
        }

        const message = String(lastError?.message || lastError || 'unknown error');
        throw new Error(`WhatsApp VoIP initialization failed: ${message}`);
      };

      const isVoipInitError = (error: any): boolean =>
        String(error?.message || error || '').includes('without successful voipInit');

      const runNativeVoipAction = async (fn: (stack: any) => Promise<any>): Promise<any> => {
        let lastError: any = null;
        const maxAttempts = 8;
        for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
          const stack = await ensureVoipRuntimeReady();
          try {
            return await fn(stack);
          } catch (error) {
            lastError = error;
            if (!isVoipInitError(error) || attempt >= maxAttempts - 1) throw error;
            // The interface object can exist before its worker-side RPC has
            // completed voipInit. Give that lazy backend a bounded window to
            // settle, then reacquire/reinitialize it on the next iteration.
            await delay(Math.min(1500, 300 * (attempt + 1)));
          }
        }
        throw lastError;
      };

      if (action === 'accept') {
        const callId = String(payload.callId || '');
        const call = findCall(callId);
        if (call) {
          const result = await runNativeVoipAction(async (voipStack: any) => {
            if (typeof voipStack?.acceptCall !== 'function') {
              throw new Error('Native VoIP acceptCall is not available');
            }
            await voipStack.acceptCall(true, call?.isVideo === true);
            return { handled: true, via: 'native-voip', call: summarizeCall(call) };
          });
          forgetIncomingCall(callId || callIdOf(call));
          return result;
        }
        const result = await win.WPP.call.accept(callId || undefined);
        forgetIncomingCall(callId);
        return result;
      }

      if (action === 'reject') {
        const callId = String(payload.callId || '');
        const call = findCall(callId);
        if (call) {
          const result = await runNativeVoipAction(async (voipStack: any) => {
            if (typeof voipStack?.rejectCall !== 'function') {
              throw new Error('Native VoIP rejectCall is not available');
            }
            call.userEndedCall = true;
            await voipStack.rejectCall();
            return { handled: true, via: 'native-voip', call: summarizeCall(call) };
          });
          forgetIncomingCall(callId || callIdOf(call));
          return result;
        }
        const reject = win.WPP.call.rejectCall || win.WPP.call.reject;
        if (typeof reject !== 'function') throw new Error('WPP.call.reject is not available');
        const result = await reject(callId || undefined);
        forgetIncomingCall(callId);
        return result;
      }

      if (action === 'end') {
        const requestedCallId = String(payload.callId || '');
        const call = findCall(requestedCallId);
        const handledCallId = requestedCallId || callIdOf(call);
        const result = await runNativeVoipAction(async (voipStack: any) => {
          if (typeof voipStack?.endCall === 'function') {
            if (call) call.userEndedCall = true;
            await voipStack.endCall(2, true);
            return { handled: true, via: 'native-voip', call: call ? summarizeCall(call) : null };
          }
          return win.WPP.call.end();
        });
        forgetIncomingCall(handledCallId);
        return result;
      }

      if (action === 'offer') {
        let call: any = null;
        const storeBeforeOffer = getCallStore();
        const preexistingIds = new Set(
          getModels(storeBeforeOffer).map((model) => callIdOf(model)).filter(Boolean)
        );
        const previousActiveId = callIdOf(storeBeforeOffer?.activeCall);

        for (let attempt = 0; attempt < 3 && !call; attempt += 1) {
          await ensureVoipRuntimeReady();
          const offered = await win.WPP.call.offer(payload.to, { isVideo: !!payload.isVideo });

          // Current WhatsApp Web promotes the real ongoing call through
          // CallStore.activeCall.  The value returned by the legacy collection
          // lookup may point at an older call model, so prefer a newly-active
          // call and only trust the direct return value when it is not stale.
          for (let waitAttempt = 0; waitAttempt < 50 && !call; waitAttempt += 1) {
            const store = getCallStore();
            const active = store?.activeCall;
            const activeId = callIdOf(active);
            if (
              active &&
              isOutgoingOrLiveCall(active) &&
              (!previousActiveId || activeId !== previousActiveId)
            ) {
              call = active;
              break;
            }

            const offeredId = callIdOf(offered);
            if (offered && offeredId && !preexistingIds.has(offeredId)) {
              call = offered;
              break;
            }

            const freshModel = getModels(store).find((model) => {
              const modelId = callIdOf(model);
              return !!modelId && !preexistingIds.has(modelId) && isOutgoingOrLiveCall(model);
            });
            if (freshModel) {
              call = freshModel;
              break;
            }
            await delay(100);
          }
          if (!call && attempt < 2) await delay(300 * (attempt + 1));
        }
        if (!call) throw new Error('WhatsApp did not create an outgoing call model');
        return summarizeCall(call);
      }

      throw new Error(`Unsupported call action: ${action}`);
    },
    { action, payload }
    );
  } catch (error: any) {
    throw error;
  }

  return result;
}

function ok(res: Response, response: any) {
  res.status(200).json({ status: 'success', response });
}

function fail(req: Request, res: Response, action: string, error: unknown) {
  req.logger.error(error);
  const message = error instanceof Error ? error.message : String(error);
  res.status(500).json({ status: 'error', message: `Error on ${action}`, error: message });
}

async function installAudioBridge(req: Request): Promise<boolean> {
  const client = req.client as any;
  const installed = await ensureCallMediaBridge(client, req.io, req.logger);
  if (!installed) throw new Error('WinZapp call media bridge is not available');
  return true;
}

async function prepareAudioBridge(req: Request): Promise<boolean> {
  await installAudioBridge(req);
  const enabled = await setCallMediaBridgeActive(req.client as any, true);
  if (!enabled) throw new Error('WinZapp call media bridge could not be enabled');
  return true;
}

async function stopAudioBridge(req: Request): Promise<void> {
  await setCallMediaBridgeActive(req.client as any, false);
}

export async function acceptCall(req: Request, res: Response) {
  try {
    await prepareAudioBridge(req);
    ok(res, await evaluateWppCall(req, 'accept', req.body || {}));
  } catch (error) {
    await stopAudioBridge(req);
    fail(req, res, 'acceptCall', error);
  }
}

export async function enableCallAudio(req: Request, res: Response) {
  try {
    ok(res, { enabled: await prepareAudioBridge(req) });
  } catch (error) {
    fail(req, res, 'enableCallAudio', error);
  }
}

export async function rejectCall(req: Request, res: Response) {
  try {
    await installAudioBridge(req);
    await stopAudioBridge(req);
    ok(res, await evaluateWppCall(req, 'reject', req.body || {}));
  } catch (error) {
    fail(req, res, 'rejectCall', error);
  }
}

export async function endCall(req: Request, res: Response) {
  try {
    await installAudioBridge(req);
    await stopAudioBridge(req);
    ok(res, await evaluateWppCall(req, 'end', req.body || {}));
  } catch (error) {
    fail(req, res, 'endCall', error);
  }
}

export async function offerCall(req: Request, res: Response) {
  try {
    const body = req.body || {};
    if (!body.to) {
      res.status(400).json({ status: 'error', message: 'Parameter to is required!' });
      return;
    }
    await prepareAudioBridge(req);
    ok(res, await evaluateWppCall(req, 'offer', body));
  } catch (error) {
    await stopAudioBridge(req);
    fail(req, res, 'offerCall', error);
  }
}

export async function callDiagnostics(req: Request, res: Response) {
  try {
    const page = getWhatsappPage(req);
    const response = await page.evaluate(async () => {
      const win = window as any;
      const functions = win.WPP?.whatsapp?.functions || {};
      const conn = win.WPP?.whatsapp?.ConnStore || win.Store?.Conn || win.WPP?.whatsapp?.Conn;
      let backend: any = null;
      let backendError = '';
      try {
        backend = await functions.requireVoipJsBackend?.();
      } catch (error: any) {
        backendError = String(error?.stack || error?.message || error);
      }
      const stack = await functions.getVoipStackInterface?.().catch?.((error: any) => {
        backendError ||= String(error?.stack || error?.message || error);
        return null;
      });
      const permission = async (name: string) => {
        try {
          return (await navigator.permissions.query({ name } as any)).state;
        } catch (error: any) {
          return `error:${error?.message || error}`;
        }
      };
      return {
        userAgent: navigator.userAgent,
        webdriver: navigator.webdriver,
        secureContext: window.isSecureContext,
        mediaDevices: !!navigator.mediaDevices,
        getUserMedia: typeof navigator.mediaDevices?.getUserMedia,
        audioPermission: await permission('microphone'),
        videoPermission: await permission('camera'),
        rtcPeerConnection: typeof win.RTCPeerConnection,
        audioContext: typeof (win.AudioContext || win.webkitAudioContext),
        worker: typeof win.Worker,
        sharedWorker: typeof win.SharedWorker,
        webAssembly: typeof win.WebAssembly,
        crossOriginIsolated: win.crossOriginIsolated,
        requireBackend: typeof functions.requireVoipJsBackend,
        backendKeys: backend ? Object.keys(backend).sort() : [],
        backendError,
        initFunction: typeof (backend?.WAWebVoipInit?.initWAWebVoip || backend?.initWAWebVoip),
        stack: !!stack,
        stackMethods: stack ? Object.keys(stack).sort() : [],
        stackVoipInitArity: stack?.voipInit?.length,
        stackVoipInitSource: stack?.voipInit ? String(stack.voipInit).slice(0, 1200) : '',
        backendInitArity: (backend?.WAWebVoipInit?.initWAWebVoip || backend?.initWAWebVoip)?.length,
        backendInitSource: (backend?.WAWebVoipInit?.initWAWebVoip || backend?.initWAWebVoip)
          ? String(backend?.WAWebVoipInit?.initWAWebVoip || backend?.initWAWebVoip).slice(0, 1200)
          : '',
        emitterReady: backend?.WAWebVoipInit?.VoipInitEventEmitter?.getIsVoipInited?.(),
        emitterFailed: backend?.WAWebVoipInit?.VoipInitEventEmitter?.getDidVoipInitError?.(),
        callMediaBridge: (() => {
          const bridge = win.__winzappCallMediaBridge;
          return bridge ? {
            version: bridge.version || 0,
            // Which remote tap actually ran. The worklet is the fix for the
            // ~1% of buffers the ScriptProcessor dropped, and its Blob-URL
            // module can be refused by the page's CSP, so a build that fell
            // back has to be recognisable from the outside rather than
            // looking fixed.
            remoteTapMode: bridge.remoteTapMode || '',
            // The mic tap has its own mode: only the PEER can hear this side
            // chop, so a silent fallback here is otherwise found by someone
            // else's ears rather than by a diagnostic.
            micTapMode: bridge.micTapMode || '',
            audioWorkletStatus: bridge.audioWorkletStatus || 'unknown',
            enabled: !!bridge.enabled,
            micFramesPushed: bridge.micFramesPushed || 0,
            micBytesPushed: bridge.micBytesPushed || 0,
            micSamplesConsumed: bridge.micSamplesConsumed || 0,
            micTrackLive: bridge.micDestination?.stream?.getAudioTracks?.()[0]?.readyState === 'live',
          } : null;
        })(),
        retryFunction: typeof backend?.WAWebVoipInit?.retryWAWebVoipInitAfterFailure,
        isVoipInitialized: conn?.isVoipInitialized,
        connectionKeys: conn ? Object.keys(conn).filter((key) => /voip|call/i.test(key)).sort() : [],
      };
    });
    ok(res, response);
  } catch (error) {
    fail(req, res, 'callDiagnostics', error);
  }
}
