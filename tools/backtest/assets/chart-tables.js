/* Accessible paginated values and event details for the offline viewer. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const time = ns => new Date(BacktestChartData.seconds(ns) * 1000).toISOString();
  const actionLabel = action => action.replaceAll('_', ' ');
  function row(parent, values) {
    const element = document.createElement('tr');
    for (const value of values) {
      const cell = document.createElement('td');
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value;
      element.append(cell);
    }
    parent.append(element);
  }
  function details(event) {
    const element = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'Original times and metadata';
    const body = document.createElement('pre');
    body.textContent = JSON.stringify(event, null, 2);
    element.append(summary, body);
    return element;
  }
  function lineage(event, report) {
    if (event.kind === 'signal') return `${event.id} · ${report.fills.filter(f => f.signal_id === event.id).length} associated fills`;
    return `Signal ${event.signal_id || 'unlinked'} · position ${event.position_before} → ${event.position_after} · fee ${event.commission}`;
  }
  class Tables {
    constructor(report, locate) {
      this.report = report;
      this.locate = locate;
      this.pages = { events: 0, candles: 0, values: 0 };
      for (const key of ['events', 'candles', 'values']) {
        const prefix = key === 'events' ? '' : key + '-';
        for (const direction of ['previous', 'next']) {
          $(prefix + direction).addEventListener('click', () => {
            this.pages[key] += direction === 'next' ? 1 : -1;
            this.update();
          });
        }
      }
      $('value-series').addEventListener('change', () => { this.pages.values = 0; this.updateValues(); });
      $('event-kind').addEventListener('change', () => { this.pages.events = 0; this.updateEvents(); });
      $('visible-events').addEventListener('change', () => { this.pages.events = 0; this.updateEvents(); });
    }
    setFeed(bars, indicators, events, range) {
      this.bars = bars; this.indicators = indicators; this.events = events; this.range = range;
      this.pages = { events: 0, candles: 0, values: 0 };
      $('value-series').replaceChildren();
      indicators.forEach((indicator, i) => {
        const option = document.createElement('option');
        option.value = i; option.textContent = indicator.label + ' · ' + indicator.pane + (indicator.provenance === 'historical_unverified' ? ' · historical, unverified' : '');
        $('value-series').append(option);
      });
      $('value-series').disabled = !indicators.length;
      this.update();
    }
    page(key, items) {
      const size = key === 'events' ? (window.innerWidth < 640 ? 15 : 25) : 100;
      const count = Math.max(1, Math.ceil(items.length / size));
      this.pages[key] = Math.max(0, Math.min(count - 1, this.pages[key]));
      const prefix = key === 'events' ? '' : key + '-';
      $(prefix + 'previous').disabled = this.pages[key] === 0;
      $(prefix + 'next').disabled = this.pages[key] + 1 >= count;
      $(key === 'events' ? 'page' : key + '-page').textContent = `${items.length} records · ${this.pages[key] + 1} / ${count}`;
      return items.slice(this.pages[key] * size, (this.pages[key] + 1) * size);
    }
    updateEvents() {
      const filtered = this.events.filter(event => {
        if ($('event-kind').value !== 'all' && event.kind !== $('event-kind').value) return false;
        if (!$('visible-events').checked || !this.range) return true;
        const aligned = BacktestChartData.alignForward(event.event_time_ns, this.bars);
        return aligned !== null && aligned >= this.range.from && aligned <= this.range.to;
      });
      $('events').replaceChildren();
      for (const event of this.page('events', filtered)) {
        const info = document.createElement('div');
        info.append(document.createTextNode(lineage(event, this.report)), details(event));
        const locate = document.createElement('button'); locate.textContent = 'Locate';
        locate.addEventListener('click', () => this.locate(event));
        locate.disabled = BacktestChartData.alignForward(event.event_time_ns, this.bars) === null;
        const action = event.kind === 'signal' ? event.action : event.legs.map(leg => leg.action).join(' + ');
        row($('events'), [`${event.kind.toUpperCase()} · ${actionLabel(action)}`, time(event.event_time_ns),
          `${event.price}${event.quantity ? ' / ' + event.quantity : ''}`, info, locate]);
      }
      if (!filtered.length) row($('events'), [this.events.length ? 'No matching events. Change Show or disable Visible candles only.' : 'This feed has no signal or fill records.', '', '', '', '']);
    }
    updateValues() {
      const indicator = this.indicators[Number($('value-series').value)];
      $('indicator-values').replaceChildren();
      for (const point of this.page('values', indicator ? indicator.points : [])) {
        row($('indicator-values'), [time(point.time_ns), point.value === null ? 'Unavailable / gap' : point.value,
          `${indicator.provenance === 'historical_unverified' ? 'Availability unverified' : point.source_time_ns ? time(point.source_time_ns) : 'Observed at this bar'} · segment ${point.segment ?? 'continuous'}`]);
      }
      if (!indicator) row($('indicator-values'), ['This strategy did not export indicator values.', '', '']);
    }
    update() {
      this.updateEvents();
      $('candles').replaceChildren();
      this.page('candles', this.bars).forEach(bar => row($('candles'), [time(bar.close_time_ns), bar.open, bar.high, bar.low, bar.close, bar.volume]));
      this.updateValues();
    }
    inspect(index, events) {
      $('selection-time').textContent = `Candle close ${time(this.bars[index].close_time_ns)}`;
      $('selected-events').replaceChildren();
      if (!events.length) $('selected-events').textContent = 'No signal or fill aligned to this candle.';
      events.forEach(event => {
        const article = document.createElement('article');
        const heading = document.createElement('h3');
        heading.textContent = `${event.kind.toUpperCase()} · ${actionLabel(event.action || event.legs.map(l => l.action).join(' + '))}`;
        const original = document.createElement('p'); original.textContent = `${time(event.event_time_ns)} · price ${event.price}${event.quantity ? ' · quantity ' + event.quantity : ''}`;
        const link = document.createElement('p'); link.textContent = lineage(event, this.report);
        article.append(heading, original, link, details(event)); $('selected-events').append(article);
      });
    }
  }
  window.BacktestTables = { Tables };
})();
