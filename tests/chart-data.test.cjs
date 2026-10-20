const test = require('node:test');
const assert = require('node:assert/strict');
const D = require('../tools/backtest/assets/chart-data.js');
const T = 1735689600000000000n;
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
