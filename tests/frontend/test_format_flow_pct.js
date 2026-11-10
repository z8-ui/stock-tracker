#!/usr/bin/env node
/* S3-2 前端契约：用 Node 真实执行 static/js/format.js 的 pct()。
 * 不复制实现、不 mock 函数 —— 直接 eval 生产文件（它是 IIFE，只往 window 挂 fmt）。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const FORMAT_JS = path.join(__dirname, '..', '..', 'static', 'js', 'format.js');
const src = fs.readFileSync(FORMAT_JS, 'utf8');

const sandbox = { window: {}, document: { createElement: () => ({ style: {} }) }, console };
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: FORMAT_JS });
const fmt = sandbox.window.fmt;

let failed = 0;
function check(label, got, want) {
    const ok = got === want;
    if (!ok) failed++;
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}: got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
}

console.log('[S3-2] format.js pct() 真实执行结果');
check('pct(null)   → 页面显示 --', fmt.pct(null), '--');
check('pct(undefined) → --', fmt.pct(undefined), '--');
check('pct("")    → --', fmt.pct(''), '--');
check('pct(NaN)   → --', fmt.pct(NaN), '--');
check('pct(0)     → 真实零不带符号', fmt.pct(0), '0.00%');
check('pct(0.0)   → 真实零', fmt.pct(0.0), '0.00%');
check('pct(3.02)  → 正数带 +', fmt.pct(3.02), '+3.02%');
check('pct(-6.03) → 负数自带 -', fmt.pct(-6.03), '-6.03%');
check('money(null) → --', fmt.money(null, '亿'), '--');
check('money(1)   → +1.00 亿', fmt.money(1, '亿'), '+1.00亿');
check('money(0)   → 0.00 亿', fmt.money(0, '亿'), '0.00亿');

// toNum(null) 必须与 toNum(0) 区分开：null 是"没有数据"，0 是真实零
check('toNum(null) 与 null 严格相等', fmt.toNum(null), null);
check('toNum(0) 是 0 而不是 null', fmt.toNum(0), 0);

console.log(failed === 0 ? '\nALL PASS' : `\n${failed} FAILED`);
process.exit(failed === 0 ? 0 : 1);
