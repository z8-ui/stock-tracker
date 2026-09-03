// 估值偏离页 专属脚本（从 valuation.html 抽出，2026-08）
// 依赖: base.html 中的 echarts CDN + app.js（getChart 等共享函数）

// ========== 共享自选股池（与个股资金页共用 localStorage key: stockWatchlist） ==========
function getWatchlist() {
    try { return JSON.parse(localStorage.getItem('stockWatchlist') || '[]'); } catch(e) { return []; }
}
function isInWatchlist(code) {
    return getWatchlist().some(function(i) { return i.code === code; });
}
function toggleValWatchlist(code, name) {
    var w = getWatchlist();
    if (isInWatchlist(code)) {
        w = w.filter(function(i) { return i.code !== code; });
    } else {
        w.push({code: code, name: name});
    }
    localStorage.setItem('stockWatchlist', JSON.stringify(w));
    renderValWatchlist();
    updateValWatchlistBtn(code);
}
function clearValWatchlist() {
    localStorage.setItem('stockWatchlist', '[]');
    renderValWatchlist();
}
function renderValWatchlist() {
    var w = getWatchlist();
    var el = document.getElementById('val-watchlist-stocks');
    if (!w.length) {
        el.innerHTML = '<p class="hint">暂无自选股，搜索后点击卡片上的"加入自选"添加</p>';
        return;
    }
    el.innerHTML = w.map(function(i) {
        return '<button class="chip" onclick="loadStockVal(\'' + i.code + '\',\'' + i.name + '\')">' + i.name +
               ' <span onclick="event.stopPropagation();removeValWatchlist(\'' + i.code + '\')" style="margin-left:4px;cursor:pointer;">✕</span></button>';
    }).join('');
}
function removeValWatchlist(code) {
    var w = getWatchlist().filter(function(i) { return i.code !== code; });
    localStorage.setItem('stockWatchlist', JSON.stringify(w));
    renderValWatchlist();
}
function updateValWatchlistBtn(code) {
    // dynamic update via button in detail area
}

// ========== 搜索历史 ==========
function getValHistory() {
    try { return JSON.parse(localStorage.getItem('valSearchHistory') || '[]'); } catch(e) { return []; }
}
function addValHistory(code, name) {
    var h = getValHistory();
    h = h.filter(function(i) { return i.code !== code; });
    h.unshift({code: code, name: name});
    if (h.length > 10) h = h.slice(0, 10);
    localStorage.setItem('valSearchHistory', JSON.stringify(h));
    renderValHistory();
}
function renderValHistory() {
    var h = getValHistory();
    var el = document.getElementById('val-search-history');
    if (!h.length) { el.innerHTML = '<p class="hint">暂无</p>'; return; }
    el.innerHTML = h.map(function(i) {
        return '<button class="chip" onclick="loadStockVal(\'' + i.code + '\',\'' + i.name + '\')">' + i.name + '</button>';
    }).join('');
}
function searchValStock() {
    var q = document.getElementById('val-stock-search').value;
    if (!q) return;
    fetch('/api/search-stock?q=' + encodeURIComponent(q))
        .then(function(r) { return r.json(); })
        .then(function(res) {
            var el = document.getElementById('val-search-results');
            if (res.code !== 200 || !res.data || !res.data.length) {
                el.innerHTML = '<p class="hint">未找到匹配的股票</p>';
                return;
            }
            el.innerHTML = res.data.map(function(s) {
                return '<button class="chip" onclick="loadStockValBySearch(\'' + s.code + '\',\'' + s.name + '\')">' + s.name + ' (' + s.code + ')</button>';
            }).join('');
        });
}
function loadStockValBySearch(code, name) {
    loadStockVal(code, name);
    document.getElementById('val-search-results').innerHTML = '';
    document.getElementById('val-stock-search').value = name;
}

// ========== 财报快照 + 政策事件（2026-08 新增） ==========
// 财报数据来自 data_service.get_latest_financials（东财F10，动态拉取+6小时缓存）
// 政策事件来自 policy_notes.py（手动维护的策池，按时间线倒序）
function buildFinancialsHtml(fin) {
    if (!fin || !fin.report_date) return '';
    function fmtYi(v) { return (v === null || v === undefined) ? '-' : (v / 1e8).toFixed(2) + '亿'; }
    function fmtPct(v) { return (v === null || v === undefined) ? '-' : ((v > 0 ? '+' : '') + Number(v).toFixed(1) + '%'); }
    function pctColor(v) { return (v || 0) > 0 ? '#ef5350' : ((v || 0) < 0 ? '#26a69a' : '#8892b0'); }
    var eps = (fin.eps === null || fin.eps === undefined) ? '-' : fin.eps;
    var roe = (fin.roe === null || fin.roe === undefined) ? '-' : Number(fin.roe).toFixed(2) + '%';
    return '<hr>' +
        '<p style="color:#e0e0e0;font-weight:bold;margin:6px 0;">📊 最新财报 ' +
        '<span style="color:#8892b0;font-size:11px;font-weight:normal;">' + (fin.report_name || '') +
        ' · 披露 ' + (fin.notice_date || '-') + '</span></p>' +
        '<div class="summary-row"><span class="label">EPS（基本）</span><span class="value">' + eps + ' 元</span></div>' +
        '<div class="summary-row"><span class="label">净利润同比</span><span class="value" style="color:' + pctColor(fin.profit_yoy) + ';">' + fmtPct(fin.profit_yoy) + '</span></div>' +
        '<div class="summary-row"><span class="label">营收同比</span><span class="value" style="color:' + pctColor(fin.revenue_yoy) + ';">' + fmtPct(fin.revenue_yoy) + '</span></div>' +
        '<div class="summary-row"><span class="label">ROE</span><span class="value">' + roe + '</span></div>' +
        '<p style="color:#8892b0;font-size:11px;margin-top:4px;">净利润 ' + fmtYi(fin.net_profit) +
        ' / 营收 ' + fmtYi(fin.revenue) + '（东财F10，缓存6小时）</p>';
}

function buildPolicyHtml(notes) {
    if (!notes || !notes.length) return '';
    var dirColor = {'利好': '#ef5350', '利空': '#26a69a', '中性': '#ffb74d'};
    var html = '<hr><p style="color:#e0e0e0;font-weight:bold;margin:6px 0;">📰 政策与事件 ' +
        '<span style="color:#8892b0;font-size:11px;font-weight:normal;">估值偏离需结合政策/财报综合判断</span></p>';
    notes.forEach(function(n) {
        var c = dirColor[n.direction] || '#ffb74d';
        html += '<div style="border-left:3px solid ' + c + ';padding:4px 8px;margin:6px 0;background:rgba(255,255,255,0.03);">' +
            '<div><span style="color:' + c + ';font-weight:bold;font-size:11px;">[' + (n.direction || '中性') + ']</span> ' +
            '<span style="font-size:12px;color:#e0e0e0;">' + n.title + '</span>' +
            '<span style="color:#8892b0;font-size:11px;margin-left:6px;">' + (n.date || '') + '</span></div>' +
            '<div style="color:#8892b0;font-size:11px;margin-top:2px;line-height:1.6;">' + (n.impact || '') + '</div>' +
            '<div style="color:#5a6a8a;font-size:10px;margin-top:2px;">来源：' + (n.source || '-') + '</div>' +
            '</div>';
    });
    return html;
}

// ========== Tab 切换 ==========
function switchTab(event, tabId) {
    document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
    event.target.classList.add('active');
    document.getElementById(tabId).classList.add('active');
    setTimeout(function() {
        if (tabId === 'sector-tab') loadSectorValuation();
        if (tabId === 'index-tab') loadIndexAnalysis();
    }, 100);
}

// ========== 个股估值 ==========
var valCurrentCode = '600519';
var valCurrentName = '贵州茅台';

function loadStockVal(code, name, refresh) {
    valCurrentCode = code;
    valCurrentName = name;
    addValHistory(code, name);
    renderValWatchlist();

    // 在估值详情卡片加"加入自选"按钮
    var inWl = isInWatchlist(code);
    var wlBtn = inWl ? '<span style="color:#ef5350;font-weight:bold;">★ 已自选</span>' :
        '<button onclick="toggleValWatchlist(\'' + code + '\',\'' + name + '\')" style="background:#ffb74d;border:none;border-radius:4px;padding:4px 10px;color:#fff;cursor:pointer;font-size:12px;">☆ 加入自选</button>';
    // 手动更新按钮：强制穿透行业PE/财报缓存重新拉取
    var refreshBtn = '<button id="val-refresh-btn" onclick="refreshStockVal()" style="background:#42a5f5;border:none;border-radius:4px;padding:4px 10px;color:#fff;cursor:pointer;font-size:12px;margin-left:6px;" title="强制重新拉取行情/行业PE/财报并更新">🔄 更新数据</button>';

    Promise.all([
        fetch('/api/valuation?code=' + code + '&name=' + name + (refresh ? '&refresh=1' : '')).then(function(r) { return r.json(); }),
        fetch('/api/intrinsic-value?code=' + code).then(function(r) { return r.json(); }),
        fetch('/api/fibonacci?code=' + code + '&name=' + name).then(function(r) { return r.json(); })
    ]).then(function(results) {
        var valRes = results[0];
        var ivRes = results[1];
        var fibRes = results[2];

        // 摆动点/支撑压力/趋势线（独立请求，失败不影响上面三个）
        loadSwingAnalysis(code, name, swingLevel);

        if (valRes.code === 200) {
            var d = valRes.data;
            renderValuationGauge('chart-valuation', d);
            var levelColor = d.level === '偏高' ? '#ef5350' : (d.level === '偏低' ? '#26a69a' : '#ffb74d');
            document.getElementById('valuation-detail').innerHTML = wlBtn + refreshBtn +
                '<div class="summary-row"><span class="label">PE (市盈率)</span><span class="value">' + d.pe + '</span></div>' +
                '<div class="summary-row"><span class="label">PB (市净率)</span><span class="value">' + d.pb + '</span></div>' +
                '<div class="summary-row"><span class="label">PE-TTM</span><span class="value">' + d.pe_ttm + '</span></div>' +
                '<div class="summary-row"><span class="label">行业平均 PE（' + (d.industry_name || '未知') + '）</span><span class="value">' + d.industry_pe + '</span></div>' +
                '<hr>' +
                '<div class="summary-row"><span class="label">偏离度</span><span class="value" style="color:'+levelColor+';font-weight:bold;">' + (d.deviation_pe > 0 ? '+' : '') + d.deviation_pe + '%</span></div>' +
                '<div class="summary-row"><span class="label">结论</span><span class="value" style="color:'+levelColor+';font-weight:bold;">' + d.level + '</span></div>' +
                '<p style="color:#8892b0;font-size:12px;margin-top:6px;">行业PE来源：' + (d.industry_pe_source === 'eastmoney' ? '东财实时板块' : '静态参考（实时获取失败）') + '</p>';

            // 2026-08: 财报快照 + 政策事件（估值偏离需结合最新季报与产业政策）
            document.getElementById('valuation-detail').innerHTML +=
                buildFinancialsHtml(d.financials) + buildPolicyHtml(d.policy_notes);
        }

        if (ivRes.code === 200) {
            var d = ivRes.data;
            renderIntrinsicValue('chart-intrinsic', d);
            var gapColor = d.gap > 0 ? '#ef5350' : '#26a69a';
            document.getElementById('intrinsic-detail').innerHTML =
                '<div class="summary-row"><span class="label">当前股价</span><span class="value">' + d.price.toFixed(2) + '</span></div>' +
                '<div class="summary-row"><span class="label">内在价值（估算）</span><span class="value">' + d.intrinsic_value.toFixed(2) + '</span></div>' +
                '<div class="summary-row"><span class="label">每股收益 EPS</span><span class="value">' + d.eps + '</span></div>' +
                '<hr>' +
                '<div class="summary-row"><span class="label">偏离</span><span class="value" style="color:'+gapColor+';font-weight:bold;">' + (d.gap > 0 ? '+' : '') + d.gap + '%</span></div>' +
                '<div class="summary-row"><span class="label">判断</span><span class="value" style="color:'+gapColor+';font-weight:bold;">' + (d.gap < -10 ? '低估' : (d.gap > 10 ? '高估' : '合理')) + '</span></div>';
        }

        // 斐波那契回调线
        if (fibRes && fibRes.code === 200) {
            renderStockFib(fibRes.data);
        }
    });
}

// ========== 手动更新估值（穿透行业PE/财报缓存，强制重新拉取） ==========
function refreshStockVal() {
    var btn = document.getElementById('val-refresh-btn');
    if (btn) { btn.disabled = true; btn.textContent = '更新中...'; }
    loadStockVal(valCurrentCode, valCurrentName, true);
    // 兜底：8 秒后若按钮仍处于禁用态（接口失败未重绘），恢复可点击
    setTimeout(function() {
        var b2 = document.getElementById('val-refresh-btn');
        if (b2 && b2.disabled) { b2.disabled = false; b2.textContent = '🔄 更新数据'; }
    }, 8000);
}

// ========== 个股斐波那契绘制（独立函数，可被定时刷新复用） ==========
function renderStockFib(fd) {
    if (!fd || !fd.fibonacci) return;
    var fib = fd.fibonacci;
    var levelKeys = Object.keys(fib.levels).sort(function(a,b) { return parseFloat(b) - parseFloat(a); });

    // 复用已有实例（统一走 app.js 的 getChart，避免重复 init）
    var fibChart = getChart('chart-fibonacci-stock');
    var fibDates = fd.dates;
    var fibCloses = fd.closes;
    var fibSeries = [
        {   name: fd.name, type: 'line',
            data: fibCloses,
            lineStyle: { color: '#42a5f5', width: 2 },
            itemStyle: { color: '#42a5f5' }
        }
    ];
    // 每个斐波那契水平线（横跨整个图表，虚线 + 右端价格标签）
    var fibColors2 = ['#ef5350','#ff8a65','#ffb74d','#66bb6a','#26a69a'];
    levelKeys.forEach(function(k, i) {
        var lineData = [];
        for (var j = 0; j < fibDates.length; j++) { lineData.push(fib.levels[k]); }
        fibSeries.push({
            name: 'Fib ' + k, type: 'line', data: lineData,
            lineStyle: { color: fibColors2[i], width: 1, type: 'dashed' },
            symbol: 'none', smooth: true,
            label: { show: true, formatter: fib.levels[k], position: 'end', color: fibColors2[i], fontSize: 10 }
        });
    });
    fibChart.setOption({
        title: { text: fd.name + ' 斐波那契回调线', textStyle: { color: '#e0e0e0', fontSize: 14 } },
        tooltip: { trigger: 'axis' },
        legend: { data: [fd.name].concat(levelKeys.map(function(k) { return 'Fib ' + k; })), textStyle: { color: '#8892b0' }, top: 30 },
        grid: { left: '3%', right: '4%', bottom: '12%', top: '20%', containLabel: true },
        xAxis: { type: 'category', data: fibDates, axisLabel: { color: '#8892b0', rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        dataZoom: [
            { type: 'inside', start: 0, end: 100 },
            { type: 'slider', start: 0, end: 100, height: 18, bottom: 2, textStyle: { color: '#8892b0' } }
        ],
        series: fibSeries
    });

    // 文字总结
    var nearestColor = fd.deviation_pct > 2 ? '#ef5350' : (fd.deviation_pct < -2 ? '#26a69a' : '#ffb74d');
    var direction = fd.deviation_pct >= 0 ? '上方' : '下方';
    var expandTip = fib.expanded ? '<p style="color:#8892b0;font-size:12px;margin-top:8px;">* 近20日区间过窄已自动展宽，虚线为展宽区间内的理论回调位</p>' : '';
    var asofTip = '<p style="color:#8892b0;font-size:12px;margin-top:4px;">数据截至 ' + (fd.asof || '-') + '（来源：' + (fd.source || '腾讯') + '）</p>';
    document.getElementById('fibonacci-stock-summary').innerHTML =
        '<div class="row"><div class="col">' +
        '<p><b>当前价：</b>' + fd.current_price + '</p>' +
        '<p><b>近20日高点：</b>' + fib.high + ' &nbsp; <b>低点：</b>' + fib.low + '</p>' +
        '</div><div class="col">' +
        '<p><b>最近支撑/阻力位：</b>Fib ' + fd.nearest_level + ' (' + fib.levels[fd.nearest_level] + ')' +
        ' <span style="color:' + nearestColor + '">（当前在' + direction + ' ' + Math.abs(fd.deviation_pct) + '%）</span></p>' +
        levelKeys.map(function(k) {
            var isNear = Math.abs(fd.current_price - fib.levels[k]) / fib.levels[k] < 0.02;
            return '<p style="margin-left:16px;' + (isNear ? 'color:#ffb74d;font-weight:bold;' : '') + '">Fib ' + k + ': <b>' + fib.levels[k] + '</b>' + (isNear ? ' ← 当前附近' : '') + '</p>';
        }).join('') +
        '</div></div>' + expandTip + asofTip;
}

// ========== 斐波那契定时刷新（盘中动态更新） ==========
var fibAutoRefreshTimer = null;
function startFibAutoRefresh() {
    stopFibAutoRefresh();
    fibAutoRefreshTimer = setInterval(function() {
        if (document.hidden) return;  // 页面不可见时不请求
        // 刷新个股斐波那契
        fetch('/api/fibonacci?code=' + valCurrentCode + '&name=' + valCurrentName)
            .then(function(r) { return r.json(); })
            .then(function(res) {
                if (res.code === 200) renderStockFib(res.data);
            });
        // 刷新摆动点/支撑压力/趋势线
        loadSwingAnalysis(valCurrentCode, valCurrentName, swingLevel);
        // 指数 Tab 激活时同步刷新
        var idxTab = document.getElementById('index-tab');
        if (idxTab && idxTab.classList.contains('active')) {
            loadIndexAnalysis(currentIndexCode, currentIndexName);
        }
    }, 5 * 60 * 1000);  // 每 5 分钟
}
function stopFibAutoRefresh() {
    if (fibAutoRefreshTimer) { clearInterval(fibAutoRefreshTimer); fibAutoRefreshTimer = null; }
}

// ========== 摆动点 · 支撑压力 · 趋势线 ==========
var swingLevel = 'day';

function loadSwingAnalysis(code, name, level) {
    swingLevel = level;
    var sumEl = document.getElementById('swing-summary');
    if (sumEl) sumEl.innerHTML = '<p class="loading-hint">加载中...</p>';
    fetch('/api/swing-analysis?code=' + code + '&name=' + name + '&level=' + level)
        .then(function(r) { return r.json(); })
        .then(function(res) {
            if (res.code !== 200) {
                if (sumEl) sumEl.innerHTML = '<p style="color:#ef5350;">' + res.msg + '</p>';
                return;
            }
            renderSwingChart(res.data);
        })
        .catch(function() {
            if (sumEl) sumEl.innerHTML = '<p style="color:#ef5350;">加载失败</p>';
        });
}

function switchSwingLevel(btn) {
    document.querySelectorAll('.swing-level-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    loadSwingAnalysis(valCurrentCode, valCurrentName, btn.getAttribute('data-level'));
}

function renderSwingChart(d) {
    // 统一走 app.js 的 getChart 管理实例
    var chart = getChart('chart-swing');
    if (!chart) return;
    var dates = d.dates;
    var kdata = d.klines.map(function(k) { return [k.open, k.close, k.low, k.high]; });

    // ---- K线 ----
    var series = [{
        name: 'K线', type: 'candlestick', data: kdata,
        itemStyle: { color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a' }
    }];

    // ---- 摆动结构折线（摆动点之间连线） ----
    var swingLine = new Array(dates.length).fill(null);
    d.swing_points.forEach(function(p) { swingLine[p.index] = p.price; });
    series.push({
        name: '摆动结构', type: 'line', data: swingLine,
        lineStyle: { color: '#ffb74d', width: 1.5, type: 'dotted' },
        symbol: 'none', connectNulls: false, z: 5
    });

    // ---- 摆动点散点（高点红三角 / 低点绿三角 / 未确认灰点） ----
    var hiPts = [], loPts = [], unconf = [];
    d.swing_points.forEach(function(p) {
        var pt = { value: [dates[p.index], p.price] };
        if (p.type === 'high') { if (p.unconfirmed) unconf.push(pt); else hiPts.push(pt); }
        else { if (p.unconfirmed) unconf.push(pt); else loPts.push(pt); }
    });
    if (hiPts.length) series.push({
        name: '摆动高点', type: 'scatter', data: hiPts,
        symbol: 'triangle', symbolSize: 10, itemStyle: { color: '#ef5350' }, z: 6
    });
    if (loPts.length) series.push({
        name: '摆动低点', type: 'scatter', data: loPts,
        symbol: 'triangle', symbolRotate: 180, symbolSize: 10, itemStyle: { color: '#26a69a' }, z: 6
    });
    if (unconf.length) series.push({
        name: '未确认极值', type: 'scatter', data: unconf,
        symbol: 'circle', symbolSize: 5, itemStyle: { color: '#8892b0', opacity: 0.6 }, z: 6
    });

    // ---- 支撑/压力水平线 + 趋势线（都挂在 K 线的 markLine 上） ----
    var markData = [];
    d.support_resistance.resistance.slice(0, 4).forEach(function(s) {
        markData.push({
            yAxis: s.price, lineStyle: { color: '#ef5350', width: 1, type: 'dashed', opacity: 0.7 },
            label: { formatter: '压力 ' + s.price + ' (x' + s.touches + ')', color: '#ef5350', fontSize: 10, position: 'insideEndTop' }
        });
    });
    d.support_resistance.support.slice(0, 4).forEach(function(s) {
        markData.push({
            yAxis: s.price, lineStyle: { color: '#26a69a', width: 1, type: 'dashed', opacity: 0.7 },
            label: { formatter: '支撑 ' + s.price + ' (x' + s.touches + ')', color: '#26a69a', fontSize: 10, position: 'insideEndBottom' }
        });
    });
    var tl = d.trendlines;
    if (tl && tl.up) {
        // 两点连线: data 项必须是 [起点, 终点] 数组（对象里不能写两个 coord，后者会覆盖前者）
        markData.push([
            { coord: [dates[tl.up.p1.x], tl.up.p1.y] },
            { coord: [dates[tl.up.p2.x], tl.up.p2.y],
              lineStyle: { color: '#ffb74d', width: 2, type: 'solid' },
              label: { formatter: '上升趋势线', color: '#ffb74d', fontSize: 10, position: 'end' } }
        ]);
    }
    if (tl && tl.down) {
        markData.push([
            { coord: [dates[tl.down.p1.x], tl.down.p1.y] },
            { coord: [dates[tl.down.p2.x], tl.down.p2.y],
              lineStyle: { color: '#ab47bc', width: 2, type: 'solid' },
              label: { formatter: '下降趋势线', color: '#ab47bc', fontSize: 10, position: 'end' } }
        ]);
    }
    series[0].markLine = { silent: true, symbol: ['none', 'none'], data: markData };

    chart.setOption({
        title: { text: d.name + ' ' + (d.level === 'week' ? '周线' : '日线') + ' 摆动结构', textStyle: { color: '#e0e0e0', fontSize: 14 } },
        tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
        legend: { data: ['K线', '摆动结构', '摆动高点', '摆动低点', '未确认极值'], textStyle: { color: '#8892b0' }, top: 30 },
        grid: { left: '3%', right: '4%', bottom: '12%', top: '20%', containLabel: true },
        xAxis: { type: 'category', data: dates, axisLabel: { color: '#8892b0', rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', scale: true, axisLabel: { color: '#8892b0' }, splitLine: { lineStyle: { color: '#233054' } } },
        dataZoom: [
            { type: 'inside', start: 0, end: 100 },
            { type: 'slider', start: 0, end: 100, height: 18, bottom: 2, textStyle: { color: '#8892b0' } }
        ],
        series: series
    });

    // ---- 文字总结 ----
    var cur = d.current_price;
    var lvlName = d.level === 'week' ? '周线' : '日线';
    var html = '<div class="row"><div class="col">' +
        '<p><b>当前价：</b>' + cur + '（' + lvlName + '，摆动阈值 ' + Math.round(d.threshold_pct * 100) + '%）</p>';
    html += '<p><b>压力位：</b></p>';
    (d.support_resistance.resistance.slice(0, 4) || []).forEach(function(s) {
        var near = Math.abs(cur - s.price) / s.price < 0.02;
        html += '<p style="margin-left:16px;color:#ef5350;' + (near ? 'font-weight:bold;' : '') + '">' + s.price +
                '（触及' + s.touches + '次）' + (near ? ' ← 当前附近' : '') + '</p>';
    });
    html += '<p><b>支撑位：</b></p>';
    (d.support_resistance.support.slice(0, 4) || []).forEach(function(s) {
        var near = Math.abs(cur - s.price) / s.price < 0.02;
        html += '<p style="margin-left:16px;color:#26a69a;' + (near ? 'font-weight:bold;' : '') + '">' + s.price +
                '（触及' + s.touches + '次）' + (near ? ' ← 当前附近' : '') + '</p>';
    });
    html += '</div><div class="col">';
    var trendDesc = '';
    if (tl && tl.up) trendDesc += '上升趋势线（' + tl.up.p1.y + ' → ' + tl.up.p2.y + '）';
    if (tl && tl.down) trendDesc += (trendDesc ? '；' : '') + '下降趋势线（' + tl.down.p1.y + ' → ' + tl.down.p2.y + '）';
    html += '<p><b>趋势：</b>' + (trendDesc || '暂无有效趋势线（摆动点不足3个）') + '</p>';
    html += '<p style="color:#8892b0;font-size:12px;">* 支撑/压力 = 摆动点价位聚类，触及次数越多越强；趋势线需至少3个连续抬升/降低的摆动点确认。切换周线看大级别结构。</p>';
    html += '<p style="color:#8892b0;font-size:12px;margin-top:4px;">数据截至 ' + (d.asof || '-') + '（来源：' + (d.source || '腾讯') + '）</p>';
    html += '</div></div>';
    document.getElementById('swing-summary').innerHTML = html;
}

// ========== 板块估值 ==========
function loadSectorValuation() {
    if (window._sectorLoaded) return;
    window._sectorLoaded = true;
    fetch('/api/sector-valuation')
        .then(function(r) { return r.json(); })
        .then(function(res) {
            if (res.code !== 200) return;
            renderSectorValuation('chart-sector-scatter', res.data);
            renderSectorPERank('chart-sector-pe-rank', res.data);
        });
}

// ========== 指数技术分析（支持切换） ==========
var currentIndexCode = '000688';
var currentIndexName = '科创50';

function loadIndexAnalysis(code, name) {
    code = code || currentIndexCode;
    name = name || currentIndexName;
    currentIndexCode = code;
    currentIndexName = name;

    // 更新标题
    var emaTitle = document.getElementById('index-ema-title');
    var fibTitle = document.getElementById('index-fib-title');
    if (emaTitle) emaTitle.textContent = name + ' EMA20 均线偏离度趋势';
    if (fibTitle) fibTitle.textContent = name + ' 斐波那契回调线';

    // 清除旧图表避免闪烁
    var emaChart = echarts.getInstanceByDom(document.getElementById('chart-ema-deviation'));
    if (emaChart) emaChart.clear();
    var fibChart = echarts.getInstanceByDom(document.getElementById('chart-fibonacci'));
    if (fibChart) fibChart.clear();

    document.getElementById('tech-analysis-summary').innerHTML = '<p class="loading-hint">加载中...</p>';

    fetch('/api/index-analysis?code=' + code + '&name=' + name)
        .then(function(r) { return r.json(); })
        .then(function(res) {
            if (res.code !== 200) {
                document.getElementById('tech-analysis-summary').innerHTML = '<p style="color:#ef5350;">数据获取失败</p>';
                return;
            }
            var d = res.data;
            renderEMADeviation('chart-ema-deviation', d);

            var klines = [];
            for (var i = 0; i < d.dates.length; i++) {
                klines.push({date: d.dates[i], close: d.closes[i]});
            }
            renderFibonacci('chart-fibonacci', d.fibonacci, klines);

            var fib = d.fibonacci;
            var lastClose = d.closes[d.closes.length - 1];
            var lastDev = d.deviation[d.deviation.length - 1];
            var nearestLevel = '';
            var nearestVal = 0;
            var levels = Object.keys(fib.levels).sort(function(a,b) { return parseFloat(b) - parseFloat(a); });
            for (var i = 0; i < levels.length; i++) {
                if (lastClose <= fib.high && lastClose >= fib.levels[levels[i]]) {
                    nearestLevel = levels[i];
                    nearestVal = fib.levels[levels[i]];
                    break;
                }
            }

            var devColor = lastDev > 2 ? '#ef5350' : (lastDev < -2 ? '#26a69a' : '#ffb74d');
            document.getElementById('tech-analysis-summary').innerHTML =
                '<div class="row"><div class="col"><p><b>EMA20偏离度：</b><span style="color:'+devColor+'">' + lastDev + '%</span> — ' +
                (lastDev > 3 ? '显著偏离，注意回调风险' : (lastDev > 1 ? '小幅偏离，趋势偏强' : (lastDev < -3 ? '显著偏离，存在反弹机会' : '在均线附近，趋势平稳'))) +
                '</p><p><b>斐波那契位置：</b>当前 ' + lastClose + '，处于 Fib ' + nearestLevel + ' (' + nearestVal + ') 附近</p></div>' +
                '<div class="col"><p><b>斐波那契关键位：</b></p>' +
                levels.map(function(k) {
                    var l = fib.levels[k];
                    var isNear = Math.abs(lastClose - l) / l < 0.02;
                    return '<p style="margin-left:16px;' + (isNear ? 'color:#ffb74d;font-weight:bold;' : '') + '">Fib ' + k + ': <b>' + l + '</b>' + (isNear ? ' ← 当前附近' : '') + '</p>';
                }).join('') +
                '</div></div>' +
                '<p style="color:#8892b0;font-size:12px;margin-top:8px;">* 数据截至 ' + (d.asof || '-') + '（来源：' + (d.source || '腾讯') + '），基于最近20个交易日，盘中预估仅供参考</p>';
        });
}

function switchIndex(btn) {
    // 更新按钮高亮
    document.querySelectorAll('.index-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    var code = btn.getAttribute('data-code');
    var name = btn.getAttribute('data-name');
    loadIndexAnalysis(code, name);
}

// 默认加载
document.addEventListener('DOMContentLoaded', function() {
    renderValHistory();
    renderValWatchlist();
    var w = getWatchlist();
    if (w.length) {
        loadStockVal(w[0].code, w[0].name);
    } else {
        loadStockVal('600519', '贵州茅台');
    }
    startFibAutoRefresh();
});