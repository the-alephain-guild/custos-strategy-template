/* Display the signal's frozen trigger observation; never infer it from later data. */
(function (root) {
  'use strict';
  const node = typeof module !== 'undefined' && module.exports;
  const I = node ? require('./chart-i18n.js').create() : root.BacktestI18n;
  const D = node ? require('./chart-data.js') : root.BacktestChartData;
  const t = I.t;
  function content(signal) {
    const snapshot = signal.metadata?.trigger_details;
    if (!snapshot) return null;
    const number = value => typeof value === 'string' ? D.formatDecimal(value,18)
      : new Intl.NumberFormat(I.locale,{maximumSignificantDigits:12,useGrouping:false}).format(value);
    return {reason:snapshot.reason_i18n[I.language],fields:snapshot.fields.map(field=>({
      key:field.key,label:field.label_i18n[I.language],raw:field.value,
      value:field.value == null ? t('未记录') : field.format === 'time' ? D.isoTime(field.value)
        : field.format === 'number' ? number(field.value)
        : field.format === 'percent' ? number(field.value)+'%'
        : field.value_i18n?.[I.language] || field.value,
      time:field.format === 'time' && field.value != null,
    }))};
  }
  function render(signal, locate, canLocate) {
    const details = document.createElement('section'); details.className = 'trigger-details';
    details.setAttribute('aria-label',t('Signal trigger details'));
    details.dataset.signalId = signal.id;
    const snapshot = content(signal);
    if (!snapshot) {
      const note = document.createElement('p'); note.className = 'note'; note.textContent = t('trigger_missing'); details.append(note);
    } else {
      const reason = document.createElement('p'); reason.className = 'trigger-reason'; reason.textContent = snapshot.reason; details.append(reason);
      const list = document.createElement('dl'); list.className = 'trigger-fields';
      for (const field of snapshot.fields) {
        const group = document.createElement('div'); group.dataset.triggerField = field.key;
        const label = document.createElement('dt'); label.textContent = field.label;
        const value = document.createElement('dd');
        const text = document.createElement(field.time ? 'time' : 'span'); text.textContent = field.value;
        if (field.time) { text.className = 'timestamp'; text.dateTime = field.value; group.className = 'trigger-wide'; }
        if (field.raw != null) text.title = t('raw_value',{value:field.raw});
        value.append(text); group.append(label,value); list.append(group);
      }
      details.append(list);
    }
    const button = document.createElement('button'); button.type = 'button'; button.textContent = t('Locate this signal');
    button.disabled = !canLocate;
    button.addEventListener('click',locate); details.append(button);
    const note = document.createElement('p'); note.className = 'note'; note.textContent = t('trigger_timing'); details.append(note);
    return details;
  }
  const api = {content,render};
  if (node) module.exports = api;
  else root.BacktestTrigger = api;
})(typeof window !== 'undefined' ? window : globalThis);
