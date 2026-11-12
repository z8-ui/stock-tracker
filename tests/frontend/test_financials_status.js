#!/usr/bin/env node
/* S3-4B 前端契约：Node 真实执行 static/js/financials_status.js 的 view()/meta()。
 * 不复制实现 —— 直接 eval 生产文件（IIFE 往 window 挂 financialsStatus）。
 * 另外用源码守卫确认 valuation.js 的空财报分支不再 return ''。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATUS_JS = path.join(__dirname, '..', '..', 'static', 'js', 'financials_status.js');
const VALUATION_JS = path.join(__dirname, '..', '..', 'static', 'js', 'valuation.js');
const src = fs.readFileSync(STATUS_JS, 'utf8');

const sandbox = { window: {}, console };
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: STATUS_JS });
const st = sandbox.window.financialsStatus;

let total = 0, failed = 0;
function check(label, got, want) {
    total++;
    const ok = JSON.stringify(got) === JSON.stringify(want);
    if (!ok) { failed++; console.log(`  FAIL  ${label}: got=${JSON.stringify(got)} want=${JSON.stringify(want)}`); }
    else console.log(`  PASS  ${label}`);
}
function checkTrue(label, cond) {
    total++;
    if (!cond) { failed++; console.log(`  FAIL  ${label}`); }
    else console.log(`  PASS  ${label}`);
}

console.log('[S3-4B] financials_status.js 真实执行');

// 1. 实时成功
check('network success kind', st.view({fetch_status: 'success', served_from: 'network', freshness: 'fresh'}).kind, 'live');
check('network success badge', st.view({fetch_status: 'success', served_from: 'network'}).badge, '实时抓取');

// 2. 上游 empty，无缓存
const empty = st.view({fetch_status: 'empty', served_from: 'none', freshness: 'unknown'});
check('empty kind', empty.kind, 'empty');
check('empty message', empty.message, '暂无财报（上游没有该股票的财报记录）');

// 3. 上游 failed，无缓存
const failedView = st.view({fetch_status: 'failed', served_from: 'none', freshness: 'unknown'});
check('failed kind', failedView.kind, 'failed');
check('failed message', failedView.message, '财报获取失败（网络或接口异常）');

// 4. 缓存命中（未请求上游）
const cacheHit = st.view({fetch_status: 'not_attempted', served_from: 'memory', freshness: 'fresh'});
check('cache kind', cacheHit.kind, 'cache');
check('cache badge', cacheHit.badge, '缓存');
checkTrue('cache 文案不写"失败"', cacheHit.message.indexOf('失败') < 0);

// 5. 刷新失败回退文件缓存
const fbFile = st.view({fetch_status: 'failed', served_from: 'file_fallback', freshness: 'stale'});
check('file fallback kind', fbFile.kind, 'degraded');
checkTrue('file fallback 标记降级', fbFile.degraded === true);
check('file fallback badge', fbFile.badge, '旧数据');

// 6. 刷新失败回退内存缓存
const fbMem = st.view({fetch_status: 'failed', served_from: 'memory_fallback', freshness: 'stale'});
check('memory fallback kind', fbMem.kind, 'degraded');
checkTrue('memory fallback 文案含"此前"', fbMem.message.indexOf('此前') >= 0);

// 7. 缓存非法且无可用财报
const invalid = st.view({fetch_status: 'failed', served_from: 'none', cache_rejected: true});
check('cache rejected kind', invalid.kind, 'invalid');
check('cache rejected message', invalid.message, '财报缓存数据无效，当前无法展示');

// 7b. empty + fallback 与 failed + fallback 必须说两件事
const fbEmptyFile = st.view({fetch_status: 'empty', served_from: 'file_fallback', freshness: 'stale'});
const fbEmptyMem = st.view({fetch_status: 'empty', served_from: 'memory_fallback', freshness: 'stale'});
const fbFailedFile = st.view({fetch_status: 'failed', served_from: 'file_fallback', freshness: 'stale'});
const fbFailedMem = st.view({fetch_status: 'failed', served_from: 'memory_fallback', freshness: 'stale'});
check('empty+file_fallback 文案', fbEmptyFile.message, '本次上游未返回财报记录，当前展示此前保存的财报快照。');
check('empty+memory_fallback 文案与 file 一致', fbEmptyMem.message, fbEmptyFile.message);
check('failed+file_fallback 文案', fbFailedFile.message, '本次财报获取失败，当前展示的是此前保存的财报（非本次抓取结果）');
check('failed+memory_fallback 文案与 file 一致', fbFailedMem.message, fbFailedFile.message);
checkTrue('empty 回退文案不含"获取失败"', fbEmptyFile.message.indexOf('获取失败') < 0);
checkTrue('failed 回退文案不含"未返回财报记录"', fbFailedFile.message.indexOf('未返回财报记录') < 0);
checkTrue('两种回退文案必须不同', fbEmptyFile.message !== fbFailedFile.message);
checkTrue('empty 回退标记 empty_upstream', fbEmptyFile.empty_upstream === true);
checkTrue('failed 回退不标记 empty_upstream', fbFailedFile.empty_upstream === false);
checkTrue('两种回退都标降级', fbEmptyFile.degraded === true && fbFailedFile.degraded === true);

// 8. 状态缺失
check('missing status kind', st.view(null).kind, 'unknown');
check('missing status message', st.view(undefined).message, '财报状态未知（接口未返回状态信息）');

// 9. 报告期 / 抓取时间分离
const metaFull = st.meta({report_date: '2026-09-30', fetched_at: '2026-11-12 20:19:52', served_from: 'network', freshness: 'fresh'});
checkTrue('meta 含报告期', metaFull.indexOf('报告期 2026-09-30') >= 0);
checkTrue('meta 含抓取时间', metaFull.indexOf('抓取 2026-11-12 20:19:52') >= 0);
checkTrue('meta 含来源', metaFull.indexOf('来源 本次抓取') >= 0);
checkTrue('meta 不把抓取时间当报告期', metaFull.indexOf('报告期 2026-11-12 20:19:52') < 0);

// 10. 抓取时间未知
const metaUnknown = st.meta({report_date: '2026-09-30', fetched_at: null, served_from: 'file', freshness: 'unknown'});
checkTrue('fetched_at 缺失时写明抓取时间未知', metaUnknown.indexOf('抓取时间未知') >= 0);

// 11. 陈旧标记
checkTrue('stale 体现在 meta', st.meta({report_date: '2026-09-30', fetched_at: '2026-11-12 09:00:00', served_from: 'file', freshness: 'stale'}).indexOf('数据已陈旧') >= 0);

// 12. 每种组合都有非空文案（不允许空字符串）
const combos = [];
['success', 'empty', 'failed', 'not_attempted', ''].forEach(f => {
    ['network', 'memory', 'file', 'file_fallback', 'memory_fallback', 'none', ''].forEach(s => {
        combos.push({fetch_status: f, served_from: s, freshness: 'fresh'});
    });
});
checkTrue('全部组合都有非空文案', combos.every(c => {
    const v = st.view(c);
    return typeof v.message === 'string' && v.message.length > 0;
}));

// 13. 源码守卫：valuation.js 空财报分支不再返回空字符串，且透传 status
const valSrc = fs.readFileSync(VALUATION_JS, 'utf8');
checkTrue('valuation.js 无 "!fin || !fin.report_date) return \'\'"',
    !/if \(!fin \|\| !fin\.report_date\) return ''/.test(valSrc));
checkTrue('valuation.js 调用 finStatusBlock 渲染无财报状态',
    /function buildFinancialsHtml\(fin, status\)/.test(valSrc) && /finStatusBlock\(status, true\)/.test(valSrc));
checkTrue('valuation.js 调用处透传 financials_status',
    /buildFinancialsHtml\(d\.financials, d\.financials_status\)/.test(valSrc));

console.log(`\n[S3-4B] ${total - failed}/${total} 断言通过`);
process.exit(failed === 0 ? 0 : 1);
