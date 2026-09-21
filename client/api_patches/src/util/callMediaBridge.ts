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
  if (win.__winzappCallMediaBridge?.version === 7) return true;
  if (!navigator.mediaDevices?.getUserMedia || !win.RTCPeerConnection) return false;

  const AudioContextCtor = win.AudioContext || win.webkitAudioContext;
  if (!AudioContextCtor) return false;

  const PAGE_MIC_QUEUE_FRAMES = 4;
  const PAGE_MIC_TARGET_BACKLOG_FRAMES = 2;

  const state: any = {
    version: 7,
    enabled: false,
    context: null,
    micDestination: null,
    micProcessor: null,
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
  // A merely-ringing call (INCOMING_RING/CALLING/etc, counted "active" by
  // isLivePageCall below) that is cancelled or rejected before anyone answers
  // has no real terminal chime to protect. state.enabled only becomes true
  // when the audio bridge is actually attached after answer, so it is the
  // signal for "this call was ever really connected" — remembered here
  // because refreshCallAudioPolicy's own poll can observe the ENDED
  // transition after state.reset() has already cleared state.enabled back to
  // false for the same call. Without this, cancelling a call before answer
  // opened the same 2500ms exemption window as a genuine hangup, and the
  // coincident missed-call message-notification ping slipped through it
  // unmuted (measured 2026-09-20).
  let callWasAnswered = false;
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
    const active = isLivePageCall(currentPageCall());
    if (!callWasActive && active) callWasAnswered = false; // a new call just started ringing
    if (state.enabled) callWasAnswered = true; // remember it was actually answered
    if (callWasActive && !active) {
      if (callWasAnswered) allowCallEndChime();
      callWasAnswered = false;
    }
    callWasActive = active;
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

  state.pushCameraFrame = (jpeg: string) => {
    if (typeof jpeg !== 'string' || jpeg.length > 350_000) return;
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
  state.stopCamera = () => {
    state.cameraGeneration += 1;
    state.cameraPending = false;
    blankCameraCanvas();
    report('camera-stop', `generation=${state.cameraGeneration}`);
  };

  // Call over: nothing holds the track any more, so release it too. Without
  // this the canvas -- and the last real frame drawn on it -- survived into
  // the NEXT call, because cameraTrack() reuses any track still 'live' and
  // reset() never cleared either. The next call therefore started by
  // transmitting a still of the previous one, including a call answered with
  // "answer without video".
  const teardownCamera = () => {
    state.stopCamera();
    try { state.cameraTrack?.stop(); } catch (_) {}
    state.cameraTrack = null;
    state.cameraCanvas = null;
    state.cameraTrackRequests = 0;
  };

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
    return state.cameraTrack.clone();
  };

  const attachRemoteVideo = (track: MediaStreamTrack) => {
    if (!track || track.kind !== 'video' || state.remoteVideoIds.has(track.id)) return;
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

  state.reset = () => {
    // Local reject/end stops the bridge immediately before WhatsApp performs
    // the native action. Arm the terminal-chime exception first so that sound
    // stays audible even if it starts before the CallStore poll observes
    // ENDED — but only for a call that was actually answered. A ringing-only
    // call being rejected/cancelled has no real terminal chime to protect,
    // and opening this window for it let a coincident missed-call message
    // ping slip through unmuted (measured 2026-09-20).
    if (state.enabled) allowCallEndChime();
    state.enabled = false;
    // Otherwise this survives into the next call: if it starts ringing
    // before refreshCallAudioPolicy()'s own poll ever observes the idle gap
    // between the two (a near-immediate redial), callWasAnswered would still
    // read true from the call this reset() just tore down, wrongly opening
    // the chime exemption if the NEW call is itself cancelled unanswered.
    callWasAnswered = false;
    state.micQueue.length = 0;
    state.micOffset = 0;
    for (const pipeline of state.remotePipelines.values()) {
      try { pipeline.source.disconnect(); } catch (_) {}
      try { pipeline.processor.disconnect(); } catch (_) {}
      try { pipeline.sink.disconnect(); } catch (_) {}
    }
    state.remotePipelines.clear();
    state.remoteTrackIds.clear();
    for (const timer of state.remoteVideoTimers.values()) win.clearInterval(timer);
    state.remoteVideoTimers.clear();
    state.remoteVideoIds.clear();
    state.localTrackIds.clear();
    teardownCamera();
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
      for (const track of stream?.getVideoTracks?.() || []) attachRemoteVideo(track);
    } catch (_) {}
  };

  const scanMediaElements = () => {
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

export async function ensureCallMediaBridge(client: any, io: any, logger: any): Promise<boolean> {
  const linuxAudio = process.platform === 'linux' && process.env.PULSE_SERVER === LINUX_PULSE_SERVER;
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
    if (!(client as any).__winzappCallMediaNewDocumentInstalled) {
      await page.evaluateOnNewDocument(installCallMediaBridgeInPage, linuxAudio);
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
    page.evaluate((frame: string) => {
      (window as any).__winzappCallMediaBridge?.pushCameraFrame?.(frame);
    }, jpeg).catch(() => undefined).finally(() => { cameraBusy = false; });
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
    logger?.info?.(`[${session}] call camera stopped by desktop`);
    page.evaluate(() => {
      (window as any).__winzappCallMediaBridge?.stopCamera?.();
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
