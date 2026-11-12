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
        // drawn 只统计"真的有有限绘制坐标"的点：正 PE（对数轴）、负 PE（线性负轴）、PE=0（独立类目轴）
        return {
            positive: pos, negative: neg, zero: zero,
            noPe: noPe, noCap: noCap,
            pbMissing: pbMissing, total: rows.length,
            drawn: pos.length + neg.length + zero.length,
            undrawn: noPe + noCap,
            pbAllMissing: rows.length > 0 && pbMissing === rows.length
        };
    }

    /* ECharts option：纯函数，便于用真实 ECharts 在 Node 里验证坐标。
     * 关键：对数轴不能表示 0，所以 PE=0 走独立 grid + 类目轴，
     * 它是真实坐标（有限值），不是被丢掉或改成 0.01 的假值。 */
    function scatterOption(pts) {
        pts = pts || { positive: [], negative: [], zero: [], total: 0, drawn: 0 };
        var negMin = -10;
        if (pts.negative.length) {
            negMin = Math.floor(Math.min.apply(null, pts.negative.map(function (p) { return p.pe; })) * 1.1);
        }
        var hasZero = pts.zero.length > 0;
        var AX = { color: '#8892b0' };

        function bubbleSize(val) {
            var c = Math.max(1, Number(val[1]) || 1);
            return Math.max(7, Math.min(34, 7 * Math.log10(c + 10)));
        }

        function series(label, arr, color, gridIndex, xIdx, yIdx, catValue) {
            var s = {
                name: label, type: 'scatter',
                xAxisIndex: xIdx, yAxisIndex: yIdx, gridIndex: gridIndex,
                data: arr.map(function (p) {
                    return {
                        name: p.name, pe: p.pe, cap: p.cap,
                        value: [catValue === undefined ? p.pe : catValue, p.cap],
                        itemStyle: { color: color }
                    };
                }),
                symbolSize: bubbleSize,
                label: { show: false },
                emphasis: {
                    label: { show: true, color: '#fff', fontSize: 11,
                             formatter: function (q) { return q.data.name; } }
                }
            };
            return s;
        }

        var grids = [{ left: '4%', width: '50%', top: 100, bottom: 28, containLabel: true }];
        var xAxes = [{ type: 'log', gridIndex: 0, name: 'PE（对数刻度，非线性）', nameLocation: 'middle',
                       nameGap: 30, nameTextStyle: AX, axisLabel: AX, splitLine: { lineStyle: { color: '#233054' } } }];
        var yAxes = [{ type: 'log', gridIndex: 0, name: '市值（亿元，对数）', nameTextStyle: AX,
                       axisLabel: AX, splitLine: { lineStyle: { color: '#233054' } } }];
        var allSeries = [series('盈利（PE>0）', pts.positive, '#ef5350', 0, 0, 0)];

        xAxes.push({ type: 'value', gridIndex: 1, min: negMin, max: 0, name: 'PE（负值，独立线性轴）',
                     nameTextStyle: { color: '#7e57c2' }, axisLabel: AX,
                     splitLine: { lineStyle: { color: '#233054' } } });
        yAxes.push({ type: 'log', gridIndex: 1, name: '市值（亿元，对数）', nameTextStyle: AX,
                     axisLabel: AX, splitLine: { lineStyle: { color: '#233054' } } });

        if (hasZero) {
            grids.push({ left: '63%', width: '33%', top: 100, height: 200, containLabel: true });
            grids.push({ left: '63%', width: '33%', top: 352, height: 96, containLabel: true });
            xAxes.push({ type: 'category', gridIndex: 2, data: ['PE=0'], name: 'PE=0（非盈利，独立区域）',
                         nameTextStyle: { color: '#90a4ae' }, axisLabel: { color: '#90a4ae' },
                         splitLine: { show: false } });
            yAxes.push({ type: 'log', gridIndex: 2, name: '市值（亿元，对数）', nameTextStyle: AX,
                         axisLabel: AX, splitLine: { lineStyle: { color: '#233054' } } });
        } else {
            grids.push({ left: '63%', width: '33%', top: 100, bottom: 28, containLabel: true });
        }

        var lossSeries = series('亏损（PE<0）', pts.negative, '#7e57c2', 1, 1, 1);
        if (pts.negative.length) {
            var worst = pts.negative.reduce(function (a, b) { return b.pe < a.pe ? b : a; });
            lossSeries.markPoint = {
                symbolSize: 8,
                itemStyle: { color: '#ffd54f' },
                label: { show: true, color: '#ffd54f', fontSize: 11,
                         formatter: worst.name + ' ' + worst.pe },
                data: [{ name: worst.name, coord: [worst.pe, worst.cap] }]
            };
        }
        allSeries.push(lossSeries);
        if (hasZero) {
            allSeries.push(series('PE=0（非盈利）', pts.zero, '#90a4ae', 2, 2, 2, 'PE=0'));
        }

        return {
            color: ['#ef5350', '#90a4ae', '#7e57c2'],
            title: {
                text: hasZero ? '板块估值气泡图（左：PE>0 / 右上：PE<0 / 右下：PE=0）'
                              : '板块估值气泡图（左：PE>0 / 右：PE<0 独立坐标）',
                subtext: 'PB 上游全部缺失，不参与坐标；左侧 PE 与两侧市值均为对数轴，右侧负 PE 为独立线性轴；负 PE 为亏损，不是低估值',
                textStyle: { color: '#e0e0e0', fontSize: 14 },
                subtextStyle: { color: '#8892b0', fontSize: 11 }
            },
            legend: {
                data: hasZero ? ['盈利（PE>0）', '亏损（PE<0）', 'PE=0（非盈利）']
                              : ['盈利（PE>0）', '亏损（PE<0）'],
                textStyle: { color: '#8892b0' }, top: 54
            },
            tooltip: {
                formatter: function (q) {
                    var pe = q.data.pe, cap = q.data.cap;
                    var peTxt = pe === 0 ? '0（PE=0，非盈利也不亏损）'
                              : (pe < 0 ? pe + '（亏损，不代表低估值）' : String(pe));
                    var money = (typeof window !== 'undefined' && window.fmt && window.fmt.money)
                        ? window.fmt.money(cap, '亿') : (cap + ' 亿');
                    return q.data.name + '<br/>PE: ' + peTxt + '<br/>市值: ' + money;
                }
            },
            grid: grids,
            xAxis: xAxes,
            yAxis: yAxes,
            series: allSeries
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

    /* 网络异常文案：只有真的有旧数据才说"保留此前数据"，否则就是"无可用数据"。 */
    function netErrorText(hadData, kind) {
        var why = kind === 'network' ? '（网络连接失败）'
                : kind === 'parse' ? '（响应解析失败）'
                : kind === 'code' ? '（接口返回异常）' : '';
        return hadData ? '⚠ 本次刷新失败，当前保留此前页面数据' + why + '。'
                       : '⚠ 请求失败，当前无可用数据' + why + '。';
    }

    /* 统一取数：任何异常都走 onError，Promise 永不 reject（不会产生未处理拒绝）。 */
    function loadSectorData(opts) {
        opts = opts || {};
        var f = opts.fetch || (typeof fetch !== 'undefined' ? fetch : null);
        var onData = opts.onData || function () {};
        var onError = opts.onError || function () {};
        if (!f) {
            var none = { kind: 'network', error: new Error('fetch unavailable') };
            onError(none);
            return Promise.resolve({ ok: false, kind: 'network' });
        }
        return Promise.resolve()
            .then(function () { return f(opts.url); })
            .then(function (resp) {
                var meta = parseHeaders(function (n) {
                    return (resp && resp.headers && resp.headers.get) ? resp.headers.get(n) : null;
                });
                return Promise.resolve()
                    .then(function () { return resp.json(); })
                    .then(function (body) { return { body: body, meta: meta }; },
                          function () { return { body: null, meta: meta, parseError: true }; });
            })
            .then(function (x) {
                if (!x.body || x.body.code !== 200) {
                    var kind = x.parseError ? 'parse' : 'code';
                    onError({ kind: kind, body: x.body, meta: x.meta });
                    return { ok: false, kind: kind, meta: x.meta, body: x.body };
                }
                onData(x.body, x.meta);
                return { ok: true, meta: x.meta, body: x.body };
            })
            .catch(function (err) {
                onError({ kind: 'network', error: err, meta: null });
                return { ok: false, kind: 'network', error: err };
            });
    }

    /* 页面级加载器：
     *   - 成功后再次 load() 直接跳过（防重复请求）；load(true) 强制刷新（定时刷新用）
     *   - 首次失败自动解除占位，允许重试
     *   - 已有数据时刷新失败只提示，不清掉已显示的数据
     */
    function sectorLoader(cfg) {
        cfg = cfg || {};
        var state = { loaded: false, data: null, lastError: null, attempts: 0, inFlight: false };

        function load(force) {
            if (state.loaded && !force) return Promise.resolve({ skipped: true, ok: true });
            if (state.inFlight) return state.inFlight;

            state.loaded = true;                 // 先占位，防止连点 Tab/并发刷新
            state.inFlight = true;
            state.attempts += 1;
            var hadData = state.data !== null;

            var p = loadSectorData({
                url: cfg.url,
                fetch: cfg.fetch,
                onData: function (body, meta) {
                    state.data = body.data;
                    state.lastError = null;
                    if (cfg.render) cfg.render(body, meta);
                },
                onError: function (info) {
                    state.lastError = info.kind;
                    if (!hadData) state.loaded = false;   // 首次失败：解除占位，允许重新请求
                    if (cfg.fail) cfg.fail(info, hadData, netErrorText(hadData, info.kind));
                }
            });
            state.inFlight = p;
            return p.then(function (r) { state.inFlight = false; return r; });
        }

        return { load: load, state: state };
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
        scatterOption: scatterOption,
        rangeText: rangeText,
        netErrorText: netErrorText,
        loadSectorData: loadSectorData,
        sectorLoader: sectorLoader,
        scopeText: scopeText,
        freshnessText: freshnessText,
        parseHeaders: parseHeaders
    };

    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    global.sectorView = api;
})(typeof window !== 'undefined' ? window : this);
