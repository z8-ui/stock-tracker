/* 板块热力图页脚本（S3-3C r3：从模板内联抽出，便于 Node 直接测试网络异常路径）
 *
 * 取数与错误处理统一走 window.sectorView.sectorLoader：
 *   - 首次失败：显示"请求失败，当前无可用数据"（不说旧缓存）
 *   - 已有数据时刷新失败：保留旧图，提示"本次刷新失败，当前保留此前页面数据"
 *   - 任何异常都不会变成未处理的 Promise rejection
 */
(function () {
    'use strict';

    var TOP_N = 20;
    var loader = null;

    function setRange(text, warn) {
        var el = document.getElementById('status-heatmap-range');
        if (!el) return;
        el.className = 'heatmap-meta' + (warn ? ' warn' : '');
        el.textContent = text;
    }

    function renderBody(res, meta) {
        var sv = window.sectorView;
        var data = (res && res.data) || [];

        renderTreemap('chart-heatmap', '行业板块热力图', data);

        var byChange = data.slice().sort(function (a, b) { return b.change - a.change; });
        var byFlow = data.slice().sort(function (a, b) { return b.flow - a.flow; });
        renderBarChart('chart-sector-rank', '涨跌幅 %', byChange.slice(0, TOP_N));
        renderBarChart('chart-sector-flow', '资金流入（亿）', byFlow.slice(0, TOP_N));

        var warn = meta.completeness !== 'complete' || meta.freshness !== 'fresh' ||
                   meta.fetch_status === 'failed';
        setRange(sv.rangeText(meta, data.length) +
                 ' 涨跌幅 / 资金流入排行各展示前 ' + TOP_N + ' 个。 ' +
                 sv.freshnessText(meta), warn);

        var st = document.getElementById('status-heatmap-page');
        if (!st) return;
        if (res.is_mock) {
            st.textContent = '⚠ 模拟数据（非真实资金流，仅供界面演示）';
            st.style.color = '#ef5350';
        } else if (res.is_stale) {
            st.textContent = '⚠ 缓存数据' + (res.asof ? '（' + res.asof + '）' : '（抓取时间未知）') + '，非实时';
            st.style.color = '#ffb74d';
        } else if (res._market_status) {
            st.textContent = res._market_status;
        }
    }

    function loadHeatmapData(force) {
        var sv = window.sectorView;
        if (!loader) {
            loader = sv.sectorLoader({
                url: '/api/heatmap',
                render: renderBody,
                fail: function (info, hadData, text) {
                    var detail = (info && info.body && info.body.msg) ? '（' + info.body.msg + '）' : '';
                    // 已有数据：保留旧图，只提示本次刷新失败；首次失败：明确无可用数据
                    setRange(text + detail + (hadData ? '' : ' 稍后会自动重试。'), true);
                }
            });
        }
        window._heatmapLoader = loader;
        // 定时刷新走 force，首次加载走普通模式（成功后不重复抓取）
        return loader.load(!!force);
    }

    window.loadHeatmapData = loadHeatmapData;

    // 供测试直接调用，不重复实现
    window.heatmapPage = { render: renderBody, setRange: setRange };
})();
