import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const { createRequestQueue } = require('../app/static/tts_timeline.js');
const source = readFileSync(new URL('../app/templates/reader.html', import.meta.url), 'utf8');
const ebookSource = readFileSync(new URL('../app/templates/ebook_reader.html', import.meta.url), 'utf8');
const offlineSource = readFileSync(new URL('../app/static/offline.js', import.meta.url), 'utf8');
const readerStylesSource = readFileSync(new URL('../app/static/reader.css', import.meta.url), 'utf8');
const serviceWorkerSource = readFileSync(new URL('../service-worker.js', import.meta.url), 'utf8');
const baseSource = readFileSync(new URL('../app/templates/base.html', import.meta.url), 'utf8');
test('both complete Reader scripts have valid JavaScript syntax', () => {
  for (const template of [source,ebookSource]) {
    const scripts=[...template.matchAll(/<script>([\s\S]*?)<\/script>/g)];
    assert.ok(scripts.length);
    for (const [,script] of scripts) new vm.Script(script.replace(/\{%[\s\S]*?%\}/g,'').replace(/\{\{[\s\S]*?\}\}/g,'null'));
  }
});

test('Ebook reader defaults to sans-serif and initializes after dynamic navigation', () => {
  assert.match(ebookSource, /data-ebook-reader-root/);
  assert.match(ebookSource, /system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, Noto Sans, Arial, sans-serif/);
  assert.match(ebookSource, /document\.readyState === 'loading'/);
  assert.match(ebookSource, /document\.addEventListener\('htmx:afterSwap', initEbookReader\)/);
});

test('offline novel chapters use the cached original Reader instead of a separate renderer', () => {
  assert.doesNotMatch(offlineSource, /renderOfflineReader/);
  assert.match(offlineSource, /novel-reader-pages-v1/);
  assert.match(offlineSource, /Could not cache original Reader page for offline use/);
  assert.match(serviceWorkerSource, /const READER_PAGE_CACHE = "novel-reader-pages-v1"/);
  assert.match(serviceWorkerSource, /cache\.match\(request, \{ ignoreSearch: true \}\)/);
  assert.match(serviceWorkerSource, /cache\.match\(READER_STYLES\)/);
  assert.doesNotMatch(serviceWorkerSource, /renderOfflineReader/);
  assert.match(source, /navigator\.onLine === false/);
});

test('offline ebook reader keeps its existing offline controls', () => {
  for (const marker of ['reader-header', 'reader-page-nav', 'reader-content', 'data-reader-player', 'reader-player-actions', 'reader-tts-active', 'data-play-icon="pause"', 'data-rate-control', 'data-sleep-button', 'reader-mobile-player-toggle', 'data-font-select', 'data-font-settings', 'data-toc-button', 'data-reader-theme-toggle', 'data-segment-prev', 'data-segment-next', 'data-volume-control', 'type:']) {
    assert.match(offlineSource, new RegExp(marker.replace(/[.*+?^${}()|[\\]\\]/g, '\\$&')));
  }
  assert.match(offlineSource, /data-sheet="toc"/);
  assert.match(offlineSource, /openSheet\("toc"\)/);
  assert.match(offlineSource, /markSegment/);
  assert.match(offlineSource, /data-voice/);
  assert.match(offlineSource, /data-sleep/);
  assert.match(offlineSource, /renderReader\(\{ book, chapter, entries, index, owner: account, ebook: true \}\)/);
  assert.match(readerStylesSource, /grid-template-columns:1fr auto 1fr auto/);
  assert.match(offlineSource, /root\.dataset\.ebookReaderRoot = ""/);
  assert.match(offlineSource, /header\.dataset\.ebookReaderHeader = ""/);
  assert.match(offlineSource, /player\.dataset\.ebookPlayer = ""/);
  assert.match(offlineSource, /reader-ebook-mode-bar/);
  assert.match(offlineSource, /site-nav-button accent/);
  assert.match(offlineSource, /reader-ebook-content-title/);
  assert.match(offlineSource, /reader-ebook-chapter-nav/);
  assert.match(offlineSource, /reader-ebook-player/);
  assert.match(offlineSource, /player\.querySelector\("\[data-mobile-player-toggle\]"\)\?\.remove\(\)/);
  assert.match(offlineSource, /if \(!ebook\) \{ player\.dataset\.mobileExpanded = "false"/);
  assert.match(offlineSource, /else \{\s*document\.querySelector\("\.reader-header-actions"\)\.insertAdjacentHTML[\s\S]*data-reader-settings-button/);
  assert.match(offlineSource, /reader-ebook-mode-bar/);
  assert.match(offlineSource, /data-follow-text/);
  assert.match(offlineSource, /data-highlight-text/);
  assert.match(offlineSource, /player\.dataset\.mobileExpanded = String\(ebook\)/);
  assert.match(offlineSource, /backdrop\.classList\.remove\("off-sheet"\)/);
  assert.match(readerStylesSource, /\.offline-reader-root\[data-ebook-reader-root\] \.reader-ebook-player\{position:fixed/);
  assert.match(readerStylesSource, /\.reader-ebook-player\.is-hidden\{opacity:0;transform:translate\(-50%,calc\(100% \+ 1\.25rem\)\)!important/);
  assert.match(readerStylesSource, /\.offline-reader-root \.reader-player-controls\{display:flex/);
  assert.match(readerStylesSource, /\.offline-reader-root \.reader-sheet-backdrop\{position:fixed/);
  assert.doesNotMatch(offlineSource, /\.offline-reader-root\{/);
  assert.match(offlineSource, /classList\.replace\("off-player-actions", "reader-player-actions"\)/);
  assert.match(offlineSource, /reader-page-nav \.reader-nav-card\[aria-disabled=/);
  assert.match(source, /href="\/static\/reader\.css\?v=3"/);
  assert.match(readerStylesSource, /grid-template-columns:minmax\(0,1fr\) auto minmax\(0,1fr\)/);
  assert.match(readerStylesSource, /\.reader-player-controls \.reader-player-actions\{grid-column:1\/-1;grid-row:2/);
});

test('offline reader asset and service worker cache versions stay in sync', () => {
  const assetVersion = baseSource.match(/offline\.js\?v=(\d+)/)?.[1];
  const cacheVersion = serviceWorkerSource.match(/novel-reader-static-v(\d+)/)?.[1];
  assert.ok(assetVersion && cacheVersion);
  assert.equal(assetVersion, cacheVersion);
  assert.match(serviceWorkerSource, new RegExp(`const OFFLINE_SCRIPT = "/static/offline\\.js\\?v=${assetVersion}"`));
  assert.match(serviceWorkerSource, /const STATIC = \[OFFLINE_SCRIPT,/);
  assert.match(serviceWorkerSource, /const READER_STYLES = "\/static\/reader\.css\?v=3"/);
  assert.match(serviceWorkerSource, /STATIC = \[OFFLINE_SCRIPT, READER_STYLES,/);
  assert.match(serviceWorkerSource, /<link rel="stylesheet" href="\$\{READER_STYLES\}">/);
  assert.match(serviceWorkerSource, /cache\.match\(OFFLINE_SCRIPT\)/);
  assert.match(serviceWorkerSource, /<script src="\$\{OFFLINE_SCRIPT\}"><\/script>/);
  new vm.Script(offlineSource);
  new vm.Script(serviceWorkerSource);
});

test('Service Worker returns the exact cached online Reader HTML while offline', async () => {
  const pageCache = new Map(), handlers = new Map();
  const keyOf = value => new URL(typeof value === 'string' || value instanceof URL ? value : value.url).href;
  const caches = { open: async () => ({
    addAll: async () => {},
    put: async (request, response) => pageCache.set(keyOf(request), response.clone()),
    match: async (request, options = {}) => {
      const key = keyOf(request);
      if (pageCache.has(key)) return pageCache.get(key).clone();
      if (options.ignoreSearch) {
        const path = new URL(key).pathname;
        for (const [cachedKey, response] of pageCache) if (new URL(cachedKey).pathname === path) return response.clone();
      }
      return undefined;
    },
  }), keys: async () => [] };
  const self = { location: { origin: 'https://leotruyen.vercel.app' }, addEventListener: (name, handler) => handlers.set(name, handler), skipWaiting: async () => {} };
  const onlineHtml = '<html><body><main data-reader-content>Original Reader UI</main><div data-reader-player></div></body></html>';
  let networkFetch = async () => new Response(onlineHtml, { status: 200, headers: { 'Content-Type': 'text/html' } });
  const context = vm.createContext({ self, caches, URL, Response, console, clients: {}, fetch: (...args) => networkFetch(...args) });
  vm.runInContext(serviceWorkerSource, context);
  const navigate = async url => {
    let result;
    handlers.get('fetch')({ request: { url, method: 'GET', mode: 'navigate', destination: 'document' }, respondWith: promise => { result = promise; } });
    return result;
  };

  await (await navigate('https://leotruyen.vercel.app/truyen/kiem-lai/1')).text();
  networkFetch = async () => { throw new TypeError('offline'); };
  const response = await navigate('https://leotruyen.vercel.app/truyen/kiem-lai/1?mode=translated');
  assert.equal(await response.text(), onlineHtml);
});

test('desktop Reader arrow keys navigate chapters without hijacking form controls', () => {
  const start = source.indexOf('const handleReaderKeyboardNavigation =');
  const end = source.indexOf('const readerHeader =', start);
  assert.ok(start >= 0 && end > start);
  const navigated = [];
  class FakeElement {
    matches(selector) { return this.kind === 'input' && selector.startsWith('input'); }
    closest() { return null; }
  }
  const context = vm.createContext({
    Element: FakeElement,
    player: { dataset: { previousUrl: '/previous', nextUrl: '/next' } },
    window: { matchMedia: () => ({ matches: true }), location: { assign: url => navigated.push(url) } },
    document: { addEventListener() {} },
  });
  vm.runInContext(`${source.slice(start, end)}; globalThis.run = handleReaderKeyboardNavigation;`, context);
  const event = key => ({ key, target: {}, defaultPrevented: false, preventDefault() { this.prevented = true; } });

  const previous = event('ArrowLeft');
  context.run(previous);
  const next = event('ArrowRight');
  context.run(next);
  const input = new FakeElement();
  input.kind = 'input';
  const fromInput = event('ArrowRight');
  fromInput.target = input;
  context.run(fromInput);

  assert.deepEqual(navigated, ['/previous', '/next']);
  assert.equal(previous.prevented, true);
  assert.equal(next.prevented, true);
  assert.equal(fromInput.prevented, undefined);
});

function loadFunction(name, nextName, bindings, templateSource = source) {
  const start = templateSource.indexOf(`const ${name} =`);
  const end = templateSource.indexOf(`const ${nextName} =`, start);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext(bindings);
  vm.runInContext(`${templateSource.slice(start, end)}; globalThis.run = ${name};`, context);
  return context;
}

function segmentContext(response) {
  let requests = 0;
  const context = loadFunction('ensureSegment', 'preloadAhead', {
    playlist: [{url:'/audio/segment'}], segmentRequests: new Map(), ttsRequestQueue:createRequestQueue(2),
    fetch: async () => { requests++; return response; },
    URL: {createObjectURL: () => 'blob:complete'},
  });
  return {context, requests: () => requests};
}

test('segment fetch preserves provider error details and permits a retry', async () => {
  const {context, requests} = segmentContext({ok:false, status:503, headers:new Map(), json:async()=>({detail:'Nam Minh returned no audio'})});
  await assert.rejects(context.run(0), /Nam Minh returned no audio/);
  assert.equal(context.playlist[0].status, 'error');
  assert.equal(context.segmentRequests.size, 0);
  await assert.rejects(context.run(0));
  assert.equal(requests(), 2);
});

test('empty audio is rejected instead of entering ready state', async () => {
  const {context} = segmentContext({ok:true, status:200, headers:new Map([['content-type','audio/mpeg']]), blob:async()=>({size:0})});
  await assert.rejects(context.run(0));
  assert.equal(context.playlist[0].objectUrl, undefined);
  assert.equal(context.playlist[0].status, 'error');
});

test('simultaneous requests for the same segment share one complete blob', async () => {
  const {context, requests} = segmentContext({ok:true, status:200, headers:new Map([['content-type','audio/mpeg']]), blob:async()=>({size:100})});
  const first = context.run(0), second = context.run(0);
  assert.equal(first, second);
  await first;
  assert.equal(context.playlist[0].objectUrl, 'blob:complete');
  assert.equal(requests(), 1);
});

test('changing voice cannot reuse an in-flight segment from the previous voice', async () => {
  const pending=[];
  const context=loadFunction('ensureSegment','preloadAhead', {
    playlist:[{url:'/audio/0?voice=nam-minh'}],segmentRequests:new Map(),ttsRequestQueue:createRequestQueue(2),
    fetch:url=>new Promise(resolve=>pending.push({url,resolve})),
    URL:{createObjectURL:blob=>`blob:${blob.voice}`},
  });
  const first=context.run(0);
  context.playlist=[{url:'/audio/0?voice=hoai-my'}];
  const second=context.run(0);
  await Promise.resolve();
  assert.notEqual(first,second);
  assert.equal(pending.length,2);
  for (const item of pending) item.resolve({ok:true,headers:new Map([['content-type','audio/mpeg']]),blob:async()=>({size:100,voice:item.url})});
  const [oldSegment,newSegment]=await Promise.all([first,second]);
  assert.match(oldSegment.objectUrl,/nam-minh/);
  assert.match(newSegment.objectUrl,/hoai-my/);
});

test('a retry after cancelling queued audio is not erased by the cancelled request cleanup', async () => {
  const queue = createRequestQueue(1);
  let releaseBlocker, fetches = 0;
  const blocker = queue.run('blocker', () => new Promise(resolve => { releaseBlocker = resolve; }));
  await Promise.resolve();
  const context = loadFunction('ensureSegment', 'preloadAhead', {
    playlist:[{url:'/audio/retry'}],segmentRequests:new Map(),ttsRequestQueue:queue,
    fetch:async()=>{fetches++;return {ok:true,headers:new Map([['content-type','audio/mpeg']]),blob:async()=>({size:100})};},
    URL:{createObjectURL:()=> 'blob:retry'},
  });
  const cancelled = context.run(0);
  queue.cancelPending();
  context.segmentRequests.clear();
  const retry = context.run(0);
  await assert.rejects(cancelled, error => error.name === 'AbortError');
  releaseBlocker();
  await blocker;
  const segment = await retry;
  assert.equal(segment.objectUrl,'blob:retry');
  assert.equal(fetches,1);
  assert.equal(context.segmentRequests.size,0);
});

test('loading the Reader warms the current online TTS segment without starting playback', async () => {
  const ensured = [], selected = [];
  const context = loadFunction('loadPlaylist', 'recoverStream', {
    hasAudio:true, playlistLoadToken:0, stopFallbackSpeech(){}, playlistReady:false,
    voiceSessions:new Map(), voiceSessionKey:()=> 'voice', playlist:[], segmentStarts:[], totalDuration:0,
    durationLabel:{textContent:''}, fetch:async()=>({ok:true,json:async()=>({segments:[{url:'/audio/0',duration:5}],total_duration:5})}),
    buildManifestUrl:()=>'/manifest', findSegmentIndex:()=>0, mapParagraphsToSegments(){},
    streamingFirstSegment:false, streamingSegmentIndexKnown:false, streamingSegmentIndex:0,
    streamingTextOffset:0, currentSegmentIndex:0, pendingSegmentOffset:0, audio:{dataset:{}},
    playerState:'idle', highlightSegment(){}, formatTime:()=> '00:05', wantToPlay:false,
    ensureSegment:index=>{ensured.push(index);return Promise.resolve({objectUrl:'blob:0'});},
    setAudioSegment:(...args)=>selected.push(args), console:{warn(){}},
  });

  await context.run(0,false,false);
  assert.deepEqual(ensured,[0]);
  assert.equal(context.playlistReady,true);
  assert.deepEqual(selected,[]);
});

test('a cached playlist also warms the resume segment when playback is not selected', async () => {
  const ensured = [];
  const playlist = [{url:'/audio/0',duration:5,objectUrl:null}];
  const context = loadFunction('loadPlaylist', 'recoverStream', {
    hasAudio:true, playlistLoadToken:0, stopFallbackSpeech(){}, playlistReady:false,
    voiceSessions:new Map([['voice',{playlist,segmentStarts:[0],totalDuration:5}]]),
    voiceSessionKey:()=> 'voice', playlist:[], segmentStarts:[], totalDuration:0,
    durationLabel:{textContent:''}, formatTime:()=> '00:05', findSegmentIndex:()=>0,
    ensureSegment:index=>{ensured.push(index);return Promise.resolve(playlist[index]);},
    setAudioSegment:()=>assert.fail('warm-up must not start playback'), console:{warn(){}},
  });

  await context.run(0,false,false);
  assert.deepEqual(ensured,[0]);
  assert.equal(context.playlistReady,true);
});

test('Play reuses the current segment warm-up request instead of starting a second stream', async () => {
  let resolveAudio, ensured = 0, streamed = 0, selected;
  const pendingAudio = new Promise(resolve=>{resolveAudio=resolve;});
  const context = loadFunction('playServerCurrent', 'browserQueue', {
    segmentRequestToken:0, playerState:'idle', getCurrentPosition:()=>0, wantToPlay:false,
    document:{querySelector:()=>null}, setPlayerState(){}, playlistReady:true,
    playlist:[{url:'/audio/0',objectUrl:null}], currentSegmentIndex:0, segmentStarts:[0], pendingSegmentOffset:0,
    segmentRequests:new Map([['/audio/0',pendingAudio]]), mapParagraphsToSegments(){},
    findSegmentForTextOffset:()=>0, visibleTextOffset:()=>0,
    ensureSegment:()=>{ensured++;return pendingAudio;},
    playFirstSegmentFromStream:()=>{streamed++;}, setAudioSegment:(...args)=>{selected=args;},
    audio:{src:'',error:null}, console:{warn(){}},
  });

  context.run();
  await Promise.resolve();
  assert.equal(ensured,1);
  assert.equal(streamed,0);
  resolveAudio({objectUrl:'blob:0'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(selected,[0,0,true]);
  assert.equal(streamed,0);
});

test('Play falls back to the live stream when the prepared segment request fails', async () => {
  let streamed;
  const context = loadFunction('playServerCurrent', 'browserQueue', {
    segmentRequestToken:0, playerState:'idle', getCurrentPosition:()=>0, wantToPlay:false,
    document:{querySelector:()=>null}, setPlayerState(){}, playlistReady:true,
    playlist:[{url:'/audio/0',objectUrl:null}], currentSegmentIndex:0, segmentStarts:[0], pendingSegmentOffset:0,
    segmentRequests:new Map([['/audio/0',new Promise(()=>{})]]),
    mapParagraphsToSegments(){}, findSegmentForTextOffset:()=>0, visibleTextOffset:()=>0,
    ensureSegment:()=>Promise.reject(new Error('segment unavailable')),
    playFirstSegmentFromStream:(...args)=>{streamed=args;}, setAudioSegment(){},
    audio:{src:'',error:null}, console:{warn(){}},
  });

  context.run();
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(streamed,[0,0,1]);
});

test('media error and rejected play recover the stream only once', async () => {
  let resolveManifest, requests = 0, played = 0;
  const context = loadFunction('recoverStream', 'playFirstSegmentFromStream', {
    streamRecovery:null, streamingFirstSegment:true, segmentRequestToken:1, wantToPlay:true,
    playlistReady:true, playlistLoading:new Promise(resolve=>{resolveManifest=resolve;}),
    findSegmentForTextOffset:()=>0, ensureSegment:async()=>{requests++;},
    setAudioSegment:()=>{played++;}, reportTtsError:error=>{throw error;},
  });
  const first = context.run(0,0,1), second = context.run(0,0,1);
  assert.equal(first, second);
  resolveManifest();
  await first;
  assert.equal(requests, 1);
  assert.equal(played, 1);
});

test('a cancelled stream cannot restart audio after switching voice', async () => {
  let resolveManifest, requests = 0;
  const context = loadFunction('recoverStream', 'playFirstSegmentFromStream', {
    streamRecovery:null, streamingFirstSegment:true, segmentRequestToken:1, wantToPlay:true,
    playlistReady:true, playlistLoading:new Promise(resolve=>{resolveManifest=resolve;}),
    ensureSegment:async()=>{requests++;}, reportTtsError:error=>{throw error;},
  });
  const recovery = context.run(0,0,1);
  context.segmentRequestToken = 2;
  resolveManifest();
  await recovery;
  assert.equal(requests, 0);
});

test('an old Play promise cannot pause the new voice stream', async () => {
  let resolvePlay, pauses = 0;
  const context = loadFunction('playFirstSegmentFromStream','playServerCurrent', {
    segmentRequestToken:1, wantToPlay:true, audio:{dataset:{},pause:()=>{pauses++;},load(){},play:()=>new Promise(resolve=>{resolvePlay=resolve;})},
    buildStreamUrl:()=>'/audio/stream', setPlayerState:()=>assert.fail('stale playback changed state'),
  });
  const pending=context.run(0,0,1);
  context.segmentRequestToken=2;
  resolvePlay();
  await pending;
  assert.equal(pauses,1);
});

test('autoplay denial does not start unnecessary server regeneration', async () => {
  let errors = 0;
  const context = loadFunction('playFirstSegmentFromStream','playServerCurrent', {
    segmentRequestToken:1,wantToPlay:true,
    audio:{dataset:{},pause(){},load(){},play:()=>Promise.reject({name:'NotAllowedError'})},
    buildStreamUrl:()=>'/audio/stream',reportTtsError:()=>{errors++;},recoverStream:()=>assert.fail('unexpected synthesis retry'),
  });
  await context.run(0,0,1);
  assert.equal(errors,1);
});

test('browser cancellation is ignored and autoplay denial asks for a Play tap', () => {
  const states = [];
  const context = loadFunction('reportTtsError', 'buildManifestUrl', {
    wantToPlay:true, deviceStatus:{}, playerStatus:{}, document:{querySelector:()=>null}, console:{warn(){}}, setPlayerState:state=>states.push(state),
  });
  context.run({name:'AbortError'});
  assert.equal(context.wantToPlay, true);
  assert.equal(states.length, 0);
  context.run({name:'NotAllowedError'});
  assert.equal(context.wantToPlay, false);
  assert.deepEqual(states, ['paused']);
  assert.match(context.deviceStatus.textContent, /Play/);
});

test('Reader and Ebook offer only the actual Vietnamese Google Chrome voice', () => {
  for (const [templateSource, firstName] of [[source, 'getChromeOnlineVoice'], [ebookSource, 'updateChromeVoiceOption']]) {
    let button, selected = null;
    const container = {querySelector:()=>button, append:value=>{button=value;}};
    const context = loadFunction(firstName,'populateDeviceVoices', {
      document:{querySelector:()=>container, createElement:()=>({dataset:{}})},
      navigator:{onLine:true},
    }, templateSource);
    if (firstName === 'getChromeOnlineVoice') vm.runInContext('globalThis.run = updateChromeVoiceOption', context);
    context.run([{name:'Google US English',lang:'en-US',voiceURI:'google-en'}], voice=>{selected=voice;});
    assert.equal(button.hidden, true);
    const google = {name:'Google Tiếng Việt',lang:'vi-VN',voiceURI:'Google Vietnamese',localService:false};
    context.run([google], voice=>{selected=voice;});
    assert.equal(button.hidden, false);
    button.onclick();
    assert.equal(selected.voiceURI, 'Google Vietnamese');
    context.run([],()=>{});
    assert.equal(button.hidden, true);
    assert.equal(button.onclick, null);
  }
});

test('Reader Chrome Online prefers the available Vietnamese 2 Natural network voice', () => {
  const context = loadFunction('getChromeOnlineVoice', 'updateChromeVoiceOption', {});
  const voices = [
    {name:'Google Tiếng Việt 2 (Natural)',lang:'vi-VN',voiceURI:'local-2',localService:true},
    {name:'Google Tiếng Việt',lang:'vi-VN',voiceURI:'remote-1',localService:false},
    {name:'Google Tiếng Việt 2',lang:'vi-VN',voiceURI:'remote-2',localService:false},
    {name:'Google Tiếng Việt 2 (Natural)',lang:'vi-VN',voiceURI:'remote-natural',localService:false},
  ];
  assert.equal(context.run(voices).voiceURI, 'remote-natural');
  assert.equal(context.run(voices.slice(0, 3)).voiceURI, 'remote-2');
  assert.equal(context.run(voices.slice(0, 2)).voiceURI, 'remote-1');
  assert.equal(context.run(voices.slice(0, 1)), undefined);
});

test('Reader defaults to Chrome browser TTS only on online Chrome mobile without a saved choice', () => {
  const start = source.indexOf('const storedTtsMode =');
  const end = source.indexOf('let sleepMode =', start);
  assert.ok(start >= 0 && end > start);
  const choose = (userAgent, online, saved) => {
    const context = vm.createContext({storage:{getItem:()=>saved}, navigator:{userAgent,onLine:online}, window:{NovelTTS:{getInitialTtsMode:()=>online?'server':'device'}}});
    vm.runInContext(`${source.slice(start, end)}; globalThis.mode = selectedTtsMode;`, context);
    return context.mode;
  };
  assert.equal(choose('Android Mobile Chrome/130.0', true, null), 'device');
  assert.equal(choose('Android Mobile Chrome/130.0', true, 'server'), 'server');
  assert.equal(choose('Android Mobile EdgA/130.0 Chrome/130.0', true, null), 'server');
  assert.equal(choose('Android Mobile Chrome/130.0', false, null), 'device');
});

test('browser TTS chooses Nam Minh on Edge and Google Vietnamese 2 on Chrome', () => {
  const source = readFileSync(new URL('../app/static/offline.js', import.meta.url), 'utf8');
  const start = source.indexOf('const getPreferredDeviceVoice =');
  const end = source.indexOf('const createDeviceProvider =', start);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext({navigator:{userAgent:''}});
  vm.runInContext(`${source.slice(start, end)}; globalThis.choose = getPreferredDeviceVoice;`, context);
  const voices = [
    {name:'Microsoft An',lang:'vi-VN',voiceURI:'vi-an'},
    {name:'Microsoft NamMinh Online (Natural)',lang:'vi-VN',voiceURI:'vi-namminh'},
    {name:'Google Tiếng Việt',lang:'vi-VN',voiceURI:'google-vi'},
    {name:'Google Tiếng Việt 2',lang:'vi-VN',voiceURI:'google-vi-2'},
  ];
  assert.equal(context.choose(voices, 'Mozilla/5.0 Edg/130.0').voiceURI, 'vi-namminh');
  assert.equal(context.choose(voices, 'Mozilla/5.0 Chrome/130.0').voiceURI, 'google-vi-2');
  assert.equal(context.choose(voices.slice(0, 1), 'Mozilla/5.0 Edg/130.0').voiceURI, 'vi-an');
});

test('device TTS keeps chunks short and preserves sentence boundaries', () => {
  const source = readFileSync(new URL('../app/static/offline.js', import.meta.url), 'utf8');
  const start = source.indexOf('const splitDeviceText =');
  const end = source.indexOf('const createDeviceProvider =', start);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext({});
  vm.runInContext(`${source.slice(start, end)}; globalThis.split = splitDeviceText;`, context);
  const chunks = context.split('Câu một '.repeat(34) + '. Câu hai kết thúc! ' + 'từ '.repeat(220));
  assert.ok(chunks.length > 1);
  assert.ok(chunks.every(chunk => chunk.length <= 280));
  assert.match(chunks[0], /\.$/);
  assert.match(chunks.find(chunk => chunk.includes('Câu hai')), /Câu hai kết thúc!/);
});

test('device TTS does not cancel between sequential chunks', () => {
  const source = readFileSync(new URL('../app/static/offline.js', import.meta.url), 'utf8');
  const start = source.indexOf('const createDeviceProvider =');
  const end = source.indexOf('const createManager =', start);
  const calls = [];
  const synth = {
    getVoices: () => [{ name: 'Mock Vietnamese', lang: 'vi-VN', voiceURI: 'mock', localService: true }],
    cancel: () => calls.push('cancel'), pause() {}, resume() {}, speak: utterance => calls.push(`speak:${utterance.text}`),
  };
  const Utterance = class { constructor(text) { this.text = text; } };
  const context = vm.createContext({ window: { speechSynthesis: synth, SpeechSynthesisUtterance: Utterance }, speechSynthesis: synth, SpeechSynthesisUtterance: Utterance });
  vm.runInContext(`${source.slice(start, end)}; globalThis.make = createDeviceProvider;`, context);
  const provider = context.make();
  provider.speak('first', { voiceURI: 'mock' });
  provider.speak('second', { voiceURI: 'mock', replaceCurrent: false });
  assert.deepEqual(calls, ['cancel', 'speak:first', 'speak:second']);
  provider.stop();
  assert.equal(calls.at(-1), 'cancel');
});

test('mobile online defaults to background-capable audio while desktop preference is preserved', () => {
  const source = readFileSync(new URL('../app/static/offline.js', import.meta.url), 'utf8');
  const start = source.indexOf('const getInitialTtsMode =');
  const end = source.indexOf('const getPreferredDeviceVoice =', start);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext({navigator:{userAgent:'',onLine:true}});
  vm.runInContext(`${source.slice(start, end)}; globalThis.choose = getInitialTtsMode;`, context);
  assert.equal(context.choose('device', 'Mozilla/5.0 Android Mobile', true), 'server');
  assert.equal(context.choose('device', 'Mozilla/5.0 Windows Chrome', true), 'device');
  assert.equal(context.choose('server', 'Mozilla/5.0 Android Mobile', false), 'device');
});

test('unsupported Media Session APIs do not interrupt Reader player setup', () => {
  const source = readFileSync(new URL('../app/static/offline.js', import.meta.url), 'utf8');
  const start = source.indexOf('const setMediaSessionState =');
  const end = source.indexOf('const getInitialTtsMode =', start);
  assert.ok(start >= 0 && end > start);
  const warnings = [];
  const session = {setActionHandler(){throw new DOMException('Unsupported', 'NotSupportedError');}};
  Object.defineProperty(session, 'playbackState', {set(){throw new DOMException('Unsupported', 'InvalidStateError');}});
  const context = vm.createContext({navigator:{mediaSession:session},window:{MediaMetadata:class {constructor(value){Object.assign(this,value);}}},console:{warn:(...args)=>warnings.push(args)}});
  vm.runInContext(`${source.slice(start, end)}; globalThis.configure = configureMediaSession; globalThis.setState = setMediaSessionState;`, context);
  assert.doesNotThrow(() => context.configure('Chapter', {play(){}, pause(){}}));
  assert.doesNotThrow(() => context.setState('playing'));
  assert.equal(warnings.length, 3);
});

test('Reader controls tolerate a stale cached TTS bridge and keep mobile toggle off desktop', () => {
  const stateStart = source.indexOf('window.NovelTTS?.setMediaSessionState');
  const stateEnd = source.indexOf(';', stateStart) + 1;
  const configureStart = source.indexOf('window.NovelTTS?.configureMediaSession');
  const configureEnd = source.indexOf('\n      });', configureStart) + '\n      });'.length;
  assert.ok(stateStart >= 0 && stateEnd > stateStart);
  assert.ok(configureStart >= 0 && configureEnd > configureStart);
  const context = vm.createContext({window:{NovelTTS:{}}, document:{title:'Reader'}, state:'idle', wantToPlay:false});
  assert.doesNotThrow(() => vm.runInContext(`${source.slice(stateStart,stateEnd)}\n${source.slice(configureStart,configureEnd)}\nglobalThis.readerControlsContinue=true;`, context));
  assert.equal(context.readerControlsContinue, true);

  assert.match(readerStylesSource, /@media\(max-width:639px\)\s*\{\s*\.reader-mobile-player-toggle\{position:absolute/);
  assert.match(source, /window\.NovelTTS\?\.getInitialTtsMode\?\.\(/);
  assert.equal((source.match(/playButton\.addEventListener\("click", handlePlayButtonClick\)/g) || []).length, 1);
  assert.match(serviceWorkerSource, /const CACHE = "novel-reader-static-v19"/);
});

test('offline Reader stylesheet v1 falls back to the pre-cached reader.css v3', async () => {
  const handlers = new Map(), stylesheet = new Response('.reader-player-shell{background:#fff!important}', { status: 200 });
  const caches = { open: async () => ({
    match: async request => new URL(typeof request === 'string' ? request : request.url, 'https://leotruyen.vercel.app').search === '?v=3' ? stylesheet.clone() : undefined,
    put: async () => {},
  }) };
  const self = { location: { origin: 'https://leotruyen.vercel.app' }, addEventListener: (name, handler) => handlers.set(name, handler) };
  const context = vm.createContext({ self, caches, URL, Response, console, clients: {}, fetch: async () => { throw new TypeError('offline'); } });
  vm.runInContext(serviceWorkerSource, context);
  let result;
  handlers.get('fetch')({ request: { url: 'https://leotruyen.vercel.app/static/reader.css?v=1', method: 'GET', mode: 'cors', destination: 'style' }, respondWith: promise => { result = promise; } });
  assert.equal(await (await result).text(), '.reader-player-shell{background:#fff!important}');
});

test('a new browser voice starts speech rather than resuming an absent utterance', () => {
  let spoken = 0, resumed = 0;
  const node = {getBoundingClientRect:()=>({bottom:1000})};
  const context = loadFunction('playCurrent','updateChromeVoiceOption', {
    ttsManager:{mode:'device',resume:()=>{resumed++;}}, playerState:'paused', devicePaused:false,
    deviceProvider:{isSupported:()=>true}, browserQueue(){}, browserChunks:[{text:'Hello',node}],
    storage:{getItem:()=>null}, readerChapterKey:'1', readerContent:{querySelectorAll:()=>[node]},
    innerHeight:800, speakBrowserChunk:()=>{spoken++;},
  });
  context.run();
  assert.equal(spoken, 1);
  assert.equal(resumed, 0);
});

test('all chunks inside a long paragraph can highlight that paragraph', () => {
  const highlighted = [];
  const node = {dataset:{ttsStart:'0',ttsEnd:'2'},classList:{add:()=>highlighted.push('paragraph'),remove(){}},getBoundingClientRect:()=>({top:100,bottom:200})};
  const start = source.indexOf('const highlightSegment =');
  const end = source.indexOf('\n      };',start) + '\n      };'.length;
  const context = vm.createContext({document:{querySelectorAll:selector=>selector==='[data-tts-start]'?[node]:[]}, highlightText:{checked:true},followText:{checked:false},window:{innerHeight:800}});
  vm.runInContext(`${source.slice(start,end)}; globalThis.run=highlightSegment;`,context);
  context.run(0); context.run(1); context.run(2); context.run(3);
  assert.deepEqual(highlighted,['paragraph','paragraph','paragraph']);
});

test('Ebook playback reuses the next-chunk preload instead of synthesizing twice', async () => {
  let requests = 0;
  const context = loadFunction('prepareSegment','fetchSegment', {
    selectedVoice:'vi-VN-NamMinhNeural',rate:1,
    fetch:async()=>{requests++;return {ok:true,headers:new Map([['content-type','audio/mpeg']]),blob:async()=>({size:100})};},
    URL:{createObjectURL:()=> 'blob:complete'},
  },ebookSource);
  const segment = {text:'A chunk'};
  const preload=context.run(segment), playback=context.run(segment);
  assert.equal(preload,playback);
  assert.equal(await playback,'blob:complete');
  assert.equal(await context.run(segment),'blob:complete');
  assert.equal(requests,1);
});

test('Ebook discards a preload returned after its voice changed', async () => {
  let resolve;
  const context = loadFunction('prepareSegment','fetchSegment', {
    selectedVoice:'vi-VN-NamMinhNeural',rate:1,fetch:()=>new Promise(done=>{resolve=done;}),
    URL:{createObjectURL:()=> 'blob:stale'},
  },ebookSource);
  const segment={text:'A chunk'};
  const pending=context.run(segment);
  context.selectedVoice='vi-VN-HoaiMyNeural';
  resolve({ok:true,headers:new Map([['content-type','audio/mpeg']]),blob:async()=>({size:100})});
  await assert.rejects(pending,error=>error.name==='AbortError');
  assert.equal(segment.url,undefined);
  assert.equal(segment.request,null);
});

test('reader player state waits for real audio playing event', () => {
  assert.doesNotMatch(source, /audio\.addEventListener\("play",\s*\(\)\s*=>\s*\{[^}]*setPlayerState\("playing"\)/s);
  assert.match(source, /audio\.addEventListener\("playing",\s*\(\)\s*=>\s*\{\s*if \(selectedTtsMode !== "server" \|\| !wantToPlay\) return;\s*setPlayerState\("playing"\)/);
});

test('online highlight follows the segment reported by audio progress', () => {
  const start = source.indexOf('const syncOnlineHighlight =');
  const end = source.indexOf('const updateSegmentButtons =', start);
  assert.ok(start >= 0 && end > start);
  const highlighted = [];
  const context = loadFunction('syncOnlineHighlight', 'updateSegmentButtons', {
    selectedTtsMode: 'server', wantToPlay: true, playlist: [{}, {}], streamingFirstSegment: false,
    streamingSegmentIndex: 0, currentPlayingChunkIndex: 0, getCurrentPosition: () => 12,
    findSegmentIndex: () => 1, highlightSegment: index => highlighted.push(index),
  });
  context.run();
  assert.deepEqual(highlighted, [1]);
});
