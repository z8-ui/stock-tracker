#!/usr/bin/env node
/* S3-3C r4：自动刷新链路集成测试。
 * 真实执行：static/js/app.js（startAutoRefresh）+ static/js/heatmap.js（loadHeatmapData）
 *          + templates/heatmap.html 里的真实注册片段（从文件里抽出来执行）。
 * 定时器用受控 setInterval，不等待真实 15 秒。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
let failed = 0;
function ok(label, cond, extra) {
    if (!cond) failed++;
    console.log('  ' + (cond ? 'PASS' : 'FAIL') + '  ' + label + (extra ? '  ' + extra : ''));
}
const unhandled = [];
process.on('unhandledRejection', (e) => unhandled.push(String(e)));

/* ---------- 受控定时器 + DOM 桩 ---------- */
const timers = [];
const handlers = [];
const els = {};
function el(id) {
    if (!els[id]) els[id] = { id, textContent: '', className: '', style: {}, addEventListener() {} };
    return els[id];
}

const state = {
    mode: 'ok',        // ok | fail
    fetchCalls: 0,
    treemap: 0,
    bars: [],
    logs: []
};
const HEAD = {
    'X-Data-Fetch-Status': 'success', 'X-Data-Completeness': 'complete',
    'X-Data-Freshness': 'fresh', 'X-Data-Meta-Status': 'ok',
    'X-Data-Rows': '2', 'X-Data-Total': '2', 'X-Data-Fetched-At': '2026-11-12 18:30:00',
    'X-Data-AsOf': 'unknown', 'X-Data-Served-From': 'file', 'X-Data-Missing': '-'
};
const BODY = { code: 200, data: [
    { name: '甲', change: 2.1, flow: 1.5 },
    { name: '乙', change: -1.2, flow: -0.7 }
], is_mock: false };

function makeResp(body, head) {
    return { headers: { get: (n) => (head && head[n] !== undefined) ? head[n] : null },
             json: () => Promise.resolve(body) };
}

const sandbox = {
    window: {},
    document: {
        addEventListener: (ev, fn) => handlers.push({ ev, fn }),
        getElementById: el,
        querySelectorAll: () => [],
        querySelector: () => null
    },
    console: { log: (m) => state.logs.push(String(m)), warn: () => {}, error: () => {} },
    Promise, Date, Math, JSON,
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearInterval: (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; },
    setTimeout: (fn) => { fn(); return 0; },
    fetch: () => {
        state.fetchCalls++;
        return state.mode === 'ok' ? Promise.resolve(makeResp(BODY, HEAD))
                                   : Promise.reject(new Error('heatmap down'));
    }
};
sandbox.window = sandbox;
vm.createContext(sandbox);

// 生产脚本（顺序与 base.html/heatmap.html 一致）
vm.runInContext(fs.readFileSync(path.join(ROOT, 'static/js/sector_view.js'), 'utf8'), sandbox, { filename: 'sector_view.js' });
vm.runInContext(fs.readFileSync(path.join(ROOT, 'static/js/app.js'), 'utf8'), sandbox, { filename: 'app.js' });
vm.runInContext(fs.readFileSync(path.join(ROOT, 'static/js/heatmap.js'), 'utf8'), sandbox, { filename: 'heatmap.js' });

// 图表渲染打点（渲染细节已由其它测试覆盖，这里只验证"每次刷新都重新渲染"）
sandbox.renderTreemap = () => { state.treemap++; };
sandbox.renderBarChart = (id) => { state.bars.push(id); };

// 真实注册片段：从 templates/heatmap.html 抽出 <script> 的内容执行
const tpl = fs.readFileSync(path.join(ROOT, 'templates/heatmap.html'), 'utf8');
const m = tpl.match(/<script>\s*(document\.addEventListener\('DOMContentLoaded'[\s\S]*?)\s*<\/script>/);
if (!m) { console.log('  FAIL  模板里找不到 DOMContentLoaded 注册片段'); process.exit(1); }
handlers.length = 0;           // 只保留模板这段注册
vm.runInContext(m[1], sandbox, { filename: 'heatmap.html:inline' });

function fireDomReady() {
    handlers.filter(h => h.ev === 'DOMContentLoaded').forEach(h => h.fn());
}
function tick() {
    const t = timers[timers.length - 1];
    return Promise.resolve(t.fn());
}

(async function () {
    console.log('[S3-3C r4] 自动刷新链路（真实 startAutoRefresh + 真实模板注册）');

    /* ---------- A. 首次加载 + 三个定时回调 ---------- */
    fireDomReady();
    await new Promise(r => setTimeout(r, 0));
    await Promise.resolve();
    ok('首次加载发起 1 次请求', state.fetchCalls === 1, 'fetch=' + state.fetchCalls);
    ok('首次加载渲染 treemap 1 次', state.treemap === 1, 'treemap=' + state.treemap);
    ok('首次加载渲染 2 个排行', state.bars.length === 2, 'bars=' + state.bars.length);
    ok('注册的定时间隔 = 15000ms', timers.length === 1 && timers[0].ms === 15000,
       'intervals=' + JSON.stringify(timers.map(t => t.ms)));

    // 关键回归点：定时器回调不带参数，也必须真的发请求
    for (let i = 0; i < 3; i++) await tick();
    ok('三个定时回调 → 请求次数 4', state.fetchCalls === 4, 'fetch=' + state.fetchCalls);
    ok('三个定时回调 → treemap 重新渲染（4 次）', state.treemap === 4, 'treemap=' + state.treemap);
    ok('三个定时回调 → 排行各渲染 4 次', state.bars.length === 8, 'bars=' + state.bars.length);
    ok('定时刷新后范围文案正常', /行业板块共 2 个/.test(el('status-heatmap-range').textContent),
       el('status-heatmap-range').textContent);

    /* ---------- B. 成功 → 失败 → 恢复 ---------- */
    state.mode = 'fail';
    await tick();
    ok('刷新失败：请求确实发出（fetch=5）', state.fetchCalls === 5, 'fetch=' + state.fetchCalls);
    ok('刷新失败：提示"保留此前页面数据"',
       /本次刷新失败，当前保留此前页面数据/.test(el('status-heatmap-range').textContent),
       el('status-heatmap-range').textContent);
    ok('刷新失败：不清空已渲染图表', state.treemap === 4, 'treemap=' + state.treemap);
    ok('刷新失败：错误态有 warn 样式', el('status-heatmap-range').className.indexOf('warn') >= 0);

    state.mode = 'ok';
    await tick();
    ok('下一次定时刷新恢复请求（fetch=6）', state.fetchCalls === 6, 'fetch=' + state.fetchCalls);
    ok('恢复后重新渲染（treemap=5）', state.treemap === 5, 'treemap=' + state.treemap);
    ok('恢复后文案回到正常（无"刷新失败"）',
       !/刷新失败/.test(el('status-heatmap-range').textContent) &&
       /行业板块共 2 个/.test(el('status-heatmap-range').textContent),
       el('status-heatmap-range').textContent);

    /* ---------- C. 请求未完成时再次触发定时回调 ---------- */
    let release;
    const gate = new Promise(r => { release = r; });
    state.fetchCalls = 0; state.treemap = 0; state.bars = [];
    sandbox.fetch = () => {
        state.fetchCalls++;
        return gate.then(() => makeResp(BODY, HEAD));
    };
    const p1 = tick();                 // 开始一次刷新（未完成）
    await Promise.resolve();
    const p2 = tick();                 // 加载中又触发一次
    await Promise.resolve();
    ok('加载中再次触发不会制造第二次并发请求', state.fetchCalls === 1, 'fetch=' + state.fetchCalls);
    ok('加载中的回调复用同一个 in-flight 请求',
       sandbox.window._heatmapLoader.state.inFlight !== false);
    release();
    await Promise.all([p1, p2]);
    ok('并发触发只渲染一次', state.treemap === 1 && state.bars.length === 2,
       'treemap=' + state.treemap + ' bars=' + state.bars.length);

    sandbox.fetch = () => { state.fetchCalls++; return Promise.resolve(makeResp(BODY, HEAD)); };
    await tick();
    ok('当前请求完成后，后续定时回调仍能刷新', state.treemap === 2 && state.fetchCalls === 2,
       'treemap=' + state.treemap + ' fetch=' + state.fetchCalls);

    await new Promise(r => setTimeout(r, 30));
    ok('全流程无未处理 Promise rejection', unhandled.length === 0, unhandled.join(' | '));

    console.log(failed ? '\n' + failed + ' FAILED' : '\nALL PASS');
    process.exit(failed ? 1 : 0);
})();
