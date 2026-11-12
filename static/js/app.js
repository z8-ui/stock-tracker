/**
 * 前端图表渲染函数 + 自动刷新
 * 所有 ECharts 图表绘制集中在这里
 */

// ========== 全局配置 ==========
var AUTO_REFRESH_INTERVAL = 15000;  // 15秒自动刷新一次（交易时段）
var IDLE_REFRESH_INTERVAL = 60000; // 1分钟刷新一次（休盘时段）
var refreshTimers = {};

// ========== 自动刷新工具 ==========

function startAutoRefresh(name, fn, interval) {
    interval = interval || AUTO_REFRESH_INTERVAL;
    stopAutoRefresh(name);
    refreshTimers[name] = setInterval(fn, interval);
    // 更新右上角刷新指示器
    var secEl = document.getElementById('refresh-secs');
    if (secEl) secEl.textContent = interval / 1000;
    console.log('[自动刷新] ' + name + ' 已启动，间隔 ' + (interval/1000) + '秒');
}

function stopAutoRefresh(name) {
    if (refreshTimers[name]) {
        clearInterval(refreshTimers[name]);
        delete refreshTimers[name];
    }
}

function updateTimestamp(domId) {
    var el = document.getElementById(domId);
    if (el) el.textContent = new Date().toLocaleTimeString();
}

function stopAllAutoRefresh() {
    Object.keys(refreshTimers).forEach(stopAutoRefresh);
}

// ========== 工具函数 ==========

function getChart(domId) {
    const dom = document.getElementById(domId);
    if (!dom) return null;
    let chart = echarts.getInstanceByDom(dom);
    if (!chart) chart = echarts.init(dom);
    return chart;
}

// ========== 饼图 ==========

function renderPieChart(domId, title, items, colors) {
    const chart = getChart(domId);
    if (!chart) return;

    // 空值语义（方案 A）：
    //   1) null = 源没给这个类别，必须原样保留 —— 不能经 Math.abs(null)→0 变成"真实的 0.00 亿"；
    //   2) ECharts 不会为 null 数据绘制扇区，所以用户看不到这一块，也不会悬停到它；
    //   3) 缺失类别对用户的实际提示只来自 gapHint()（下方 putPieGapHint 展示的那行文字）；
    //   4) pieTip() 里的 null 分支只是防御性格式化（保证万一被调用也不显示 0.00亿），
    //      不代表"真实浏览器可以悬停 null 扇区看 tooltip"—— 这条路径不存在。
    const seriesItems = window.fmt.pieItems(items);
    putPieGapHint(domId, window.fmt.gapHint(seriesItems));

    chart.setOption({
        tooltip: {
            trigger: 'item',
            // 饼图给的是绝对值（流入/流出规模），不显示正负号，但必须两位小数
            formatter: function (p) {
                return window.fmt.pieTip(p.name, p.value, p.percent);
            }
        },
        color: colors,
        legend: {
            data: items.map(i => i.name),
            textStyle: { color: '#8892b0', fontSize: 12 },
            bottom: 0,
            itemWidth: 14,
            itemHeight: 14
        },
        series: [{
            type: 'pie',
            radius: ['35%', '55%'],
            center: ['50%', '42%'],
            label: { color: '#e0e0e0', fontSize: 12 },
            data: seriesItems,
            emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.3)' } }
        }]
    });
}

// 缺失类别提示：动态文字，没有缺失时移除节点（不占位、不留空行）
function putPieGapHint(domId, text) {
    const box = document.getElementById(domId);
    if (!box || !box.parentNode) return;
    const id = domId + '-gap';
    let el = document.getElementById(id);
    if (!text) {
        if (el) el.remove();
        return;
    }
    if (!el) {
        el = document.createElement('div');
        el.id = id;
        el.style.cssText = 'color:#ffb74d; font-size:12px; padding:4px 8px 0;';
        box.parentNode.appendChild(el);
    }
    el.textContent = text;
}

// ========== 横向柱状图 ==========

function renderBarChart(domId, xLabel, data) {
    const chart = getChart(domId);
    if (!chart) return;

    var suffix = '%';
    if (xLabel.indexOf('亿') !== -1) suffix = '亿';

    chart.setOption({
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: '16%', right: '14%', bottom: '3%', containLabel: true },
        xAxis: {
            type: 'value',
            name: xLabel,
            nameTextStyle: { color: '#8892b0', fontSize: 11 },
            axisLabel: { color: '#8892b0' },
            splitLine: { lineStyle: { color: '#233054' } }
        },
        yAxis: {
            type: 'category',
            data: data.map(d => d.name).reverse(),
            axisLabel: { color: '#e0e0e0', fontSize: 11 }
        },
        series: [{
            type: 'bar',
            data: data.map(d => ({
                value: d.change || d.flow || 0,
                itemStyle: { color: (d.change || 0) >= 0 ? '#ef5350' : '#26a69a' }
            })).reverse(),
            barWidth: 14,
            label: {
                show: true,
                position: 'right',
                formatter: function(p) {
                    return suffix === '亿' ? window.fmt.money(p.value, '亿') : window.fmt.pct(p.value);
                },
                color: '#8892b0',
                fontSize: 11
            }
        }]
    });
}

// ========== 主力资金柱状图 ==========

function renderMainForceChart(domId, data) {
    const chart = getChart(domId);
    if (!chart) return;

    const items = data.items;
    chart.setOption({
        tooltip: {
            trigger: 'axis',
            formatter: function (ps) {
                var one = ps[0] || {};
                return one.name + '<br/>主力净流入: ' + window.fmt.money(one.value, '亿');
            }
        },
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: {
            type: 'category',
            data: items.map(i => i.name),
            axisLabel: { color: '#e0e0e0' }
        },
        yAxis: {
            type: 'value',
            axisLabel: { color: '#8892b0', formatter: function (v) { return window.fmt.fixed(v) + ' 亿'; } },
            splitLine: { lineStyle: { color: '#233054' } }
        },
        series: [{
            type: 'bar',
            data: items.map(i => ({
                value: i.value,
                itemStyle: { color: i.value >= 0 ? '#ef5350' : '#26a69a' }
            })),
            barWidth: '40%',
            label: {
                show: true,
                position: 'top',
                formatter: (p) => window.fmt.money(p.value, '亿'),
                color: '#8892b0'
            }
        }]
    });
}

// ========== Treemap（热力图） ==========

function renderTreemap(domId, title, data) {
    const chart = getChart(domId);
    if (!chart) return;

    // 转换数据：资金流向红绿着色，资金规模决定方块大小
    var maxFlow = 1;
    data.forEach(function(d) { var f = Math.abs(d.flow || 1); if (f > maxFlow) maxFlow = f; });

    var treeData = data.map(function(d) {
        var flow = d.flow || 0;
        // 颜色按资金流向方向：红流入、绿流出，深浅随规模变化
        var absFlow = Math.abs(flow);
        var intensity = Math.min(absFlow / 10, 1);  // 10亿以上饱和度封顶
        var r, g, b;
        if (flow >= 0) {
            // 流入：红~橙
            r = Math.round(239 - (1 - intensity) * 60);
            g = Math.round(83 - intensity * 70);
            b = Math.round(80 - intensity * 70);
        } else {
            // 流出：浅绿~深绿
            r = Math.round(102 - intensity * 90);
            g = Math.round(187 - intensity * 140);
            b = Math.round(106 - intensity * 90);
        }
        return {
            name: d.name,
            value: Math.max(absFlow, 0.5),
            change: d.change,
            flow: d.flow,
            itemStyle: {
                color: 'rgb(' + r + ',' + g + ',' + b + ')'
            }
        };
    });

    chart.setOption({
        tooltip: {
            formatter: function(p) {
                const d = p.data;
                const direction = d.flow >= 0 ? '流入' : '流出';
                return d.name + '<br/>涨跌幅: ' + window.fmt.pct(d.change) +
                    '<br/>资金' + direction + ': ' + window.fmt.fixed(Math.abs(d.flow)) + '亿';
            }
        },
        series: [{
            type: 'treemap',
            roam: false,
            nodeClick: false,
            width: '100%',
            height: '100%',
            top: 0,
            label: {
                show: true,
                formatter: function(p) {
                    return p.name + '\n' + window.fmt.pct(p.data.change) + '\n' +
                        window.fmt.money(p.data.flow, '亿');
                },
                color: '#fff',
                fontSize: 13,
                fontWeight: 'bold'
            },
            itemStyle: {
                borderWidth: 2,
                borderColor: '#1a1a2e'
            },
            data: treeData
        }]
    });
}

// ========== 个股资金流向（堆叠柱状图） ==========

function renderStockFlow(domId, data) {
    const chart = getChart(domId);
    if (!chart) return;

    chart.setOption({
        title: {
            text: data.title,
            subtext: data.subtitle || '',
            textStyle: { color: '#e0e0e0', fontSize: 14 },
            subtextStyle: { color: data.is_mock ? '#ffb74d' : '#8892b0', fontSize: 11 }
        },
        tooltip: {
            trigger: 'axis',
            formatter: function (ps) {
                if (!ps || !ps.length) return '';
                var rows = ps.map(function (p) {
                    return p.marker + p.seriesName + ': ' + window.fmt.money(p.value, '亿');
                });
                return ps[0].name + '<br/>' + rows.join('<br/>');
            }
        },
        legend: {
            data: data.series.map(s => s.name),
            textStyle: { color: '#8892b0' },
            top: 30
        },
        grid: { left: '3%', right: '4%', bottom: '3%', top: '20%', containLabel: true },
        xAxis: {
            type: 'category',
            data: data.dates,
            axisLabel: { color: '#8892b0', rotate: 45, fontSize: 11 }
        },
        yAxis: {
            type: 'value',
            axisLabel: { color: '#8892b0', formatter: function (v) { return window.fmt.fixed(v) + ' 亿'; } },
            splitLine: { lineStyle: { color: '#233054' } }
        },
        series: data.series.map((s, i) => ({
            name: s.name,
            type: 'bar',
            stack: 'total',
            data: s.data,
            itemStyle: { color: data.colors[i] }
        }))
    });
}

// ========== 估值仪表盘 ==========

function renderValuationGauge(domId, data) {
    const chart = getChart(domId);
    if (!chart) return;

    // 亏损股/停牌：PE 缺失时不再拿行业 PE 顶替，直接显式提示"数据不可用"
    if (data.pe === null || data.pe === undefined || data.deviation_pe === null || data.deviation_pe === undefined) {
        chart.clear();
        chart.setOption({
            title: {
                text: '估值数据不可用',
                subtext: data.level_reason || 'PE 缺失（亏损股 / 停牌），无法计算偏离度',
                left: 'center', top: '38%',
                textStyle: { color: '#ffb74d', fontSize: 15 },
                subtextStyle: { color: '#8892b0', fontSize: 11 }
            }
        });
        return;
    }

    chart.setOption({
        title: { text: data.title, textStyle: { color: '#e0e0e0', fontSize: 14 } },
        tooltip: { trigger: 'axis' },
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: { type: 'category', data: ['个股PE', '行业平均PE', '偏离度'], axisLabel: { color: '#e0e0e0' } },
        yAxis: { type: 'value', axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        series: [{
            type: 'bar',
            data: [
                { value: data.pe, itemStyle: { color: data.deviation_pe > 20 ? '#ef5350' : (data.deviation_pe < -20 ? '#26a69a' : '#ffb74d') } },
                { value: data.industry_pe, itemStyle: { color: '#42a5f5' } },
                { value: data.deviation_pe, itemStyle: { color: '#ab47bc' } }
            ],
            barWidth: '40%',
            label: { show: true, position: 'top', formatter: function(p) { return p.seriesIndex === 2 ? p.value + '%' : p.value; }, color: '#8892b0' }
        }]
    });
}

// ========== EMA20 偏离度趋势图 ==========

function renderEMADeviation(domId, data) {
    const chart = getChart(domId);
    if (!chart) return;

    chart.setOption({
        title: { text: data.index_name + ' EMA20 均线偏离度', textStyle: { color: '#e0e0e0', fontSize: 14 } },
        tooltip: { trigger: 'axis' },
        legend: { data: ['收盘价', 'EMA20', '偏离度%'], textStyle: { color: '#8892b0' }, top: 30 },
        grid: { left: '3%', right: '4%', bottom: '3%', top: '20%', containLabel: true },
        xAxis: { type: 'category', data: data.dates, axisLabel: { color: '#8892b0', rotate: 45, fontSize: 10 } },
        yAxis: [
            { type: 'value', name: '价格', axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
            { type: 'value', name: '偏离%', axisLabel: { color: '#8892b0', formatter: '{value}%' }, splitLine: { show: false } }
        ],
        series: [
            { name: '收盘价', type: 'line', data: data.closes, smooth: true, lineStyle: { color: '#42a5f5', width: 2 }, itemStyle: { color: '#42a5f5' } },
            { name: 'EMA20', type: 'line', data: data.ema20, smooth: true, lineStyle: { color: '#ffb74d', width: 2, type: 'dashed' }, itemStyle: { color: '#ffb74d' } },
            { name: '偏离度%', type: 'bar', yAxisIndex: 1, data: data.deviation.map(function(v) {
                return { value: v, itemStyle: { color: v > 0 ? '#ef5350' : '#26a69a' } };
            }), barWidth: 8 }
        ]
    });
}

// ========== 斐波那契回调线 ==========

function renderFibonacci(domId, fib, klines) {
    const chart = getChart(domId);
    if (!chart) return;

    var dates = klines.map(function(k) { return k.date; });
    var closes = klines.map(function(k) { return k.close; });
    var levels = fib.levels;
    var levelKeys = Object.keys(levels).sort(function(a,b) { return parseFloat(b) - parseFloat(a); });

    var series = [
        { name: '收盘价', type: 'line', data: closes, smooth: true,
          lineStyle: { color: '#42a5f5', width: 2 }, itemStyle: { color: '#42a5f5' },
          markLine: { silent: true, data: levelKeys.map(function(k) {
              return { yAxis: levels[k], label: { formatter: 'Fib ' + k + ' (' + levels[k] + ')', color: '#aaa', fontSize: 10 } };
          }) } }
    ];

    // 添加每一条斐波那契线作为单独的序列（用于图例）
    var fibColors = ['#ef5350','#ff8a65','#ffb74d','#66bb6a','#26a69a'];
    levelKeys.forEach(function(k, i) {
        var lineData = [];
        for (var j = 0; j < dates.length; j++) {
            lineData.push(levels[k]);
        }
        series.push({
            name: 'Fib ' + k, type: 'line', data: lineData,
            lineStyle: { color: fibColors[i], width: 1, type: 'dashed' },
            symbol: 'none', smooth: true
        });
    });

    chart.setOption({
        title: { text: '斐波那契回调线', textStyle: { color: '#e0e0e0', fontSize: 14 } },
        tooltip: { trigger: 'axis' },
        legend: { data: ['收盘价'].concat(levelKeys.map(function(k) { return 'Fib ' + k; })), textStyle: { color: '#8892b0' }, top: 30 },
        grid: { left: '3%', right: '4%', bottom: '12%', top: '20%', containLabel: true },
        xAxis: { type: 'category', data: dates, axisLabel: { color: '#8892b0', rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        dataZoom: [
            { type: 'inside', start: 0, end: 100 },
            { type: 'slider', start: 0, end: 100, height: 18, bottom: 2, textStyle: { color: '#8892b0' } }
        ],
        series: series
    });
}

// ========== 股价与内在价值对比 ==========

function renderIntrinsicValue(domId, data) {
    const chart = getChart(domId);
    if (!chart) return;

    // EPS/行业PE 不可得时后端返回 intrinsic_value=null（不再用股价顶替）
    if (data.available === false || data.intrinsic_value === null || data.intrinsic_value === undefined) {
        chart.clear();
        chart.setOption({
            title: {
                text: '内在价值不可用',
                subtext: data.reason || '缺少可用的 EPS / 行业 PE',
                left: 'center', top: '38%',
                textStyle: { color: '#ffb74d', fontSize: 15 },
                subtextStyle: { color: '#8892b0', fontSize: 11 }
            }
        });
        return;
    }

    var color = data.gap > 0 ? '#ef5350' : '#26a69a';
    chart.setOption({
        title: { text: '股价 vs 内在价值', textStyle: { color: '#e0e0e0', fontSize: 14 } },
        tooltip: { trigger: 'axis' },
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: { type: 'category', data: ['当前股价', '内在价值', '偏离'], axisLabel: { color: '#e0e0e0' } },
        yAxis: { type: 'value', axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        series: [{
            type: 'bar',
            data: [
                { value: data.price, itemStyle: { color: '#42a5f5' } },
                { value: data.intrinsic_value, itemStyle: { color: '#ab47bc' } },
                { value: data.gap, itemStyle: { color: color } }
            ],
            barWidth: '35%',
            label: { show: true, position: 'top', formatter: function(p) { return p.seriesIndex === 2 ? p.value + '%' : window.fmt.fixed(p.value); }, color: '#8892b0' }
        }]
    });
}

// ========== 板块估值气泡图（S3-3C：PE × 市值，PB 缺失不再画成 0） ==========

function renderSectorValuation(domId, sectors, meta) {
    const chart = getChart(domId);
    if (!chart) return;
    const sv = window.sectorView;
    const pts = sv.bubblePoints(sectors || []);
    // 板块 Tab 默认隐藏：ECharts 初始化时量到的是隐藏宽度，必须重新量
    chart.resize();
    setTimeout(function () { chart.resize(); }, 60);
    chart.setOption(sv.scatterOption(pts), true);

    var note = document.getElementById('sector-scatter-note');
    if (note) {
        var parts = [];
        parts.push(pts.pbAllMissing
            ? 'PB：暂无数据（上游 ' + pts.total + '/' + pts.total + ' 返回 "-"），本图不使用 PB 轴'
            : 'PB：部分板块缺失（' + pts.pbMissing + '/' + pts.total + ' 为 null）');
        parts.push('已绘制 ' + pts.drawn + '/' + pts.total + ' 个：正 PE ' + pts.positive.length +
            ' 个（左，PE 对数轴）、亏损 ' + pts.negative.length + ' 个（右，独立线性负轴）' +
            (pts.zero.length ? '、PE=0 ' + pts.zero.length + ' 个（右下独立区域，类目轴）' : ''));
        if (pts.noPe) parts.push('未绘制 ' + pts.noPe + ' 个：PE 缺失（显示 --）');
        if (pts.noCap) parts.push('未绘制 ' + pts.noCap + ' 个：市值缺失');
        if (meta) parts.push(sv.rangeText(meta, pts.total));
        note.textContent = parts.join('；');
    }
}

// ========== 板块 PE 排行（S3-3C：只排正 PE，负 PE 单列） ==========

function renderSectorPERank(domId, sectors, meta) {
    const chart = getChart(domId);
    if (!chart) return;
    const sv = window.sectorView;
    const TOP = 20;
    const r = sv.peRank(sectors || [], TOP);

    var rows = r.ranked.slice().reverse();      // ECharts 横向柱：自下而上
    chart.setOption({
        title: { text: '板块 PE 排行（仅正 PE，低估值在前）', textStyle: { color: '#e0e0e0', fontSize: 14 } },
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: '16%', right: '12%', bottom: '8%', top: 44, containLabel: true },
        xAxis: {
            type: 'value', name: 'PE', nameTextStyle: { color: '#8892b0' },
            axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } }
        },
        yAxis: {
            type: 'category', data: rows.map(function (x) { return x.name; }),
            axisLabel: { color: '#e0e0e0', fontSize: 11 }
        },
        series: [{
            type: 'bar',
            data: rows.map(function (x) {
                return { value: x.pe, itemStyle: { color: x.pe > 40 ? '#ef5350' : (x.pe < 15 ? '#26a69a' : '#ffb74d') } };
            }),
            barWidth: 14,
            label: {
                show: true, position: 'right', color: '#8892b0', fontSize: 11,
                formatter: function (p) { return Number(p.value).toFixed(2); }
            }
        }]
    }, true);

    var note = document.getElementById('sector-rank-note');
    if (note) {
        var c = r.counts;
        var scope = sv.scopeText(meta, c.total);
        var txt = scope + '：正 PE ' + c.positive + ' 个（排行只用这些）；' +
            '亏损 ' + c.negative + ' 个（PE<0，不代表低估值）；' +
            'PE=0 ' + c.zero + ' 个；PE 缺失 ' + c.missing + ' 个（显示 --，不参与排行）。';
        if (r.loss.length) {
            var lossShown = r.loss.slice(0, 8);   // 只列亏损最大的 8 个，避免长列表溢出卡片
            txt += ' 亏损最大的 ' + lossShown.length + ' 个（共 ' + r.counts.negative + ' 个）：' +
                lossShown.map(function (x) { return x.name + '(' + x.pe.toFixed(2) + ')'; }).join('、') + '。';
        }
        if (meta) txt += ' ' + sv.rangeText(meta, r.shown);
        note.textContent = txt;
    }
}

/* ---------------------------------------------------------------- 数据健康
   只读 /api/data-health（后端不联网）：状态永远是"真实来源+时间"推出来的，
   缓存就写 CACHED，过期就写 STALE，绝不把缓存说成实时。 */
function dhEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
}

function renderDataHealth() {
    var box = document.getElementById('data-health-body');
    if (!box) return;
    fetch('/api/data-health').then(function (r) { return r.json(); }).then(function (res) {
        if (!res || res.code !== 200) { box.textContent = '数据健康获取失败'; return; }
        var rows = (res.data && res.data.rows) || [];
        var color = {REAL: '#4caf50', CACHED: '#64b5f6', STALE: '#ffb74d',
                     PARTIAL: '#ffb74d', UNAVAILABLE: '#ef5350'};
        var html = '<table style="width:100%;border-collapse:collapse;font-size:12px;">' +
            '<tr style="color:#8892b0;"><th style="text-align:left;padding:4px 6px;">模块</th>' +
            '<th style="text-align:left;">状态</th><th style="text-align:left;">更新时间</th>' +
            '<th style="text-align:left;">来源 / 说明</th></tr>';
        rows.forEach(function (r) {
            html += '<tr><td style="padding:4px 6px;white-space:nowrap;">' + dhEsc(r.module) + '</td>' +
                '<td style="color:' + (color[r.status] || '#8892b0') + ';white-space:nowrap;">' + dhEsc(r.status) + '</td>' +
                '<td style="white-space:nowrap;">' + dhEsc(r.updated_at || '--') + '</td>' +
                '<td>' + dhEsc((r.source ? r.source + ' · ' : '') + (r.detail || '')) + '</td></tr>';
        });
        html += '</table><p style="color:#5c6b8a;font-size:11px;margin-top:6px;">' +
            'REAL=真实(15分钟内) · CACHED=缓存(1天内) · STALE=过期/时间未知 · PARTIAL=部分可用 · ' +
            'UNAVAILABLE=不可用。单个数据源失败不代表整个系统失败；' +
            '休市期间不抓取更新，展示最近交易日数据属正常，不是故障。</p>';
        box.innerHTML = html;
        var ts = document.getElementById('ts-data-health');
        if (ts) ts.textContent = (res.data && res.data.generated_at) || '';
        var st = document.getElementById('status-data-health');
        if (st && res.data && res.data.summary) {
            var s = res.data.summary;
            st.textContent = s.abnormal ? ('异常 ' + s.abnormal + ' / ' + s.total) : '全部正常';
            st.style.color = s.level === 'ok' ? '#4caf50' : (s.level === 'warn' ? '#ffb74d' : '#ef5350');
        }
    }).catch(function () { box.textContent = '数据健康获取失败（接口异常）'; });
}

document.addEventListener('DOMContentLoaded', renderDataHealth);
