const test = require('node:test');
const assert = require('node:assert/strict');
const D = require('../tools/backtest/assets/chart-data.js');
const S = require('../tools/backtest/assets/chart-summary.js');
const Z = require('../tools/backtest/assets/chart-sizing.js');
const L = require('../tools/backtest/assets/chart-i18n.js');
test('both languages cover the entire message catalogue and localize enums and indicators', () => {
  const zh=L.create('zh'),en=L.create('en');
  for(const [key,entry] of Object.entries(L.messages)) {
    const parameters=Object.fromEntries([...entry.en.matchAll(/\{(\w+)\}/g)].map(m=>[m[1],'1']));
    assert.equal(typeof zh.t(key,parameters),'string');assert.equal(typeof en.t(key,parameters),'string');
  }
  assert.equal(zh.action('exit_long + enter_short'),'平多 + 开空');
  assert.equal(en.action('exit_long + enter_short'),'exit long + enter short');
  assert.equal(zh.orderType('STOP_LIMIT'),'止损限价');assert.equal(en.orderType('STOP_LIMIT'),'Stop limit');
  assert.equal(zh.named('compound'),'复利');assert.equal(en.named('compound'),'Compounding');
  assert.equal(zh.bar('1-DAY'),'1日');assert.equal(en.bar('1-DAY'),'1 day');
  const series={label:'raw-series',label_i18n:{zh:'候选值',en:'Candidate'}};
  assert.equal(zh.indicator(series),'候选值');assert.equal(en.indicator(series),'Candidate');
  assert.equal(en.indicator({label:'raw-name'}),'raw-name');
  assert.throws(()=>L.create('fr'),/language/);assert.throws(()=>en.t('nonexistent'),/translation/);
});
test('English summary and sizing output contain no Chinese UI text and retain numeric values', () => {
  const fs=require('node:fs'),vm=require('node:vm');
  const win={BacktestI18n:L.create('en'),BacktestChartData:D};
  for(const source of ['chart-summary.js','chart-sizing.js']) vm.runInNewContext(fs.readFileSync(require.resolve('../tools/backtest/assets/'+source),'utf8'),{window:win});
  const summary={currency:'USDT',pnl:{'PnL% (total)':-3.255966919,'PnL (total)':-325.5966919,'Win Rate':0},returns:{'Max Drawdown':-0.034049,'Profit Factor':0.5,'Sharpe Ratio (252 days)':-3}};
  const english=win.BacktestSummary.metrics(summary),chinese=S.metrics(summary);
  assert.deepEqual(Array.from(english,x=>x.text),chinese.map(x=>x.text));
  assert.ok(english.every(x=>!/[\u4e00-\u9fff]/.test(x.label)));
  const fields=win.BacktestSizing.rows({capital_mode:'compound',size_type:'percentage',available_balance:'9950.86719370',raw_quantity:'0.01026705447671953140879671235'},{commission:'0.96920300 USDT'});
  assert.ok(fields.every(x=>!/[\u4e00-\u9fff]/.test(x.label+x.value)));
  assert.equal(fields.find(x=>x.label==='Available balance').value,'9950.8671937');
});
test('sizing details use immutable snapshots, preserve unknowns and separate each fill fee', () => {
  const s={currency:'USDT',currency_precision:8,available_balance:'9950.86719370',account_equity:'9950.86719370',base_target_notional:'995.086719370',target_notional:'995.086',raw_quantity:'0.01026705447671953140879671235',rounded_quantity:'0.010',quantity_step:'0.001'};
  const original=JSON.stringify(s),a=Z.rows(s,{commission:'0.01 USDT'}),b=Z.rows(s,{commission:'0.02 USDT'});
  assert.equal(a.find(x=>x.label==='可用余额').value,'9950.8671937 USDT');
  assert.equal(a.find(x=>x.label==='基础目标名义本金').value,'995.08671937 USDT');
  assert.equal(a.find(x=>x.label.startsWith('取整前')).value,s.raw_quantity);
  assert.equal(a.at(-1).value,'0.01 USDT');assert.equal(b.at(-1).value,'0.02 USDT');
  assert.equal(a.find(x=>x.label==='实际提交数量').value,'未记录');
  assert.equal(JSON.stringify(s),original);
  assert.equal(Z.rows({},{}).find(x=>x.label==='账户权益').value,'未记录');
  assert.equal(Z.rows({observed_time_ns:'invalid'},{}).find(x=>x.label==='定仓观察时间 UTC').value,'未记录');
  assert.equal(Z.rows({available_balance:'0'},{}).find(x=>x.label==='可用余额').value,'0');
});
test('whole-run statistics preserve percent units and never invent missing results', () => {
  const summary = {currency:'USDT',pnl:{'PnL% (total)':-2.673896194,'PnL (total)':-267.3896194,'Win Rate':0.25},returns:{'Max Drawdown':-0.029865499,'Profit Factor':0,'Sharpe Ratio (252 days)':-2.317837}};
  const original = JSON.stringify(summary), values = S.metrics(summary);
  assert.deepEqual(values.map(x=>x.text), ['-2.6739%', '-267.3896194 USDT', '-2.9865%', '25%', '0', '-2.3178']);
  assert.equal(JSON.stringify(summary), original);
  assert.equal(S.metrics({}).length, 6);
  assert.ok(S.metrics({}).every(x=>x.text==='未提供'));
  for (const invalid of [null, '', true, NaN, Infinity, 'NaN']) {
    assert.equal(S.metrics({pnl:{'PnL% (total)':invalid}})[0].text, '未提供');
  }
  assert.equal(S.metrics({pnl:{'PnL% (total)':0}})[0].text,'0%');
  assert.equal(S.metrics({currency:'TEST',pnl:{'PnL (total)':'9007199254740993.123456785'}})[1].text,'9,007,199,254,740,993.12345679 TEST');
});
const T = 1735689600000000000n;
const ns = n => String(T + BigInt(n)*1000000000n);
test('sparse confirmed paths use source coordinates only after observation; projections use the current candidate', () => {
  const pivot = {key:'arbitrary-pivots',render:'path',points:[
    {time_ns:ns(3),source_time_ns:ns(1),value:100},
    {time_ns:ns(4),value:null},
    {time_ns:ns(8),source_time_ns:ns(5),value:120},
  ]};
  const candidate = {key:'arbitrary-leg',render:'path',projection_from:pivot.key,points:[
    {time_ns:ns(4),source_time_ns:ns(4),value:115},
    {time_ns:ns(5),source_time_ns:ns(5),value:120},
    {time_ns:ns(8),source_time_ns:ns(8),value:114},
    {time_ns:ns(9),value:null},
  ]};
  const all = [pivot,candidate], original = JSON.stringify(all);
  assert.deepEqual(D.indicatorParts(pivot,all,ns(2)), []);
  assert.deepEqual(D.indicatorParts(pivot,all,ns(7))[0].data,[{time:D.seconds(ns(1)),value:100}]);
  assert.deepEqual(D.indicatorParts(pivot,all,ns(8))[0].data,[{time:D.seconds(ns(1)),value:100},{time:D.seconds(ns(5)),value:120}]);
  assert.deepEqual(D.indicatorParts(candidate,all,ns(7))[0].data,[{time:D.seconds(ns(1)),value:100},{time:D.seconds(ns(5)),value:120}]);
  assert.deepEqual(D.indicatorParts(candidate,all,ns(8))[0].data,[{time:D.seconds(ns(5)),value:120},{time:D.seconds(ns(8)),value:114}]);
  assert.deepEqual(D.indicatorParts(candidate,all,ns(9)), []);
  const changed = structuredClone(all); changed[0].points.at(-1).value=900; changed[1].points.at(-2).value=800;
  for (let cut=3; cut<8; cut++) for(let i=0;i<2;i++) {
    assert.deepEqual(D.indicatorParts(all[i],all,ns(cut)),D.indicatorParts(changed[i],changed,ns(cut)));
    const prefix=all.map(s=>({...s,points:s.points.filter(p=>BigInt(p.time_ns)<=BigInt(ns(cut)))}));
    assert.deepEqual(D.indicatorParts(all[i],all,ns(cut)),D.indicatorParts(prefix[i],prefix,ns(cut)));
  }
  assert.equal(JSON.stringify(all),original);
});
test('replay excludes future candles, values, signals, partial fills and terminal orders without modifying records', () => {
  const report={candles:[1,2,3].map(n=>({instrument_id:'A',bar_type:'day',close_time_ns:ns(n)})),
    indicators:[{instrument_id:'A',bar_type:'day',points:[1,2,3].map(n=>({time_ns:ns(n),value:n}))}],
    signals:[{id:'s',instrument_id:'A',event_time_ns:ns(2)}],
    fills:[{id:'f1',signal_id:'s',instrument_id:'A',event_time_ns:ns(2)},{id:'f2',signal_id:'s',instrument_id:'A',event_time_ns:ns(3)}],
    orders:[{id:'order',status:'FILLED'}]};
  const original=JSON.stringify(report),early=D.replayView(report,'A','day',1),mid=D.replayView(report,'A','day',2);
  assert.equal(early.signals.length,0); assert.equal(early.fills.length,0);
  assert.equal(mid.candles.length,2); assert.equal(mid.indicators[0].points.length,2);
  assert.deepEqual(mid.fills,[report.fills[0]]); assert.deepEqual(mid.orders,[]);
  assert.equal(mid.signals[0].event_time_ns,ns(2)); assert.equal(mid.fills[0].event_time_ns,ns(2));
  assert.equal(D.groupSignals(mid.signals,mid.fills).groups[0].fills.length,1);
  assert.deepEqual(D.replayView(report,'A','day',Infinity).orders,report.orders);
  assert.equal(JSON.stringify(report),original);
});
test('partial replay withholds unverified legacy histories and isolates another candle feed', () => {
  const report={candles:[1,2].map(n=>({instrument_id:'A',bar_type:'day',close_time_ns:ns(n)})),
    indicators:[{instrument_id:'A',bar_type:'day',provenance:'historical_unverified',points:[{time_ns:ns(1),value:99}]},
      {instrument_id:'B',bar_type:'day',points:[{time_ns:ns(1),value:88}]}],
    signals:[{instrument_id:'B',event_time_ns:ns(1)}],fills:[],orders:[]};
  const view=D.replayView(report,'A','day',1);
  assert.equal(view.indicators.length,1); assert.deepEqual(view.indicators[0].points,[]);
  assert.deepEqual(view.signals,[]);
  assert.equal(D.replayView(report,'A','day',Infinity).indicators[0].points[0].value,99);
});
test('financial display rounds decimal strings without binary-float precision loss', () => {
  assert.equal(D.formatDecimal('9007199254740993.123456785',8),'9007199254740993.12345679');
  assert.equal(D.formatDecimal('-0.000000004',8),'0');
  assert.equal(D.formatDecimal('0.000000005',8),'0.00000001');
  assert.equal(D.formatDecimal('2.5',0),'3');
  assert.equal(D.formatDecimal('-16.666666666666',4),'-16.6667');
  assert.equal(D.formatDecimal(null,8),null);
  assert.equal(D.formatDecimal('NaN',8),null);
});
test('execution types retain native semantics and missing history stays unknown', () => {
  assert.equal(D.orderTypeLabel('MARKET'), 'Market');
  assert.equal(D.orderTypeLabel('LIMIT'), 'Limit');
  assert.equal(D.orderTypeLabel('STOP_LIMIT'), 'Stop limit');
  assert.equal(D.orderTypeLabel('TRAILING_STOP_MARKET'), 'Trailing stop market');
  for (const missing of [undefined, null, '', 0]) assert.equal(D.orderTypeLabel(missing), 'Unknown');
});
const bars = [1n, 2n, 3n].map(i => ({open_time_ns: String(T+(i-1n)*1000000000n), close_time_ns: String(T+i*1000000000n)}));
test('off-grid events align forward; original nanoseconds remain untouched', () => {
  const ns = String(T+1000000001n);
  assert.equal(D.alignForward(ns, bars), Number(T/1000000000n)+2);
  assert.equal(ns, '1735689601000000001');
  assert.equal(D.alignForward(String(T+4000000000n), bars), null);
  assert.equal(D.alignForward(String(T-1n), bars), null);
});
test('exact close boundaries do not shift events one bar backward', () => {
  assert.equal(D.alignForward(String(T+1000000000n), bars), Number(T/1000000000n)+1);
});
test('millisecond text does not round a fill backward through floating-point seconds', () => {
  assert.equal(D.isoTime(String(T+2000000n)), '2025-01-01T00:00:00.002Z');
  assert.equal(D.isoTime(String(T+2999999n)), '2025-01-01T00:00:00.002Z');
});
test('gaps and segment colors split arbitrary indicators without name-specific logic', () => {
  const points = [1, 2, null, 4, 5].map((value, i) => ({time_ns: String(T+BigInt(i)*1000000000n), value, segment: 1, color: i === 4 ? '#ef5350' : '#26a69a'}));
  const result = D.segments(points);
  assert.deepEqual(result.map(p => p.data.map(p => p.value)), [[1, 2], [4], [5]]);
});
test('one signal retains N fills across orders and partial executions without changing times', () => {
  const signals = [{id:'signal-1',event_time_ns:String(T)}, {id:'signal-2',event_time_ns:String(T+10n)}];
  const fills = [
    {id:'f2',order_id:'o1',signal_id:'signal-1',quantity:'0.4',event_time_ns:String(T+2n)},
    {id:'f1',order_id:'o1',signal_id:'signal-1',quantity:'0.3',event_time_ns:String(T+1n)},
    {id:'f3',order_id:'o2',signal_id:'signal-1',quantity:'0.3',event_time_ns:String(T+2n)},
    {id:'unknown',order_id:'o3',signal_id:'absent',event_time_ns:String(T+3n)},
    {id:'null',order_id:'o4',signal_id:null,event_time_ns:String(T+4n)},
  ];
  const original = JSON.stringify({signals,fills});
  const tree = D.groupSignals(signals,fills);
  assert.deepEqual(tree.groups[0].fills.map(f=>f.id), ['f1','f2','f3']);
  assert.equal(tree.groups[0].number, 1);
  assert.equal(tree.groups[1].fills.length, 0);
  assert.deepEqual(tree.unlinked.map(f=>f.id), ['unknown','null']);
  assert.equal(tree.groups.reduce((n,g)=>n+g.fills.length,0)+tree.unlinked.length, fills.length);
  assert.equal(tree.groups[0].fills[0].event_time_ns, String(T+1n));
  assert.equal(JSON.stringify({signals,fills}), original);
});
test('duplicate signal identities fail instead of arbitrarily assigning fills', () => {
  assert.throws(()=>D.groupSignals([{id:'s'},{id:'s'}],[]), /duplicate signal/);
});
test('pixel labels merge collisions without moving or dropping original event references', () => {
  const items = Array.from({length:80}, (_, i) => ({x:i*3, text:`SIG${i+1} EL (3 fills)`, events:[{id:`s${i}`}]}));
  const original = JSON.stringify(items);
  for (const width of [160, 280, 640, 1440]) {
    const layout = D.layoutLabels(items, width, text => text.length*7, 'SIG');
    layout.forEach((label, i) => {
      assert.ok(label.left >= 0 && label.left+label.width <= width);
      if (i) assert.ok(layout[i-1].left+layout[i-1].width+8 <= label.left);
    });
    assert.deepEqual(layout.flatMap(label=>label.items).map(item=>item.events[0].id), items.map(item=>item.events[0].id));
  }
  assert.equal(JSON.stringify(items), original);
});
test('coincident labels and labels at plot edges remain accessible with no overlap', () => {
  const items = [0,0,1,278,280].map((x,i)=>({x,text:`Long label ${i}`,events:[{id:String(i)}]}));
  const layout = D.layoutLabels(items, 280, text=>text.length*8, 'SIG');
  assert.equal(layout.flatMap(label=>label.items).length, 5);
  assert.ok(layout.length < items.length);
  assert.ok(layout.every(label=>label.left >= 0 && label.left+label.width <= 280));
  assert.deepEqual(D.layoutLabels([],280,text=>text.length,'SIG'), []);
});
test('resizing or changing visible groups preserves the exact selected fill page', () => {
  const groups = Array.from({length:36}, (_,i)=>({signal:{kind:'signal',id:`s${i+1}`},fills:[{kind:'fill',id:`f${i+1}`,event_time_ns:String(T)}]}));
  const selected = new Set(['fill:f33']);
  assert.equal(D.selectionPage(groups, selected, 15, 0), 2);
  assert.equal(D.selectionPage(groups, selected, 25, 0), 1);
  assert.equal(D.selectionPage(groups.slice(20), selected, 15, 2), 0);
  assert.equal(D.selectionPage(groups, new Set(['signal:f33']), 15, 1), 1);
  assert.equal(D.selectionPage(groups, new Set(), 15, 2), 2);
});
