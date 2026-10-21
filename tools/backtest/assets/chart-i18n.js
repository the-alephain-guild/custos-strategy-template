/* One language per rendered report; raw identities and exported data stay intact. */
(function (root) {
  'use strict';
  const node = typeof module !== 'undefined' && module.exports;
  const config = node ? {language:'zh',messages:require('./report-locales.json')}
    : JSON.parse(document.getElementById('report-i18n').textContent);
  function create(language = 'zh') {
    if (!['zh','en'].includes(language)) throw new Error('Report language must be zh or en');
    const t = (key, values = {}) => {
      const entry = config.messages[key];
      if (!entry || typeof entry[language] !== 'string') throw new Error('Missing report translation: '+key);
      return entry[language].replace(/\{(\w+)\}/g,(match,name)=>{
        if (!(name in values)) throw new Error('Missing translation value: '+name);
        return String(values[name]);
      });
    };
    const named = value => config.messages[value] ? t(value) : value;
    return {language,locale:language === 'zh' ? 'zh-CN' : 'en-US',t,named,
      indicator:item=>item.label_i18n?.[language] || item.label,
      orderType:value=>typeof value !== 'string' || !value.trim() ? t('Unknown')
        : config.messages[value.toUpperCase()] ? t(value.toUpperCase()) : t('other_type',{value}),
      action:value=>value.split(' + ').map(named).join(' + '),
      bar:value=>value.replace(/(\d+)-(DAY|HOUR|MINUTE|SECOND|WEEK|MONTH)/g,(_,count,unit)=>t('unit.'+unit,{count})),
    };
  }
  if (node) { module.exports = {create,messages:config.messages}; return; }
  const requested = new URLSearchParams(location.search).get('lang');
  const valid = requested === null || ['zh','en'].includes(requested);
  const api = create(valid && requested ? requested : config.language);
  document.documentElement.lang = api.locale;
  document.title = api.t('Backtest chart');
  document.querySelectorAll('[data-i18n]').forEach(element=>{
    element.textContent = api.t(element.dataset.i18n);
  });
  for (const attribute of ['aria-label','title']) {
    document.querySelectorAll(`[data-i18n-${attribute}]`).forEach(element=>{
      element.setAttribute(attribute,api.t(element.getAttribute('data-i18n-'+attribute)));
    });
  }
  if (!valid) {
    const warning = document.getElementById('language-warning'); warning.hidden = false;
    warning.textContent = api.t('invalid_lang',{value:requested});
  }
  root.BacktestI18n = api;
})(typeof window !== 'undefined' ? window : globalThis);
