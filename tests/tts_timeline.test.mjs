import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const { calculateProgress, mediaTimeToTimelineTime, timelineTimeToMediaTime } = require('../app/static/tts_timeline.js');

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
