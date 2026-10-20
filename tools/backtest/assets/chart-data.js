/* Pure chart transformations. Raw event times are never changed. */
(function (root) {
  'use strict';
  const seconds = ns => Number(BigInt(ns)) / 1e9;
  const isoTime = ns => new Date(Number(BigInt(ns) / 1000000n)).toISOString();
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
  function groupSignals(signals, fills) {
    const groups = signals.map((signal, index) => ({signal, number:index + 1, fills:[]}));
    const byId = new Map();
    for (const group of groups) {
      if (byId.has(group.signal.id)) throw new Error('duplicate signal identity');
      byId.set(group.signal.id, group);
    }
    const unlinked = [];
    for (const fill of fills) {
      const parent = byId.get(fill.signal_id);
      if (parent) parent.fills.push(fill);
      else unlinked.push(fill);
    }
    const compare = (a, b) => BigInt(a.event_time_ns) < BigInt(b.event_time_ns) ? -1 : BigInt(a.event_time_ns) > BigInt(b.event_time_ns) ? 1 : 0;
    groups.forEach(group => group.fills.sort(compare));
    unlinked.sort(compare);
    return {groups, unlinked};
  }
  const eventKey = event => `${event.kind}:${event.id}`;
  function orderTypeLabel(value) {
    if (typeof value !== 'string' || !value.trim()) return 'Unknown';
    const text = value.trim().toLowerCase().replaceAll('_', ' ');
    return text[0].toUpperCase() + text.slice(1);
  }
  function layoutLabels(items, width, measure, kind) {
    if (width <= 0) return [];
    const placed = [];
    function label(members) {
      const count = members.reduce((n, item) => n + item.events.length, 0);
      const text = members.length === 1 ? members[0].text : `${kind} ×${count}`;
      const size = Math.min(width, Math.max(44, Math.ceil(measure(text)) + 20));
      const x = (members[0].x + members[members.length-1].x) / 2;
      return {items:members, text, width:size, left:Math.max(0, Math.min(width-size, x-size/2))};
    }
    for (const item of [...items].sort((a,b) => a.x-b.x)) {
      let current = label([item]);
      while (placed.length && placed[placed.length-1].left+placed[placed.length-1].width+8 > current.left) {
        current = label([...placed.pop().items, ...current.items]);
      }
      placed.push(current);
    }
    return placed;
  }
  function selectionPage(groups, selected, size, fallback) {
    const index = groups.findIndex(group => (group.signal && selected.has(eventKey(group.signal)))
      || group.fills.some(fill => selected.has(eventKey(fill))));
    return index < 0 ? fallback : Math.floor(index / size);
  }
  const api = { seconds, isoTime, alignForward, segments, groupSignals, eventKey, orderTypeLabel, layoutLabels, selectionPage };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BacktestChartData = api;
})(typeof window !== 'undefined' ? window : globalThis);
