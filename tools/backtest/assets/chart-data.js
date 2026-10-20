/* Pure chart transformations. Raw event times are never changed. */
(function (root) {
  'use strict';
  const seconds = ns => Number(BigInt(ns)) / 1e9;
  function alignForward(ns, bars) {
    const time = BigInt(ns);
    if (!bars.length || time < BigInt(bars[0].open_time_ns)) return null;
    let left = 0, right = bars.length;
    while (left < right) {
      const middle = (left + right) >>> 1;
      if (BigInt(bars[middle].close_time_ns) < time) left = middle + 1;
      else right = middle;
    }
    return left === bars.length ? null : seconds(bars[left].close_time_ns);
  }
  function segments(points) {
    const result = [];
    let current = null;
    for (const point of points) {
      if (point.value === null) { current = null; continue; }
      if (!current || current.segment !== point.segment || current.color !== point.color) {
        current = { segment: point.segment, color: point.color, data: [] };
        result.push(current);
      }
      current.data.push({ time: seconds(point.time_ns), value: point.value });
    }
    return result;
  }
  const api = { seconds, alignForward, segments };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BacktestChartData = api;
})(typeof window !== 'undefined' ? window : globalThis);
