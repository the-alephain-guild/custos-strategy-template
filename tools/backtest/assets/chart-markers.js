/* Pixel-based label grouping does not change the recorded chart timestamps. */
(function () {
  'use strict';
  const D = window.BacktestChartData;
  const t = window.BacktestI18n.t;
  class Annotations {
    constructor(chart, select) {
      this.chart = chart;
      this.select = select;
      this.items = [];
      this.selected = new Set();
      this.layer = document.createElement('div');
      this.layer.className = 'chart-annotations';
      this.line = document.createElement('div');
      this.line.className = 'chart-selection-line';
      this.line.hidden = true;
      this.layer.append(this.line);
      const pane = chart.panes()[0].getHTMLElement();
      pane.style.position = 'relative';
      pane.append(this.layer);
      this.measure = document.createElement('canvas').getContext('2d');
      this.measure.font = '12px system-ui';
    }
    setItems(items) { this.items = items; this.draw(); }
    highlight(events, stamp) {
      this.selected = new Set(events.map(D.eventKey));
      this.stamp = stamp;
      this.draw();
    }
    draw() {
      this.layer.querySelectorAll('button').forEach(button => button.remove());
      const width = this.chart.timeScale().width();
      for (const kind of ['signal', 'fill']) {
        const items = this.items.filter(item => item.kind === kind).map(item => ({...item, x:this.chart.timeScale().timeToCoordinate(item.time)}))
          .filter(item => item.x !== null && item.x >= 0 && item.x <= width);
        for (const label of D.layoutLabels(items, width, text => this.measure.measureText(text).width, t(kind === 'signal' ? 'SIG' : 'FILL'))) {
          const events = label.items.flatMap(item => item.events);
          const button = document.createElement('button');
          button.type = 'button';
          button.className = `chart-event-label ${kind}`;
          button.textContent = label.text;
          button.style.left = label.left + 'px';
          button.style.width = label.width + 'px';
          const description = events.length > 3 ? t('crowded',{label:label.text,count:events.length})
            : label.items.map(item => item.description).join('; ');
          button.setAttribute('aria-label', description);
          button.title = description;
          button.setAttribute('aria-pressed', String(events.some(event => this.selected.has(D.eventKey(event)))));
          button.addEventListener('click', event => {
            event.stopPropagation();
            this.select(events, label.items[0].time);
          });
          this.layer.append(button);
        }
      }
      const x = this.stamp === undefined ? null : this.chart.timeScale().timeToCoordinate(this.stamp);
      this.line.hidden = x === null || x < 0 || x > width;
      if (!this.line.hidden) this.line.style.left = x + 'px';
    }
  }
  window.BacktestAnnotations = { Annotations };
})();
