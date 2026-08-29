import { ServerOptions } from './types/ServerOptions';

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
      '--disable-3d-apis',
      '--disable-webgl',
      '--disable-component-update',
      '--disable-speech-api',
      '--disable-voice-input',
      '--disable-renderer-backgrounding',
      '--disable-backgrounding-occluded-windows',
      '--disable-features=OptimizationGuideOnDeviceModel,PromptAPIForGeminiNano,AISummarization,HelpMeWrite,OptimizationGuide,OptimizationHints,OptimizationTargetPrediction',
      '--disable-software-rasterizer',
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
