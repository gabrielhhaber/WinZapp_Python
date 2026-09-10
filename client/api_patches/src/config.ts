import { ServerOptions } from './types/ServerOptions';

// DO NOT MERGE UPSTREAM'S dotenv LOADING INTO THIS FILE.
//
// wppconnect-server 2.10.19 added `dotenv.config({ override: false })` here
// (commit b66881b, "load and document optional environment configuration"),
// and removed the dead `//require('dotenv').config();` from index.ts at the
// same time. Both are inert for WinZapp and one of them is actively unsafe, so
// this patch keeps overriding them — deliberately, not by accident.
//
// Inert, because this file is not a variant of upstream's: upstream resolves
// every setting from process.env (`env.SECRET_KEY || ...`, `env.PORT ||
// '21465'`, `envNumber('MAX_LISTENERS', 15)`), while WinZapp hardcodes them
// below and reads exactly ONE environment variable. There is no `env` object
// here for a .env file to feed.
//
// Unsafe, because that one variable is WINZAPP_USER_DATA_DIR, and it is the
// path to the Chrome profile that carries the WhatsApp login — computed per
// install and per account by main.py, and the single thing that must never
// come from a file lying next to the exe. dotenv's `override: false` protects
// a variable we DO inject; it does nothing about a stale value inherited by a
// process that was launched without one. Pointing Chrome at the wrong
// userDataDir is the session-loss failure mode, not a configuration nicety.
// WinZapp also deliberately stopped shipping a .env (see
// wpp_minimum_version.txt's own history), so installs upgraded from older
// builds can still have one on disk.
//
// Consequence to know about: upstream's src/config.test.ts (added in the same
// commit) asserts secretKey 'THISISMYSECURETOKEN' from a .env and port '21465'
// by default. Against this file all three of its cases fail, by design.
// Nothing in WinZapp runs jest — setup_api.py and build.py only ever run
// `npm run build` — and tsconfig.json's `src*` include does compile it, which
// type-checks fine. So it costs nothing today; it is written down here so a
// future rebase does not "fix" the failure by merging the dotenv call in.

// customUserDataDir is used as `customUserDataDir + session` (string
// concatenation, not path.join) to build each session's Puppeteer/Chrome
// profile directory — see createSessionUtil.ts. Left as the literal default
// './userDataDir/' it resolves relative to the Node process's own cwd, which
// is only a stable, persistent location in a --onedir build or dev mode; in
// --onefile it is PyInstaller's per-launch extraction temp dir, so the whole
// WhatsApp Web browser profile (and therefore the paired session) is
// silently orphaned every time the app closes. main.py sets
// WINZAPP_USER_DATA_DIR to an absolute, install-writable path (with the
// trailing separator this concatenation needs) before spawning Node; the
// './userDataDir/' fallback below is only for running this server outside
// WinZapp entirely.
const customUserDataDir = process.env.WINZAPP_USER_DATA_DIR || './userDataDir/';

export default {
  secretKey: 'THISISMYSECURETOKEN',
  host: 'http://localhost',
  port: '6300',
  deviceName: 'WppConnect',
  poweredBy: 'WPPConnect-Server',
  startAllSession: false,
  tokenStoreType: 'file',
  maxListeners: 15,
  customUserDataDir,
  webhook: {
    url: null,
    autoDownload: true,
    uploadS3: false,
    readMessage: true,
    allUnreadOnStart: false,
    listenAcks: true,
    onPresenceChanged: true,
    onParticipantsChanged: true,
    onReactionMessage: true,
    onPollResponse: true,
    onRevokedMessage: true,
    onLabelUpdated: true,
    onSelfMessage: false,
    ignore: ['status@broadcast'],
  },
  websocket: {
    autoDownload: false,
    uploadS3: false,
  },
  chatwoot: {
    sendQrCode: true,
    sendStatus: true,
  },
  archive: {
    enable: false,
    waitTime: 10,
    daysToArchive: 45,
  },
  log: {
    level: 'silly', // Before open a issue, change level to silly and retry a action
    logger: ['console', 'file'],
  },
  createOptions: {
    autoClose: 0,
    // autoClose alone does NOT mean "never close the page on us", and reading
    // it that way cost a real investigation. wppconnect has a second timer,
    // deviceSyncTimeout (default 180000), which waitForLogin() starts right
    // after a successful login — `startAutoClose(this.options.deviceSyncTimeout)`
    // in host.layer.js — and which closes the page through the same
    // tryAutoClose() path. It even logs under the same wording, so
    // wppconnect.log says "Auto close configured to 180s" on a session
    // configured with autoClose: 0, which reads like the setting was ignored.
    //
    // Three minutes is a plausible amount of time for a first sync on a busy
    // account to still be running, and killing the page mid-sync is the exact
    // opposite of what autoClose: 0 was set here to express. Pinned to 0 so
    // both halves of "WinZapp closes its own browser, nothing else does" are
    // actually stated. Nothing in WinZapp consumes the 'phoneNotConnected'
    // status this timer produces.
    deviceSyncTimeout: 0,
    // IMPORTANT: index.ts merges this with the config start.js passes to
    // initServer() using `merge-deep`, and merge-deep UNIONS arrays rather
    // than replacing them. A flag listed here therefore reaches Chrome no
    // matter what start.js does — start.js can only ever *add* flags, never
    // take one away. Removing a browser flag means removing it here as well.
    // (That is how '--disable-notifications' survived being deleted from
    // start.js: this list kept putting it back, the Notification API stayed
    // undefined, and WhatsApp Web still could not get a persistent storage
    // bucket. See the long comment in start.js for why that matters.)
    browserArgs: [
      '--disable-web-security',
      '--no-sandbox',
      '--aggressive-cache-discard',
      '--disable-cache',
      '--disable-application-cache',
      '--disable-offline-load-stale-cache',
      '--disk-cache-size=0',
      '--disable-background-networking',
      '--disable-default-apps',
      '--disable-extensions',
      '--disable-sync',
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--disable-translate',
      '--hide-scrollbars',
      '--metrics-recording-only',
      '--mute-audio',
      '--no-first-run',
      '--safebrowsing-disable-auto-update',
      '--ignore-certificate-errors',
      '--ignore-ssl-errors',
      '--ignore-certificate-errors-spki-list',
      '--disable-component-update',
      '--disable-speech-api',
      '--disable-voice-input',
      '--disable-renderer-backgrounding',
      '--disable-backgrounding-occluded-windows',
      '--disable-features=OptimizationGuideOnDeviceModel,PromptAPIForGeminiNano,AISummarization,HelpMeWrite,OptimizationGuide,OptimizationHints,OptimizationTargetPrediction',
      // '--disable-software-rasterizer', '--disable-3d-apis' and
      // '--disable-webgl' used to sit in this list and have been removed, not
      // moved: stacked on top of '--disable-gpu' they leave the renderer with
      // no rasterization backend at all, and every video send failed with
      // "MediaUnsupportedError: video loaded with duration but no dims"
      // because reading videoWidth/videoHeight needs one frame actually
      // decoded. start.js documents that at length and dropped them from its
      // own list — but per the note at the top of this array, merge-deep
      // UNIONS the two, so deleting a flag there while it stayed here changed
      // nothing whatsoever. The fix was only ever half-applied; this is the
      // other half. Do not re-add them here.
      '--disable-ipc-flooding-protection',
      '--password-store=basic',
      '--use-mock-keychain',
      '--no-pings',
      '--disable-client-side-phishing-detection',
    ],
    /**
     * Example of configuring the linkPreview generator
     * If you set this to 'null', it will use global servers; however, you have the option to define your own server
     * Clone the repository https://github.com/wppconnect-team/wa-js-api-server and host it on your server with ssl
     *
     * Configure the attribute as follows:
     * linkPreviewApiServers: [ 'https://www.yourserver.com/wa-js-api-server' ]
     */
    linkPreviewApiServers: null,

    /**
     * Set specific whatsapp version
     */
    // whatsappVersion: '2.xxxxx',
  },
  mapper: {
    enable: false,
    prefix: 'tagone-',
  },
  db: {
    mongodbDatabase: 'tokens',
    mongodbCollection: '',
    mongodbUser: '',
    mongodbPassword: '',
    mongodbHost: '',
    mongoIsRemote: true,
    mongoURLRemote: '',
    mongodbPort: 27017,
    redisHost: 'localhost',
    redisPort: 6379,
    redisPassword: '',
    redisDb: 0,
    redisPrefix: 'docker',
  },
  aws_s3: {
    region: 'sa-east-1',
    access_key_id: null,
    secret_key: null,
    defaultBucketName: null,
    endpoint: null,
    forcePathStyle: null,
  },
} as unknown as ServerOptions;
