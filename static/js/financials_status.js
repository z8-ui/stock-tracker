// F10 财报状态 → 页面文案（S3-4B 纯函数层）
// 依赖：无（可被 Node 直接加载测试；valuation.js 通过 window.financialsStatus 调用）
//
// 为什么单独一层：财报"有没有 / 从哪来 / 新不新 / 为什么没有"是四个不同事实，
// 以前 buildFinancialsHtml() 遇到空财报直接 return ''，页面上就是一片空白，
// 用户无法区分"上游没有财报""接口挂了""缓存无效"。
//
// 后端契约（data_service._fin_build_status）：
//   {fetch_status, served_from, freshness, fetched_at, report_date, cache_rejected, reason}
//   fetch_status: success | empty | failed | not_attempted
//   served_from:  network | memory | file | file_fallback | memory_fallback | none
//   freshness:    fresh | stale | unknown

(function (root) {
    'use strict';

    var SERVED_TEXT = {
        network: '本次抓取',
        memory: '内存缓存',
        file: '本地缓存',
        file_fallback: '本地缓存（降级）',
        memory_fallback: '内存缓存（降级）',
        none: '无'
    };

    function pick(status) {
        return (status && typeof status === 'object') ? status : {};
    }

    // 返回 {kind, tone, badge, message, degraded}
    // kind 给测试和样式用；tone 决定颜色；message 是用户看到的那句话。
    function view(status) {
        var s = pick(status);
        var fetch = s.fetch_status || '';
        var served = s.served_from || '';
        var freshness = s.freshness || '';
        var rejected = !!s.cache_rejected;
        var degraded = (served === 'file_fallback' || served === 'memory_fallback');

        if (!fetch && !served) {
            return {
                kind: 'unknown', tone: 'muted', badge: '',
                message: '财报状态未知（接口未返回状态信息）', degraded: false
            };
        }

        if (fetch === 'success' && served === 'network') {
            return {
                kind: 'live', tone: 'ok', badge: '实时抓取',
                message: '财报已按本次请求实时取得', degraded: false
            };
        }

        if (degraded) {
            // 上游"没有财报记录"和"请求失败"是两件事，回退时的文案必须分开
            var emptyUpstream = (fetch === 'empty');
            return {
                kind: 'degraded', tone: 'warn', badge: '旧数据',
                message: emptyUpstream
                    ? '本次上游未返回财报记录，当前展示此前保存的财报快照。'
                    : '本次财报获取失败，当前展示的是此前保存的财报（非本次抓取结果）',
                degraded: true,
                empty_upstream: emptyUpstream
            };
        }

        // 缓存被判定非法、又没拿到任何财报：先说"数据无效"，别笼统说成网络失败
        if (rejected && served === 'none') {
            return {
                kind: 'invalid', tone: 'error', badge: '',
                message: '财报缓存数据无效，当前无法展示', degraded: false
            };
        }

        if (fetch === 'empty') {
            return {
                kind: 'empty', tone: 'muted', badge: '',
                message: '暂无财报（上游没有该股票的财报记录）', degraded: false
            };
        }

        if (fetch === 'failed') {
            return {
                kind: 'failed', tone: 'error', badge: '',
                message: '财报获取失败（网络或接口异常）', degraded: false
            };
        }

        // 纯缓存命中：没有请求上游
        if (fetch === 'not_attempted' && (served === 'memory' || served === 'file')) {
            if (rejected) {
                return {
                    kind: 'cache', tone: 'info', badge: '缓存',
                    message: '缓存中的异常数据已被剔除，当前展示剩余可用缓存', degraded: false
                };
            }
            return {
                kind: 'cache', tone: 'info', badge: '缓存',
                message: freshness === 'stale'
                    ? '展示的是本地缓存的上一轮财报（本次未请求上游）'
                    : '展示的是本地缓存中的财报（本次未请求上游）',
                degraded: false
            };
        }

        if (rejected) {
            return {
                kind: 'invalid', tone: 'error', badge: '',
                message: '财报缓存数据无效，当前无法展示', degraded: false
            };
        }

        return {
            kind: 'unknown', tone: 'muted', badge: '',
            message: '财报状态未知：' + (s.reason || '未取得可用财报'), degraded: false
        };
    }

    // 报告期 / 抓取时间 / 来源 —— 两个时间必须分开显示，绝不互相顶替
    function meta(status) {
        var s = pick(status);
        var parts = [];
        if (s.report_date) {
            parts.push('报告期 ' + s.report_date);
        }
        if (s.fetched_at) {
            parts.push('抓取 ' + s.fetched_at);
        } else if (s.served_from && s.served_from !== 'none') {
            parts.push('抓取时间未知');
        }
        var served = SERVED_TEXT[s.served_from];
        if (served && served !== '无') {
            parts.push('来源 ' + served);
        }
        if (s.freshness === 'stale') {
            parts.push('数据已陈旧');
        }
        return parts.join(' · ');
    }

    var api = { view: view, meta: meta, SERVED_TEXT: SERVED_TEXT };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;            // Node 测试直接 require
    }
    root.financialsStatus = api;         // 浏览器里走 window.financialsStatus
}(typeof window !== 'undefined' ? window : globalThis));
