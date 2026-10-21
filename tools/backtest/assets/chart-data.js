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
  function indicatorParts(indicator, indicators, cutoff) {
    const known = series => series.points.filter(point => BigInt(point.time_ns) <= BigInt(cutoff));
    if (indicator.render !== 'path') return segments(known(indicator));
    const points = known(indicator);
    const vertex = point => ({time:seconds(point.source_time_ns), value:point.value});
    if (indicator.projection_from) {
      const parent = indicators.find(series => series.key === indicator.projection_from
        && series.instrument_id === indicator.instrument_id && series.bar_type === indicator.bar_type);
      const anchor = parent && known(parent).filter(point => point.value !== null).at(-1);
      // A missing current candidate is an explicit gap, never a carry forward.
      const candidate = points.at(-1);
      if (!anchor || !candidate || candidate.value === null
        || BigInt(candidate.source_time_ns) <= BigInt(anchor.source_time_ns)) return [];
      return [{color:indicator.color, data:[vertex(anchor), vertex(candidate)]}];
    }
    const data = points.filter(point => point.value !== null).map(vertex);
    return data.length ? [{color:indicator.color, data}] : [];
  }
  function replayView(report, instrument, barType, count) {
    const all = report.candles.filter(bar => bar.instrument_id === instrument && bar.bar_type === barType)
      .sort((a,b) => BigInt(a.close_time_ns) < BigInt(b.close_time_ns) ? -1 : 1);
    const length = Math.max(1, Math.min(all.length, Number.isFinite(count) ? count : all.length));
    const candles = all.slice(0,length), cutoff = candles.at(-1)?.close_time_ns;
    const known = ns => cutoff !== undefined && BigInt(ns) <= BigInt(cutoff);
    return {candles, cutoff, total:all.length,
      indicators:report.indicators.filter(series => series.instrument_id === instrument && series.bar_type === barType)
        .map(series => ({...series, points:length < all.length && series.provenance === 'historical_unverified' ? [] : series.points.filter(point => known(point.time_ns))})),
      signals:report.signals.filter(event => event.instrument_id === instrument && known(event.event_time_ns)),
      fills:report.fills.filter(event => event.instrument_id === instrument && known(event.event_time_ns)),
      // Cached orders describe terminal state, which is not a replay observation.
      orders:length === all.length ? report.orders : []};
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
  const actionStyles = {
    enter_long:{color:'#2dd4bf',shape:'arrowUp',path:'M8 2 L14 9 H10 V14 H6 V9 H2 Z'},
    exit_long:{color:'#fbbf24',shape:'square',path:'M3 3 H13 V13 H3 Z'},
    enter_short:{color:'#fb7185',shape:'arrowDown',path:'M8 14 L14 7 H10 V2 H6 V7 H2 Z'},
    exit_short:{color:'#93c5fd',shape:'circle',path:'M8 2 A6 6 0 1 0 8 14 A6 6 0 1 0 8 2 Z'},
    mixed:{color:'#c4b5fd',shape:'circle',path:'M8 2 L14 8 L8 14 L2 8 Z'},
  };
  const actions = event => [...new Set(event.kind === 'signal' ? [event.action] : event.legs.map(leg => leg.action))];
  function markerStyle(events) {
    const kinds = [...new Set(events.flatMap(actions))];
    return actionStyles[kinds.length === 1 ? kinds[0] : 'mixed'] || actionStyles.mixed;
  }
  function actionCounts(events) {
    const counts = new Map();
    for (const event of events) for (const action of actions(event)) counts.set(action,(counts.get(action)||0)+1);
    return [...counts].map(([action,count])=>({action,count}));
  }
  function orderTypeLabel(value) {
    if (typeof value !== 'string' || !value.trim()) return 'Unknown';
    const text = value.trim().toLowerCase().replaceAll('_', ' ');
    return text[0].toUpperCase() + text.slice(1);
  }
  function formatDecimal(value, places) {
    if (typeof value !== 'string' || !Number.isInteger(places) || places < 0 || places > 18) return null;
    const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
    if (!match) return null;
    const fraction = (match[3] || '').padEnd(places+1, '0');
    let scaled = BigInt(match[2]+fraction.slice(0, places));
    if (fraction[places] >= '5') scaled += 1n;
    const digits = scaled.toString().padStart(places+1, '0');
    const integer = places ? digits.slice(0,-places) : digits;
    const tail = places ? digits.slice(-places).replace(/0+$/, '') : '';
    return (match[1] && scaled !== 0n ? '-' : '') + integer + (tail ? '.'+tail : '');
  }
  function layoutLabels(items, width, measure, kind, summarize) {
    if (width <= 0) return [];
    const placed = [];
    function label(members) {
      const count = members.reduce((n, item) => n + item.events.length, 0);
      const text = members.length === 1 ? members[0].text : summarize ? summarize(members.flatMap(item=>item.events)) : `${kind} ×${count}`;
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
  const api = { seconds, isoTime, alignForward, segments, indicatorParts, replayView, groupSignals, eventKey, actions, actionStyles, markerStyle, actionCounts, orderTypeLabel, formatDecimal, layoutLabels, selectionPage };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BacktestChartData = api;
})(typeof window !== 'undefined' ? window : globalThis);
