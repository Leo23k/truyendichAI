import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const { calculateProgress, createRequestQueue, mediaTimeToTimelineTime, timelineTimeToMediaTime } = require('../app/static/tts_timeline.js');

test('maps actual audio time onto a stable estimated chapter timeline', () => {
  assert.equal(mediaTimeToTimelineTime(5, 10, 8), 4);
  assert.equal(mediaTimeToTimelineTime(10, 10, 8), 8);
});

test('converts timeline seek offsets back to actual media duration', () => {
  assert.equal(timelineTimeToMediaTime(4, 8, 10), 5);
  assert.equal(timelineTimeToMediaTime(12, 8, 10), 10);
  assert.equal(timelineTimeToMediaTime(4, 8, Number.NaN), 4);
});

test('progress advances monotonically across fixed segment boundaries', () => {
  const durations = [8, 12];
  const progress = [[0, 0], [0, 4], [0, 8], [1, 0], [1, 3]].map(([index, offset]) =>
    calculateProgress(durations, index, offset).value
  );
  assert.deepEqual(progress, [0, 200, 400, 400, 550]);
});

test('clamps invalid and out-of-range values without producing NaN', () => {
  assert.equal(mediaTimeToTimelineTime(99, 0, 8), 8);
  assert.deepEqual(calculateProgress([8], 0, 99), { current: 8, total: 8, value: 1000 });
  assert.deepEqual(calculateProgress([], 0, 0), { current: 0, total: 0, value: 0 });
});

test('request queue deduplicates, caps concurrency at two, and promotes current playback', async () => {
  const queue = createRequestQueue(2);
  const started = [];
  const resolvers = new Map();
  const task = key => () => new Promise(resolve => { started.push(key); resolvers.set(key, resolve); });
  const first = queue.run('first', task('first'));
  const second = queue.run('second', task('second'));
  const preload = queue.run('preload', task('preload'), 2);
  const later = queue.run('later', task('later'), 2);
  const playbackRequest = queue.run('preload', task('duplicate'), 0);
  assert.equal(preload, playbackRequest);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(started, ['first', 'second']);
  assert.equal(queue.activeCount, 2);
  assert.equal(queue.pendingCount, 2);
  resolvers.get('second')();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(started, ['first', 'second', 'preload']);
  resolvers.get('first')();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(started, ['first', 'second', 'preload', 'later']);
  resolvers.get('preload')(); resolvers.get('later')();
  await Promise.all([first, second, preload, later]);
  assert.equal(queue.activeCount, 0);
});

test('cancelPending rejects queued work without stopping active requests', async () => {
  const queue = createRequestQueue(1);
  let finishActive;
  let queuedStarted = false;
  const active = queue.run('active', () => new Promise(resolve => { finishActive = resolve; }));
  await Promise.resolve();
  const queued = queue.run('queued', () => { queuedStarted = true; });
  assert.equal(queue.cancelPending(), 1);
  await assert.rejects(queued, error => error.name === 'AbortError');
  assert.equal(queuedStarted, false);
  assert.equal(queue.activeCount, 1);
  finishActive();
  await active;
  assert.equal(queue.activeCount, 0);
});
