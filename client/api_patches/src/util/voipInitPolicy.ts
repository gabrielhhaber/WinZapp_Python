/* Serialized into the page: no imports, devices, reloads or account data. */
export function installVoipInitPolicyInPage(): void {
  const win = window as any;
  if (win.__winzappVoipPolicyVersion === 1) return;
  win.__winzappVoipPolicyVersion = 1;
  const read = (initModule: any) => {
    const emitter = initModule?.VoipInitEventEmitter;
    const get = (name: string) => {
      try {
        if (typeof emitter?.[name] === 'function') return emitter[name]();
        if (typeof initModule?.[name] === 'function') return initModule[name]();
      } catch (_) { /* A renamed/throwing optional getter is unknown. */ }
      return null;
    };
    const bool = (value: any) => typeof value === 'boolean' ? value : null;
    const failure = get('getVoipInitFailureClass');
    return {
      ready: bool(get('getIsVoipInited')),
      failed: bool(get('getDidVoipInitError')),
      // Raw strings can contain account data. Only measured, bounded codes.
      voipInitFailureClass: failure === 'wasm_contract_mismatch'
        ? failure : (failure == null ? '' : 'other'),
      canRetry: bool(get('canRetryWAWebVoipInitAfterFailure')),
    };
  };
  const refuseFatal = (initModule: any) => {
    const state = read(initModule);
    if (state.ready !== true && state.failed === true && state.canRetry === false
        && state.voipInitFailureClass === 'wasm_contract_mismatch') {
      const error = Object.assign(new Error(
        'WhatsApp VoIP initialization failed: wasm_contract_mismatch (retry=false)'
      ), { winzappVoipFatal: true });
      throw error;
    }
    return state;
  };
  win.__winzappReadVoipInitState = read;
  win.__winzappInitializeVoip = async (initModule: any, reason: string,
                                     init = initModule?.initWAWebVoip) => {
    // Inspect the fatal state BEFORE calling init again. An error bit alone
    // is insufficient: pending/unknown and old backends keep bounded polling.
    if (refuseFatal(initModule).ready === true) return;
    if (!win.__winzappVoipLoadedOnce) {
      if (!win.__winzappVoipInitFlight) {
        const flight = Promise.resolve().then(async () => {
          try {
            await init.call(initModule, reason);
            const state = refuseFatal(initModule);
            if (state.ready !== true && state.failed === true && state.canRetry !== false
                && !win.__winzappVoipRetryUsed
                && typeof initModule.retryWAWebVoipInitAfterFailure === 'function') {
              // One retry per page, shared between session warmup and actions.
              win.__winzappVoipRetryUsed = true;
              await initModule.retryWAWebVoipInitAfterFailure();
              refuseFatal(initModule);
            }
          } catch (error: any) {
            if (/more than once/i.test(String(error?.message || error))) {
              win.__winzappVoipLoadedOnce = true;
            } else {
              throw error;
            }
          }
        }).finally(() => {
          if (win.__winzappVoipInitFlight === flight) delete win.__winzappVoipInitFlight;
        });
        win.__winzappVoipInitFlight = flight;
      }
      await win.__winzappVoipInitFlight;
    }
    refuseFatal(initModule);
  };
}
