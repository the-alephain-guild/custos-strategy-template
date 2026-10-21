/* Accessible paginated values and event details for the offline viewer. */
(function () {
  'use strict';
  const I = window.BacktestI18n, t = I.t;
  const $ = id => document.getElementById(id);
  const time = ns => BacktestChartData.isoTime(ns);
  const actionLabel = I.action;
  const signalHeadings = ['Signal', 'Action', 'Signal time (UTC)', 'Reference price', 'Fills', 'Trigger details', 'Original record', 'Inspect'];
  const fillHeadings = ['Fill', 'Action', 'Side', 'Actual time (UTC)', 'Price', 'Quantity', 'Position before', 'Position after', 'Order type', 'Entry sizing', 'Fee', 'Realized P&L (net)', 'Return (net %)', 'Cumulative P&L (net)', 'Order ID', 'Position ID', 'Original record', 'Inspect'];
  function timestamp(ns) {
    const element = document.createElement('time');
    element.className = 'timestamp'; element.dateTime = time(ns); element.textContent = time(ns);
    return element;
  }
  function row(parent, values, headings = []) {
    const element = document.createElement('tr');
    values.forEach((value, index) => {
      const cell = document.createElement('td');
      if (headings[index]) { cell.dataset.field = headings[index]; cell.dataset.label = t(headings[index]); }
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value;
      element.append(cell);
    });
    parent.append(element);
    return element;
  }
  function details(event) {
    const element = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = t('Original times and metadata');
    const body = document.createElement('pre');
    body.textContent = JSON.stringify(event, null, 2);
    element.append(summary, body);
    return element;
  }
  function pnlCell(fill, key) {
    const cell = document.createElement('span'); cell.className = 'pnl-value';
    const pnl = fill.pnl;
    if (!pnl || pnl.status !== 'ok') {
      cell.textContent = t('Unavailable');
      cell.title = t('pnl_missing');
      return cell;
    }
    if (key === 'return_pct' && pnl[key] === null) {
      cell.textContent = '—'; cell.title = t('no_closing');
      return cell;
    }
    const value = BacktestChartData.formatDecimal(pnl[key], key === 'return_pct' ? 4 : pnl.currency_precision);
    if (value === null) {
      cell.textContent = t('Unavailable'); cell.title = t('pnl_invalid');
      return cell;
    }
    cell.textContent = value + (key === 'return_pct' ? '%' : ' '+pnl.currency);
    if (/[1-9]/.test(value)) cell.classList.add(value.startsWith('-') ? 'pnl-loss' : 'pnl-profit');
    cell.title = key === 'return_pct'
      ? t('return_rule',{net:pnl.closed_net,notional:pnl.entry_notional,entryFee:pnl.entry_fee_allocated,exitFee:pnl.exit_fee_allocated})
      : key === 'realized_net'
        ? t('realized_rule',{gross:pnl.realized_gross,fee:pnl.fee,value:pnl[key],currency:pnl.currency})
        : t('cumulative_rule',{value:pnl[key],currency:pnl.currency});
    return cell;
  }
  class Tables {
    constructor(locate) {
      this.locate = locate;
      this.selected = new Set();
      this.rowRefs = new Map();
      this.expanded = new Set();
      this.childLimits = new Map();
      this.openSizing = new Set();
      this.openTriggers = new Set();
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
    setFeed(bars, indicators, events, range, orders = []) {
      this.bars = bars; this.indicators = indicators; this.events = events; this.range = range;
      this.orders = new Map(orders.map(order => [JSON.stringify([order.instrument_id, order.id]), order]));
      this.selected.clear();
      this.pages = { events: 0, candles: 0, values: 0 };
      this.tree = BacktestChartData.groupSignals(events.filter(event => event.kind === 'signal'), events.filter(event => event.kind === 'fill'));
      this.bySignal = new Map(this.tree.groups.map(group => [group.signal.id, group]));
      this.fillContext = new Map();
      this.tree.groups.forEach(group => group.fills.forEach((fill, index) => this.fillContext.set(fill.id, {group, index})));
      $('value-series').replaceChildren();
      indicators.forEach((indicator, i) => {
        const option = document.createElement('option');
        option.value = i; option.textContent = I.indicator(indicator) + ' · ' + (indicator.pane === 'price' ? t('Price') : indicator.pane) + (indicator.provenance === 'historical_unverified' ? ' · '+t('historical, unverified') : '');
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
      $(key === 'events' ? 'page' : key + '-page').textContent = t(key === 'events' ? 'page_groups' : 'page_records',{count:items.length,page:this.pages[key]+1,pages:count});
      return items.slice(this.pages[key] * size, (this.pages[key] + 1) * size);
    }
    filteredGroups() {
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
      return filtered;
    }
    markRow(element, event) {
      const key = BacktestChartData.eventKey(event);
      element.dataset.eventKey = key;
      element.tabIndex = -1;
      if (this.selected.has(key)) {
        element.classList.add('selected-record');
        element.setAttribute('aria-current', 'true');
      }
      this.rowRefs.set(key, element);
    }
    select(events) {
      this.selected = new Set(events.map(BacktestChartData.eventKey));
      if (events.length) $('event-kind').value = 'all';
      const keys = new Set();
      events.forEach(event => {
        if (events.length === 1 && event.kind === 'signal') this.openTriggers.add(event.id);
        const context = event.kind === 'fill' ? this.fillContext.get(event.id) : null;
        const key = event.kind === 'signal' ? event.id : context ? context.group.signal.id : 'unlinked';
        keys.add(key);
        if (event.kind === 'fill') {
          this.expanded.add(key);
          const index = context ? context.index : this.tree.unlinked.findIndex(fill => fill.id === event.id);
          this.childLimits.set(key, Math.max(this.childLimits.get(key) || 50, index+1));
        }
      });
      let filtered = this.filteredGroups();
      const matching = group => keys.has(group.signal ? group.signal.id : 'unlinked');
      if (events.length && !filtered.some(matching)) {
        $('visible-events').checked = false;
        filtered = this.filteredGroups();
      }
      const index = filtered.findIndex(matching);
      if (index >= 0) this.pages.events = Math.floor(index / (window.innerWidth < 640 ? 15 : 25));
      this.updateEvents();
    }
    reveal(events) {
      if (events.length) {
        const target = events.map(event => this.rowRefs.get(BacktestChartData.eventKey(event))).find(Boolean);
        if (target) {
          target.scrollIntoView({block:'center', inline:'nearest', behavior:'auto'});
          target.focus({preventScroll:true});
        }
      }
    }
    updateEvents(keepSelection = false) {
      const filtered = this.filteredGroups();
      if (keepSelection) this.pages.events = BacktestChartData.selectionPage(filtered, this.selected,
        window.innerWidth < 640 ? 15 : 25, this.pages.events);
      this.rowRefs.clear();
      $('events').replaceChildren();
      for (const group of this.page('events', filtered)) {
        const event = group.signal;
        const key = event ? event.id : 'unlinked';
        const childId = `children-${encodeURIComponent(key)}`;
        const open = this.expanded.has(key) && $('event-kind').value !== 'signal';
        const triggerId = `trigger-${encodeURIComponent(key)}`;
        const parent = row($('events'), [this.signalIdentity(group, key, childId, open),
          event ? actionLabel(event.action) : t('Unlinked fills'),
          event ? timestamp(event.event_time_ns) : t('Unknown'), event ? event.price : t('Unknown'),
          group.fills.length, event ? this.triggerButton(event,triggerId) : t('未记录'),
          event ? details(event) : t('No parent signal identity is available.'),
          event ? this.locateButton(event) : ''], signalHeadings);
        parent.className = 'signal-row';
        if (event) this.markRow(parent, event);
        if (event) {
          const triggerRow = document.createElement('tr'); triggerRow.id = triggerId; triggerRow.className = 'trigger-children';
          triggerRow.hidden = !this.openTriggers.has(event.id);
          const triggerCell = document.createElement('td'); triggerCell.colSpan = signalHeadings.length;
          if (!triggerRow.hidden) triggerCell.append(BacktestTrigger.render(event,()=>this.locate(event),BacktestChartData.alignForward(event.event_time_ns,this.bars) !== null));
          triggerRow.append(triggerCell); $('events').append(triggerRow);
        }
        const child = document.createElement('tr'); child.id = childId; child.className = 'fill-children';
        child.hidden = !open;
        const cell = document.createElement('td'); cell.colSpan = signalHeadings.length;
        if (!child.hidden) cell.append(this.fillTable(group));
        child.append(cell); $('events').append(child);
      }
      if (!filtered.length) {
        const empty = document.createElement('td'); empty.colSpan = signalHeadings.length;
        empty.textContent = t(this.events.length ? 'No matching events. Change Show or disable Visible candles only.' : 'This feed has no signal or fill records.');
        const element = document.createElement('tr'); element.append(empty); $('events').append(element);
      }
    }
    signalIdentity(group, key, childId, open) {
      const identity = document.createElement('div'); identity.className = 'signal-identity';
      const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'expand-fills';
      toggle.id = `toggle-${encodeURIComponent(key)}`;
      const label = t(group.signal ? (open ? 'collapse_fills' : 'expand_fills') : (open ? 'collapse_unlinked' : 'expand_unlinked'),{number:group.number,count:group.fills.length});
      toggle.setAttribute('aria-label', label); toggle.title = label;
      toggle.disabled = !group.fills.length || $('event-kind').value === 'signal';
      toggle.setAttribute('aria-expanded', String(open)); toggle.setAttribute('aria-controls', childId);
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('viewBox', '0 0 16 16'); icon.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M6 4l4 4-4 4'); icon.append(path); toggle.append(icon);
      toggle.addEventListener('click', () => {
        if (this.expanded.has(key)) this.expanded.delete(key); else this.expanded.add(key);
        this.updateEvents(); $(toggle.id).focus({preventScroll:true});
      });
      const number = document.createElement('span'); number.className = 'signal-number'; number.textContent = group.signal ? group.number : '—';
      identity.append(toggle, number);
      return identity;
    }
    locateButton(event) {
      const button = document.createElement('button'); button.textContent = t('Locate');
      button.addEventListener('click', () => this.locate(event));
      button.disabled = BacktestChartData.alignForward(event.event_time_ns, this.bars) === null;
      return button;
    }
    triggerButton(event, childId) {
      const button = document.createElement('button'); button.type = 'button';
      button.id = `toggle-trigger-${encodeURIComponent(event.id)}`;
      button.textContent = t('Signal trigger details');
      button.setAttribute('aria-expanded',String(this.openTriggers.has(event.id)));
      button.setAttribute('aria-controls',childId);
      button.addEventListener('click',()=>{
        if (this.openTriggers.has(event.id)) this.openTriggers.delete(event.id); else this.openTriggers.add(event.id);
        this.updateEvents(); $(button.id).focus({preventScroll:true});
      });
      return button;
    }
    fillTable(group) {
      const wrapper = document.createElement('div'); wrapper.className = 'child-table';
      wrapper.tabIndex = 0; wrapper.setAttribute('role', 'region');
      wrapper.setAttribute('aria-label', group.signal ? t('fill_records',{number:group.number}) : t('Unlinked fill records'));
      const table = document.createElement('table'); table.className = 'fill-table';
      table.setAttribute('aria-label', group.signal ? t('fill_records',{number:group.number}) : t('Unlinked fill records'));
      const head = document.createElement('thead'); const headings = document.createElement('tr');
      for (const label of fillHeadings) {
        const th = document.createElement('th'); th.scope = 'col'; th.textContent = t(label); headings.append(th);
      }
      head.append(headings); table.append(head);
      const body = document.createElement('tbody');
      const key = group.signal ? group.signal.id : 'unlinked';
      const limit = this.childLimits.get(key) || 50;
      group.fills.slice(0, limit).forEach((fill, index) => {
        const order = this.orders.get(JSON.stringify([fill.instrument_id, fill.order_id]));
        const element = row(body, [index + 1, actionLabel(fill.legs.map(leg => leg.action).join(' + ')),
          I.named(fill.side) ?? t('Unknown'), timestamp(fill.event_time_ns), fill.price, fill.quantity,
          fill.position_before ?? t('Unknown'), fill.position_after ?? t('Unknown'),
          I.orderType(fill.order_type ?? order?.order_type),
          BacktestSizing.render(fill,this.openSizing.has(fill.id),open=>{
            if (open) this.openSizing.add(fill.id); else this.openSizing.delete(fill.id);
          }), fill.commission,
          pnlCell(fill, 'realized_net'), pnlCell(fill, 'return_pct'), pnlCell(fill, 'cumulative_net'),
          fill.order_id ?? t('Unknown'), fill.position_id ?? t('Unknown'), details(fill), this.locateButton(fill)], fillHeadings);
        this.markRow(element, fill);
      });
      table.append(body); wrapper.append(table);
      if (group.fills.length > limit) {
        const more = document.createElement('button'); more.textContent = t('more_fills',{count:Math.min(50,group.fills.length-limit),shown:limit,total:group.fills.length});
        more.addEventListener('click', () => { this.childLimits.set(key, limit+50); wrapper.replaceWith(this.fillTable(group)); });
        wrapper.append(more);
      }
      return wrapper;
    }
    updateValues() {
      const indicator = this.indicators[Number($('value-series').value)];
      $('indicator-values').replaceChildren();
      for (const point of this.page('values', indicator ? indicator.points : [])) {
        row($('indicator-values'), [timestamp(point.time_ns), point.value === null ? t('Unavailable / gap') : point.value,
          t('segment',{availability:indicator.provenance === 'historical_unverified' ? t('Availability unverified') : point.source_time_ns ? time(point.source_time_ns) : t('Observed at this bar'),segment:point.segment ?? t('continuous')})]);
      }
      if (!indicator) row($('indicator-values'), [t('This strategy did not export indicator values.'), '', '']);
    }
    update() {
      this.updateEvents();
      $('candles').replaceChildren();
      this.page('candles', this.bars).forEach(bar => row($('candles'), [timestamp(bar.close_time_ns), bar.open, bar.high, bar.low, bar.close, bar.volume]));
      this.updateValues();
    }
    label(event) {
      if (event.kind === 'signal') return `${t('SIG')}${this.bySignal.get(event.id).number} · ${actionLabel(event.action)}`;
      const context = this.fillContext.get(event.id);
      return t('fill_order',{fill:context ? `${t('FILL')}${context.group.number}.${context.index+1}` : t('Unlinked fill'),order:event.order_id || t('unavailable')});
    }
    inspect(index, events, select) {
      $('selection-time').textContent = t('candle_close',{time:time(this.bars[index].close_time_ns)});
      $('selected-events').replaceChildren();
      if (!events.length) $('selected-events').textContent = t('No signal or fill aligned to this candle.');
      events.forEach(event => {
        const article = document.createElement('article');
        const button = document.createElement('button'); button.textContent = t('select_event',{event:this.label(event)});
        button.setAttribute('aria-pressed', String(this.selected.has(BacktestChartData.eventKey(event))));
        button.addEventListener('click', () => select(event));
        const original = document.createElement('p'); original.append(timestamp(event.event_time_ns));
        article.append(button, original, details(event));
        $('selected-events').append(article);
      });
    }
  }
  window.BacktestTables = { Tables };
})();
