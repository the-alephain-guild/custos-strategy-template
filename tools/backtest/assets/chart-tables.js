/* Accessible paginated values and event details for the offline viewer. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const time = ns => BacktestChartData.isoTime(ns);
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
    return element;
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
  class Tables {
    constructor(locate) {
      this.locate = locate;
      this.expanded = new Set();
      this.childLimits = new Map();
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
      this.tree = BacktestChartData.groupSignals(events.filter(event => event.kind === 'signal'), events.filter(event => event.kind === 'fill'));
      this.bySignal = new Map(this.tree.groups.map(group => [group.signal.id, group]));
      this.fillContext = new Map();
      this.tree.groups.forEach(group => group.fills.forEach((fill, index) => this.fillContext.set(fill.id, {group, index})));
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
      $(key === 'events' ? 'page' : key + '-page').textContent = `${items.length} ${key === 'events' ? 'groups' : 'records'} · ${this.pages[key] + 1} / ${count}`;
      return items.slice(this.pages[key] * size, (this.pages[key] + 1) * size);
    }
    updateEvents() {
      const visible = event => {
        if (!$('visible-events').checked || !this.range) return true;
        const aligned = BacktestChartData.alignForward(event.event_time_ns, this.bars);
        return aligned !== null && aligned >= this.range.from && aligned <= this.range.to;
      };
      const filtered = this.tree.groups.filter(group => {
        if ($('event-kind').value === 'fill' && !group.fills.length) return false;
        return visible(group.signal) || group.fills.some(visible);
      });
      if ($('event-kind').value !== 'signal' && this.tree.unlinked.some(visible)) filtered.push({signal:null, number:null, fills:this.tree.unlinked});
      $('events').replaceChildren();
      for (const group of this.page('events', filtered)) {
        const event = group.signal;
        const key = event ? event.id : 'unlinked';
        const childId = `children-${encodeURIComponent(key)}`;
        const open = this.expanded.has(key) && $('event-kind').value !== 'signal';
        const info = document.createElement('div');
        const toggle = document.createElement('button');
        toggle.textContent = `${open ? 'Hide' : 'Show'} ${group.fills.length} fills`;
        toggle.disabled = !group.fills.length || $('event-kind').value === 'signal';
        toggle.setAttribute('aria-expanded', String(open));
        toggle.setAttribute('aria-controls', childId);
        toggle.addEventListener('click', () => { if (this.expanded.has(key)) this.expanded.delete(key); else this.expanded.add(key); this.updateEvents(); });
        info.append(toggle);
        if (event) info.append(details(event));
        else info.append(document.createTextNode(' No signal identity is available; these fills are not assigned by time.'));
        const parent = row($('events'), [event ? `SIG${group.number} · ${actionLabel(event.action)}` : 'Unlinked fills',
          event ? time(event.event_time_ns) : 'No parent signal', event ? event.price : 'See child fills',
          info, event ? this.locateButton(event) : '']);
        parent.className = 'signal-row';
        const child = document.createElement('tr'); child.id = childId; child.className = 'fill-children';
        child.hidden = !open;
        const cell = document.createElement('td'); cell.colSpan = 5;
        if (!child.hidden) cell.append(this.fillTable(group));
        child.append(cell); $('events').append(child);
      }
      if (!filtered.length) row($('events'), [this.events.length ? 'No matching events. Change Show or disable Visible candles only.' : 'This feed has no signal or fill records.', '', '', '', '']);
    }
    locateButton(event) {
      const button = document.createElement('button'); button.textContent = 'Locate';
      button.addEventListener('click', () => this.locate(event));
      button.disabled = BacktestChartData.alignForward(event.event_time_ns, this.bars) === null;
      return button;
    }
    fillTable(group) {
      const wrapper = document.createElement('div'); wrapper.className = 'child-table';
      const table = document.createElement('table');
      table.setAttribute('aria-label', group.signal ? `Fills for SIG${group.number}` : 'Unlinked fill records');
      const head = document.createElement('thead'); const headings = document.createElement('tr');
      for (const label of ['Fill / action', 'Actual time (UTC)', 'Price / quantity', 'Fee', 'Original record', 'Inspect']) {
        const th = document.createElement('th'); th.scope = 'col'; th.textContent = label; headings.append(th);
      }
      head.append(headings); table.append(head);
      const body = document.createElement('tbody');
      const key = group.signal ? group.signal.id : 'unlinked';
      const limit = this.childLimits.get(key) || 50;
      group.fills.slice(0, limit).forEach((fill, index) => {
        const label = group.signal ? `FILL${group.number}.${index + 1}` : `Unlinked fill ${index + 1}`;
        row(body, [`${label} · ${actionLabel(fill.legs.map(leg => leg.action).join(' + '))}`, time(fill.event_time_ns),
          `${fill.price} / ${fill.quantity}`, fill.commission, details(fill), this.locateButton(fill)]);
      });
      table.append(body); wrapper.append(table);
      if (group.fills.length > limit) {
        const more = document.createElement('button'); more.textContent = `Show next ${Math.min(50, group.fills.length-limit)} fills (${limit} / ${group.fills.length})`;
        more.addEventListener('click', () => { this.childLimits.set(key, limit+50); wrapper.replaceWith(this.fillTable(group)); });
        wrapper.append(more);
      }
      return wrapper;
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
      const groups = new Map();
      events.forEach(event => {
        const group = event.kind === 'signal' ? this.bySignal.get(event.id) : this.fillContext.get(event.id)?.group;
        if (group) groups.set(group.signal.id, group);
        else groups.set('unlinked', {signal:null, fills:events.filter(item => item.kind === 'fill' && !this.fillContext.has(item.id))});
      });
      groups.forEach(group => {
        const article = document.createElement('article');
        const heading = document.createElement('h3');
        const event = group.signal;
        heading.textContent = event ? `SIG${group.number} · ${actionLabel(event.action)} · ${group.fills.length} fills` : 'Unlinked fills';
        article.append(heading);
        if (event) {
          const original = document.createElement('p'); original.textContent = `${time(event.event_time_ns)} · reference price ${event.price}`;
          article.append(original, details(event));
        }
        if (group.fills.length) article.append(this.fillTable(group));
        $('selected-events').append(article);
      });
    }
  }
  window.BacktestTables = { Tables };
})();
