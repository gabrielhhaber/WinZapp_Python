import { Socket } from 'socket.io';
import { ChildProcessWithoutNullStreams, execFileSync, spawn } from 'child_process';

import { clientsArray } from './sessionUtil';

const MAX_AUDIO_FRAME_BYTES = 64 * 1024;
// Call audio is live media: after a local/page stall, stale microphone frames
// are worse than a tiny discontinuity because they permanently put our voice
// behind the conversation.
const MAX_MIC_QUEUE_FRAMES = 12;
const MIC_TARGET_BACKLOG_FRAMES = 3;
const micQueues = new Map<string, Buffer[]>();
const micDraining = new Set<string>();
const micReceived = new Map<string, number>();
const linuxAudioProcesses = new Map<
  string,
  { playback: ChildProcessWithoutNullStreams; capture: ChildProcessWithoutNullStreams }
>();
const LINUX_PULSE_SERVER = 'unix:/run/pulse/winzapp-native';

/*
 * Everything below down to ensureLinuxCallAudio() is for ONE deployment, and
 * it is not the one it looks like: WinZapp itself never runs on Linux.
 *
 * With local API mode off, the user points WinZapp at a WPPConnect Server
 * running on their own Linux host, so the Chrome holding the WhatsApp session
 * is on that host and not on the machine the person is sitting at. The page
 * bridge further down cannot help there: it hands PCM to and from the *page*,
 * but on a headless Linux server there is no audio device for Chrome to talk
 * to at all. So each session gets its own pair of virtual PulseAudio devices
 * (a null sink plus a remapped source, named winzapp_<kind>_<session> so
 * nothing here can touch another application's), Chrome is bound to them
 * through PULSE_SOURCE/PULSE_SINK, and `pacat`/`parec` move the bytes between
 * those devices and the Socket.IO stream the Windows client is on.
 *
 * Which is why every function in this block starts by refusing to run unless
 * process.platform is 'linux' AND PULSE_SERVER is the socket above: on the
 * normal Windows install the page bridge is the whole mechanism and none of
 * this exists. Reading it as dead Linux code and deleting it would silently
 * remove calls from remote-API installs, where they would go on *appearing*
 * to work — signaling, ringing and the call window all come from the page.
 *
 * pulse_audio_lifecycle.py at the repository root is the other half: the
 * modules loaded here outlive a crashed Node, so the start/stop scripts sweep
 * the winzapp_-prefixed ones before a new session creates its own.
 */

function linuxDeviceName(kind: 'mic' | 'speaker', session: string): string {
  return `winzapp_${kind}_${session.replace(/[^a-zA-Z0-9_]/g, '_').slice(0, 48)}`;
}

function pulseEnv(): NodeJS.ProcessEnv {
  return { ...process.env, PULSE_SERVER: LINUX_PULSE_SERVER };
}

export function prepareLinuxCallAudioEnvironment(session: string, logger: any): boolean {
  if (process.platform !== 'linux') return false;
  const mic = linuxDeviceName('mic', session);
  const micInput = `${mic}_input`;
  const speaker = linuxDeviceName('speaker', session);
  try {
    const sinks = execFileSync('pactl', ['list', 'short', 'sinks'], {
      env: pulseEnv(),
      encoding: 'utf8',
    });
    for (const sink of [mic, speaker]) {
      if (!sinks.split(/\r?\n/).some((line) => line.split(/\s+/)[1] === sink)) {
        execFileSync(
          'pactl',
          [
            'load-module',
            'module-null-sink',
            `sink_name=${sink}`,
            'format=s16le',
            'rate=48000',
            'channels=1',
          ],
          { env: pulseEnv(), stdio: 'ignore' }
        );
      }
    }
    const sources = execFileSync('pactl', ['list', 'short', 'sources'], {
      env: pulseEnv(),
      encoding: 'utf8',
    });
    if (!sources.split(/\r?\n/).some((line) => line.split(/\s+/)[1] === micInput)) {
      execFileSync(
        'pactl',
        [
          'load-module',
          'module-remap-source',
          `source_name=${micInput}`,
          `master=${mic}.monitor`,
          'channels=1',
          'channel_map=mono',
          'master_channel_map=mono',
          'remix=no',
        ],
        { env: pulseEnv(), stdio: 'ignore' }
      );
    }
    execFileSync('pactl', ['set-default-source', micInput], {
      env: pulseEnv(),
      stdio: 'ignore',
    });
    // Chromium inherits these variables from this account's dedicated Node
    // process. Its microphone is a real remapped Pulse source backed by the
    // monitor of the sink Python writes to. Chromium deliberately omits raw
    // monitor sources from enumerateDevices(), so the remap is required.
    // its speaker is another sink whose monitor Node streams back to Python.
    process.env.PULSE_SERVER = LINUX_PULSE_SERVER;
    process.env.PULSE_SOURCE = micInput;
    process.env.PULSE_SINK = speaker;
    logger?.info?.(`[${session}] Linux call audio devices ready mic=${micInput} speaker=${speaker}`);
    return true;
  } catch (error: any) {
    logger?.warn?.(`[${session}] Linux call audio unavailable: ${error?.message || error}`);
    return false;
  }
}

function stopLinuxCallAudio(session: string): void {
  const processes = linuxAudioProcesses.get(session);
  if (!processes) return;
  linuxAudioProcesses.delete(session);
  for (const child of [processes.playback, processes.capture]) {
    try { child.kill('SIGTERM'); } catch (_) {}
  }
}

function ensureLinuxCallAudio(
  session: string,
  socket: Socket,
  logger: any
): { playback: ChildProcessWithoutNullStreams; capture: ChildProcessWithoutNullStreams } | null {
  if (process.platform !== 'linux' || process.env.PULSE_SERVER !== LINUX_PULSE_SERVER) return null;
  const existing = linuxAudioProcesses.get(session);
  if (existing && !existing.playback.killed && !existing.capture.killed) return existing;

  stopLinuxCallAudio(session);
  const common = ['--raw', '--format=s16le', '--rate=48000', '--channels=1', '--latency-msec=20'];
  const playback = spawn(
    'pacat',
    ['--playback', ...common, `--device=${linuxDeviceName('mic', session)}`],
    { env: pulseEnv() }
  );
  const capture = spawn(
    'parec',
    ['--record', ...common, `--device=${linuxDeviceName('speaker', session)}.monitor`],
    { env: pulseEnv() }
  );
  const processes = { playback, capture };
  linuxAudioProcesses.set(session, processes);
  const discardProcessError = (name: string) => (error: Error) => {
    logger?.debug?.(`[${session}] ${name} stream closed: ${error.message}`);
  };
  playback.on('error', discardProcessError('pacat'));
  playback.stdin.on('error', discardProcessError('pacat stdin'));
  capture.on('error', discardProcessError('parec'));
  capture.stdout.on('error', discardProcessError('parec stdout'));
  const removeExitedRelay = () => {
    if (linuxAudioProcesses.get(session) === processes) {
      stopLinuxCallAudio(session);
    }
  };
  playback.once('exit', removeExitedRelay);
  capture.once('exit', removeExitedRelay);
  let remoteFrames = 0;
  capture.stdout.on('data', (chunk: Buffer) => {
    for (let offset = 0; offset < chunk.length; offset += MAX_AUDIO_FRAME_BYTES) {
      const pcm = chunk.subarray(offset, offset + MAX_AUDIO_FRAME_BYTES);
      if (!pcm.length) continue;
      remoteFrames += 1;
      socket.emit('call:audio:remote', {
        session,
        sampleRate: 48000,
        encoding: 'base64',
        pcm: pcm.toString('base64'),
      });
    }
    if (remoteFrames === 1 || remoteFrames % 250 === 0) {
      logger?.info?.(`[${session}] Linux call speaker captured frames=${remoteFrames}`);
    }
  });
  playback.stderr.on('data', (data: Buffer) => logger?.debug?.(`[${session}] pacat: ${data}`));
  capture.stderr.on('data', (data: Buffer) => logger?.debug?.(`[${session}] parec: ${data}`));
  logger?.info?.(`[${session}] Linux call audio relay started`);
  return processes;
}

function installCallMediaBridgeInPage(linuxAudio = false): boolean {
  const win = window as any;
  if (win.__winzappCallMediaBridge?.version === 9) return true;
  if (!navigator.mediaDevices?.getUserMedia || !win.RTCPeerConnection) return false;

  const AudioContextCtor = win.AudioContext || win.webkitAudioContext;
  if (!AudioContextCtor) return false;

  const PAGE_MIC_QUEUE_FRAMES = 4;
  const PAGE_MIC_TARGET_BACKLOG_FRAMES = 2;
  // The remote tap batches AudioWorklet quanta (128 samples) up to roughly the
  // 1024-sample frame the ScriptProcessor used to deliver, so nothing
  // downstream -- Socket.IO framing, the Python jitter buffer -- sees a
  // different packet rate than it was tuned for. Batching happens INSIDE the
  // worklet: batching on the main thread would put the main thread back on the
  // audio path, which is the whole defect being fixed.
  const REMOTE_TAP_BATCH_SAMPLES = 1024;
  // Mirrors MAX_AUDIO_FRAME_BYTES on the Node side (this function is
  // serialised into the page and cannot close over module constants). Node
  // drops anything larger, so a frame over it would be silently lost.
  const REMOTE_TAP_MAX_FRAME_BYTES = 64 * 1024;
  // How often the microphone worklet reports its counters back to the main
  // thread: 48 quanta of 128 samples is ~128 ms at 48 kHz, cheap enough to be
  // invisible and fresh enough for the watchdog below and for diagnostics.
  const PAGE_MIC_REPORT_QUANTA = 48;
  // How long the microphone worklet gets to consume its first sample before
  // it is declared unscheduled and swapped for the ScriptProcessor.
  const MIC_WORKLET_WATCHDOG_MS = 1500;

  const state: any = {
    version: 9,
    enabled: false,
    context: null,
    micDestination: null,
    // The producer feeding micDestination: exactly one of these is live. Both
    // feed the SAME MediaStreamDestination, so swapping between them never
    // changes the track WhatsApp already holds and never renegotiates.
    micNode: null,
    micProcessor: null,
    micSchedulerSink: null,
    micWorkletWatchdog: 0,
    // The MICROPHONE's own verdict on the worklet, kept apart from
    // audioWorkletStatus on purpose: that one answers "did addModule()
    // register the module" and the remote tap reads it. See the watchdog.
    micWorkletUnscheduled: false,
    // Only the ScriptProcessor fallback reads these: when the worklet is live
    // the queue lives inside it, on the audio thread. See ensureMicTrack.
    micQueue: [] as Float32Array[],
    micOffset: 0,
    remotePipelines: new Map<string, any>(),
    remoteTrackIds: new Set<string>(),
    remoteVideoIds: new Set<string>(),
    remoteVideoTimers: new Map<string, number>(),
    remoteVideoFramesSent: 0,
    cameraCanvas: null,
    cameraTrack: null,
    cameraPending: false,
    // Bumped by stopCamera(). An Image decode started before the stop can
    // only land after it, and drawing it then would repaint the very frame
    // the blank was meant to erase -- so the onload handler checks that the
    // generation it captured is still current before touching the canvas.
    cameraGeneration: 0,
    // Highest capture epoch the desktop has declared STOPPED. Every desktop
    // camera capture carries its own increasing epoch, so a frame that was
    // already in flight when its capture was stopped (a send racing the
    // stop, or a capture discarded because the call ended while the camera
    // was opening) is recognised and dropped instead of repainting a live
    // picture of the user after video was turned off. Keyed on the
    // desktop's own epochs rather than on state.enabled on purpose: enable()
    // never runs on the Linux/PulseAudio path, and reset() -- which clears
    // enabled -- also runs mid-call on an audio device restart.
    cameraStoppedEpoch: -1,
    // The last real camera frame, redrawn by the steady pump below while video
    // is on, and whether video is on at all (false paints black instead).
    cameraLastPicture: null as HTMLImageElement | null,
    cameraShowing: false,
    cameraPump: 0,
    cameraPumpIdleTicks: 0,
    // Ids of every track that is OUR camera -- the canvas track, each clone
    // handed to WhatsApp, and any clone WhatsApp makes of those. The remote
    // video extraction must never pick one of these up: see attachRemoteVideo.
    cameraCloneIds: new Set<string>(),
    cameraFramesReceived: 0,
    cameraFramesDropped: 0,
    cameraTrackRequests: 0,
    localTrackIds: new Set<string>(),
    micFramesPushed: 0,
    micBytesPushed: 0,
    micSamplesConsumed: 0,
    micFramesDroppedForLatency: 0,
    remoteFramesCaptured: 0,
    remoteTracksAttached: 0,
    // 'unknown' until addModule() has been tried once, then 'ready' or
    // 'unavailable'. Read by the CDP probes as well as by the tap: a build
    // that silently fell back to the ScriptProcessor would look fixed and
    // would not be, which costs another live call with a blind user to find.
    audioWorkletStatus: 'unknown',
    remoteTapMode: '',
    // Which producer is feeding the synthetic microphone track. Same reason
    // as remoteTapMode: the peer is the only one who can hear this side chop,
    // so a silent fallback here is discovered by someone else's ears.
    micTapMode: '',
  };

  const report = (event: string, details = '') => {
    try { win.__winzappOnCallBridgeEvent?.(event, details); } catch (_) {}
  };

  // WhatsApp Web's page-native audio must stay out of WinZapp except for
  // the short call-ended chime. Real call audio is carried separately by the
  // MediaStream/WebAudio bridge below, so this policy never touches the PCM
  // that Python sends to the user's selected call speaker.
  //
  // The previous selective fix only muted loop=true, which correctly stopped
  // the incoming-call ringtone but accidentally let the ordinary incoming-
  // message notification ping through. Keep page audio muted by default and
  // open a very small exception window when a real WhatsApp call transitions
  // from a live/ringing state to terminal. That preserves the familiar
  // call-ended sound without bringing message notifications back.
  const pageAudioState = new WeakMap<
    HTMLMediaElement,
    { muted: boolean; volume: number }
  >();
  const mutedPageElements = new Set<HTMLMediaElement>();
  let callWasActive = false;
  // Key of the page call last seen live, so a call replaced by another one
  // with no idle poll in between (a near-immediate redial) still counts as
  // having ended.
  let lastPageCallKey = '';
  // Key of the call that was actually CONNECTED, or null. A merely-ringing
  // call (INCOMING_RING/CALLING/etc, counted "active" by isLivePageCall) that
  // is cancelled or rejected before anyone answers has no real terminal chime
  // to protect, and opening the 2500ms exemption for it let a coincident
  // missed-call message ping slip through unmuted (measured 2026-09-20).
  //
  // "Connected" is read from the page's own CallStore state, NOT from
  // state.enabled, which is what this used to be and was wrong three ways:
  // - offerCall() enables the bridge BEFORE the offer is sent, so an OUTGOING
  //   call counted as answered while still ringing, and an unanswered outgoing
  //   call reopened exactly the leak the flag was introduced to close;
  // - on the Linux/PulseAudio path setCallMediaBridgeActive() returns before
  //   enable(), so no call was ever "answered" and the real terminal chime
  //   was muted on every remote-API install;
  // - reset() cleared the flag, yet the poll can observe ENDED after reset()
  //   (a slow teardown), and then the genuine chime was not re-armed.
  // Keyed by call id rather than a boolean, it survives reset() without
  // leaking into the next call: a new call has a new key.
  let answeredPageCallKey: string | null = null;
  let allowCallEndChimeUntil = 0;

  const pageAudioNow = () => {
    try {
      return Number(win.performance?.now?.() ?? Date.now());
    } catch (_) {
      return Date.now();
    }
  };

  const pageCallState = (call: any): string => {
    try {
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
    } catch (_) {
      return '';
    }
  };

  const currentPageCall = (): any => {
    try {
      const module = win.require?.('WAWebCallCollection');
      const store =
        module?.activeCall !== undefined ? module : module?.get?.() || module;
      const active = store?.activeCall || store?.get?.('activeCall');
      if (active) return active;
    } catch (_) {}
    try {
      const store = win.WPP?.whatsapp?.CallStore || win.Store?.Call;
      return store?.activeCall || store?.get?.('activeCall') || null;
    } catch (_) {
      return null;
    }
  };

  const isLivePageCall = (call: any): boolean => {
    if (!call) return false;
    const callState = pageCallState(call);
    return !['', '0', 'NONE', 'ENDED', 'HANDLED_REMOTELY'].includes(callState);
  };

  // States a call only reaches once it has been answered, on either side.
  // PREACCEPT_RECEIVED is deliberately absent: it arrives while still ringing.
  const CONNECTED_PAGE_CALL_STATES = [
    'ACCEPT_SENT',
    'ACCEPT_RECEIVED',
    'ACTIVE',
    'CONNECTED_LONELY',
    'REJOINING',
  ];

  const isConnectedPageCall = (call: any): boolean =>
    !!call && CONNECTED_PAGE_CALL_STATES.includes(pageCallState(call));

  const pageCallKey = (call: any): string => {
    try {
      const id = call?.id ?? call?.get?.('id') ?? '';
      return String(id?._serialized ?? id ?? '');
    } catch (_) {
      return '';
    }
  };

  const lastCallWasAnswered = (): boolean =>
    answeredPageCallKey !== null && answeredPageCallKey === lastPageCallKey;

  const restorePageAudio = (el: HTMLMediaElement) => {
    try {
      const original = pageAudioState.get(el);
      if (!original) return;
      el.muted = original.muted;
      el.volume = original.volume;
      pageAudioState.delete(el);
      mutedPageElements.delete(el);
    } catch (_) {}
  };

  const allowCallEndChime = () => {
    allowCallEndChimeUntil = Math.max(
      allowCallEndChimeUntil,
      pageAudioNow() + 2500
    );
    // A sound object may have been created/muted just before the call state
    // flips. Restore those objects immediately so the terminal chime is not
    // clipped while waiting for the next media scan.
    for (const element of Array.from(mutedPageElements)) {
      restorePageAudio(element);
    }
  };

  const refreshCallAudioPolicy = () => {
    const call = currentPageCall();
    const active = isLivePageCall(call);
    const key = active ? pageCallKey(call) : '';
    // The previous call ended -- or was replaced by a different one without
    // an idle poll in between. Settle it BEFORE looking at the new one.
    if (callWasActive && (!active || key !== lastPageCallKey)) {
      if (lastCallWasAnswered()) allowCallEndChime();
      answeredPageCallKey = null;
    }
    if (active && isConnectedPageCall(call)) answeredPageCallKey = key;
    callWasActive = active;
    lastPageCallKey = key;
  };

  const isPageRingtone = (el: HTMLMediaElement) => {
    try {
      return !(el.srcObject instanceof MediaStream) && el.loop === true;
    } catch (_) {
      return false;
    }
  };

  const silencePageAudio = (el: HTMLMediaElement) => {
    try {
      if (!pageAudioState.has(el)) {
        pageAudioState.set(el, { muted: el.muted, volume: el.volume });
        el.addEventListener(
          'ended',
          () => mutedPageElements.delete(el),
          { once: true }
        );
      }
      mutedPageElements.add(el);
      el.muted = true;
      el.volume = 0;
    } catch (_) {}
  };

  const applyPageAudioPolicy = (el: HTMLMediaElement) => {
    refreshCallAudioPolicy();

    // The ringtone is always suppressed, even if a previous call just ended
    // and the short terminal-chime exception window is still open.
    if (isPageRingtone(el)) {
      silencePageAudio(el);
      return true;
    }

    if (pageAudioNow() <= allowCallEndChimeUntil) {
      restorePageAudio(el);
      return false;
    }

    // Everything else is muted here, including an element whose srcObject is
    // the call's own live MediaStream. WinZapp's own audio/video extraction
    // (attachRemoteTrack/attachRemoteVideo above) taps the raw
    // MediaStreamTrack directly through the Web Audio/RTCPeerConnection
    // APIs, never this element's rendered output, so muting it cannot affect
    // what Python receives — it only stops WhatsApp Web's own native
    // playback from doubling up with WinZapp's separately decoded copy. An
    // earlier rewrite carved out an exemption for srcObject streams here,
    // which silently reintroduced the exact duplicate/choppy call audio that
    // fix(calls) 810acca5 had already fixed once (measured on a real call:
    // clean native audio followed by a delayed, jittery Python-relayed
    // copy).
    silencePageAudio(el);
    return true;
  };

  state.pushCameraFrame = (jpeg: string, epoch?: number) => {
    if (typeof jpeg !== 'string' || jpeg.length > 350_000) return;
    if (typeof epoch === 'number' && epoch <= state.cameraStoppedEpoch) return;
    state.cameraFramesReceived += 1;
    if (state.cameraFramesReceived === 1 || state.cameraFramesReceived % 100 === 0) {
      report(
        'camera-frame',
        `received=${state.cameraFramesReceived} dropped=${state.cameraFramesDropped}`
      );
    }
    if (state.cameraPending) {
      state.cameraFramesDropped += 1;
      return;
    }
    state.cameraPending = true;
    const canvas = state.cameraCanvas || document.createElement('canvas');
    if (!state.cameraCanvas) {
      canvas.width = 640;
      canvas.height = 360;
      state.cameraCanvas = canvas;
      const context = canvas.getContext('2d');
      context?.fillRect(0, 0, canvas.width, canvas.height);
    }
    const generation = state.cameraGeneration;
    const picture = new Image();
    picture.onload = () => {
      try {
        // Discard a decode that finished after the camera was turned off:
        // drawing it would repaint exactly the frame stopCamera() blanked.
        if (generation === state.cameraGeneration) {
          canvas.getContext('2d')?.drawImage(picture, 0, 0, 640, 360);
          state.cameraLastPicture = picture;
          state.cameraShowing = true;
          ensureCameraPump();
        }
      } finally {
        state.cameraPending = false;
      }
    };
    picture.onerror = () => { state.cameraPending = false; };
    picture.src = `data:image/jpeg;base64,${jpeg}`;
  };

  const blankCameraCanvas = () => {
    const canvas = state.cameraCanvas;
    const context = canvas?.getContext('2d');
    if (!canvas || !context) return;
    context.fillStyle = '#000';
    context.fillRect(0, 0, canvas.width, canvas.height);
  };

  // "Turn video off" mid-call. Deliberately does NOT stop the track: the peer
  // connection holds a clone of it, so stopping it would end the video sender
  // outright and leave nothing to resume when video is turned back on.
  //
  // Blanking is what actually closes the leak. captureStream(10) emits
  // whatever the canvas currently holds, forever -- so stopping only the
  // Python-side ffmpeg capture froze the user's last frame and went on
  // transmitting that picture of them at 10 fps while WinZapp announced video
  // was off. Painting black keeps the stream valid and shows the peer nothing.
  state.stopCamera = (epoch?: number, native?: boolean) => {
    if (typeof epoch === 'number' && epoch > state.cameraStoppedEpoch) {
      state.cameraStoppedEpoch = epoch;
    }
    blankCamera();
    report('camera-stop', `epoch=${state.cameraStoppedEpoch} native=${!!native}`);
    // The user turned video off: tell WhatsApp's own call engine, exactly
    // as its camera button does. The blank above stays regardless -- it is
    // the privacy guarantee if this native call ever fails.
    if (native) setNativeVideoMute(true);
  };

  // The user turned video back on and the desktop capture is running again.
  state.resumeCamera = () => {
    report('camera-resume', 'desktop capture restarted');
    setNativeVideoMute(false);
  };

  // WhatsApp Web sends our camera through its own WASM call engine, not a
  // page-visible RTCPeerConnection sender (a live sender report showed
  // senders=0 on every connection). That engine decides what the peer sees,
  // and after the canvas went black it treated the camera as off and never
  // came back: re-enabled frames kept landing on the canvas while the peer
  // stayed on black for the rest of the call. The engine's interface exposes
  // setCallVideoMute -- the same toggle as the camera button in WhatsApp's
  // own UI -- so video off/on now goes through it.
  //
  // Its arguments are undocumented (wa-js types the interface as `any`), so
  // this dispatches on arity. The arity and the start of the function's
  // source are logged once per page -- enough to pin the signature down if a
  // WhatsApp update changes it (verified live 2026-09-21: arity 1, returns
  // 0) -- and every toggle logs only its outcome.
  let videoMuteSignatureReported = false;
  const setNativeVideoMute = (muted: boolean) => {
    const getter =
      win.WPP?.whatsapp?.functions?.getVoipStackInterface ||
      win.WPP?.whatsapp?.getVoipStackInterface;
    if (typeof getter !== 'function') {
      report('video-mute', `muted=${muted} getVoipStackInterface unavailable`);
      return;
    }
    Promise.resolve(getter())
      .then(async (stack: any) => {
        const fn = stack?.setCallVideoMute;
        if (typeof fn !== 'function') {
          report('video-mute', `muted=${muted} setCallVideoMute unavailable`);
          return;
        }
        const callId = pageCallKey(currentPageCall());
        const args = fn.length >= 2 ? [callId, muted] : [muted];
        if (!videoMuteSignatureReported) {
          videoMuteSignatureReported = true;
          const source = String(fn).replace(/\s+/g, ' ').slice(0, 160);
          report(
            'video-mute',
            `signature arity=${fn.length} args=${fn.length >= 2 ? 'callId,muted' : 'muted'} src=${source}`
          );
        }
        const result = await fn.apply(stack, args);
        let shown = '';
        try { shown = JSON.stringify(result); } catch (_) { shown = String(result); }
        report('video-mute', `muted=${muted} ok result=${String(shown).slice(0, 120)}`);
      })
      .catch((error: any) =>
        report('video-mute', `muted=${muted} error=${String(error?.message || error)}`)
      );
  };

  // Blank without gating any epoch. This is what reset() uses, and it must
  // NOT destroy the canvas or the track: reset() does not only mean "the call
  // ended" -- an audio device change from the call window restarts the audio
  // session, which emits call:audio:stop and lands here mid-call. WhatsApp
  // keeps its clone of the ORIGINAL track and never asks for a new one, so
  // replacing the canvas left the peer watching a black, orphaned canvas for
  // the rest of the call while WinZapp said video was on. Blanking the same
  // canvas instead lets the next frame from the still-running capture
  // repaint it, and still means the next call starts black rather than on a
  // still of the previous one -- including a call answered without video.
  const blankCamera = () => {
    state.cameraShowing = false;
    state.cameraLastPicture = null;
    if (!state.cameraCanvas) return;
    state.cameraGeneration += 1;
    state.cameraPending = false;
    blankCameraCanvas();
  };

  // A canvas captureStream() only emits a frame when the canvas is PAINTED.
  // Left to the desktop's frames alone, the track went idle whenever they
  // stopped or stuttered: for the seconds between the offer and the first
  // camera frame, across socket jitter, and for good after "turn video off"
  // painted black once. WebRTC then reports the track as muted, and WhatsApp
  // Web reacts on its own -- measured live 2026-09-21: 1 s after the blank it
  // re-requested media with video=false, and after video was turned back on
  // it asked for video=false AGAIN, never video=true. The peer saw the image
  // flicker at the start of the call, and black for good after re-enabling.
  //
  // So the canvas is repainted at a steady 10 fps for as long as a page call
  // is live: the last real frame while video is on, black while it is off.
  // The track never goes idle, WhatsApp never pauses it, and turning video on
  // again only changes what is painted. The pump stops itself once the page
  // has had no live call for 5 s, so it costs nothing between calls.
  const CAMERA_PUMP_MS = 100;
  const CAMERA_PUMP_IDLE_STOP_TICKS = 50;

  const stopCameraPump = () => {
    if (state.cameraPump) win.clearInterval(state.cameraPump);
    state.cameraPump = 0;
    state.cameraPumpIdleTicks = 0;
  };

  const pumpCamera = () => {
    const canvas = state.cameraCanvas;
    const context = canvas?.getContext('2d');
    if (!canvas || !context) return;
    if (!isLivePageCall(currentPageCall())) {
      state.cameraPumpIdleTicks += 1;
      if (state.cameraPumpIdleTicks >= CAMERA_PUMP_IDLE_STOP_TICKS) stopCameraPump();
      return;
    }
    state.cameraPumpIdleTicks = 0;
    if (state.cameraShowing && state.cameraLastPicture) {
      context.drawImage(state.cameraLastPicture, 0, 0, 640, 360);
    } else {
      context.fillStyle = '#000';
      context.fillRect(0, 0, canvas.width, canvas.height);
    }
  };

  const ensureCameraPump = () => {
    state.cameraPumpIdleTicks = 0;
    if (state.cameraPump) return;
    state.cameraPump = win.setInterval(pumpCamera, CAMERA_PUMP_MS);
  };

  const isOurCameraTrack = (track: any): boolean =>
    !!track && (track === state.cameraTrack || state.cameraCloneIds.has(track.id));

  // WhatsApp may clone the track we hand it; a clone of our camera is still our
  // camera, so it inherits the mark.
  try {
    const proto = win.MediaStreamTrack?.prototype;
    const nativeClone = proto?.clone;
    if (typeof nativeClone === 'function' && !nativeClone.__winzappCameraMark) {
      const clone = function (this: any) {
        const copy = nativeClone.call(this);
        try {
          if (copy?.id && isOurCameraTrack(this)) state.cameraCloneIds.add(copy.id);
        } catch (_) {}
        return copy;
      };
      (clone as any).__winzappCameraMark = true;
      proto.clone = clone;
    }
  } catch (_) {}

  const cameraTrack = () => {
    state.cameraTrackRequests += 1;
    if (state.cameraTrackRequests === 1) {
      report('camera-track', 'WhatsApp requested the synthetic video track');
    }
    if (!state.cameraCanvas) {
      const canvas = document.createElement('canvas');
      canvas.width = 640;
      canvas.height = 360;
      canvas.getContext('2d')?.fillRect(0, 0, 640, 360);
      state.cameraCanvas = canvas;
    }
    if (!state.cameraTrack || state.cameraTrack.readyState !== 'live') {
      state.cameraTrack = state.cameraCanvas.captureStream(10).getVideoTracks()[0];
    }
    ensureCameraPump();
    const clone = state.cameraTrack.clone();
    if (clone?.id) state.cameraCloneIds.add(clone.id);
    return clone;
  };

  const attachRemoteVideo = (track: MediaStreamTrack) => {
    if (!track || track.kind !== 'video' || state.remoteVideoIds.has(track.id)) return;
    // Never our own camera. WhatsApp plays the track we hand it in hidden
    // <video> elements to feed its encoder, and the media scan below picks up
    // every <video> with a stream -- so without this, the "remote" picture
    // WinZapp showed in the call window was the user's OWN camera (confirmed
    // live on 2026-09-21 with a sighted-assistance description of the call
    // window). Two WinZapp users calling each other each saw themselves, or
    // black with the camera off, while the video they sent arrived intact.
    // The microphone has had the same guard all along (localTrackIds in
    // attachRemoteTrack); video never did.
    if (isOurCameraTrack(track)) return;
    state.remoteVideoIds.add(track.id);
    // One-shot: proves attachPeerConnection/the track event/the receiver scan
    // actually delivered a remote video track at all. If this never appears
    // in a real call's log, the bug is upstream of everything below.
    report('remote-video-track', `track=${track.id} kind=${track.kind}`);
    const video = document.createElement('video');
    video.muted = true;
    video.autoplay = true;
    video.playsInline = true;
    video.srcObject = new MediaStream([track]);
    void video.play().catch(() => undefined);
    const canvas = document.createElement('canvas');
    canvas.width = 640;
    canvas.height = 360;
    let stalledTicks = 0;
    const timer = win.setInterval(() => {
      if (track.readyState !== 'live' || !video.videoWidth) {
        stalledTicks += 1;
        // Every ~5s (40 ticks * 125ms) while blocked, not every tick: tells a
        // log reviewer "track attached but never got real pixels" apart from
        // the other stages below.
        if (stalledTicks % 40 === 0) {
          report(
            'remote-video-stalled',
            `readyState=${track.readyState} videoWidth=${video.videoWidth}`
          );
        }
        return;
      }
      canvas.getContext('2d')?.drawImage(video, 0, 0, 640, 360);
      const jpeg = canvas.toDataURL('image/jpeg', 0.6).split(',')[1];
      if (jpeg) {
        state.remoteVideoFramesSent += 1;
        if (
          state.remoteVideoFramesSent === 1 ||
          state.remoteVideoFramesSent % 100 === 0
        ) {
          report(
            'remote-video-frame',
            `sent=${state.remoteVideoFramesSent} track=${track.id}`
          );
        }
        win.__winzappOnCallRemoteVideo?.(jpeg).catch?.(() => undefined);
      }
    }, 125);
    state.remoteVideoTimers.set(track.id, timer);
    track.addEventListener('ended', () => {
      win.clearInterval(timer);
      state.remoteVideoTimers.delete(track.id);
      state.remoteVideoIds.delete(track.id);
      video.srcObject = null;
    }, { once: true });
  };

  const ensureContext = () => {
    if (!state.context || state.context.state === 'closed') {
      state.context = new AudioContextCtor({
        latencyHint: 'interactive',
        sampleRate: 48000,
      });
      // A worklet module is registered per AudioContext, so a brand-new one
      // has none: the cached answer belongs to the context that is gone, and
      // leaving it 'ready' here would only make the diagnostics lie about
      // which tap ran.
      state.audioWorkletStatus = 'unknown';
    }
    if (state.context.state === 'suspended') {
      state.context.resume().catch(() => undefined);
    }
    return state.context;
  };

  const decodePcm16 = (base64: string): Float32Array => {
    const binary = atob(base64 || '');
    const frames = Math.floor(binary.length / 2);
    const samples = new Float32Array(frames);
    for (let i = 0; i < frames; i += 1) {
      const lo = binary.charCodeAt(i * 2);
      const hi = binary.charCodeAt(i * 2 + 1);
      let value = (hi << 8) | lo;
      if (value & 0x8000) value -= 0x10000;
      samples[i] = Math.max(-1, Math.min(1, value / 32768));
    }
    return samples;
  };

  const encodePcm16 = (samples: Float32Array): string => {
    const bytes = new Uint8Array(samples.length * 2);
    for (let i = 0; i < samples.length; i += 1) {
      const clipped = Math.max(-1, Math.min(1, samples[i] || 0));
      const value = clipped < 0 ? Math.round(clipped * 32768) : Math.round(clipped * 32767);
      bytes[i * 2] = value & 0xff;
      bytes[i * 2 + 1] = (value >> 8) & 0xff;
    }
    let binary = '';
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {
      binary += String.fromCharCode(...bytes.subarray(i, i + step));
    }
    return btoa(binary);
  };

  // The worklet already did the float -> PCM16 conversion on the audio thread;
  // the bytes are written out explicitly little-endian rather than reusing the
  // Int16Array's own buffer, so the wire format does not depend on the host's
  // endianness (exactly as encodePcm16 above does).
  const encodeInt16 = (samples: Int16Array): string => {
    const bytes = new Uint8Array(samples.length * 2);
    for (let i = 0; i < samples.length; i += 1) {
      const value = samples[i];
      bytes[i * 2] = value & 0xff;
      bytes[i * 2 + 1] = (value >> 8) & 0xff;
    }
    let binary = '';
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {
      binary += String.fromCharCode(...bytes.subarray(i, i + step));
    }
    return btoa(binary);
  };

  // The remote tap used to be a createScriptProcessor(1024, 1, 1), and that is
  // where the popping came from. Measured over CDP on a live, popping call
  // (2026-09-23): the page's audio clock was exact (10.0000 s of ctx.
  // currentTime over 10.0014 s of wall clock), but the ScriptProcessor fired
  // 464 callbacks where 468.8 were due -- it dropped ~1% of its buffers
  // outright, ~0.5 per second, 21.3 ms of audio gone each time, with
  // longTaskCount 0. Net delivery to Python was 47,507 samples/s against the
  // 48,000 it declares, and a source running under real time drains any fixed
  // reservoir forever, so the Python jitter buffer starved ~2.8 times a second
  // for 35 minutes. A worklet runs on the audio render thread and cannot be
  // starved by main-thread scheduling, so delivery becomes exactly real time.
  //
  // It is deliberately a no-op on its outputs: the node is still connected
  // through the muted gain to the destination, the same way the
  // ScriptProcessor was, because that is what keeps the graph pulling.
  //
  // The MICROPHONE tap lives in the same module, registered by the same single
  // addModule() call, because the one thing that can refuse it -- the page's
  // CSP refusing a blob: script -- refuses both or neither, and one verdict is
  // easier to read in diagnostics than two that can never disagree. The two
  // processors are otherwise unrelated: this one consumes an input, the mic
  // one has no input at all and produces.
  const CALL_TAP_WORKLET_SOURCE = `
class WinzappCallTapProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const size = (options && options.processorOptions && options.processorOptions.batchSamples) || 1024;
    // Two batches used alternately, and postMessage is called WITHOUT a
    // transfer list, so the batch is structured-cloned rather than neutered
    // and these two buffers are the only ones process() ever writes into --
    // no per-batch slice() to be collected ~47 times a second. The clone
    // itself is still a copy taken on this thread; what is avoided is the JS
    // allocation and the GC churn behind it, not the copy. The alternation is
    // belt and braces: a structured clone is taken synchronously at post
    // time, so reusing one buffer would also be safe.
    this.batches = [new Int16Array(size), new Int16Array(size)];
    this.active = 0;
    this.filled = 0;
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    // No input yet (the track has not started, or it ended) is not a reason to
    // let the node be collected: return true so it keeps running.
    if (!channel || !channel.length) return true;
    let batch = this.batches[this.active];
    for (let i = 0; i < channel.length; i += 1) {
      let sample = channel[i] || 0;
      if (sample > 1) sample = 1;
      else if (sample < -1) sample = -1;
      batch[this.filled] = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
      this.filled += 1;
      if (this.filled === batch.length) {
        this.filled = 0;
        this.port.postMessage(batch);
        this.active = this.active === 0 ? 1 : 0;
        batch = this.batches[this.active];
      }
    }
    return true;
  }
}
registerProcessor('winzapp-call-tap', WinzappCallTapProcessor);

// The microphone tap. It is NOT a mirror of the one above: it has zero inputs
// and its whole job is to produce, so what has to leave the main thread is the
// PULL, not a delivery. The frame QUEUE therefore lives here, on the audio
// render thread, fed by postMessage from pushMicrophone(); process() drains it
// without asking the main thread for anything. A queue left on the main thread
// would have put main-thread scheduling straight back on the audio path, which
// is the defect being fixed. The main thread still decodes base64 -> Float32
// and posts, but a stall there now costs only the frames it failed to hand
// over, not the frames already in hand.
//
// It has no view of state.enabled and does not need one: pushMicrophone()
// refuses to hand over a frame while the bridge is disabled, and reset()
// posts {type:'clear'}, so a disabled bridge simply starves this queue and
// process() writes the silence it writes whenever the queue is empty.
class WinzappCallMicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.queue = [];
    this.offset = 0;
    this.maxFrames = opts.maxFrames || 4;
    this.targetFrames = opts.targetFrames || 2;
    this.reportQuanta = opts.reportQuanta || 48;
    this.consumed = 0;
    this.dropped = 0;
    this.quanta = 0;
    this.port.onmessage = (event) => {
      const data = event.data;
      if (!data) return;
      if (data.type === 'clear') {
        this.queue.length = 0;
        this.offset = 0;
        return;
      }
      if (!data.length) return;
      this.queue.push(data);
      // The same latency policy pushMicrophone() used to apply on the main
      // thread, moved to where the queue now is: keep a partially consumed
      // head frame, then drop the oldest complete frames until only ~40 ms of
      // microphone audio is queued.
      while (this.queue.length > this.maxFrames) {
        const dropIndex = this.offset > 0 ? 1 : 0;
        if (dropIndex >= this.queue.length) break;
        this.queue.splice(dropIndex, 1);
        this.dropped += 1;
      }
      while (this.queue.length > this.targetFrames + (this.offset > 0 ? 1 : 0)) {
        const dropIndex = this.offset > 0 ? 1 : 0;
        if (dropIndex >= this.queue.length) break;
        this.queue.splice(dropIndex, 1);
        this.dropped += 1;
      }
    };
  }
  process(inputs, outputs) {
    const output = outputs[0] && outputs[0][0];
    if (!output) return true;
    output.fill(0);
    let written = 0;
    while (written < output.length && this.queue.length) {
      const head = this.queue[0];
      const available = head.length - this.offset;
      const take = Math.min(output.length - written, available);
      output.set(head.subarray(this.offset, this.offset + take), written);
      written += take;
      this.offset += take;
      if (this.offset >= head.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    this.consumed += written;
    this.quanta += 1;
    if (this.quanta >= this.reportQuanta) {
      this.quanta = 0;
      // DELTAS, never totals: the main thread adds them to the very counters
      // the ScriptProcessor fallback increments, so a mid-call swap from one
      // producer to the other cannot make micSamplesConsumed jump or go back.
      this.port.postMessage({ type: 'counters', consumed: this.consumed, dropped: this.dropped });
      this.consumed = 0;
      this.dropped = 0;
    }
    // A node with no inputs has no active source, so returning false would let
    // it be collected -- and the microphone would go silent for the rest of
    // the call with nothing to show for it.
    return true;
  }
}
registerProcessor('winzapp-call-mic', WinzappCallMicProcessor);
`;

  // Resolves true once 'winzapp-call-tap' and 'winzapp-call-mic' are
  // registered on this context.
  // addModule() needs a URL and a Blob URL is the usual way to ship an inline
  // worklet, but WhatsApp Web's CSP is entitled to refuse blob: scripts -- and
  // a refusal must fall back to the old ScriptProcessor rather than leave the
  // call with no remote audio at all. The promise is cached on the context
  // because a worklet module is registered per AudioContext and ensureContext
  // builds a new one whenever the old was closed.
  const ensureCallTapWorklet = (context: any): Promise<boolean> => {
    if (context.__winzappCallTapWorklet) return context.__winzappCallTapWorklet;
    let pending: Promise<boolean>;
    // Declared out here so the catch below can revoke it: a context with no
    // audioWorklet at all throws between creating the URL and reaching the
    // .finally that would have released it.
    let url = '';
    try {
      url = URL.createObjectURL(
        new Blob([CALL_TAP_WORKLET_SOURCE], { type: 'application/javascript' })
      );
      pending = context.audioWorklet
        .addModule(url)
        .then(() => {
          state.audioWorkletStatus = 'ready';
          report('call-tap-worklet', 'audio worklet module registered');
          return true;
        })
        .catch((error: any) => {
          state.audioWorkletStatus = 'unavailable';
          report(
            'call-tap-worklet',
            `addModule refused: ${String(error?.message || error)}`
          );
          return false;
        })
        .finally(() => {
          try { URL.revokeObjectURL(url); } catch (_) {}
        });
    } catch (error: any) {
      if (url) {
        try { URL.revokeObjectURL(url); } catch (_) {}
      }
      state.audioWorkletStatus = 'unavailable';
      report(
        'call-tap-worklet',
        `unavailable: ${String(error?.message || error)}`
      );
      pending = Promise.resolve(false);
    }
    context.__winzappCallTapWorklet = pending;
    return pending;
  };

  // The microphone producer, and the swap between its two shapes.
  //
  // Exactly one of state.micNode (AudioWorklet) and state.micProcessor
  // (ScriptProcessor) is ever connected, and both write into the SAME
  // MediaStreamDestination. That matters: WhatsApp cloned the destination's
  // track at getUserMedia time and never asks for another one, so swapping
  // producers underneath it costs no renegotiation and the peer hears
  // nothing beyond the audio still queued in the producer being dropped.
  const disconnectMicProducer = () => {
    try { state.micNode?.port?.postMessage({ type: 'clear' }); } catch (_) {}
    try { state.micNode?.port?.close?.(); } catch (_) {}
    try { state.micNode?.disconnect(); } catch (_) {}
    state.micNode = null;
    if (state.micProcessor) state.micProcessor.onaudioprocess = null;
    try { state.micProcessor?.disconnect(); } catch (_) {}
    state.micProcessor = null;
  };

  const startMicScriptProcessorTap = (context: any, destination: any, reason: string) => {
    const processor = context.createScriptProcessor(1024, 0, 1);
    processor.onaudioprocess = (event: AudioProcessingEvent) => {
      const output = event.outputBuffer.getChannelData(0);
      output.fill(0);
      if (!state.enabled) return;

      let written = 0;
      while (written < output.length && state.micQueue.length) {
        const head: Float32Array = state.micQueue[0];
        const available = head.length - state.micOffset;
        const take = Math.min(output.length - written, available);
        output.set(head.subarray(state.micOffset, state.micOffset + take), written);
        written += take;
        state.micOffset += take;
        if (state.micOffset >= head.length) {
          state.micQueue.shift();
          state.micOffset = 0;
        }
      }
      state.micSamplesConsumed += written;
    };
    processor.connect(destination);
    // A zero-input ScriptProcessor is not scheduled reliably by Chromium
    // unless it also has an audible graph sink. Keep that sink muted; the
    // MediaStreamDestination remains the only call track source.
    processor.connect(state.micSchedulerSink);
    state.micProcessor = processor;
    state.micTapMode = 'script-processor';
    report('mic-tap', `tap=script-processor reason=${reason}`);
  };

  const startMicWorkletTap = (context: any, destination: any) => {
    const node = new win.AudioWorkletNode(context, 'winzapp-call-mic', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: {
        maxFrames: PAGE_MIC_QUEUE_FRAMES,
        targetFrames: PAGE_MIC_TARGET_BACKLOG_FRAMES,
        reportQuanta: PAGE_MIC_REPORT_QUANTA,
      },
    });
    // Recorded before either connect(), so that a throw from connect() leaves
    // a node disconnectMicProducer() can still find -- the remote tap learned
    // this one in review on #281.
    state.micNode = node;
    node.port.onmessage = (event: MessageEvent) => {
      const data: any = event.data;
      if (!data || data.type !== 'counters') return;
      // Deltas, added to the same counters the ScriptProcessor increments, so
      // a mid-call swap cannot make micSamplesConsumed jump or go backwards.
      state.micSamplesConsumed += data.consumed || 0;
      state.micFramesDroppedForLatency += data.dropped || 0;
    };
    node.connect(destination);
    node.connect(state.micSchedulerSink);
    state.micTapMode = 'audio-worklet';
    report('mic-tap', 'tap=audio-worklet');
    armMicWorkletWatchdog();
  };

  // The terminal fallback: hand the destination back to the ScriptProcessor
  // and make sure SOMETHING is producing when this returns.
  //
  // The restart is wrapped because both callers are terminal -- there is no
  // further fallback behind them -- and it can genuinely throw: a context
  // closed between arming and firing, or a micSchedulerSink belonging to a
  // context ensureContext() has since replaced. Unwrapped, the throw escapes
  // into a setTimeout callback or an unhandled rejection, leaves micNode and
  // micProcessor both null, and ensureMicTrack() will NOT rebuild, because
  // the destination's track is still 'live' and it early-returns on that.
  // The microphone would then be silent for the rest of the session -- the
  // exact failure the watchdog exists to prevent. Dropping the destination
  // makes the next ensureMicTrack() build a fresh one instead.
  const swapMicProducerToScriptProcessor = (
    context: any, destination: any, reason: string
  ) => {
    disconnectMicProducer();
    try {
      startMicScriptProcessorTap(context, destination, reason);
    } catch (error: any) {
      state.micDestination = null;
      state.micTapMode = '';
      report('mic-tap', `no producer: ${String(error?.message || error)}`);
    }
  };

  // A silent microphone is discovered by the PEER, never by the user -- he
  // cannot hear his own side, and the only symptom is other people saying
  // "you cut out". So the worklet gets a deadline instead of trust: if frames
  // were handed over and none were consumed by the time it expires, the
  // ScriptProcessor takes the same destination back. Re-armed, not cancelled,
  // while nothing has been pushed yet: before the call is answered there is
  // no microphone audio to consume and silence proves nothing.
  const armMicWorkletWatchdog = () => {
    if (state.micWorkletWatchdog) win.clearTimeout(state.micWorkletWatchdog);
    const pushedAtArm = state.micFramesPushed;
    const consumedAtArm = state.micSamplesConsumed;
    state.micWorkletWatchdog = win.setTimeout(() => {
      state.micWorkletWatchdog = 0;
      if (!state.micNode) return;
      if (state.micFramesPushed === pushedAtArm) {
        armMicWorkletWatchdog();
        return;
      }
      if (state.micSamplesConsumed > consumedAtArm) return;
      const context = state.context;
      const destination = state.micDestination;
      if (!context || !destination) return;
      // NOT audioWorkletStatus: that one means "addModule() registered the
      // module", and the REMOTE tap branches on it. A microphone starved for
      // 1.5 s by a suspended context or a late render thread would otherwise
      // condemn every remote track for the life of the page to the
      // ScriptProcessor -- reintroducing, in the ear of the blind user, the
      // 1% callback loss #281 exists to fix -- as a side effect of a
      // microphone heuristic, with /call/diagnostics then reporting
      // audioWorkletStatus=unavailable next to remoteTapMode=audio-worklet.
      state.micWorkletUnscheduled = true;
      swapMicProducerToScriptProcessor(
        context, destination, 'worklet consumed nothing while frames were arriving'
      );
    }, MIC_WORKLET_WATCHDOG_MS);
  };

  const ensureMicTrack = () => {
    const context = ensureContext();
    if (state.micDestination?.stream?.getAudioTracks?.()[0]?.readyState === 'live') {
      return state.micDestination.stream.getAudioTracks()[0];
    }

    const destination = context.createMediaStreamDestination();
    // The previous sink is dropped here, not left wired to context.destination:
    // it is silent, so an abandoned one is inaudible rather than harmless, and
    // the graph would grow by one node per rebuild for the life of the page.
    try { state.micSchedulerSink?.disconnect(); } catch (_) {}
    const schedulerSink = context.createGain();
    schedulerSink.gain.value = 0;
    schedulerSink.connect(context.destination);
    state.micSchedulerSink = schedulerSink;
    state.micDestination = destination;
    disconnectMicProducer();

    // bridgedGetUserMedia needs a track back in this same turn, so the
    // producer is built now from what is already known: the worklet when the
    // module is registered (enable() warms it before any call arrives), the
    // ScriptProcessor otherwise, upgraded in place when the module resolves.
    if (state.audioWorkletStatus === 'ready' && !state.micWorkletUnscheduled) {
      try {
        startMicWorkletTap(context, destination);
      } catch (error: any) {
        // A node that will not construct is a microphone-side verdict, like
        // the watchdog's: the module itself registered fine and the remote
        // tap must keep using it.
        state.micWorkletUnscheduled = true;
        swapMicProducerToScriptProcessor(
          context, destination, `worklet node failed: ${String(error?.message || error)}`
        );
      }
    } else {
      startMicScriptProcessorTap(
        context,
        destination,
        state.micWorkletUnscheduled
          ? 'worklet swapped out for this microphone'
          : state.audioWorkletStatus === 'unavailable'
            ? 'worklet unavailable'
            : 'worklet still registering'
      );
      if (state.audioWorkletStatus !== 'unavailable' && !state.micWorkletUnscheduled) {
        ensureCallTapWorklet(context).then((ready: boolean) => {
          // Not "is there still a destination": it has to be THIS one. A
          // second call built its own while the module was registering, and
          // upgrading that one from here would swap a producer out from under
          // a track nobody here owns any more.
          if (!ready || state.micDestination !== destination || state.micNode) return;
          try {
            disconnectMicProducer();
            // The handful of frames queued on the main thread (~40 ms, capped
            // by the drop policy) go with the producer that held them.
            state.micQueue.length = 0;
            state.micOffset = 0;
            startMicWorkletTap(context, destination);
          } catch (error: any) {
            state.micWorkletUnscheduled = true;
            swapMicProducerToScriptProcessor(
              context, destination, `worklet node failed: ${String(error?.message || error)}`
            );
          }
        });
      }
    }

    const track = destination.stream.getAudioTracks()[0];
    if (track?.id) state.localTrackIds.add(track.id);
    return track;
  };

  state.enable = () => {
    state.enabled = true;
    ensureMicTrack();
    // The destination (and with it the worklet) survives between calls, so
    // the watchdog is re-armed here rather than only where the producer is
    // built: reset() stood it down at the end of the previous call, and
    // without this the second and later calls of a session would run with no
    // protection at all against a worklet that stopped being scheduled.
    if (state.micNode) armMicWorkletWatchdog();
    const context = ensureContext();
    context.resume().catch(() => undefined);
    // Registering the worklet module is asynchronous, so warm it up here
    // rather than when the remote track arrives: by then the answer is
    // cached and the tap is built in the same turn the track appears.
    ensureCallTapWorklet(context);
    return true;
  };

  state.pushMicrophone = (base64: string) => {
    if (!state.enabled) return;
    ensureContext();
    const samples = decodePcm16(base64);
    if (!samples.length) return;
    state.micFramesPushed += 1;
    state.micBytesPushed += Math.floor(base64.length * 3 / 4);

    // With the worklet live the queue and the drop policy below live INSIDE
    // it, on the audio render thread, and this side only hands frames over.
    // Posted without a transfer list on purpose: a structured clone of 20 ms
    // of mono audio is under 4 KB, while a transfer would neuter `samples`
    // and, if postMessage then threw, leave an empty frame to fall through
    // to the queue below.
    if (state.micNode) {
      try {
        state.micNode.port.postMessage(samples);
        return;
      } catch (_) {
        // A closed port is the one way this fails; keep the frame rather
        // than drop it, and let the watchdog swap the producer.
      }
    }

    state.micQueue.push(samples);

    // Keep the page-side WebAudio queue close to real time. If Chromium was
    // briefly busy, retain a partially-consumed head frame but skip old
    // complete frames until only ~40 ms of queued microphone audio remains.
    while (state.micQueue.length > PAGE_MIC_QUEUE_FRAMES) {
      const dropIndex = state.micOffset > 0 ? 1 : 0;
      if (dropIndex >= state.micQueue.length) break;
      state.micQueue.splice(dropIndex, 1);
      state.micFramesDroppedForLatency += 1;
    }
    while (state.micQueue.length > PAGE_MIC_TARGET_BACKLOG_FRAMES + (state.micOffset > 0 ? 1 : 0)) {
      const dropIndex = state.micOffset > 0 ? 1 : 0;
      if (dropIndex >= state.micQueue.length) break;
      state.micQueue.splice(dropIndex, 1);
      state.micFramesDroppedForLatency += 1;
    }
  };

  state.pushMicrophoneBatch = (frames: string[]) => {
    for (const frame of frames || []) state.pushMicrophone(frame);
  };

  // A remote pipeline carries either a worklet node or a ScriptProcessor,
  // never both, so tearing one down has to cover the two shapes.
  const disconnectRemotePipeline = (pipeline: any) => {
    try { pipeline.source.disconnect(); } catch (_) {}
    try { pipeline.processor?.disconnect(); } catch (_) {}
    try { pipeline.node?.port?.close?.(); } catch (_) {}
    try { pipeline.node?.disconnect(); } catch (_) {}
    try { pipeline.sink.disconnect(); } catch (_) {}
  };

  state.reset = () => {
    // Local reject/end stops the bridge immediately before WhatsApp performs
    // the native action. Arm the terminal-chime exception first so that sound
    // stays audible even if it starts before the CallStore poll observes
    // ENDED — but only for a call that was actually answered. A ringing-only
    // call being rejected/cancelled has no real terminal chime to protect,
    // and opening this window for it let a coincident missed-call message
    // ping slip through unmuted (measured 2026-09-20).
    //
    // The "answered" record is NOT cleared here. The poll can observe ENDED
    // after this reset() on a slow teardown, and must still be able to re-arm
    // the genuine chime then. It cannot leak into the next call either: that
    // call has a different key, and refreshCallAudioPolicy() settles the old
    // one as soon as it sees the key change.
    if (lastCallWasAnswered()) allowCallEndChime();
    state.enabled = false;
    state.micQueue.length = 0;
    state.micOffset = 0;
    // The worklet holds its own queue on the audio thread, so clearing the
    // main-thread one is no longer enough to stop stale microphone audio
    // being played out after a reset.
    try { state.micNode?.port?.postMessage({ type: 'clear' }); } catch (_) {}
    // And the watchdog is stood down rather than left looping: with the
    // bridge disabled no frames are pushed, so it would take its re-arm
    // branch every 1.5 s for the life of the page between calls. That was
    // wasteful, not broken -- the re-arm recaptures both baselines, so the
    // last one before the next call held current totals and would have
    // fired correctly. Standing it down here and re-arming in enable()
    // replaces a permanent timer with two explicit edges.
    if (state.micWorkletWatchdog) {
      win.clearTimeout(state.micWorkletWatchdog);
      state.micWorkletWatchdog = 0;
    }
    for (const pipeline of state.remotePipelines.values()) {
      disconnectRemotePipeline(pipeline);
    }
    state.remotePipelines.clear();
    state.remoteTrackIds.clear();
    for (const timer of state.remoteVideoTimers.values()) win.clearInterval(timer);
    state.remoteVideoTimers.clear();
    state.remoteVideoIds.clear();
    state.localTrackIds.clear();
    blankCamera();
  };

  const attachRemoteTrack = (track: MediaStreamTrack) => {
    if (!track || track.kind !== 'audio') return;
    const id = track.id || String(Math.random());
    // The broad Web Audio hook also observes WhatsApp consuming our synthetic
    // microphone. Never loop that local track back into the speaker pipeline.
    if (state.localTrackIds.has(id)) return;
    // Creating our own MediaStreamSource below goes through the global audio
    // graph hook too. Reserve this id before doing so, otherwise that hook
    // re-enters here and recursively builds an unbounded number of pipelines.
    if (state.remoteTrackIds.has(id)) return;
    state.remoteTrackIds.add(id);

    const context = ensureContext();
    const stream = new MediaStream([track]);
    const source = context.createMediaStreamSource(stream);
    const sink = context.createGain();
    sink.gain.value = 0;
    sink.connect(context.destination);
    const pipeline: any = { source, sink, track, processor: null, node: null };

    // Both taps hand Python the same thing: mono PCM16 plus the context's REAL
    // sample rate (never a hardcoded 48000 -- only the Linux relay may assume
    // a rate, because it created the device itself).
    const deliver = (base64: string) => {
      const callback = win.__winzappOnCallRemoteAudio;
      if (typeof callback !== 'function' || !base64) return;
      state.remoteFramesCaptured += 1;
      if (state.remoteFramesCaptured === 1 || state.remoteFramesCaptured % 250 === 0) {
        report(
          'remote-frame',
          `track=${id} frames=${state.remoteFramesCaptured} tap=${state.remoteTapMode}`
        );
      }
      callback(base64, context.sampleRate).catch?.(() => undefined);
    };

    const startWorkletTap = () => {
      const node = new win.AudioWorkletNode(context, 'winzapp-call-tap', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: 'explicit',
        processorOptions: { batchSamples: REMOTE_TAP_BATCH_SAMPLES },
      });
      // Registered on the pipeline before anything else can throw: a node
      // connected but never recorded would survive disconnectRemotePipeline()
      // and go on sending frames alongside the fallback tap.
      pipeline.node = node;
      node.port.onmessage = (event: MessageEvent) => {
        if (!state.enabled) return;
        const samples = event.data as Int16Array;
        if (!samples?.length || samples.length * 2 > REMOTE_TAP_MAX_FRAME_BYTES) return;
        deliver(encodeInt16(samples));
      };
      source.connect(node);
      node.connect(sink);
      state.remoteTapMode = 'audio-worklet';
      report('remote-tap', `track=${id} tap=audio-worklet`);
    };

    const startScriptProcessorTap = (reason: string) => {
      const processor = context.createScriptProcessor(1024, 1, 1);
      processor.onaudioprocess = (event: AudioProcessingEvent) => {
        if (!state.enabled) return;
        const input = event.inputBuffer.getChannelData(0);
        if (input.length) deliver(encodePcm16(input));
        event.outputBuffer.getChannelData(0).fill(0);
      };
      source.connect(processor);
      processor.connect(sink);
      pipeline.processor = processor;
      state.remoteTapMode = 'script-processor';
      report('remote-tap', `track=${id} tap=script-processor reason=${reason}`);
    };

    state.remotePipelines.set(id, pipeline);
    state.remoteTracksAttached += 1;
    report('remote-track', `track=${id} tracks=${state.remoteTracksAttached}`);

    // Once the module's fate is known the tap is built synchronously, which is
    // the normal case: enable() warms the module up before any track arrives.
    // Only the very first call on a page can land here with it still pending,
    // and then the pipeline is checked for still being the current one --
    // a track that ended while addModule() was in flight must not be revived.
    // The module registering is not quite a guarantee that the node can be
    // built (an older Chromium, a context that closed underneath us), and a
    // throw here would leave the track with no tap at all -- silence on the
    // call, which is the one outcome worse than the popping.
    const startTap = (ready: boolean) => {
      if (ready) {
        try {
          startWorkletTap();
          return;
        } catch (error: any) {
          // A half-built worklet tap can already be connected and delivering,
          // so take it out before adding the fallback: two taps on one track
          // send every frame to Python twice.
          try { pipeline.node?.port?.close?.(); } catch (_) {}
          try { pipeline.node?.disconnect(); } catch (_) {}
          pipeline.node = null;
          state.audioWorkletStatus = 'unavailable';
          startScriptProcessorTap(`worklet node failed: ${String(error?.message || error)}`);
          return;
        }
      }
      startScriptProcessorTap('worklet unavailable');
    };

    if (state.audioWorkletStatus === 'ready') {
      startTap(true);
    } else if (state.audioWorkletStatus === 'unavailable') {
      startTap(false);
    } else {
      ensureCallTapWorklet(context).then((ready: boolean) => {
        if (state.remotePipelines.get(id) !== pipeline) return;
        startTap(ready);
      });
    }

    track.addEventListener('ended', () => {
      const ended = state.remotePipelines.get(id);
      if (!ended) return;
      disconnectRemotePipeline(ended);
      state.remotePipelines.delete(id);
      state.remoteTrackIds.delete(id);
    }, { once: true });
  };

  const attachRemoteStream = (stream: MediaStream | null | undefined) => {
    try {
      for (const track of stream?.getAudioTracks?.() || []) attachRemoteTrack(track);
      for (const track of stream?.getVideoTracks?.() || []) attachRemoteVideo(track);
    } catch (_) {}
  };

  // The other person's video. WhatsApp Web never exposes it as a
  // MediaStreamTrack or as a <video> the page can read -- the media scan and
  // the RTCPeerConnection track event both come up empty for it. Its WASM
  // engine decodes the peer's video itself (measured live on 2026-09-21:
  // "Decoding 640x432 (H264) @ 19 fps") and draws frames only into canvases
  // registered with WAWebVoipVideoRendererRegistry, which is what its own
  // call UI does for the peer tile. So WinZapp registers a canvas of its own
  // for the peer exactly the same way, and captures it at the same ~8 fps and
  // through the same callback the call window already consumes. The registry
  // keeps a set of canvases per source, so this coexists with WhatsApp's own.
  //
  // Everything before this was wrong in the same direction: the only "remote"
  // video the bridge ever found was the user's own camera, played in hidden
  // <video> elements to feed WhatsApp's encoder (see attachRemoteVideo).
  const PEER_VIDEO_CAPTURE_MS = 125;
  const peerVideo = {
    key: '',
    canvas: null as HTMLCanvasElement | null,
    source: null as any,
    timer: 0,
    // The call key already reported as "renderer unavailable", so waiting for
    // the lazily loaded VoIP bundle does not log every 250 ms scan.
    unavailableReported: '',
  };

  const peerVideoRegistry = (): any => {
    try {
      return win.require?.('WAWebVoipVideoRendererRegistry')?.videoRendererRegistry || null;
    } catch (_) {
      return null;
    }
  };

  const stopPeerVideo = () => {
    if (peerVideo.timer) win.clearInterval(peerVideo.timer);
    const registry = peerVideoRegistry();
    try {
      if (registry && peerVideo.canvas) {
        if (peerVideo.source) registry.unassignSourceFromCanvas?.(peerVideo.source, peerVideo.canvas);
        registry.unregisterVideoCanvas?.(peerVideo.canvas);
      }
    } catch (_) {}
    peerVideo.key = '';
    peerVideo.canvas = null;
    peerVideo.source = null;
    peerVideo.timer = 0;
  };

  const syncPeerVideo = () => {
    const call = currentPageCall();
    const isVideo = !!(call?.isVideo ?? call?.attributes?.isVideo ?? call?.get?.('isVideo'));
    const key = isLivePageCall(call) && isVideo ? pageCallKey(call) : '';
    if (!key) {
      if (peerVideo.key) stopPeerVideo();
      return;
    }
    if (peerVideo.key === key) return;
    if (peerVideo.key) stopPeerVideo();
    // Claimed up front, so a registration that throws is not retried on every
    // 250 ms scan for the rest of the call; the next call tries again.
    peerVideo.key = key;
    try {
      const registry = peerVideoRegistry();
      const renderSource = win.require?.('WAWebVoipVideoRenderSource');
      const peer = call?.peerJid ?? call?.attributes?.peerJid ?? call?.get?.('peerJid');
      if (!registry || !renderSource || !peer) {
        // Not a failure yet: WhatsApp loads its VoIP bundle on demand, so the
        // renderer modules (or the peer) can still be missing when the call
        // starts ringing. Release the claim so a later scan tries again --
        // keeping it made the peer's video never appear for that call.
        peerVideo.key = '';
        if (peerVideo.unavailableReported !== key) {
          peerVideo.unavailableReported = key;
          report('peer-video', `renderer unavailable registry=${!!registry} source=${!!renderSource} peer=${!!peer}`);
        }
        return;
      }
      const source = renderSource.WAWebVoipVideoRenderSource.peer(
        peer,
        renderSource.WAWebVoipVideoRenderStream.CAMERA
      );
      const canvas = document.createElement('canvas');
      canvas.width = 640;
      canvas.height = 360;
      registry.registerVideoCanvas(canvas, false);
      registry.assignSourceToCanvas({ canvas, mirror: false, source });
      peerVideo.canvas = canvas;
      peerVideo.source = source;
      // Which renderer WhatsApp picked matters: it depends on what the browser
      // offers (WEBCODECS_H264 = 4 on Windows' headless Chrome), and a remote
      // Linux chrome-headless-shell may pick another (WEBGL = 3, RASTER = 1).
      // Logged so a remote-API report can be read without anyone looking at
      // the screen.
      let rendererType = '?';
      try { rendererType = String(registry.getRendererType?.()); } catch (_) {}
      report('peer-video', `renderer canvas registered for the peer renderer=${rendererType}`);

      const capture = document.createElement('canvas');
      capture.width = 640;
      capture.height = 360;
      peerVideo.timer = win.setInterval(() => {
        // Nothing to send until the engine has actually painted a frame here.
        if (registry.hasRenderedFirstFrameForCanvas?.(canvas) === false) return;
        const context = capture.getContext('2d');
        if (!context) return;
        context.drawImage(canvas, 0, 0, 640, 360);
        const jpeg = capture.toDataURL('image/jpeg', 0.6).split(',')[1];
        if (!jpeg) return;
        state.remoteVideoFramesSent += 1;
        if (state.remoteVideoFramesSent === 1 || state.remoteVideoFramesSent % 100 === 0) {
          report('remote-video-frame', `sent=${state.remoteVideoFramesSent} source=peer-renderer`);
        }
        win.__winzappOnCallRemoteVideo?.(jpeg)?.catch?.(() => undefined);
      }, PEER_VIDEO_CAPTURE_MS);
    } catch (error: any) {
      report('peer-video', `could not register a renderer canvas: ${String(error?.message || error)}`);
    }
  };

  const scanMediaElements = () => {
    try {
      syncPeerVideo();
    } catch (_) {}
    try {
      // Keep call lifecycle tracking alive even on pages with no media
      // elements. The HTMLMediaElement.play wrapper also refreshes this
      // synchronously, which closes the race around the terminal chime.
      refreshCallAudioPolicy();
      for (const element of Array.from(document.querySelectorAll('audio, video')) as HTMLMediaElement[]) {
        // Catch autoplay, element reuse and later property changes.
        applyPageAudioPolicy(element);
        attachRemoteStream(element.srcObject instanceof MediaStream ? element.srcObject : null);
      }
    } catch (_) {}
  };

  const attachPeerConnection = (pc: RTCPeerConnection) => {
    const tagged = pc as any;
    if (tagged.__winzappCallMediaAttached) return pc;
    tagged.__winzappCallMediaAttached = true;
    const attachRemoteReceivers = () => {
      try {
        for (const receiver of pc.getReceivers?.() || []) {
          attachRemoteTrack(receiver?.track);
          attachRemoteVideo(receiver?.track);
        }
      } catch (_) {}
    };
    pc.addEventListener('track', (event) => {
      attachRemoteTrack(event.track);
      attachRemoteVideo(event.track);
    });
    // Some WhatsApp Web builds populate receivers while applying the remote
    // description without dispatching a page-visible `track` event. Inspecting
    // receivers after the native promise settles covers that path too.
    try {
      const nativeSetRemoteDescription = pc.setRemoteDescription.bind(pc);
      pc.setRemoteDescription = ((description: RTCSessionDescriptionInit) => {
        const result = nativeSetRemoteDescription(description);
        Promise.resolve(result).then(attachRemoteReceivers).catch(() => undefined);
        return result;
      }) as typeof pc.setRemoteDescription;
    } catch (_) {}
    // Do not replace pc.addTrack here. WhatsApp receives the synthetic
    // microphone directly from bridgedGetUserMedia. Replacing a track after
    // its call graph has been created forces a renegotiation in current Web
    // builds, which makes the phone show "reconnecting" and ends the call.
    return pc;
  };

  const NativeRTCPeerConnection = win.RTCPeerConnection;
  try {
    // Install at construction time as well as on setRemoteDescription. Some
    // WhatsApp Web builds create the peer and apply the remote description in
    // the same task; installing only the prototype hook can therefore miss
    // the first remote receiver (and leave the call connected but silent).
    class BridgedRTCPeerConnection extends NativeRTCPeerConnection {
      constructor(...args: any[]) {
        super(...args);
        attachPeerConnection(this as unknown as RTCPeerConnection);
      }
    }
    Object.setPrototypeOf(BridgedRTCPeerConnection, NativeRTCPeerConnection);
    win.RTCPeerConnection = BridgedRTCPeerConnection;
    const nativeSetRemoteDescription = NativeRTCPeerConnection.prototype.setRemoteDescription;
    NativeRTCPeerConnection.prototype.setRemoteDescription = function (...args: any[]) {
      attachPeerConnection(this);
      return nativeSetRemoteDescription.apply(this, args);
    };
  } catch (_) {}

  // A few Chromium/WPP builds expose the remote stream only through an
  // audio/video element instead of a page-visible track event. Keep this scan
  // cheap and short-lived; it is stopped with the bridge reset by navigation.
  const mediaScan = win.setInterval(scanMediaElements, 250);
  state.mediaScan = mediaScan;
  try {
    const WrappedRTCPeerConnection = new Proxy(NativeRTCPeerConnection, {
      construct(target, args) {
        return attachPeerConnection(Reflect.construct(target, args));
      },
    });
    WrappedRTCPeerConnection.prototype = NativeRTCPeerConnection.prototype;
    win.RTCPeerConnection = WrappedRTCPeerConnection;
    if (win.webkitRTCPeerConnection) win.webkitRTCPeerConnection = WrappedRTCPeerConnection;
  } catch (_) {}

  // Newer WhatsApp Web builds can own the PeerConnection in an internal
  // context and only surface the remote MediaStream by assigning it to an
  // HTMLMediaElement. Capture that equally valid media path, including a
  // stream that was assigned before this bridge was injected.
  try {
    const mediaProto = win.HTMLMediaElement?.prototype;
    const descriptor = mediaProto && Object.getOwnPropertyDescriptor(mediaProto, 'srcObject');
    if (descriptor?.get && descriptor?.set && !mediaProto.__winzappCallMediaSrcObjectWrapped) {
      Object.defineProperty(mediaProto, 'srcObject', {
        configurable: true,
        enumerable: descriptor.enumerable,
        get: descriptor.get,
        set(value: any) {
          descriptor.set!.call(this, value);
          attachRemoteStream(value instanceof MediaStream ? value : null);
        },
      });
      mediaProto.__winzappCallMediaSrcObjectWrapped = true;
    }
    scanMediaElements();
    // Installed at document start now (registerCallMediaBridgeBeforeLoad), where
    // documentElement can still be null; observing the document itself with
    // subtree sees the same insertions.
    new MutationObserver(scanMediaElements).observe(document.documentElement || document, {
      childList: true,
      subtree: true,
    });
  } catch (_) {}

  // WhatsApp can keep the WebRTC connection in an internal context but still
  // build its playback graph in this document. Hook the graph boundary so a
  // remote stream/track reaches the Python speaker bridge in that layout too.
  try {
    const contextProto = AudioContextCtor.prototype as any;
    if (!contextProto.__winzappCallMediaSourceWrapped) {
      const nativeCreateMediaStreamSource = contextProto.createMediaStreamSource;
      if (typeof nativeCreateMediaStreamSource === 'function') {
        contextProto.createMediaStreamSource = function (stream: MediaStream) {
          attachRemoteStream(stream);
          return nativeCreateMediaStreamSource.call(this, stream);
        };
      }
      const nativeCreateMediaStreamTrackSource = contextProto.createMediaStreamTrackSource;
      if (typeof nativeCreateMediaStreamTrackSource === 'function') {
        contextProto.createMediaStreamTrackSource = function (track: MediaStreamTrack) {
          attachRemoteTrack(track);
          return nativeCreateMediaStreamTrackSource.call(this, track);
        };
      }
      contextProto.__winzappCallMediaSourceWrapped = true;
    }
  } catch (_) {}

  // ── Silence WhatsApp Web notification audio, preserving call-end ────────
  // Reported live: the incoming-call ringtone played audibly through THIS
  // Chromium process at the same time WinZapp's own ring sound played, so
  // the user heard it twice — and separately, on a call nobody answered
  // (WhatsApp's own 120s ring timeout), it kept looping because nothing
  // ever told the PAGE to stop it: the terminal callstate/incomingcall
  // events are handled entirely on the Python side, which correctly stops
  // WinZapp's own sound, but has no way to reach into the page.
  //
  // Diagnostic instrumentation (kept below, now silencing instead of only
  // logging) confirmed the ringtone plays via a plain looping <audio>
  // element's native .play():
  //   media.play tag=AUDIO isRtcStream=false src=.../kAbvQpjkfMK.ogg loop=true
  // That is a categorically different path from the call's own remote audio
  // track, which never touches an <audio>/Audio() element — it is tapped
  // directly off the RTCPeerConnection's MediaStreamTrack via the Web Audio
  // API (attachRemoteTrack above) and was ALREADY muted before this fix
  // (`sink.gain.value = 0`). Page-native media can therefore be muted
  // independently. The only exception is the short terminal-call chime,
  // opened by the lifecycle-aware policy above; ordinary message pings remain
  // muted. Even a WhatsApp Web build that surfaces the remote track through
  // <audio srcObject=...> is muted by this same policy like everything else —
  // an earlier version exempted srcObject streams here, which let that
  // element's own native playback run audibly alongside WinZapp's separately
  // decoded copy (heard on a real call as a clean track followed by a
  // delayed, jittery duplicate). Muting it is safe because the PCM tap reads
  // the raw MediaStreamTrack independently of the element's playback/mute
  // state.
  //
  // --mute-audio cannot be used at the Chromium launch level to get the same
  // effect (see start.js: that flag starves the Chromium audio SERVICE
  // before the RTC pipeline above can even capture PCM from it, taking the
  // whole call down) — this has to happen at the page's own API surface.
  try {
    let mutedLogCount = 0;
    const logMuted = (kind: string, details: string) => {
      mutedLogCount += 1;
      if (mutedLogCount > 40) return;
      report('page-audio-muted', `${kind} ${details}`);
    };

    const nativeMediaPlay = win.HTMLMediaElement?.prototype?.play;
    if (
      typeof nativeMediaPlay === 'function' &&
      !win.HTMLMediaElement.prototype.__winzappPageAudioMuteWrapped
    ) {
      win.HTMLMediaElement.prototype.play = function (...args: any[]) {
        try {
          const el = this as HTMLMediaElement;
          const isRtcStream = el.srcObject instanceof MediaStream;
          const silenced = applyPageAudioPolicy(el);
          if (silenced) {
            logMuted(
              'media.play',
              `tag=${el.tagName} isRtcStream=${isRtcStream} ` +
                `src=${String(el.currentSrc || (el as any).src || '').slice(0, 120)} loop=${el.loop}`
            );
          }
        } catch (_) {}
        return nativeMediaPlay.apply(this, args);
      };
      win.HTMLMediaElement.prototype.__winzappPageAudioMuteWrapped = true;
    }

    const NativeAudio = win.Audio;
    if (typeof NativeAudio === 'function' && !win.__winzappPageAudioMuteAudioWrapped) {
      win.Audio = new Proxy(NativeAudio, {
        construct(target, args) {
          const instance: HTMLAudioElement = Reflect.construct(target, args);
          try {
            // Construction alone does not identify a ringtone: WhatsApp
            // commonly sets .loop only after creating the element. The play
            // wrapper and media scan apply the selective policy later.
            applyPageAudioPolicy(instance);
          } catch (_) {}
          return instance;
        },
      });
      win.__winzappPageAudioMuteAudioWrapped = true;
    }
    // The autoplay-attribute backstop (elements that start playing without
    // going through either wrapper above) is scanMediaElements() itself,
    // just above — it already walks every <audio>/<video> element on a
    // 250ms interval and on every DOM mutation for the RTC srcObject case,
    // and now silences each one it finds too.
  } catch (_) {}

  const nativeGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  const nativeEnumerateDevices = navigator.mediaDevices.enumerateDevices?.bind(
    navigator.mediaDevices
  );

  // A remote/headless WPPConnect host has no physical webcam.  WhatsApp Web
  // checks the media-device inventory before it asks getUserMedia() for video,
  // so merely returning our canvas track from bridgedGetUserMedia() is not
  // enough: with zero videoinput devices the page can decide there is no
  // camera and never request that synthetic track at all.
  //
  // Always advertise one stable WinZapp camera device.  getUserMedia below
  // ignores the physical device constraint and returns cameraTrack(), whose
  // canvas is fed by JPEG frames captured on the Windows client.  Preserve the
  // native inventory as well so audio-device discovery keeps its normal shape.
  if (nativeEnumerateDevices) {
    const virtualCamera = {
      deviceId: 'winzapp-camera',
      kind: 'videoinput',
      label: 'WinZapp Camera',
      groupId: 'winzapp-call-media',
      toJSON() {
        return {
          deviceId: this.deviceId,
          kind: this.kind,
          label: this.label,
          groupId: this.groupId,
        };
      },
    };
    const bridgedEnumerateDevices = async () => {
      let devices: any[] = [];
      try {
        devices = Array.from(await nativeEnumerateDevices());
      } catch (_) {}
      if (!devices.some((device: any) =>
        device?.kind === 'videoinput' && device?.deviceId === virtualCamera.deviceId
      )) {
        devices.unshift(virtualCamera);
      }
      return devices;
    };
    try {
      Object.defineProperty(navigator.mediaDevices, 'enumerateDevices', {
        configurable: true,
        writable: true,
        value: bridgedEnumerateDevices,
      });
    } catch (_) {
      (navigator.mediaDevices as any).enumerateDevices = bridgedEnumerateDevices;
    }
  }
  const permissionResult = (
    permissionName: string,
    stateValue: PermissionState = 'granted'
  ): PermissionStatus => ({
    name: permissionName,
    state: stateValue,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  } as unknown as PermissionStatus);

  try {
    const nativePermissionQuery = navigator.permissions?.query?.bind(navigator.permissions);
    if (nativePermissionQuery) {
      Object.defineProperty(navigator.permissions, 'query', {
        configurable: true,
        writable: true,
        value: async (descriptor: PermissionDescriptor) => {
          const name = String((descriptor as any)?.name || '');
          // WhatsApp's VoIP bootstrap gates on microphone/camera permission.
          // Neither physical device is opened through this wrapper: audio is
          // supplied by the WinZapp/Pulse bridge and video by cameraTrack().
          if (name === 'microphone' || name === 'camera') {
            return permissionResult(name, 'granted');
          }
          return nativePermissionQuery(descriptor);
        },
      });
    }
  } catch (_) {}

  const bridgedGetUserMedia = async (constraints: MediaStreamConstraints = {}) => {
    // Instrumentation only: confirms from wppconnect.log whether the native
    // VoIP stack (getNativeVoipStack/runNativeVoipAction/warmCallVoipRuntime
    // — see createSessionUtil.ts) ever actually asks the page for a camera
    // through this override, or acquires it some other way that would leave
    // the whole camera bridge below unused. Not a fix by itself.
    report('get-user-media', `video=${!!constraints.video} audio=${!!constraints.audio}`);
    if (constraints.video) {
      const stream = new MediaStream([cameraTrack()]);
      if (constraints.audio) {
        if (linuxAudio) {
          const nativeAudio = await nativeGetUserMedia({ audio: constraints.audio, video: false });
          nativeAudio.getAudioTracks().forEach((track) => stream.addTrack(track));
        } else {
          const micTrack = ensureMicTrack().clone();
          if (micTrack.id) state.localTrackIds.add(micTrack.id);
          stream.addTrack(micTrack);
        }
      }
      return stream;
    }
    if (!constraints?.audio && !constraints?.video) {
      return nativeGetUserMedia(constraints);
    }
    if (linuxAudio) return nativeGetUserMedia(constraints);

    // Never let WhatsApp Web open the physical microphone. Even while the
    // Python call engine is not active, expose a live silent synthetic track so
    // the native VoIP bootstrap can complete without touching audio hardware.
    // Once state.enabled becomes true, Python PCM is written into this track.
    const micTrack = ensureMicTrack().clone();
    if (micTrack.id) state.localTrackIds.add(micTrack.id);
    return new MediaStream([micTrack]);
  };
  try {
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
      configurable: true,
      writable: true,
      value: bridgedGetUserMedia,
    });
  } catch (_) {
    (navigator.mediaDevices as any).getUserMedia = bridgedGetUserMedia;
  }
  try {
    win.navigator.getUserMedia = (constraints: MediaStreamConstraints, ok: any, fail: any) => {
      bridgedGetUserMedia(constraints).then(ok, fail);
    };
    win.navigator.webkitGetUserMedia = win.navigator.getUserMedia;
  } catch (_) {}

  win.__winzappCallMediaBridge = state;
  return true;
}

function toBuffer(value: any): Buffer | null {
  if (typeof value === 'string') {
    try { return Buffer.from(value, 'base64'); } catch (_) { return null; }
  }
  if (Buffer.isBuffer(value)) return value;
  if (value instanceof Uint8Array) return Buffer.from(value);
  if (Array.isArray(value)) return Buffer.from(value);
  if (value?.type === 'Buffer' && Array.isArray(value.data)) return Buffer.from(value.data);
  return null;
}

function linuxAudioRelayActive(): boolean {
  return process.platform === 'linux' && process.env.PULSE_SERVER === LINUX_PULSE_SERVER;
}

/**
 * Register the bridge as a new-document script BEFORE WhatsApp Web first loads.
 *
 * WhatsApp's VoIP bundle may cache getUserMedia and RTCPeerConnection when its
 * modules evaluate, so the media hooks have to exist before that. This used to
 * be done by registering the script once WPPConnect handed the page over (after
 * WhatsApp had loaded) and then calling page.reload(). That reload wedged the
 * page for good: measured 2026-09-24 on two consecutive starts, WhatsApp Web
 * reloaded itself right after its first load, the priming reload landed on top
 * of it, and the renderer stopped answering anything - not even a CDP
 * Runtime.evaluate or the trace start - while idle, with no dialog, no freeze
 * and no pending navigation to blame. The session stayed INITIALIZING forever
 * and WinZapp went offline; skipping the reload alone brought it straight back.
 *
 * start.js calls this from its initWhatsapp wrapper, before the first goto(),
 * so the hooks are in place without any reload. The flag lives on the page:
 * no client object exists yet at that point, and the page is the same object
 * WPPConnect later exposes as client.page.
 */
export async function registerCallMediaBridgeBeforeLoad(page: any): Promise<boolean> {
  if (!page || typeof page.evaluateOnNewDocument !== 'function') return false;
  if (page.__winzappCallMediaNewDocumentInstalled) return true;
  await page.evaluateOnNewDocument(installCallMediaBridgeInPage, linuxAudioRelayActive());
  page.__winzappCallMediaNewDocumentInstalled = true;
  return true;
}

export async function ensureCallMediaBridge(client: any, io: any, logger: any): Promise<boolean> {
  const linuxAudio = linuxAudioRelayActive();
  const page = client?.waPage || client?.page;
  if (!page) return false;

  try {
    await page.exposeFunction(
      '__winzappOnCallRemoteAudio',
      (base64: string, sampleRate: number) => {
        if (typeof base64 !== 'string' || base64.length > MAX_AUDIO_FRAME_BYTES * 2) return;
        const pcmBytes = Math.floor((base64.length * 3) / 4);
        if (!pcmBytes || pcmBytes > MAX_AUDIO_FRAME_BYTES) return;
        io.to(`session:${client.session}`).emit('call:audio:remote', {
          session: client.session,
          sampleRate: Number(sampleRate) || 48000,
          encoding: 'base64',
          pcm: base64,
        });
      }
    );
  } catch (_) {
    // Puppeteer bindings survive navigation; duplicate registration is expected.
  }

  try {
    await page.exposeFunction('__winzappOnCallBridgeEvent', (event: string, details: string) => {
      logger?.info?.(`[${client.session}] call media ${event}: ${details}`);
    });
  } catch (_) {
    // Puppeteer bindings survive navigation; duplicate registration is expected.
  }

  try {
    await page.exposeFunction('__winzappOnCallRemoteVideo', (jpeg: string) => {
      if (typeof jpeg !== 'string' || jpeg.length > 350_000) return;
      io.to(`session:${client.session}`).emit('call:video:remote', {
        session: client.session,
        jpeg,
      });
    });
  } catch (_) {
    // Puppeteer bindings survive navigation.
  }

  try {
    if (!page.__winzappCallMediaNewDocumentInstalled) {
      // start.js registers the bridge before WhatsApp's first load. Reaching
      // here without that means its initWhatsapp wrapper did not run: register
      // for future documents and install into the running one below, but
      // NEVER reload to make up for it - that reload is what wedged the page
      // (see registerCallMediaBridgeBeforeLoad). Calls may lack audio until
      // WhatsApp next loads a document; that is recoverable, a dead session
      // is not.
      await registerCallMediaBridgeBeforeLoad(page);
      logger?.warn?.(
        `[${client.session}] call media bridge registered after WhatsApp loaded ` +
          '(not before the first load); installing into the running page without a reload'
      );
    }
    const installed = await page.evaluate(installCallMediaBridgeInPage, linuxAudio);
    if (installed) logger?.info?.(`[${client.session}] WinZapp call media bridge ready`);
    return !!installed;
  } catch (error: any) {
    logger?.warn?.(
      `[${client.session}] WinZapp call media bridge unavailable: ${error?.message || error}`
    );
    return false;
  }
}

export async function warmCallVoipRuntime(client: any, logger: any): Promise<boolean> {
  const page = client?.waPage || client?.page;
  if (!page) return false;

  const pending = (client as any).__winzappVoipWarmupPromise;
  if (pending) return pending;

  const warmup = (async (): Promise<boolean> => {
    try {
      const result = await page.evaluate(async () => {
        const win = window as any;
        const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));
        let lastError = '';

        for (let attempt = 0; attempt < 10; attempt += 1) {
          try {
            const enable = win.WPP?.call?.enableCallInterface;
            if (typeof enable !== 'function') {
              lastError = 'WPP.call.enableCallInterface is not ready';
              await delay(250);
              continue;
            }
            await enable();

            const functions = win.WPP?.whatsapp?.functions || {};
            const requireBackend =
              functions.requireVoipJsBackend || win.WPP?.whatsapp?.requireVoipJsBackend;
            if (typeof requireBackend === 'function') {
              const backend = await requireBackend();
              const init = backend?.WAWebVoipInit?.initWAWebVoip || backend?.initWAWebVoip;
              if (typeof init === 'function') {
                const initModule = backend?.WAWebVoipInit || backend;
                await init.call(initModule, 'winzapp_session_warmup');
                const emitter = initModule?.VoipInitEventEmitter;
                if (
                  emitter?.getIsVoipInited?.() !== true &&
                  emitter?.getDidVoipInitError?.() === true &&
                  typeof initModule?.retryWAWebVoipInitAfterFailure === 'function'
                ) {
                  await initModule.retryWAWebVoipInitAfterFailure();
                }
                if (emitter?.getIsVoipInited?.() === false) {
                  lastError = 'WhatsApp VoIP initializer did not become ready';
                  await delay(250 * (attempt + 1));
                  continue;
                }
              }
            }

            const getStack =
              functions.getVoipStackInterface || win.WPP?.whatsapp?.getVoipStackInterface;
            if (typeof getStack !== 'function') {
              lastError = 'getVoipStackInterface is not ready';
              await delay(250);
              continue;
            }
            const stack = await getStack();
            if (stack) {
              // WA-JS exposes the stack before WhatsApp's worker-side
              // initialization has completed.  The connection model is the
              // only public readiness signal in newer builds; accepting the
              // stack object alone causes RPC attempted without successful
              // voipInit during the first call.
              const conn =
                win.WPP?.whatsapp?.ConnStore ||
                win.Store?.Conn ||
                win.WPP?.whatsapp?.Conn;
              if (conn && conn.isVoipInitialized === false) {
                lastError = 'WhatsApp VoIP connection initialization is pending';
                await delay(250 * (attempt + 1));
                continue;
              }
              return {
                ready: true,
                acceptCall: typeof stack.acceptCall === 'function',
                rejectCall: typeof stack.rejectCall === 'function',
                endCall: typeof stack.endCall === 'function',
              };
            }
            lastError = 'VoIP stack interface returned no value';
          } catch (error: any) {
            lastError = String(error?.message || error || 'unknown error');
          }
          await delay(250 * (attempt + 1));
        }

        return { ready: false, error: lastError };
      });

      if (result?.ready) {
        logger?.info?.(
          `[${client.session}] WinZapp VoIP runtime warmed ` +
            `(accept=${!!result.acceptCall}, reject=${!!result.rejectCall}, end=${!!result.endCall})`
        );
        return true;
      }
      logger?.warn?.(
        `[${client.session}] WinZapp VoIP runtime warmup incomplete: ${result?.error || 'unknown error'}`
      );
      return false;
    } catch (error: any) {
      logger?.warn?.(
        `[${client.session}] WinZapp VoIP runtime warmup failed: ${error?.message || error}`
      );
      return false;
    }
  })();

  (client as any).__winzappVoipWarmupPromise = warmup;
  try {
    return await warmup;
  } finally {
    if ((client as any).__winzappVoipWarmupPromise === warmup) {
      delete (client as any).__winzappVoipWarmupPromise;
    }
  }
}

export async function setCallMediaBridgeActive(client: any, active: boolean): Promise<boolean> {
  if (process.platform === 'linux' && process.env.PULSE_SERVER === LINUX_PULSE_SERVER) {
    return true;
  }
  const page = client?.waPage || client?.page;
  if (!page) return false;
  try {
    return !!(await page.evaluate((activeInPage: boolean) => {
      const bridge = (window as any).__winzappCallMediaBridge;
      if (!bridge) return false;
      if (activeInPage) return !!bridge.enable?.();
      bridge.reset?.();
      return true;
    }, active));
  } catch (_) {
    return false;
  }
}

async function drainMicrophoneQueue(session: string, logger: any): Promise<void> {
  if (micDraining.has(session)) return;
  micDraining.add(session);
  try {
    const queue = micQueues.get(session);
    while (queue?.length) {
      const client: any = (clientsArray as any)[session];
      const page = client?.waPage || client?.page;
      if (!page) {
        queue.length = 0;
        break;
      }
      // page.evaluate can occasionally stall behind Chromium work. Do not
      // replay everything accumulated during that stall; jump back near live
      // audio and send only the freshest short batch.
      if (queue.length > MIC_TARGET_BACKLOG_FRAMES) {
        const dropped = queue.length - MIC_TARGET_BACKLOG_FRAMES;
        queue.splice(0, dropped);
        logger?.debug?.(
          `[${session}] skipped stale microphone frames before page bridge=${dropped}`
        );
      }
      const frames = queue.splice(0, MIC_TARGET_BACKLOG_FRAMES);
      if (!frames.length) continue;
      const base64Frames = frames.map((frame) => frame.toString('base64'));
      try {
        await page.evaluate((payloads: string[]) => {
          const bridge = (window as any).__winzappCallMediaBridge;
          if (bridge?.pushMicrophoneBatch) bridge.pushMicrophoneBatch(payloads);
          else payloads.forEach((payload) => bridge?.pushMicrophone?.(payload));
        }, base64Frames);
      } catch (error: any) {
        logger?.debug?.(
          `[${session}] call microphone batch dropped (${frames.length} frames): ${error?.message || error}`
        );
      }
    }
  } finally {
    micDraining.delete(session);
  }
}

export function registerCallAudioSocket(
  socket: Socket,
  logger: any,
  authenticatedSession: string
): void {
  let cameraBusy = false;
  let cameraFramesReceived = 0;

  socket.on('call:audio:start', (payload: any) => {
    const session = String(payload?.session || '');
    if (!session || session !== authenticatedSession) return;
    if (!(clientsArray as any)[session]) return;
    const linuxAudio = ensureLinuxCallAudio(session, socket, logger);
    if (linuxAudio) {
      logger?.info?.(`[${session}] Linux call speaker monitor started before answer`);
    }
  });

  socket.on('call:video:camera', (payload: any) => {
    const session = String(payload?.session || '');
    const jpeg = payload?.jpeg;
    if (session !== authenticatedSession || typeof jpeg !== 'string' ||
        jpeg.length > 350_000 || cameraBusy) return;
    const client: any = (clientsArray as any)[session];
    const page = client?.waPage || client?.page;
    if (!page) return;
    cameraFramesReceived += 1;
    if (cameraFramesReceived === 1 || cameraFramesReceived % 100 === 0) {
      logger?.info?.(
        `[${session}] call camera frames received from desktop=${cameraFramesReceived}`
      );
    }
    cameraBusy = true;
    const epoch = typeof payload?.epoch === 'number' ? payload.epoch : undefined;
    page.evaluate((frame: string, frameEpoch?: number) => {
      (window as any).__winzappCallMediaBridge?.pushCameraFrame?.(frame, frameEpoch);
    }, jpeg, epoch).catch(() => undefined).finally(() => { cameraBusy = false; });
  });
  // Turning video off has to reach the page: the canvas keeps being captured
  // at 10 fps regardless of whether the desktop is still sending frames, so
  // without this the peer went on seeing the user's last frame, frozen, for
  // the rest of the call.
  socket.on('call:video:camera:stop', (payload: any) => {
    const session = String(payload?.session || '');
    if (session !== authenticatedSession) return;
    const client: any = (clientsArray as any)[session];
    const page = client?.waPage || client?.page;
    if (!page) return;
    const epoch = typeof payload?.epoch === 'number' ? payload.epoch : undefined;
    const native = payload?.native === true;
    logger?.info?.(`[${session}] call camera stopped by desktop epoch=${epoch} native=${native}`);
    page.evaluate(
      ({ stoppedEpoch, nativeMute }: { stoppedEpoch?: number; nativeMute: boolean }) => {
        (window as any).__winzappCallMediaBridge?.stopCamera?.(stoppedEpoch, nativeMute);
      },
      { stoppedEpoch: epoch, nativeMute: native }
    ).catch(() => undefined);
  });

  // The user turned video back on from the call window.
  socket.on('call:video:camera:start', (payload: any) => {
    const session = String(payload?.session || '');
    if (session !== authenticatedSession) return;
    const client: any = (clientsArray as any)[session];
    const page = client?.waPage || client?.page;
    if (!page) return;
    logger?.info?.(`[${session}] call camera resumed by desktop`);
    page.evaluate(() => {
      (window as any).__winzappCallMediaBridge?.resumeCamera?.();
    }).catch(() => undefined);
  });

  socket.on('call:audio:mic', (payload: any) => {
    const session = String(payload?.session || '');
    const pcm =
      payload?.encoding === 'base64' && typeof payload?.pcm === 'string'
        ? Buffer.from(payload.pcm, 'base64')
        : toBuffer(payload?.pcm);
    if (
      !session ||
      session !== authenticatedSession ||
      !pcm?.length ||
      pcm.length > MAX_AUDIO_FRAME_BYTES
    ) return;
    if (!(clientsArray as any)[session]) return;
    const linuxAudio = ensureLinuxCallAudio(session, socket, logger);
    if (linuxAudio) {
      // Live audio must never queue without bounds. If PulseAudio is applying
      // backpressure, drop this 20 ms frame; adding one `drain` listener per
      // frame leaks listeners and eventually writes to a process that has
      // already exited, crashing Node with an unhandled EPIPE.
      if (
        linuxAudio.playback.exitCode === null &&
        !linuxAudio.playback.stdin.destroyed &&
        linuxAudio.playback.stdin.writable
      ) {
        try { linuxAudio.playback.stdin.write(pcm); } catch (_) {}
      }
      const received = (micReceived.get(session) || 0) + 1;
      micReceived.set(session, received);
      if (received === 1 || received % 250 === 0) {
        logger?.info?.(`[${session}] call microphone relayed to Linux audio frames=${received}`);
      }
      return;
    }
    const queue = micQueues.get(session) || [];
    queue.push(pcm);
    while (queue.length > MAX_MIC_QUEUE_FRAMES) queue.shift();
    micQueues.set(session, queue);
    const received = (micReceived.get(session) || 0) + 1;
    micReceived.set(session, received);
    if (received === 1 || received % 250 === 0) {
      logger?.info?.(`[${session}] call microphone received frames=${received} bytes=${pcm.length}`);
    }
    void drainMicrophoneQueue(session, logger);
  });

  socket.on('call:audio:stop', (payload: any) => {
    const session = String(payload?.session || '');
    if (!session || session !== authenticatedSession) return;
    micQueues.delete(session);
    micReceived.delete(session);
    stopLinuxCallAudio(session);
    const client: any = (clientsArray as any)[session];
    const page = client?.waPage || client?.page;
    page
      ?.evaluate(() => (window as any).__winzappCallMediaBridge?.reset?.())
      .catch(() => undefined);
  });

  socket.on('disconnect', () => stopLinuxCallAudio(authenticatedSession));
}
