(function (root, factory) {
  const timeline = factory();
  if (typeof module === "object" && module.exports) module.exports = timeline;
  else root.NovelTTSTimeline = timeline;
})(globalThis, function () {
  const clamp = (value, max) => Math.max(0, Math.min(Number(max) || 0, Number(value) || 0));

  const mediaTimeToTimelineTime = (mediaTime, mediaDuration, timelineDuration) => {
    const targetDuration = Math.max(0, Number(timelineDuration) || 0);
    const sourceDuration = Number(mediaDuration);
    const sourceTime = Math.max(0, Number(mediaTime) || 0);
    if (!targetDuration) return 0;
    if (!Number.isFinite(sourceDuration) || sourceDuration <= 0) return clamp(sourceTime, targetDuration);
    return clamp((sourceTime / sourceDuration) * targetDuration, targetDuration);
  };

  const timelineTimeToMediaTime = (timelineTime, timelineDuration, mediaDuration) => {
    const sourceDuration = Math.max(0, Number(timelineDuration) || 0);
    const targetDuration = Number(mediaDuration);
    const sourceTime = Math.max(0, Number(timelineTime) || 0);
    if (!Number.isFinite(targetDuration) || targetDuration <= 0) return clamp(sourceTime, sourceDuration);
    if (!sourceDuration) return 0;
    return clamp((sourceTime / sourceDuration) * targetDuration, targetDuration);
  };

  const calculateProgress = (durations, index, offset) => {
    const normalized = (Array.isArray(durations) ? durations : []).map(value => Math.max(0, Number(value) || 0));
    const total = normalized.reduce((sum, duration) => sum + duration, 0);
    const activeIndex = Math.max(0, Math.min(normalized.length - 1, Math.trunc(Number(index) || 0)));
    const current = normalized.slice(0, activeIndex).reduce((sum, duration) => sum + duration, 0)
      + clamp(offset, normalized[activeIndex] || 0);
    return { current: clamp(current, total), total, value: total ? Math.round(clamp(current, total) / total * 1000) : 0 };
  };

  const createRequestQueue = (concurrency = 2) => {
    const limit = Math.max(1, Math.trunc(Number(concurrency) || 2));
    const pending = [];
    const requests = new Map();
    let active = 0;
    let sequence = 0;

    const orderPending = () => pending.sort((left, right) => left.priority - right.priority || left.sequence - right.sequence);
    const pump = () => {
      while (active < limit && pending.length) {
        const request = pending.shift();
        if (requests.get(request.key) !== request) continue;
        request.pending = false;
        active += 1;
        const finish = () => {
          active -= 1;
          if (requests.get(request.key) === request) requests.delete(request.key);
          pump();
        };
        Promise.resolve()
          .then(request.task)
          .then(value => { finish(); request.resolve(value); }, error => { finish(); request.reject(error); });
      }
    };

    const run = (key, task, priority = 0) => {
      const requestKey = String(key);
      const existing = requests.get(requestKey);
      const selectedPriority = Number.isFinite(Number(priority)) ? Number(priority) : 0;
      if (existing) {
        if (existing.pending && selectedPriority < existing.priority) {
          existing.priority = selectedPriority;
          orderPending();
          pump();
        }
        return existing.promise;
      }

      let resolve;
      let reject;
      const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
      });
      const request = { key: requestKey, task, priority: selectedPriority, sequence: sequence++, resolve, reject, promise, pending: true };
      requests.set(requestKey, request);
      pending.push(request);
      orderPending();
      pump();
      return promise;
    };

    const cancelPending = (matches = () => true) => {
      let cancelled = 0;
      for (let index = pending.length - 1; index >= 0; index -= 1) {
        const request = pending[index];
        if (!matches(request.key)) continue;
        pending.splice(index, 1);
        if (requests.get(request.key) === request) requests.delete(request.key);
        const error = new Error("TTS request cancelled before starting.");
        error.name = "AbortError";
        request.reject(error);
        cancelled += 1;
      }
      return cancelled;
    };

    return {
      run,
      cancelPending,
      get activeCount() { return active; },
      get pendingCount() { return pending.length; },
    };
  };

  return { calculateProgress, createRequestQueue, mediaTimeToTimelineTime, timelineTimeToMediaTime };
});
