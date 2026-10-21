/* Audit observed entry inputs; do not recalculate budgets from later fills. */
(function (root) {
  'use strict';
  const D = typeof module !== 'undefined' && module.exports ? require('./chart-data.js') : root.BacktestChartData;
  function observedTime(value) {
    try { return typeof value === 'string' && /^\d+$/.test(value) ? D.isoTime(value) : '未记录'; }
    catch { return '未记录'; }
  }
  function rows(snapshot, fill) {
    const s = snapshot || {}, currency = s.currency || '';
    const amount = (key, money = false) => {
      const raw = s[key];
      const formatted = typeof raw === 'string' ? D.formatDecimal(raw, money ? (s.currency_precision ?? 8) : 18) : null;
      return formatted === null ? '未记录' : formatted + (money && currency ? ' '+currency : '');
    };
    const item = (label, key, money = false) => ({label, value:amount(key,money), raw:s[key] ?? null});
    return [
      {label:'定仓观察时间 UTC',value:observedTime(s.observed_time_ns),wide:true},
      item('可用余额','available_balance',true), item('账户权益','account_equity',true),
      item('账户总额','account_total',true), item('锁定资金','locked_balance',true),
      {label:'资金模式',value:s.capital_mode || '未记录'}, {label:'定仓方法',value:s.size_type || '未记录'},
      item('实际基础资金基数','sizing_capital',true), item('基础目标名义本金','base_target_notional',true),
      item('定仓返回名义本金','target_notional',true), item('订单输入名义本金','order_notional',true),
      item('参考价格','reference_price',true), item('合约乘数','multiplier'),
      {label:'取整前数量 · 按线性订单输入复算',value:s.raw_quantity || '未记录',wide:true},
      item('订单生成数量','rounded_quantity'), item('实际提交数量','submitted_quantity'),
      item('数量步长','quantity_step'),
      {label:'Maker 费率',value:s.maker_fee_pct == null ? '未记录' : s.maker_fee_pct+'%'},
      {label:'Taker 费率',value:s.taker_fee_pct == null ? '未记录' : s.taker_fee_pct+'%'},
      item('Taker 参考手续费','estimated_taker_fee',true),
      {label:'实际手续费 · 本笔 Fill',value:fill.commission || '未记录',wide:true},
    ];
  }
  function render(fill, open, toggle) {
    if (!fill.legs.some(leg=>leg.action.startsWith('enter_'))) {
      const note = document.createElement('span'); note.textContent = '退出已有仓位'; return note;
    }
    const details = document.createElement('details'); details.className = 'sizing-details'; details.open = open;
    const heading = document.createElement('summary'); heading.textContent = '定仓明细'; details.append(heading);
    const snapshot = fill.sizing;
    if (!snapshot) {
      const note = document.createElement('p'); note.textContent = '此报告未记录该订单的定仓过程。重新回测可采集；不以最终余额反推历史。'; details.append(note);
    } else {
      const list = document.createElement('dl'); list.className = 'sizing-fields';
      rows(snapshot, fill).forEach(field => {
        const group = document.createElement('div'); if (field.wide) group.className = 'sizing-wide';
        const label = document.createElement('dt'); label.textContent = field.label;
        const value = document.createElement('dd'); value.textContent = field.value;
        if (field.raw != null) value.title = `原始记录：${field.raw}`;
        group.append(label,value); list.append(group);
      });
      details.append(list);
      const note = document.createElement('p'); note.className = 'note';
      note.textContent = '资金为定仓调用时快照。基础目标、风控与精度处理后的定仓返回值、订单实际输入可能不同。取整前数量按当时名义本金、价格和乘数复算；生成及提交数量直接观察真实订单。一订单多笔 Fill 共用此快照，实际手续费仅属于本笔成交。参考手续费不是预留凭据，不从历史余额重复扣除。'; details.append(note);
      if (snapshot.balance_status || snapshot.equity_status || snapshot.component_status) {
        const reason = document.createElement('p'); reason.className = 'note';
        reason.textContent = [snapshot.balance_status ? '资金快照未取得完整记录。' : '',snapshot.equity_status ? '账户权益未取得可靠估值。' : '',snapshot.component_status ? '此入口未提供完整的标准定仓字段。' : ''].filter(Boolean).join(' '); details.append(reason);
      }
    }
    let expanded = open;
    details.addEventListener('toggle',()=>{
      toggle(details.open);
      if (details.open && !expanded) details.scrollIntoView({block:'start',inline:'nearest',behavior:'auto'});
      expanded = details.open;
    });
    return details;
  }
  const api = {rows,render};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BacktestSizing = api;
})(typeof window !== 'undefined' ? window : globalThis);
