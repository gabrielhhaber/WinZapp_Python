import { Socket } from 'socket.io';
import { ChildProcessWithoutNullStreams, execFileSync, spawn } from 'child_process';

import { clientsArray } from './sessionUtil';

const MAX_AUDIO_FRAME_BYTES = 64 * 1024;
const MAX_MIC_QUEUE_FRAMES = 75;
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

function installCallMediaBridgeInPage(): boolean {
  const win = window as any;
  if (win.__winzappCallMediaBridge?.version === 4) return true;
  if (!navigator.mediaDevices?.getUserMedia || !win.RTCPeerConnection) return false;

  const AudioContextCtor = win.AudioContext || win.webkitAudioContext;
  if (!AudioContextCtor) return false;

  const state: any = {
    version: 4,
    enabled: false,
    context: null,
    micDestination: null,
    micProcessor: null,
    micQueue: [] as Float32Array[],
    micOffset: 0,
    remotePipelines: new Map<string, any>(),
    remoteTrackIds: new Set<string>(),
    localTrackIds: new Set<string>(),
    micFramesPushed: 0,
    micBytesPushed: 0,
    micSamplesConsumed: 0,
    remoteFramesCaptured: 0,
    remoteTracksAttached: 0,
  };

  const report = (event: string, details = '') => {
    try { win.__winzappOnCallBridgeEvent?.(event, details); } catch (_) {}
  };

  // Silences WhatsApp Web's own page-native sounds (ringtone, message
  // chimes, ...) without touching real call audio, which never plays
  // through an <audio>/<video> element in the first place — see the wiring
  // below (scanMediaElements, HTMLMediaElement.play, `new Audio()`) for why
  // that split is safe.
  const silenceElement = (el: HTMLMediaElement) => {
    try {
      el.muted = true;
      el.volume = 0;
    } catch (_) {}
  };

  const ensureContext = () => {
    if (!state.context || state.context.state === 'closed') {
      state.context = new AudioContextCtor({
        latencyHint: 'interactive',
        sampleRate: 48000,
      });
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

  const ensureMicTrack = () => {
    const context = ensureContext();
    if (state.micDestination?.stream?.getAudioTracks?.()[0]?.readyState === 'live') {
      return state.micDestination.stream.getAudioTracks()[0];
    }

    const processor = context.createScriptProcessor(1024, 0, 1);
    const destination = context.createMediaStreamDestination();
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
    const schedulerSink = context.createGain();
    schedulerSink.gain.value = 0;
    processor.connect(schedulerSink);
    schedulerSink.connect(context.destination);
    state.micProcessor = processor;
    state.micDestination = destination;
    const track = destination.stream.getAudioTracks()[0];
    if (track?.id) state.localTrackIds.add(track.id);
    return track;
  };

  state.enable = () => {
    state.enabled = true;
    ensureMicTrack();
    ensureContext().resume().catch(() => undefined);
    return true;
  };

  state.pushMicrophone = (base64: string) => {
    if (!state.enabled) return;
    ensureContext();
    const samples = decodePcm16(base64);
    if (!samples.length) return;
    state.micQueue.push(samples);
    state.micFramesPushed += 1;
    state.micBytesPushed += Math.floor(base64.length * 3 / 4);
    while (state.micQueue.length > 75) state.micQueue.shift();
  };

  state.pushMicrophoneBatch = (frames: string[]) => {
    for (const frame of frames || []) state.pushMicrophone(frame);
  };

  state.reset = () => {
    state.enabled = false;
    state.micQueue.length = 0;
    state.micOffset = 0;
    for (const pipeline of state.remotePipelines.values()) {
      try { pipeline.source.disconnect(); } catch (_) {}
      try { pipeline.processor.disconnect(); } catch (_) {}
      try { pipeline.sink.disconnect(); } catch (_) {}
    }
    state.remotePipelines.clear();
    state.remoteTrackIds.clear();
    state.localTrackIds.clear();
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
    const processor = context.createScriptProcessor(1024, 1, 1);
    const sink = context.createGain();
    sink.gain.value = 0;
    processor.onaudioprocess = (event: AudioProcessingEvent) => {
      if (!state.enabled) return;
      const input = event.inputBuffer.getChannelData(0);
      const callback = win.__winzappOnCallRemoteAudio;
      if (typeof callback === 'function' && input.length) {
        state.remoteFramesCaptured += 1;
        if (state.remoteFramesCaptured === 1 || state.remoteFramesCaptured % 250 === 0) {
          report('remote-frame', `track=${id} frames=${state.remoteFramesCaptured}`);
        }
        callback(encodePcm16(input), context.sampleRate).catch?.(() => undefined);
      }
      event.outputBuffer.getChannelData(0).fill(0);
    };
    source.connect(processor);
    processor.connect(sink);
    sink.connect(context.destination);
    state.remotePipelines.set(id, { source, processor, sink, track });
    state.remoteTracksAttached += 1;
    report('remote-track', `track=${id} tracks=${state.remoteTracksAttached}`);
    track.addEventListener('ended', () => {
      const pipeline = state.remotePipelines.get(id);
      if (!pipeline) return;
      try { pipeline.source.disconnect(); } catch (_) {}
      try { pipeline.processor.disconnect(); } catch (_) {}
      try { pipeline.sink.disconnect(); } catch (_) {}
      state.remotePipelines.delete(id);
      state.remoteTrackIds.delete(id);
    }, { once: true });
  };

  const attachRemoteStream = (stream: MediaStream | null | undefined) => {
    try {
      for (const track of stream?.getAudioTracks?.() || []) attachRemoteTrack(track);
    } catch (_) {}
  };

  const scanMediaElements = () => {
    try {
      for (const element of Array.from(document.querySelectorAll('audio, video')) as HTMLMediaElement[]) {
        // Silencing here too (not just on .play()/new Audio()) catches
        // anything that starts playing without going through either wrapper
        // — e.g. the native `autoplay` attribute, which Chromium does not
        // route through the JS-visible .play() method.
        silenceElement(element);
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
        for (const receiver of pc.getReceivers?.() || []) attachRemoteTrack(receiver?.track);
      } catch (_) {}
    };
    pc.addEventListener('track', (event) => attachRemoteTrack(event.track));
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
    new MutationObserver(scanMediaElements).observe(document.documentElement, {
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

  // ── Silence WhatsApp Web's own page-native sounds ────────────────────────
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
  // (`sink.gain.value = 0`). So muting every native <audio>/<video> element
  // and every `new Audio()` instance, unconditionally, for the life of the
  // page, silences WhatsApp Web's own sound effects (ringtone, message
  // chimes, anything else) without ever touching real call audio — no need
  // to track call state and toggle mute on/off around it. Even a WhatsApp
  // Web build that plays the remote track through a real
  // <audio srcObject=...> element (the fallback path handled above) is
  // unaffected: that pipeline reads PCM from the MediaStreamTrack directly,
  // never from the element's own rendered output.
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
          silenceElement(el);
          logMuted(
            'media.play',
            `tag=${el.tagName} isRtcStream=${isRtcStream} ` +
              `src=${String(el.currentSrc || (el as any).src || '').slice(0, 120)} loop=${el.loop}`
          );
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
            silenceElement(instance);
            logMuted('new Audio()', `src=${String(args?.[0] || '').slice(0, 120)}`);
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
          // Microphone only. WhatsApp's VoIP bootstrap gates on this, and the
          // physical device is never opened anyway — getUserMedia below hands
          // back a synthetic track. The camera is deliberately NOT claimed:
          // video calls are out of scope, and answering 'granted' here would
          // undo removing videoCapture from the CDP grant for any page that
          // checks before asking.
          if (name === 'microphone') {
            return permissionResult(name, 'granted');
          }
          return nativePermissionQuery(descriptor);
        },
      });
    }
  } catch (_) {}

  const bridgedGetUserMedia = async (constraints: MediaStreamConstraints = {}) => {
    // Anything asking for a CAMERA OR A MICROPHONE is served synthetically,
    // and the check covers video on its own. An earlier version returned to
    // the native call whenever `audio` was falsy, which let a page-side
    // getUserMedia({ video: true }) past the refusal further down: with
    // --use-fake-ui-for-media-stream the browser auto-accepts the request, so
    // the real webcam opened with no prompt and no indicator, in a page the
    // user never sees. Removing videoCapture from the CDP grant does not close
    // that — the grant governs the Permissions API, the flag governs the
    // prompt.
    if (!constraints?.audio && !constraints?.video) {
      return nativeGetUserMedia(constraints);
    }

    // Never let WhatsApp Web open the physical microphone. Even while the
    // Python call engine is not active, expose a live silent synthetic track so
    // the native VoIP bootstrap can complete without touching audio hardware.
    // Once state.enabled becomes true, Python PCM is written into this track.
    const micTrack = ensureMicTrack().clone();
    if (micTrack.id) state.localTrackIds.add(micTrack.id);
    // Audio only, whatever was asked for: the returned stream has no video
    // track, so every video path fails closed instead of reaching hardware.
    // Deliberately not a rejection — WhatsApp's VoIP bootstrap probes this and
    // a throw here would take voice calls down with video.
    if (constraints.video) {
      report('video-request-refused', 'video calls are not supported');
    }
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

export async function ensureCallMediaBridge(client: any, io: any, logger: any): Promise<boolean> {
  if (process.platform === 'linux' && process.env.PULSE_SERVER === LINUX_PULSE_SERVER) {
    return true;
  }
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
    if (!(client as any).__winzappCallMediaNewDocumentInstalled) {
      await page.evaluateOnNewDocument(installCallMediaBridgeInPage);
      (client as any).__winzappCallMediaNewDocumentInstalled = true;

      // WPPConnect hands the page to us only after WhatsApp Web has loaded.
      // Installing the bridge in that already-running document is too late:
      // WhatsApp's VoIP bundle may already have cached getUserMedia and
      // RTCPeerConnection. Reload exactly once after registering the
      // new-document script, so the media hooks exist before any WhatsApp
      // module evaluates. The authenticated profile lives in userDataDir, so
      // this is a normal WhatsApp Web reload and does not re-pair the account.
      if (!(client as any).__winzappCallMediaPrimed) {
        (client as any).__winzappCallMediaPrimed = true;
        logger?.info?.(`[${client.session}] priming call media bridge before WhatsApp load`);
        await page.reload({ waitUntil: 'domcontentloaded', timeout: 120000 });
      }
    }
    const installed = await page.evaluate(installCallMediaBridgeInPage);
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
      const frames = queue.splice(0, 8);
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
