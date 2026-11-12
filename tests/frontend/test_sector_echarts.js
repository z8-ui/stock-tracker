#!/usr/bin/env node
/* S3-3C r3：用项目自带 ECharts 5.5.1 真实执行生产 option 构造器，验证每个点都有有限坐标。
 * 关键：对数轴画不了 0，PE=0 必须走独立类目轴，且要证明它的坐标不是 NaN/-Infinity。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
const echarts = require(path.join(ROOT, 'static', 'vendor', 'echarts.min.js'));

const sandbox = { window: {}, console, Promise };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'static/js/sector_view.js'), 'utf8'),
                sandbox, { filename: 'sector_view.js' });
const sv = sandbox.window.sectorView;

let failed = 0;
function ok(label, cond, extra) {
    if (!cond) failed++;
    console.log('  ' + (cond ? 'PASS' : 'FAIL') + '  ' + label + (extra ? '  ' + extra : ''));
}
function fin(v) { return typeof v === 'number' && isFinite(v); }

function makeChart(pts) {
    const c = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 900, height: 500 });
    c.setOption(sv.scatterOption(pts));
    return c;
}
function coordOf(c, seriesIndex, value) {
    try { return c.convertToPixel({ seriesIndex }, value); } catch (e) { return null; }
}

console.log('[S3-3C r3] ECharts ' + echarts.version + ' 真实坐标验证');

/* ---------- 1. 三个板块：正 20 / 零 0 / 负 -20 ---------- */
const three = [
    { name: '正PE板块', pe: 20, pb: null, market_cap: 1000 },
    { name: '零PE板块', pe: 0, pb: null, market_cap: 200 },
    { name: '负PE板块', pe: -20, pb: null, market_cap: 500 }
];
const pts3 = sv.bubblePoints(three);
const opt3 = sv.scatterOption(pts3);
ok('存在 PE=0 时是 3 个 grid（正/负/零各自独立）', opt3.grid.length === 3, 'grids=' + opt3.grid.length);
ok('第三个 x 轴是类目轴（能表示 0）', opt3.xAxis[2].type === 'category',
   'type=' + opt3.xAxis[2].type);

const c3 = makeChart(pts3);
ok('series 数量 = 3（盈利/亏损/PE=0）', c3.getOption().series.length === 3);

const px = coordOf(c3, 0, [20, 1000]);
const pz = coordOf(c3, 2, ['PE=0', 200]);
const pn = coordOf(c3, 1, [-20, 500]);
ok('正 PE 坐标有限', px && fin(px[0]) && fin(px[1]), JSON.stringify(px));
ok('PE=0 坐标有限（不是 -Infinity/NaN）', pz && fin(pz[0]) && fin(pz[1]), JSON.stringify(pz));
ok('负 PE 坐标有限', pn && fin(pn[0]) && fin(pn[1]), JSON.stringify(pn));

/* 0 值点真的落在自己的 grid 内 */
const g2 = c3.getOption().grid[2];
ok('PE=0 点位于独立 grid 区域',
   pz && pz[0] > 0 && pz[0] < 900 && pz[1] > 0 && pz[1] < 500,
   'grid2=' + JSON.stringify(g2));

/* tooltip 保持真实 0 与真实市值 */
const tip = opt3.tooltip.formatter({ data: { name: '零PE板块', pe: 0, cap: 200 } });
ok('PE=0 的 tooltip 写的是 0', /PE: 0（PE=0，非盈利也不亏损）/.test(tip), tip);
ok('PE=0 的 tooltip 保留市值', /200/.test(tip), tip);

/* 已绘制数量 = 真实可绘制的点数 */
const drawnReal = [px, pz, pn].filter(Boolean).length;
ok('drawn 与真实可绘制点数一致（3）', pts3.drawn === 3 && drawnReal === 3,
   'drawn=' + pts3.drawn + ' finite=' + drawnReal);
ok('无法绘制的情况单独计数（此处为 0）', pts3.undrawn === 0, 'undrawn=' + pts3.undrawn);

/* ---------- 2. 没有 PE=0 时保持 2 个 grid，不退化 ---------- */
const noZero = three.filter(r => r.pe !== 0);
const opt2 = sv.scatterOption(sv.bubblePoints(noZero));
ok('无 PE=0 时仍是 2 个 grid', opt2.grid.length === 2, 'grids=' + opt2.grid.length);

/* ---------- 3. 真实 496 条形态（406 正 / 89 负 + 1 极端负）全部可绘制 ---------- */
const rows = [];
for (let i = 0; i < 406; i++) rows.push({ name: 'P' + i, pe: 6.17 + i * 3.3, pb: null, market_cap: 35 + i * 40 });
for (let i = 0; i < 89; i++) rows.push({ name: 'L' + i, pe: -3.46 - i * 3.1, pb: null, market_cap: 40 + i * 30 });
rows.push({ name: '诊断服务', pe: -36378.67, pb: null, market_cap: 12.3 });

const pts496 = sv.bubblePoints(rows);
const c496 = makeChart(pts496);
ok('496 行分类正确 406/90', pts496.positive.length === 406 && pts496.negative.length === 90,
   pts496.positive.length + '/' + pts496.negative.length);
ok('496 行 drawn = 496', pts496.drawn === 496, 'drawn=' + pts496.drawn);
ok('496 行 undrawn = 0', pts496.undrawn === 0);

let bad = 0, checked = 0;
const opt = c496.getOption();
opt.series.forEach(function (s, si) {
    (s.data || []).forEach(function (d) {
        const q = coordOf(c496, si, d.value);
        checked++;
        if (!q || !fin(q[0]) || !fin(q[1])) bad++;
    });
});
ok('496 个点逐个 convertToPixel 均为有限值', bad === 0 && checked === 496, 'checked=' + checked + ' bad=' + bad);

const negExtreme = sv.scatterOption(pts496).series[1].markPoint;
ok('极端负 PE 仍被标注真实值', !!negExtreme && /-36378\.67/.test(negExtreme.label.formatter));

/* ---------- 4. 不可绘制的情况要单独计数 ---------- */
const badRows = [
    { name: '无PE', pe: null, pb: null, market_cap: 100 },
    { name: '无市值', pe: 15, pb: null, market_cap: null },
    { name: '正常', pe: 10, pb: null, market_cap: 300 }
];
const ptsBad = sv.bubblePoints(badRows);
ok('缺 PE / 缺市值 不计入 drawn', ptsBad.drawn === 1 && ptsBad.undrawn === 2,
   'drawn=' + ptsBad.drawn + ' undrawn=' + ptsBad.undrawn);

console.log(failed ? '\n' + failed + ' FAILED' : '\nALL PASS');
process.exit(failed ? 1 : 0);
