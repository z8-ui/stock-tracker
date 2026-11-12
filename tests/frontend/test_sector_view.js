#!/usr/bin/env node
/* S3-3C 前端契约测试：直接执行生产文件 static/js/sector_view.js。
 * 不复制实现、不 mock 业务函数 —— vm 里跑同一份源码。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', '..', 'static', 'js', 'sector_view.js');
const sandbox = { window: {}, console };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SRC, 'utf8'), sandbox, { filename: SRC });
const sv = sandbox.window.sectorView;

let failed = 0;
function ok(label, cond, extra) {
    if (!cond) failed++;
    console.log('  ' + (cond ? 'PASS' : 'FAIL') + '  ' + label + (extra ? '  ' + extra : ''));
}
function eq(label, got, want) {
    const pass = JSON.stringify(got) === JSON.stringify(want);
    if (!pass) failed++;
    console.log('  ' + (pass ? 'PASS' : 'FAIL') + '  ' + label +
        (pass ? '' : '\n        got=' + JSON.stringify(got) + '\n        want=' + JSON.stringify(want)));
}

console.log('[S3-3C] sector_view.js 真实执行');

/* ---------- 1. PB 全 null：不能被当成 0，气泡图只用 PE + 市值 ---------- */
const pbNull = [
    { name: '银行', pe: 6.1, pb: null, market_cap: 21000, change: 0.5 },
    { name: '证券', pe: 22.4, pb: null, market_cap: 8000, change: -1.2 },
    { name: '煤炭', pe: null, pb: null, market_cap: 5000, change: 1.0 }
];
const bp = sv.bubblePoints(pbNull);
ok('PB 全 null 时标记 pbAllMissing', bp.pbAllMissing === true);
ok('PB 缺失数为 3（不是 0）', bp.pbMissing === 3, 'got=' + bp.pbMissing);
ok('PE 缺失的板块不画点', bp.noPe === 1);
eq('气泡点只含 PE+市值', bp.positive.map(p => p.name), ['银行', '证券']);
ok('气泡点里没有 pb 字段', bp.positive.every(p => !('pb' in p)));

/* ---------- 2. 负 PE 保留并单独归类 ---------- */
const withLoss = [
    { name: '盈利A', pe: 12.5, pb: 1.2, market_cap: 900 },
    { name: '亏损B', pe: -18.0, pb: 0.8, market_cap: 700 },
    { name: '亏损C', pe: -3.2, pb: null, market_cap: 600 }
];
const c2 = sv.classifyPE(withLoss);
eq('负 PE 全部进 negative', c2.negative.map(x => x.pe), [-18, -3.2]);
ok('负 PE 不进 positive', c2.positive.length === 1);
const r2 = sv.peRank(withLoss, 20);
eq('常规排行只含正 PE', r2.ranked.map(x => x.name), ['盈利A']);
ok('亏损单独一组', r2.loss.length === 2);
ok('排行不含任何非正 PE', r2.ranked.every(x => x.pe > 0));

/* ---------- 3. PE null 不参与排名、不变成 0 ---------- */
const withNull = [
    { name: 'A', pe: 15, market_cap: 10 },
    { name: 'B', pe: null, market_cap: 20 },
    { name: 'C', pe: 8, market_cap: 30 }
];
const r3 = sv.peRank(withNull, 20);
eq('null PE 不进入排行', r3.ranked.map(x => x.name), ['C', 'A']);
ok('null PE 计数正确', r3.counts.missing === 1);
ok('null PE 没有被写成 0', r3.ranked.every(x => x.pe !== 0));

/* ---------- 4. PE=0 是真实值：单独归类，不进常规排行也不当亏损 ---------- */
const withZero = [
    { name: '零', pe: 0, market_cap: 5 },
    { name: '正', pe: 9, market_cap: 6 },
    { name: '负', pe: -2, market_cap: 7 }
];
const c4 = sv.classifyPE(withZero);
ok('PE=0 归 zero', c4.zero.length === 1 && c4.zero[0].pe === 0);
const r4 = sv.peRank(withZero, 20);
eq('排行只有正 PE', r4.ranked.map(x => x.name), ['正']);
ok('0 不在亏损组', r4.loss.length === 1 && r4.loss[0].pe === -2);
ok('计数 zero=1', r4.counts.zero === 1);

/* ---------- 5/6. Top N 与 496 条完整数据 ---------- */
const big = [];
for (let i = 0; i < 496; i++) {
    big.push({ name: 'S' + i, pe: (i % 100) + 1, pb: null, market_cap: 100 + i });
}
const r6 = sv.peRank(big, 20);
ok('Top20 只返回 20 条', r6.ranked.length === 20, 'got=' + r6.ranked.length);
ok('正 PE 计数 = 496', r6.counts.positive === 496);
ok('排行升序（低 PE 在前）', r6.ranked[0].pe <= r6.ranked[19].pe);

const metaComplete = { rows: 496, total: 496, completeness: 'complete' };
eq('完整 496 + 展示 20 的文案', sv.rangeText(metaComplete, 20),
    '行业板块共 496 个，当前展示前 20 个。');
eq('完整 496 + 全部展示的文案', sv.rangeText(metaComplete, 496),
    '行业板块共 496 个，当前全部展示。');

/* ---------- 7. partial：不能出现"全部板块" ---------- */
const metaPartial = { rows: 400, total: 496, completeness: 'partial' };
const tPartial = sv.rangeText(metaPartial, 20);
eq('partial 文案', tPartial, '数据不完整：本次取得 400/496 个板块（缺 96 个），当前展示前 20 个。');
ok('partial 不出现"共 … 个板块"的口径', !/^行业板块共/.test(tPartial));

/* ---------- 8. total=unknown：不得凭 rows 猜总量 ---------- */
const metaUnknown = { rows: 496, total: null, completeness: 'unknown' };
const tUnknown = sv.rangeText(metaUnknown, 20);
eq('unknown 文案', tUnknown, '完整性未知：本次取得 496 个板块（上游总量未知），当前展示前 20 个。');
ok('unknown 不声称"全部"', tUnknown.indexOf('全部') === -1);
ok('unknown 不把 rows 当 total', tUnknown.indexOf('共 496') === -1);

/* ---------- 9/10. 新鲜度与降级 ---------- */
ok('stale 文案提示过期', /数据已过期/.test(sv.freshnessText(
    { freshness: 'stale', fetched_at: '2026-11-12 09:00:00', served_from: 'file' })));
const fbText = sv.freshnessText({ fetch_status: 'failed', served_from: 'file_fallback',
    freshness: 'stale', fetched_at: '2026-11-11 15:00:00' });
ok('file_fallback 明确说抓取失败', /本次抓取失败/.test(fbText), fbText);
ok('file_fallback 明确说旧缓存', /旧缓存/.test(fbText), fbText);
ok('asof=unknown 不冒充交易所时间',
    /上游未提供数据时间/.test(sv.freshnessText({ freshness: 'fresh', asof: 'unknown',
        fetched_at: '2026-11-12 16:41:10' })));

/* ---------- 11. Header 解析 ---------- */
const H = {
    'X-Data-Fetch-Status': 'failed',
    'X-Data-Completeness': 'unknown',
    'X-Data-Freshness': 'stale',
    'X-Data-Meta-Status': 'missing',
    'X-Data-Rows': '496',
    'X-Data-Total': 'unknown',
    'X-Data-Fetched-At': '2026-11-11 00:29:10',
    'X-Data-AsOf': 'unknown',
    'X-Data-Served-From': 'file_fallback',
    'X-Data-Missing': '-'
};
const meta = sv.parseHeaders(function (n) { return H[n] === undefined ? null : H[n]; });
eq('rows 解析成数字', meta.rows, 496);
eq('total=unknown → null', meta.total, null);
eq('completeness 原样', meta.completeness, 'unknown');
eq('served_from 原样', meta.served_from, 'file_fallback');
eq('缺失 header → null', sv.parseHeaders(function () { return null; }).rows, null);
ok('unknown 不写成 complete', meta.completeness !== 'complete');

/* ---------- 12. 不改动入参与全局 ---------- */
const frozen = [{ name: 'X', pe: 10, pb: null, market_cap: 100 }];
const snapshot = JSON.stringify(frozen);
sv.peRank(frozen, 5); sv.bubblePoints(frozen); sv.classifyPE(frozen);
eq('纯函数不修改入参', JSON.stringify(frozen), snapshot);
eq('window 上只多一个 sectorView', Object.keys(sandbox.window), ['sectorView']);
ok('无 NaN 泄漏: 全 null 输入不抛异常',
    sv.peRank([{ name: 'a', pe: null }, { name: 'b', pe: null }], 20).counts.missing === 2);

console.log('\n' + (failed === 0 ? 'ALL PASS' : failed + ' FAILED'));

/* ---------- 13. 极端负 PE 与正 PE 共存：分组与计数必须对得上 ---------- */
const extreme = [];
for (let i = 0; i < 406; i++) extreme.push({ name: 'P' + i, pe: 5 + i * 0.3, pb: null, market_cap: 100 + i });
for (let i = 0; i < 89; i++) extreme.push({ name: 'L' + i, pe: -2 - i, pb: null, market_cap: 50 + i });
extreme.push({ name: '极端亏损', pe: -36378.67, pb: null, market_cap: 12.3 });
const bpX = sv.bubblePoints(extreme);
ok('极端负 PE 被保留在 negative（不被删除）', bpX.negative.some(p => p.pe === -36378.67));
ok('负 PE 数量 90', bpX.negative.length === 90, 'got=' + bpX.negative.length);
ok('正 PE 数量 406', bpX.positive.length === 406, 'got=' + bpX.positive.length);
ok('已绘制数 = 各组之和 = 总数（496）',
   bpX.drawn === 496 && bpX.drawn === bpX.positive.length + bpX.negative.length + bpX.zero.length);
ok('极端值后仍保留原始 PE 数值', bpX.negative.find(p => p.name === '极端亏损').pe === -36378.67);
ok('极端值后仍保留市值', bpX.negative.find(p => p.name === '极端亏损').cap === 12.3);

/* ---------- 14. PE=0 语义：不是盈利，也不是亏损 ---------- */
const zeroRows = [
    { name: '零PE', pe: 0, pb: null, market_cap: 300 },
    { name: '正PE', pe: 9.9, pb: null, market_cap: 400 },
    { name: '负PE', pe: -8.8, pb: null, market_cap: 500 }
];
const bpZ = sv.bubblePoints(zeroRows);
eq('PE=0 单独一组', bpZ.zero.map(p => p.name), ['零PE']);
ok('PE=0 不在盈利组', !bpZ.positive.some(p => p.pe === 0));
ok('PE=0 不在亏损组', !bpZ.negative.some(p => p.pe === 0));
ok('PE=0 仍被绘制（计入 drawn）', bpZ.drawn === 3);
const rZ = sv.peRank(zeroRows, 20);
ok('PE=0 不进入正 PE 排行', rZ.ranked.length === 1 && rZ.ranked[0].pe === 9.9);
ok('PE=0 计入 zero 计数', rZ.counts.zero === 1);

/* ---------- 15. completeness=unknown 但 total 已知 ---------- */
const tU = sv.rangeText({ rows: 400, total: 496, completeness: 'unknown' }, 20);
ok('unknown + total 已知：不能写"上游总量未知"', !/上游总量未知/.test(tU), tU);
ok('unknown + total 已知：要写出上游报告总量 496', /上游报告总量 496/.test(tU), tU);
ok('unknown + total 已知：不能声称完整', !/行业板块共/.test(tU), tU);
ok('unknown + total 已知：保留 Top N 后缀', /当前展示前 20 个/.test(tU), tU);

/* ---------- 16. failed + served_from=none：无可用数据 ---------- */
const tNone = sv.freshnessText({ fetch_status: 'failed', served_from: 'none', freshness: 'unknown' });
ok('完全失败不说"展示旧缓存"', !/旧缓存/.test(tNone), tNone);
ok('完全失败明确说"当前无可用数据"', /当前无可用数据/.test(tNone), tNone);

/* ---------- 17. failed + file_fallback：才可以说旧缓存 ---------- */
const tFb = sv.freshnessText({ fetch_status: 'failed', served_from: 'file_fallback',
                               freshness: 'stale', fetched_at: '2026-11-11 15:00:00' });
ok('file_fallback 说旧缓存', /旧缓存/.test(tFb), tFb);
ok('file_fallback 带本地抓取时间', /2026-11-11 15:00:00/.test(tFb), tFb);

/* ---------- 18/19. 排行计数作用域 ---------- */
const sPartial = sv.scopeText({ rows: 400, total: 496, completeness: 'partial' });
ok('partial：说"本次取得 400/496"', /本次取得 400\/496/.test(sPartial), sPartial);
ok('partial：不说"行业板块共"', !/行业板块共/.test(sPartial), sPartial);
const sUnknown = sv.scopeText({ rows: 400, total: 496, completeness: 'unknown' });
ok('unknown：说"完整性未知"且给出上游总量', /完整性未知/.test(sUnknown) && /496/.test(sUnknown), sUnknown);
const sComplete = sv.scopeText({ rows: 496, total: 496, completeness: 'complete' });
ok('complete 且 rows=total：才允许"行业板块共 496 个"', sComplete === '行业板块共 496 个', sComplete);
const sMismatch = sv.scopeText({ rows: 400, total: 496, completeness: 'complete' });
ok('complete 但 rows≠total：不得说"行业板块共"', !/行业板块共/.test(sMismatch), sMismatch);

/* ---------- 20. 文案里的数字必须等于实际绘制数 ---------- */
const note = '已绘制 ' + bpX.drawn + '/' + bpX.total + ' 个：正 PE ' + bpX.positive.length +
    ' 个、PE=0 ' + bpX.zero.length + ' 个、亏损 ' + bpX.negative.length + ' 个';
const m = note.match(/已绘制 (\d+)\/(\d+) 个：正 PE (\d+) 个、PE=0 (\d+) 个、亏损 (\d+) 个/);
ok('文案可解析', !!m);
ok('文案已绘制数 = 实际绘制数', Number(m[1]) === bpX.drawn && Number(m[1]) === 496);
ok('文案各组之和 = 已绘制数',
   Number(m[3]) + Number(m[4]) + Number(m[5]) === Number(m[1]));

process.exit(failed === 0 ? 0 : 1);
