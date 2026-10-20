/* Offline viewer: plot recorded observations, never recalculate trading rules. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const D = window.BacktestChartData;
  const labels = { enter_long: 'EL', exit_long: 'XL', enter_short: 'ES', exit_short: 'XS' };
  let chart, candleSeries, markerApi, observer, report, tables;
  let bars = [], events = [], indicators = [], selectedIndex = 0;
  let alignedEvents = new Map(), indicatorValues = new Map();
  let swatches = new Map();
  const time = ns => new Date(D.seconds(ns) * 1000).toISOString();
  const number = value => new Intl.NumberFormat('en', { maximumSignificantDigits: 12, useGrouping: false }).format(value);
  const paneName = pane => pane === 'price' ? 'Price' : pane;

  function recent() {
    const count = Math.max(30, Math.min(250, Math.floor($('chart').clientWidth / 6)));
    if (bars.length <= count) { chart.timeScale().fitContent(); return; }
    const from = Math.max(0, bars.length - count);
    chart.timeScale().setVisibleLogicalRange({ from, to: bars.length + 1 });
  }
  function readout(index) {
    const bar = bars[index];
    if (!bar) return;
    const values = indicatorValues.get(D.seconds(bar.close_time_ns)) || [];
    values.forEach(value => swatches.get(value.key).style.setProperty('--series-color', value.color));
    $('hover').textContent = `${time(bar.close_time_ns)}  O ${number(bar.open)}  H ${number(bar.high)}  L ${number(bar.low)}  C ${number(bar.close)}\n${values.map(value => `${value.label}: ${value.value === null ? 'unavailable' : number(value.value)}`).join(' · ')}`;
  }
  function inspect(index, scroll) {
    if (!bars[index]) return;
    selectedIndex = index;
    const stamp = D.seconds(bars[index].close_time_ns);
    chart.setCrosshairPosition(bars[index].close, stamp, candleSeries);
    readout(index);
    tables.inspect(index, alignedEvents.get(stamp) || []);
    if (scroll) $('chart').scrollIntoView({ block: 'start', behavior: 'auto' });
  }
  function locate(event) {
    const stamp = D.alignForward(event.event_time_ns, bars);
    const index = bars.findIndex(bar => D.seconds(bar.close_time_ns) === stamp);
    if (index < 0) return;
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, index - 20), to: Math.min(bars.length + 1, index + 20) });
    inspect(index, true);
    $('chart').focus({ preventScroll: true });
  }
  function updateMarkers() {
    const markers = [];
    for (const [stamp, bucket] of alignedEvents) {
      for (const kind of ['signal', 'fill']) {
        if (!$(kind + 's').checked) continue;
        const members = bucket.filter(event => event.kind === kind);
        if (!members.length) continue;
        const actions = members.flatMap(event => event.kind === 'signal' ? [event.action] : event.legs.map(leg => leg.action));
        const up = actions[0] === 'enter_long' || actions[0] === 'exit_short';
        markers.push({ time: stamp, position: kind === 'signal' ? 'aboveBar' : 'belowBar',
          color: kind === 'signal' ? '#fbbf24' : '#93c5fd',
          shape: kind === 'signal' ? 'circle' : (up ? 'arrowUp' : 'arrowDown'),
          text: `${kind === 'signal' ? 'SIG' : 'FILL'} ${actions.length === 1 ? labels[actions[0]] : actions.length + ' events'}`, size: 1 });
      }
    }
    markers.sort((a, b) => a.time - b.time);
    markerApi.setMarkers(markers);
  }
  function addIndicators() {
    const paneIndexes = new Map([['price', 0]]);
    const controls = new Map();
    $('layers').replaceChildren();
    indicators.forEach((indicator, index) => {
      if (!paneIndexes.has(indicator.pane)) paneIndexes.set(indicator.pane, paneIndexes.size);
      const pane = paneIndexes.get(indicator.pane);
      const type = { line: LightweightCharts.LineSeries, histogram: LightweightCharts.HistogramSeries, area: LightweightCharts.AreaSeries }[indicator.style];
      const parts = D.segments(indicator.points);
      const series = parts.map(part => {
        const color = part.color || indicator.color;
        const item = chart.addSeries(type, { color, lineColor: color, topColor: color + '66', bottomColor: color + '00',
          title: indicator.label, lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
          pointMarkersVisible: part.data.length === 1 }, pane);
        item.setData(part.data);
        return item;
      });
      if (!series.length) series.push(chart.addSeries(type, { color: indicator.color }, pane));
      indicator.levels.forEach(price => series[0].createPriceLine({ price, color: '#94a3b8', lineWidth: 1, lineStyle: 2, axisLabelVisible: true }));
      if (!controls.has(indicator.pane)) {
        const group = document.createElement('fieldset');
        const legend = document.createElement('legend'); legend.textContent = paneName(indicator.pane);
        group.append(legend); controls.set(indicator.pane, group); $('layers').append(group);
      }
      const label = document.createElement('label');
      const input = document.createElement('input'); input.type = 'checkbox'; input.checked = true; input.id = `indicator-${index}`;
      input.addEventListener('change', () => series.forEach(item => item.applyOptions({ visible: input.checked })));
      const swatch = document.createElement('span'); swatch.className = 'swatch'; swatch.setAttribute('aria-hidden', 'true'); swatch.style.setProperty('--series-color', indicator.color);
      swatches.set(indicator.key, swatch);
      label.append(input, swatch, document.createTextNode(indicator.label + (indicator.provenance === 'historical_unverified' ? ' · historical, unverified' : '')));
      controls.get(indicator.pane).append(label);
      indicator.points.forEach(point => {
        const stamp = D.seconds(point.time_ns);
        if (!indicatorValues.has(stamp)) indicatorValues.set(stamp, []);
        indicatorValues.get(stamp).push({ key: indicator.key, label: indicator.label, value: point.value, color: point.color || indicator.color });
      });
    });
    const panes = chart.panes();
    panes.forEach((pane, index) => pane.setStretchFactor(index === 0 ? (window.innerWidth < 640 ? 300 : 440) : 140));
    requestAnimationFrame(() => {
      for (const [name, index] of paneIndexes) {
        const element = panes[index].getHTMLElement();
        if (!element) continue;
        const title = document.createElement('span'); title.className = 'pane-label'; title.textContent = paneName(name);
        element.style.position = 'relative'; element.append(title);
      }
    });
    $('indicator-count').textContent = `(${indicators.length})`;
    return panes.length;
  }
  function draw() {
    if (observer) observer.disconnect();
    if (chart) chart.remove();
    const [instrument, barType] = JSON.parse($('feed').value);
    bars = report.candles.filter(bar => bar.instrument_id === instrument && bar.bar_type === barType)
      .sort((a, b) => BigInt(a.close_time_ns) < BigInt(b.close_time_ns) ? -1 : 1);
    indicators = report.indicators.filter(item => item.instrument_id === instrument && item.bar_type === barType);
    events = [...report.signals.filter(event => event.instrument_id === instrument && event.bar_type === barType).map(event => ({ ...event, kind: 'signal' })),
      ...report.fills.filter(event => event.instrument_id === instrument).map(event => ({ ...event, kind: 'fill' }))]
      .sort((a, b) => BigInt(a.event_time_ns) < BigInt(b.event_time_ns) ? -1 : BigInt(a.event_time_ns) > BigInt(b.event_time_ns) ? 1 : 0);
    alignedEvents = new Map(); indicatorValues = new Map(); swatches = new Map();
    events.forEach(event => {
      const stamp = D.alignForward(event.event_time_ns, bars);
      if (stamp !== null) {
        if (!alignedEvents.has(stamp)) alignedEvents.set(stamp, []);
        alignedEvents.get(stamp).push(event);
      }
    });
    chart = LightweightCharts.createChart($('chart'), {
      width: $('chart').clientWidth, height: $('chart').clientHeight,
      layout: { background: { color: '#0b1019' }, textColor: '#9aacc3', attributionLogo: true },
      grid: { vertLines: { color: '#1a2636' }, horzLines: { color: '#1a2636' } },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 1 },
    });
    candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    });
    candleSeries.setData(bars.map(bar => ({ time: D.seconds(bar.close_time_ns), open: bar.open, high: bar.high, low: bar.low, close: bar.close })));
    markerApi = LightweightCharts.createSeriesMarkers(candleSeries, []);
    const paneCount = addIndicators();
    const height = Math.max(window.innerWidth < 640 ? 420 : 560, (window.innerWidth < 640 ? 300 : 440) + (paneCount - 1) * 140 + 28);
    $('chart').style.height = height + 'px';
    chart.applyOptions({ height });
    // The native logical scale advances through gaps in sparse indicator
    // series too. fitContent establishes the data range before narrowing it.
    chart.timeScale().fitContent();
    recent();
    tables.setFeed(bars, indicators, events, chart.timeScale().getVisibleRange());
    let frame;
    chart.timeScale().subscribeVisibleTimeRangeChange(range => {
      tables.range = range;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => tables.updateEvents());
    });
    let sized = false;
    observer = new ResizeObserver(() => {
      chart.applyOptions({ width: $('chart').clientWidth, height: $('chart').clientHeight });
      if (!sized) { sized = true; recent(); }
    });
    observer.observe($('chart'));
    chart.subscribeCrosshairMove(param => {
      if (param.time === undefined) return;
      const index = bars.findIndex(bar => D.seconds(bar.close_time_ns) === Number(param.time));
      if (index >= 0) readout(index);
    });
    chart.subscribeClick(param => {
      const index = bars.findIndex(bar => D.seconds(bar.close_time_ns) === Number(param.time));
      if (index >= 0) inspect(index, false);
    });
    const outside = events.filter(event => D.alignForward(event.event_time_ns, bars) === null).length;
    $('status').textContent = `${bars.length} candles · ${indicators.length} indicator series · ${events.length} events${outside ? ` · ${outside} outside candle coverage (table only)` : ''}`;
    selectedIndex = bars.length - 1;
    readout(selectedIndex);
    $('selection-time').textContent = 'Select a candle, or use Locate in an event row.';
    $('selected-events').replaceChildren();
    updateMarkers();
  }
  try {
    report = JSON.parse($('backtest-data').textContent);
    if (report.schema_version !== 1) throw new Error('Unsupported chart schema');
    if (!window.LightweightCharts) throw new Error('Local chart library is missing. Keep report.html with its assets directory.');
    $('title').textContent = report.summary.strategy;
    const period = report.summary.start && report.summary.end ? `${report.summary.start.slice(0, 10)} to ${report.summary.end.slice(0, 10)}` : '';
    $('summary').textContent = [report.summary.connector, period, `${report.fills.length} fills / ${report.signals.length} signals`].filter(Boolean).join(' · ');
    const feeds = new Map(report.candles.map(bar => [JSON.stringify([bar.instrument_id, bar.bar_type]), bar]));
    if (!feeds.size) throw new Error('No candles were exported.');
    for (const [key, bar] of feeds) {
      const option = document.createElement('option'); option.value = key; option.title = bar.bar_type;
      option.textContent = `${bar.instrument_id} · ${bar.bar_type.replace(bar.instrument_id + '-', '').replace('-LAST-EXTERNAL', '')}`;
      $('feed').append(option);
    }
    tables = new BacktestTables.Tables(report, locate);
    $('indicator-controls').open = window.innerWidth >= 640;
    $('feed').addEventListener('change', draw);
    $('signals').addEventListener('change', updateMarkers); $('fills').addEventListener('change', updateMarkers);
    $('fit').addEventListener('click', () => chart.timeScale().fitContent());
    $('recent').addEventListener('click', recent);
    $('chart').addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const index = Math.max(0, Math.min(bars.length - 1, selectedIndex + (event.key === 'ArrowLeft' ? -1 : 1)));
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, index - 20), to: Math.min(bars.length + 1, index + 20) });
      inspect(index, false);
    });
    draw();
  } catch (error) {
    $('status').className = 'error';
    $('status').textContent = `Chart unavailable: ${error.message}. Keep the assets directory beside this report, or use the complete JSON download above.`;
    document.querySelectorAll('button, select, input').forEach(control => { control.disabled = true; });
  }
})();
