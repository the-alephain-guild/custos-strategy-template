/* Display the full engine summary independently of chart and event filters. */
(function (root) {
  'use strict';
  const I = typeof module !== 'undefined' && module.exports ? require('./chart-i18n.js').create() : root.BacktestI18n;
  const t = I.t;
  const D = typeof module !== 'undefined' && module.exports ? require('./chart-data.js') : root.BacktestChartData;
  function numeric(value) {
    if (typeof value !== 'number' && typeof value !== 'string') return null;
    if (typeof value === 'string' && !value.trim()) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  function statistic(label, raw, { percent = false, fraction = false, money = false, currency = '' } = {}) {
    let value = numeric(raw);
    if (value !== null && fraction) value *= 100;
    const decimal = money && typeof raw === 'string' ? D.formatDecimal(raw, 8) : null;
    const [integer, tail] = (decimal || '').split('.');
    const digits = decimal === null ? new Intl.NumberFormat(I.locale, { maximumFractionDigits: money ? 8 : 4 }).format(Object.is(value, -0) ? 0 : value)
      : integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (tail ? '.' + tail : '');
    const formatted = value === null || !Number.isFinite(value) ? t('未提供') : digits
        + (percent ? '%' : '') + (money && currency ? ` ${currency}` : '');
    return { label, text: formatted, raw: raw == null ? null : String(raw), value };
  }
  function metrics(summary) {
    const pnl = summary.pnl || {}, returns = summary.returns || {};
    return [
      statistic(t('净已实现收益率'), pnl['PnL% (total)'], {percent:true}),
      statistic(t('净已实现盈亏'), pnl['PnL (total)'], {money:true, currency:summary.currency}),
      statistic(t('最大回撤'), returns['Max Drawdown'], {percent:true, fraction:true}),
      statistic(t('胜率'), pnl['Win Rate'], {percent:true, fraction:true}),
      statistic(t('Profit factor'), returns['Profit Factor']),
      statistic(t('Sharpe · 252 days'), returns['Sharpe Ratio (252 days)']),
    ];
  }
  function render(summary, element) {
    const list = document.createElement('dl'); list.className = 'performance-metrics';
    metrics(summary).forEach((metric, index) => {
      const item = document.createElement('div');
      const label = document.createElement('dt'); label.textContent = metric.label;
      const value = document.createElement('dd'); value.textContent = metric.text;
      value.title = metric.raw === null ? t('missing_stat') : t('engine_value',{value:metric.raw});
      if (index < 2 && metric.value) value.className = metric.value > 0 ? 'pnl-profit' : 'pnl-loss';
      item.append(label, value); list.append(item);
    });
    const balances = document.createElement('p'); balances.className = 'note';
    const start = statistic('', summary.starting_balance, {money:true, currency:summary.currency});
    const end = statistic('', summary.final_balance, {money:true, currency:summary.currency});
    balances.textContent = t('balances',{start:start.text,end:end.text,orders:summary.orders ?? t('未提供'),positions:summary.positions ?? t('未提供'),currencyNote:summary.currency ? '' : t('missing_currency')});
    element.replaceChildren(list, balances);
  }
  const api = {metrics, render};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BacktestSummary = api;
})(typeof window !== 'undefined' ? window : globalThis);
