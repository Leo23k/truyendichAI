import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../app/templates/reader.html', import.meta.url), 'utf8');
const ebookSource = readFileSync(new URL('../app/templates/ebook_reader.html', import.meta.url), 'utf8');
test('both complete Reader scripts have valid JavaScript syntax', () => {
  for (const template of [source,ebookSource]) {
    const scripts=[...template.matchAll(/<script>([\s\S]*?)<\/script>/g)];
    assert.ok(scripts.length);
    for (const [,script] of scripts) new vm.Script(script.replace(/\{%[\s\S]*?%\}/g,'').replace(/\{\{[\s\S]*?\}\}/g,'null'));
  }
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
    playlist: [{url:'/audio/segment'}], segmentRequests: new Map(),
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
    playlist:[{url:'/audio/0?voice=nam-minh'}],segmentRequests:new Map(),
    fetch:url=>new Promise(resolve=>pending.push({url,resolve})),
    URL:{createObjectURL:blob=>`blob:${blob.voice}`},
  });
  const first=context.run(0);
  context.playlist=[{url:'/audio/0?voice=hoai-my'}];
  const second=context.run(0);
  assert.notEqual(first,second);
  assert.equal(pending.length,2);
  for (const item of pending) item.resolve({ok:true,headers:new Map([['content-type','audio/mpeg']]),blob:async()=>({size:100,voice:item.url})});
  const [oldSegment,newSegment]=await Promise.all([first,second]);
  assert.match(oldSegment.objectUrl,/nam-minh/);
  assert.match(newSegment.objectUrl,/hoai-my/);
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
  for (const templateSource of [source, ebookSource]) {
    let button, selected = null;
    const container = {querySelector:()=>button, append:value=>{button=value;}};
    const context = loadFunction('updateChromeVoiceOption','populateDeviceVoices', {
      document:{querySelector:()=>container, createElement:()=>({dataset:{}})},
    }, templateSource);
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
