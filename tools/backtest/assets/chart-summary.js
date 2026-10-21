/* Display the full engine summary independently of chart and event filters. */
(function (root) {
  'use strict';
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
    const digits = decimal === null ? new Intl.NumberFormat('en', { maximumFractionDigits: money ? 8 : 4 }).format(Object.is(value, -0) ? 0 : value)
      : integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (tail ? '.' + tail : '');
    const formatted = value === null || !Number.isFinite(value) ? '未提供' : digits
        + (percent ? '%' : '') + (money && currency ? ` ${currency}` : '');
    return { label, text: formatted, raw: raw == null ? null : String(raw), value };
  }
  function metrics(summary) {
    const pnl = summary.pnl || {}, returns = summary.returns || {};
    return [
      statistic('净已实现收益率', pnl['PnL% (total)'], {percent:true}),
      statistic('净已实现盈亏', pnl['PnL (total)'], {money:true, currency:summary.currency}),
      statistic('最大回撤', returns['Max Drawdown'], {percent:true, fraction:true}),
      statistic('胜率', pnl['Win Rate'], {percent:true, fraction:true}),
      statistic('Profit factor', returns['Profit Factor']),
      statistic('Sharpe · 252 days', returns['Sharpe Ratio (252 days)']),
    ];
  }
  function render(summary, element) {
    const list = document.createElement('dl'); list.className = 'performance-metrics';
    metrics(summary).forEach((metric, index) => {
      const item = document.createElement('div');
      const label = document.createElement('dt'); label.textContent = metric.label;
      const value = document.createElement('dd'); value.textContent = metric.text;
      value.title = metric.raw === null ? '本次引擎未导出此统计，不按图表或成交筛选估算。' : `引擎原始值：${metric.raw}`;
      if (index < 2 && metric.value) value.className = metric.value > 0 ? 'pnl-profit' : 'pnl-loss';
      item.append(label, value); list.append(item);
    });
    const balances = document.createElement('p'); balances.className = 'note';
    const start = statistic('', summary.starting_balance, {money:true, currency:summary.currency});
    const end = statistic('', summary.final_balance, {money:true, currency:summary.currency});
    balances.textContent = `起始余额 ${start.text} → 期末余额 ${end.text} · 订单 ${summary.orders ?? '未提供'} · 仓位记录 ${summary.positions ?? '未提供'}${summary.currency ? '' : ' · 余额币种未记录'}`;
    element.replaceChildren(list, balances);
  }
  const api = {metrics, render};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BacktestSummary = api;
})(typeof window !== 'undefined' ? window : globalThis);
