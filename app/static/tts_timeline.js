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

  return { calculateProgress, mediaTimeToTimelineTime, timelineTimeToMediaTime };
});
