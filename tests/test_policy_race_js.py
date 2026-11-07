# -*- coding: utf-8 -*-
"""V2.0.1 P1：政策事件异步竞态（真实执行 valuation.js，受控 fetch）

审查场景：请求 A=贵州茅台 / B=紫金矿业，B 先返回、A 后返回 → 页面必须只留紫金矿业。
旧实现 policySeq 只在手动检索里 ++，loadStockVal 不同步，A 的慢响应能把 B 覆盖掉。
这里用 node + 最小 DOM 桩真跑 valuation.js 的 loadPolicy，按指定顺序放行响应。
"""

import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
JS = os.path.join(ROOT, "static", "js", "valuation.js")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function makeCtx() {
  const els = {};
  const el = (id) => els[id] || (els[id] = {
    id, innerHTML: '', textContent: '', disabled: false, style: {},
    classList: { add(){}, remove(){} }, setAttribute(){}, getAttribute(){ return null; },
    hasAttribute(){ return false; }, closest(){ return null; }
  });
  const calls = [];
  function fetch(url, opts) {
    let res, rej;
    const p = new Promise((a, b) => { res = a; rej = b; });
    const call = {
      url,
      resolve: (body) => res({ json: () => Promise.resolve(body) }),
      reject: (err) => rej(err),
    };
    if (opts && opts.signal) {
      opts.signal.addEventListener('abort', () => {
        const e = new Error('aborted'); e.name = 'AbortError'; rej(e);
      });
    }
    calls.push(call);
    return p;
  }
  const ctx = {
    console, setTimeout, clearTimeout, URL, AbortController, Promise, JSON, Math,
    document: {
      addEventListener() {},
      getElementById: el,
      querySelectorAll() { return []; },
      querySelector() { return null; },
      createElement: () => el('tmp'),
    },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    fetch,
    echarts: { getInstanceByDom() { return null; }, init: () => ({ setOption(){}, clear(){}, on(){} }) },
    __calls: calls,
    __el: el,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx, { filename: 'valuation.js' });
  return ctx;
}

const flush = () => new Promise((r) => setTimeout(r, 0));
const boxText = (ctx) => ctx.__el('valuation-policy').innerHTML;

async function drain(ctx, n) {
  for (let i = 0; i < (n || 6); i++) await flush();
  return boxText(ctx);
}

function policyCalls(ctx) {
  return ctx.__calls.filter((c) => String(c.url).indexOf('/api/policy-events') !== -1);
}

const results = {};

// ---------- 1. B 先回、A 后回（审查指定场景） ----------
(async () => {
  let ctx = makeCtx();
  ctx.valCurrentCode = '600519'; ctx.valCurrentName = '贵州茅台';
  ctx.loadPolicy('600519', '贵州茅台', {});
  const a = policyCalls(ctx)[0];

  ctx.valCurrentCode = '601899'; ctx.valCurrentName = '紫金矿业';
  ctx.loadPolicy('601899', '紫金矿业', {});
  const b = policyCalls(ctx)[1];

  b.resolve({ code: 200, data: { stock_code: '601899', industry: '有色金属',
    events: [{ title: 'EVENT-ZIJIN', relation_type: 'direct' }], meta: {} } });
  await drain(ctx);
  const afterB = boxText(ctx);
  a.resolve({ code: 200, data: { stock_code: '600519', industry: '白酒',
    events: [{ title: 'EVENT-MAOTAI', relation_type: 'direct' }], meta: {} } });
  const afterA = await drain(ctx);
  results.b_then_a = { afterB, afterA };

  // ---------- 2. 同一股票连续刷新，旧响应后到 ----------
  ctx = makeCtx();
  ctx.valCurrentCode = '600519'; ctx.valCurrentName = '贵州茅台';
  ctx.loadPolicy('600519', '贵州茅台', {});
  const first = policyCalls(ctx)[0];
  ctx.loadPolicy('600519', '贵州茅台', { force: true });
  const second = policyCalls(ctx)[1];
  second.resolve({ code: 200, data: { stock_code: '600519', industry: '白酒',
    events: [{ title: 'EVENT-NEW', relation_type: 'direct' }], meta: {} } });
  await drain(ctx);
  first.resolve({ code: 200, data: { stock_code: '600519', industry: '白酒',
    events: [{ title: 'EVENT-OLD', relation_type: 'direct' }], meta: {} } });
  results.same_stock = await drain(ctx);

  // ---------- 3. 三只快速连切，最后一只最慢返回 ----------
  ctx = makeCtx();
  const seq = [['600519', '贵州茅台'], ['601899', '紫金矿业'], ['002594', '比亚迪']];
  seq.forEach(([c, n]) => { ctx.valCurrentCode = c; ctx.valCurrentName = n; ctx.loadPolicy(c, n, {}); });
  const cs = policyCalls(ctx);
  cs[1].resolve({ code: 200, data: { stock_code: '601899', events: [{ title: 'EVENT-ZIJIN' }], meta: {} } });
  cs[0].resolve({ code: 200, data: { stock_code: '600519', events: [{ title: 'EVENT-MAOTAI' }], meta: {} } });
  cs[2].resolve({ code: 200, data: { stock_code: '002594', events: [{ title: 'EVENT-BYD' }], meta: {} } });
  results.three_switch = await drain(ctx);

  // ---------- 4. 旧请求被 abort 后返回，不许把错误状态贴上去 ----------
  ctx = makeCtx();
  ctx.valCurrentCode = '600519'; ctx.valCurrentName = '贵州茅台';
  ctx.loadPolicy('600519', '贵州茅台', {});
  const old = policyCalls(ctx)[0];
  ctx.valCurrentCode = '601899'; ctx.valCurrentName = '紫金矿业';
  ctx.loadPolicy('601899', '紫金矿业', {});
  policyCalls(ctx)[1].resolve({ code: 200, data: { stock_code: '601899',
    events: [{ title: 'EVENT-ZIJIN' }], meta: {} } });
  await drain(ctx);
  old.reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
  results.aborted_then_ok = await drain(ctx);

  // ---------- 5. 失败状态不能被当成"没有政策" ----------
  ctx = makeCtx();
  ctx.valCurrentCode = '600519'; ctx.valCurrentName = '贵州茅台';
  ctx.loadPolicy('600519', '贵州茅台', {});
  policyCalls(ctx)[0].resolve({ code: 500, msg: 'boom' });
  results.failure = await drain(ctx);

  // ---------- 6. 空结果 = 正常空状态 ----------
  ctx = makeCtx();
  ctx.valCurrentCode = '600519'; ctx.valCurrentName = '贵州茅台';
  ctx.loadPolicy('600519', '贵州茅台', {});
  policyCalls(ctx)[0].resolve({ code: 200, data: { stock_code: '600519', events: [],
    industry: '白酒', meta: { data_status: 'ok' } } });
  results.empty = await drain(ctx);

  // ---------- 7. 响应里的股票码与页面不一致 → 丢弃 ----------
  ctx = makeCtx();
  ctx.valCurrentCode = '601899'; ctx.valCurrentName = '紫金矿业';
  ctx.loadPolicy('601899', '紫金矿业', {});
  policyCalls(ctx)[0].resolve({ code: 200, data: { stock_code: '600519',
    events: [{ title: 'EVENT-MAOTAI' }], meta: {} } });
  results.mismatched = await drain(ctx);

  // ---------- 8. 切股票时确实发出了新的 policy 请求（自动检索）+ 加载态 ----------
  ctx = makeCtx();
  try {
    ctx.loadStockVal('601899', '紫金矿业');
    const pc = policyCalls(ctx);
    results.loadstock_auto = { ok: true, urls: pc.map((c) => c.url), loading: boxText(ctx) };
  } catch (e) {
    results.loadstock_auto = { ok: false, error: String(e && e.message || e) };
  }

  ctx = makeCtx();
  ctx.valCurrentCode = '600519'; ctx.valCurrentName = '贵州茅台';
  ctx.loadPolicy('600519', '贵州茅台', {});
  results.loading_state = { loading: boxText(ctx), url: policyCalls(ctx)[0].url };

  console.log(JSON.stringify(results));
})();
"""


@unittest.skipUnless(NODE, "环境没有 node，无法真实执行 valuation.js")
class TestPolicyRace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run([NODE, "-e", HARNESS, JS],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-2000:]
        cls.r = json.loads(r.stdout.strip().splitlines()[-1])

    def test_b_then_a_keeps_b(self):
        """审查场景：B 先返回、A 后返回，页面必须仍是紫金矿业。"""
        d = self.r["b_then_a"]
        self.assertIn("EVENT-ZIJIN", d["afterB"])
        self.assertIn("EVENT-ZIJIN", d["afterA"], "晚到的旧响应把新股票覆盖了")
        self.assertNotIn("EVENT-MAOTAI", d["afterA"])

    def test_same_stock_refresh_keeps_latest(self):
        d = self.r["same_stock"]
        self.assertIn("EVENT-NEW", d)
        self.assertNotIn("EVENT-OLD", d, "同一股票的旧响应覆盖了新结果")

    def test_three_rapid_switches_keep_last(self):
        d = self.r["three_switch"]
        self.assertIn("EVENT-BYD", d)
        for bad in ("EVENT-ZIJIN", "EVENT-MAOTAI"):
            self.assertNotIn(bad, d)

    def test_aborted_request_does_not_paint_error(self):
        d = self.r["aborted_then_ok"]
        self.assertIn("EVENT-ZIJIN", d)
        self.assertNotIn("请求失败", d)

    def test_failure_state_is_explicit(self):
        d = self.r["failure"]
        self.assertIn("请求失败", d)
        self.assertNotIn("暂无已确认的相关政策与事件", d)

    def test_empty_state_is_not_failure(self):
        d = self.r["empty"]
        self.assertIn("暂无已确认的相关政策与事件", d)
        self.assertNotIn("请求失败", d)

    def test_mismatched_code_discarded(self):
        self.assertNotIn("EVENT-MAOTAI", self.r["mismatched"])

    def test_loading_state_and_auto_url(self):
        d = self.r["loading_state"]
        self.assertIn("正在加载", d["loading"])
        self.assertIn("live=1", d["url"])
        self.assertIn("600519", d["url"])

    def test_stock_switch_auto_triggers_policy_fetch(self):
        """切股票必须自动检索，不能等用户点按钮。"""
        d = self.r["loadstock_auto"]
        self.assertTrue(d["ok"], d.get("error"))
        self.assertTrue(any("stock_code=601899" in u and "live=1" in u for u in d["urls"]),
                        d["urls"])


if __name__ == "__main__":
    unittest.main()
