/**
 * 前端图表渲染函数 + 自动刷新
 * 所有 ECharts 图表绘制集中在这里
 */

// ========== 全局配置 ==========
var AUTO_REFRESH_INTERVAL = 30000;  // 30秒自动刷新一次
var refreshTimers = {};

// ========== 自动刷新工具 ==========

function startAutoRefresh(name, fn, interval) {
    interval = interval || AUTO_REFRESH_INTERVAL;
    stopAutoRefresh(name);
    refreshTimers[name] = setInterval(fn, interval);
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

    chart.setOption({
        tooltip: { trigger: 'item', formatter: '{b}: {c}亿 ({d}%)' },
        color: colors,
        series: [{
            type: 'pie',
            radius: ['40%', '70%'],
            center: ['50%', '50%'],
            label: { color: '#e0e0e0', fontSize: 12 },
            data: items.map(i => ({ name: i.name, value: Math.abs(i.value) })),
            emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.3)' } }
        }]
    });
}

// ========== 横向柱状图 ==========

function renderBarChart(domId, xLabel, data) {
    const chart = getChart(domId);
    if (!chart) return;

    chart.setOption({
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: { type: 'value', axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
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
                formatter: (p) => p.value + '%',
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
        tooltip: { trigger: 'axis' },
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: {
            type: 'category',
            data: items.map(i => i.name),
            axisLabel: { color: '#e0e0e0' }
        },
        yAxis: {
            type: 'value',
            axisLabel: { color: '#8892b0', formatter: '{value} 亿' },
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
                formatter: (p) => p.value + '亿',
                color: '#8892b0'
            }
        }]
    });
}

// ========== Treemap（热力图） ==========

function renderTreemap(domId, title, data) {
    const chart = getChart(domId);
    if (!chart) return;

    chart.setOption({
        tooltip: {
            formatter: function(p) {
                const d = p.data;
                return `${d.name}<br/>涨跌幅: ${d.change}%<br/>资金流入: ${d.flow}亿`;
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
                    return `${p.name}\n${p.data.change}%\n${p.data.flow}亿`;
                },
                color: '#fff',
                fontSize: 13,
                fontWeight: 'bold'
            },
            itemStyle: {
                borderWidth: 2,
                borderColor: '#1a1a2e'
            },
            levels: [
                { colorSaturation: [0.3, 0.7], colorMappingBy: 'value' }
            ],
            data: data
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
            textStyle: { color: '#e0e0e0', fontSize: 14 }
        },
        tooltip: { trigger: 'axis' },
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
            axisLabel: { color: '#8892b0', formatter: '{value} 亿' },
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
        grid: { left: '3%', right: '4%', bottom: '3%', top: '20%', containLabel: true },
        xAxis: { type: 'category', data: dates, axisLabel: { color: '#8892b0', rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        series: series
    });
}

// ========== 股价与内在价值对比 ==========

function renderIntrinsicValue(domId, data) {
    const chart = getChart(domId);
    if (!chart) return;

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
            label: { show: true, position: 'top', formatter: function(p) { return p.seriesIndex === 2 ? p.value + '%' : p.value.toFixed(2); }, color: '#8892b0' }
        }]
    });
}

// ========== 板块估值气泡图 ==========

function renderSectorValuation(domId, sectors) {
    const chart = getChart(domId);
    if (!chart) return;

    var data = sectors.map(function(s) {
        return {
            name: s.name,
            value: [s.pe, s.pb, s.market_cap, s.name],
            itemStyle: { color: s.change >= 0 ? '#ef5350' : '#26a69a' }
        };
    });

    chart.setOption({
        title: { text: '板块估值散点图（PE vs PB）', textStyle: { color: '#e0e0e0', fontSize: 14 } },
        tooltip: { formatter: function(p) { return p.data.name + '<br/>PE: ' + p.data.value[0] + '<br/>PB: ' + p.data.value[1] + '<br/>市值: ' + p.data.value[2] + '亿'; } },
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: { name: 'PE', nameTextStyle: { color: '#8892b0' }, axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        yAxis: { name: 'PB', nameTextStyle: { color: '#8892b0' }, axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        series: [{
            type: 'scatter',
            data: data,
            symbolSize: function(val) { return Math.max(10, Math.min(50, val[2] / 500)); },
            label: { show: true, formatter: function(p) { return p.data.name; }, color: '#e0e0e0', fontSize: 10, position: 'right' }
        }]
    });
}

// ========== 板块估值排行（PE排行） ==========

function renderSectorPERank(domId, sectors) {
    const chart = getChart(domId);
    if (!chart) return;

    var sorted = sectors.slice().sort(function(a,b) { return b.pe - a.pe; });
    chart.setOption({
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: { type: 'value', axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        yAxis: { type: 'category', data: sorted.map(function(s) { return s.name; }).reverse(), axisLabel: { color: '#e0e0e0', fontSize: 11 } },
        series: [{
            type: 'bar',
            data: sorted.map(function(s) { return { value: s.pe, itemStyle: { color: s.pe > 40 ? '#ef5350' : (s.pe < 15 ? '#26a69a' : '#ffb74d') } }; }).reverse(),
            barWidth: 14,
            label: { show: true, position: 'right', formatter: function(p) { return p.value; }, color: '#8892b0', fontSize: 11 }
        }]
    });
}
