/* 板块展示的纯函数层（S3-3C）
 *
 * 为什么单独一个文件：这些判断（PE 正/负/0/缺失、Top N 截断、完整性文案、
 * X-Data-* 解析）必须能被 Node 直接执行测试，不能埋在 DOM/ECharts 里。
 * 浏览器里挂在 window.sectorView，Node 里用 vm 跑同一个文件。
 *
 * 铁律：
 *   - PE null 不是 0；0 是真实值；负数代表亏损，不是"便宜"。
 *   - 完整性只由后端元数据决定：total 未知就不许说"全部/共 N 个"。
 */
(function (global) {
    'use strict';

    function num(v) {
        if (v === null || v === undefined || v === '') return null;
        var n = Number(v);
        return isFinite(n) ? n : null;
    }

    /* PE 四分类：正 / 负 / 零 / 缺失。排序只在正 PE 内做（低 PE 在前）。 */
    function classifyPE(rows) {
        rows = rows || [];
        var out = { positive: [], negative: [], zero: [], missing: [], total: rows.length };
        rows.forEach(function (r) {
            var pe = r ? num(r.pe) : null;
            var name = r && r.name != null ? String(r.name) : '';
            if (pe === null) { out.missing.push({ name: name, pe: null }); return; }
            if (pe > 0) out.positive.push({ name: name, pe: pe });
            else if (pe < 0) out.negative.push({ name: name, pe: pe });
            else out.zero.push({ name: name, pe: 0 });
        });
        out.positive.sort(function (a, b) { return a.pe - b.pe; });
        out.negative.sort(function (a, b) { return a.pe - b.pe; });
        return out;
    }

    /* 常规 PE 排行：只取正 PE，Top N。负 PE 单独一组，绝不混进"低估值"。 */
    function peRank(rows, topN) {
        var c = classifyPE(rows);
        var n = (topN === null || topN === undefined) ? c.positive.length : Math.max(0, topN);
        return {
            ranked: c.positive.slice(0, n),
            loss: c.negative.slice(0, n),
            counts: {
                total: c.total,
                positive: c.positive.length,
                negative: c.negative.length,
                zero: c.zero.length,
                missing: c.missing.length
            },
            shown: Math.min(n, c.positive.length)
        };
    }

    /* 气泡图数据：x=PE，y=市值（亿），气泡大小=市值。PB 缺失不画成 0。 */
    function bubblePoints(rows) {
        rows = rows || [];
        var pos = [], neg = [], zero = [], noPe = 0, noCap = 0, pbMissing = 0;
        rows.forEach(function (r) {
            var pe = r ? num(r.pe) : null;
            var cap = r ? num(r.market_cap) : null;
            var pb = r ? num(r.pb) : null;
            if (pb === null) pbMissing++;
            if (pe === null) { noPe++; return; }          // 缺 PE：不画点（不能当 0 画在轴上）
            if (cap === null || cap <= 0) { noCap++; return; }  // 缺市值：不画点，但计数说明
            var pt = { name: String(r.name || ''), pe: pe, cap: cap };
            // PE=0 是真实值，不是盈利也不是亏损：单独一组，避免"0 被算成盈利"
            if (pe < 0) neg.push(pt); else if (pe > 0) pos.push(pt); else zero.push(pt);
        });
        return {
            positive: pos, negative: neg, zero: zero,
            noPe: noPe, noCap: noCap,
            pbMissing: pbMissing, total: rows.length,
            drawn: pos.length + neg.length + zero.length,
            pbAllMissing: rows.length > 0 && pbMissing === rows.length
        };
    }

    function fmtInt(n) { return String(Math.round(n)); }

    /* 范围文案：完整量从元数据来，绝不由本地行数推断上游总量。 */
    function rangeText(meta, shown) {
        meta = meta || {};
        var rows = num(meta.rows), total = num(meta.total);
        var completeness = meta.completeness || 'unknown';
        var s;

        if (rows === null && total === null) {
            return '板块数量未知（接口未返回元数据）。';
        }
        if (completeness === 'complete' && total !== null && rows !== null && rows === total) {
            s = '行业板块共 ' + fmtInt(total) + ' 个';
        } else if (completeness === 'partial') {
            if (total !== null && rows !== null) {
                s = '数据不完整：本次取得 ' + fmtInt(rows) + '/' + fmtInt(total) +
                    ' 个板块（缺 ' + fmtInt(Math.max(0, total - rows)) + ' 个）';
            } else {
                s = '数据不完整：本次取得的板块数未知';
            }
        } else if (completeness === 'unknown') {
            if (rows !== null && total !== null) {
                s = '完整性未知：本次取得 ' + fmtInt(rows) + ' 个板块，上游报告总量 ' +
                    fmtInt(total) + ' 个，但无法确认本次数据完整性';
            } else if (rows !== null) {
                s = '完整性未知：本次取得 ' + fmtInt(rows) + ' 个板块（上游总量未知）';
            } else {
                s = '完整性未知：板块数量无法确认';
            }
        } else {
            // 元数据缺失时最保守的说法
            s = (rows !== null) ? '本次取得 ' + fmtInt(rows) + ' 个板块（完整性未知）'
                                : '板块数量未知';
        }

        var n = num(shown);
        if (n !== null && rows !== null && n < rows) {
            s += '，当前展示前 ' + fmtInt(n) + ' 个。';
        } else if (n !== null && rows !== null) {
            s += '，当前全部展示。';
        } else {
            s += '。';
        }
        return s;
    }

    /* 新鲜度/来源文案：本地抓取时间 ≠ 交易所数据时间。 */
    function freshnessText(meta) {
        meta = meta || {};
        var served = meta.served_from || '';
        var fresh = meta.freshness || 'unknown';
        var when = meta.fetched_at || '';
        var tail = when ? '（本地抓取时间 ' + when + '）' : '';
        var hasData = served && served !== 'none';
        if (meta.fetch_status === 'failed' && !hasData) {
            return '⚠ 本次抓取失败，当前无可用数据。';
        }
        if (served === 'file_fallback') {
            return '⚠ 本次抓取失败，展示的是旧缓存' + tail + '，不是实时数据。';
        }
        if (meta.fetch_status === 'failed' && hasData) {
            return '⚠ 本次抓取失败，展示的是已有缓存' + tail + '。';
        }
        if (fresh === 'stale') return '⚠ 数据已过期' + tail + '。';
        if (fresh === 'unknown') return '⚠ 无法确认数据新鲜度' + tail + '。';
        if (meta.asof && meta.asof !== 'unknown') return '数据时间 ' + meta.asof + '；' + tail;
        return '上游未提供数据时间，仅显示本地抓取时间' + (when ? ' ' + when : '') + '。';
    }

    /* 计数作用域：complete 才说"共 N 个"，partial/unknown 只能说"本次取得"。 */
    function scopeText(meta, rowsFromData) {
        meta = meta || {};
        var rows = num(meta.rows);
        if (rows === null) rows = num(rowsFromData);
        var total = num(meta.total);
        var c = meta.completeness || 'unknown';
        if (c === 'complete' && total !== null && rows !== null && rows === total) {
            return '行业板块共 ' + fmtInt(total) + ' 个';
        }
        if (c === 'partial') {
            if (rows !== null && total !== null) {
                return '数据不完整：本次取得 ' + fmtInt(rows) + '/' + fmtInt(total) +
                    ' 个板块（缺 ' + fmtInt(Math.max(0, total - rows)) + ' 个）';
            }
            return '数据不完整：本次取得 ' + (rows === null ? '未知' : fmtInt(rows)) + ' 个板块';
        }
        if (c === 'unknown' && rows !== null) {
            return total !== null
                ? '完整性未知：本次取得 ' + fmtInt(rows) + ' 个板块（上游报告总量 ' + fmtInt(total) + ' 个）'
                : '完整性未知：本次取得 ' + fmtInt(rows) + ' 个板块（上游总量未知）';
        }
        return '完整性未知：本次取得 ' + (rows === null ? '未知' : fmtInt(rows)) + ' 个板块';
    }

    /* 读取 X-Data-*：只接受真实响应头，缺失一律 null（不猜）。 */
    var HEADER_MAP = {
        'X-Data-Fetch-Status': 'fetch_status',
        'X-Data-Completeness': 'completeness',
        'X-Data-Freshness': 'freshness',
        'X-Data-Meta-Status': 'meta_status',
        'X-Data-Rows': 'rows',
        'X-Data-Total': 'total',
        'X-Data-Fetched-At': 'fetched_at',
        'X-Data-AsOf': 'asof',
        'X-Data-Served-From': 'served_from',
        'X-Data-Missing': 'missing'
    };

    function parseHeaders(get) {
        var meta = {};
        Object.keys(HEADER_MAP).forEach(function (h) {
            var raw = typeof get === 'function' ? get(h) : null;
            var key = HEADER_MAP[h];
            if (raw === null || raw === undefined || raw === '' || raw === 'unknown' || raw === '-') {
                meta[key] = (key === 'rows' || key === 'total') ? null : (raw === '-' ? '' : 'unknown');
                return;
            }
            if (key === 'rows' || key === 'total') meta[key] = num(raw);
            else meta[key] = String(raw);
        });
        return meta;
    }

    var api = {
        classifyPE: classifyPE,
        peRank: peRank,
        bubblePoints: bubblePoints,
        rangeText: rangeText,
        scopeText: scopeText,
        freshnessText: freshnessText,
        parseHeaders: parseHeaders
    };

    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    global.sectorView = api;
})(typeof window !== 'undefined' ? window : this);
