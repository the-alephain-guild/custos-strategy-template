const test = require('node:test');
const assert = require('node:assert/strict');
const D = require('../tools/backtest/assets/chart-data.js');
const T = 1735689600000000000n;
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
test('gaps and segment colors split arbitrary indicators without name-specific logic', () => {
  const points = [1, 2, null, 4, 5].map((value, i) => ({time_ns: String(T+BigInt(i)*1000000000n), value, segment: 1, color: i === 4 ? '#ef5350' : '#26a69a'}));
  const result = D.segments(points);
  assert.deepEqual(result.map(p => p.data.map(p => p.value)), [[1, 2], [4], [5]]);
});
