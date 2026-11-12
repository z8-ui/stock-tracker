#!/usr/bin/env node
/* S3-3C r3：直接执行生产取数逻辑（sector_view.sectorLoader / static/js/heatmap.js），
 * 用 Promise.resolve / Promise.reject 模拟网络失败、JSON 解析失败、code=503。
 * 不依赖真实断网、不依赖 sleep。
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

function newSandbox(extra) {
    const sb = Object.assign({ window: {}, console, Promise, setTimeout, clearTimeout }, extra || {});
    vm.createContext(sb);
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'static/js/sector_view.js'), 'utf8'),
                    sb, { filename: 'sector_view.js' });
    return sb;
}

function resp(body, headers) {
    return {
        headers: { get: (n) => (headers && headers[n] !== undefined) ? headers[n] : null },
        json: () => Promise.resolve(body)
    };
}
const OK_HEAD = {
    'X-Data-Fetch-Status': 'success', 'X-Data-Completeness': 'complete',
    'X-Data-Freshness': 'fresh', 'X-Data-Meta-Status': 'ok',
    'X-Data-Rows': '2', 'X-Data-Total': '2', 'X-Data-Fetched-At': '2026-11-12 10:00:00',
    'X-Data-AsOf': 'unknown', 'X-Data-Served-From': 'file', 'X-Data-Missing': '-'
};
const BODY = { code: 200, data: [{ name: 'A', pe: 10, market_cap: 100, change: 1, flow: 1 }], is_mock: false };

console.log('[S3-3C r3] 网络异常 / 重试语义（生产函数）');

/* ---------- 1. fetch 直接 reject ---------- */
(async function () {
    const sb = newSandbox();
    const sv = sb.window.sectorView;
    let err = null;
    const r1 = await sv.loadSectorData({
        url: '/api/x', fetch: () => Promise.reject(new Error('ECONNREFUSED')),
        onData: () => { throw new Error('不该走 onData'); },
        onError: (e) => { err = e; }
    });
    ok('fetch reject → ok=false / kind=network', r1.ok === false && r1.kind === 'network' && err.kind === 'network',
       JSON.stringify({ ok: r1.ok, kind: r1.kind }));
    ok('fetch reject → 文案是"无可用数据"而不是旧缓存',
       sv.netErrorText(false, 'network').indexOf('无可用数据') >= 0 &&
       sv.netErrorText(false, 'network').indexOf('旧缓存') < 0,
       sv.netErrorText(false, 'network'));

    /* ---------- 2. JSON 解析失败 ---------- */
    let err2 = null;
    const r2 = await sv.loadSectorData({
        url: '/api/x',
        fetch: () => Promise.resolve({ headers: { get: () => null }, json: () => Promise.reject(new SyntaxError('bad json')) }),
        onError: (e) => { err2 = e; }
    });
    ok('json() reject → kind=parse，不抛异常', r2.ok === false && r2.kind === 'parse' && err2.kind === 'parse');

    /* ---------- 3. code=503 ---------- */
    let err3 = null;
    const r3 = await sv.loadSectorData({
        url: '/api/x',
        fetch: () => Promise.resolve(resp({ code: 503, msg: '上游不可用' })),
        onError: (e) => { err3 = e; }
    });
    ok('code=503 → kind=code，带上 body.msg', r3.ok === false && r3.kind === 'code' && err3.body.msg === '上游不可用');

    /* ---------- 4. 估值页：首次失败 → 文案 + 可重试 ---------- */
    let fetchCalls = 0, failMsg = '', rendered = 0;
    const loader = sv.sectorLoader({
        url: '/api/sector-valuation',
        fetch: () => { fetchCalls++; return Promise.reject(new Error('down')); },
        render: () => rendered++,
        fail: (info, hadData, text) => { failMsg = text; }
    });
    const a1 = await loader.load();
    ok('首次失败：暴露失败（ok=false）', a1.ok === false);
    ok('首次失败：文案为"请求失败，当前无可用数据"', /请求失败，当前无可用数据/.test(failMsg), failMsg);
    ok('首次失败：解除占位，允许重试', loader.state.loaded === false);
    const a2 = await loader.load();
    ok('失败后可以再次请求（fetch 被调用 2 次）', fetchCalls === 2 && a2.ok === false, 'calls=' + fetchCalls);

    /* ---------- 5. 成功后不重复请求 ---------- */
    let okCalls = 0, renderedOk = 0;
    const okLoader = sv.sectorLoader({
        url: '/api/sector-valuation',
        fetch: () => { okCalls++; return Promise.resolve(resp(BODY, OK_HEAD)); },
        render: () => renderedOk++,
        fail: () => {}
    });
    await okLoader.load();
    await okLoader.load();
    await okLoader.load();
    ok('成功后重复调用不会重复抓取', okCalls === 1, 'calls=' + okCalls);
    ok('成功后 render 只执行一次', renderedOk === 1);

    /* ---------- 6. 已有数据 + 刷新失败：保留旧数据并明确提示 ---------- */
    let mode = 'ok', refreshMsg = '', renderedRefresh = 0;
    const mixLoader = sv.sectorLoader({
        url: '/api/heatmap',
        fetch: () => mode === 'ok' ? Promise.resolve(resp(BODY, OK_HEAD)) : Promise.reject(new Error('boom')),
        render: () => renderedRefresh++,
        fail: (info, hadData, text) => { refreshMsg = text; }
    });
    await mixLoader.load();
    ok('先成功一次：已有数据', mixLoader.state.data !== null);
    mode = 'fail';
    const rr = await mixLoader.load(true);          // force：定时刷新路径
    ok('刷新失败时返回 ok=false', rr.ok === false);
    ok('已有数据时提示"保留此前页面数据"', /保留此前页面数据/.test(refreshMsg), refreshMsg);
    ok('已有数据时不说"无可用数据"', refreshMsg.indexOf('无可用数据') < 0, refreshMsg);
    ok('刷新失败不清空已显示数据', mixLoader.state.data !== null && renderedRefresh === 1);
    mode = 'ok';
    const rr2 = await mixLoader.load(true);
    ok('下一次刷新可以恢复', rr2.ok === true);

    /* ---------- 7. 热力图页面脚本（生产文件）---------- */
    const els = {};
    function el(id) {
        if (!els[id]) els[id] = { id, textContent: '', className: '', style: {} };
        return els[id];
    }
    const draws = { treemap: 0, bar: [] };
    const sb2 = newSandbox({
        document: { getElementById: el, addEventListener: () => {} },
        renderTreemap: () => draws.treemap++,
        renderBarChart: (id) => draws.bar.push(id),
        startAutoRefresh: () => {},
        fetch: () => Promise.reject(new Error('heatmap down'))
    });
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'static/js/heatmap.js'), 'utf8'), sb2,
                    { filename: 'heatmap.js' });
    const rH1 = await sb2.window.loadHeatmapData();
    ok('热力图网络失败：返回 ok=false', rH1.ok === false);
    ok('热力图网络失败：页面明确写出无可用数据',
       /请求失败，当前无可用数据/.test(el('status-heatmap-range').textContent),
       el('status-heatmap-range').textContent);
    ok('热力图网络失败：不调用图表渲染（但也不留空白提示）',
       draws.treemap === 0 && el('status-heatmap-range').className.indexOf('warn') >= 0);

    // 恢复：下一次刷新成功
    sb2.fetch = () => Promise.resolve(resp(BODY, OK_HEAD));
    const rH2 = await sb2.window.loadHeatmapData(true);
    ok('热力图下一次刷新可以恢复', rH2.ok === true && draws.treemap === 1, 'treemap=' + draws.treemap);
    ok('恢复后展示正常范围文案', /行业板块共 2 个/.test(el('status-heatmap-range').textContent),
       el('status-heatmap-range').textContent);
    ok('恢复后两个排行各画一次', draws.bar.length === 2, 'bars=' + draws.bar.length);

    await new Promise(r => setTimeout(r, 30));
    ok('全流程没有未处理的 Promise rejection', unhandled.length === 0, unhandled.join(' | '));

    console.log(failed ? '\n' + failed + ' FAILED' : '\nALL PASS');
    process.exit(failed ? 1 : 0);
})();
