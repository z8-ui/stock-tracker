/* 政策事件中心 - 时间线页面
 * 数据来自 /api/event-timeline，口径见 events_store.timeline：
 * 行业维度 COUNT(DISTINCT event_id)，一条事件挂多只股票不会重复计数。 */

const EVT_COLORS = ['#ef5350', '#42a5f5', '#ffb74d', '#26a69a', '#ab47bc',
                    '#66bb6a', '#ff7043', '#5c6bc0', '#26c6da', '#d4e157'];
let evtTimelineChart = null;
let evtIndustryChart = null;
let evtMonth = '';          // 点柱子选中的月份（YYYY-MM），空串=不筛
let evtMonths = [];         // 当前图表的月份轴，供点击回查

function evtRenderChip() {
    const chip = document.getElementById('events-month-chip');
    const text = document.getElementById('events-month-text');
    if (!chip) return;
    if (evtMonth) {
        text.textContent = `已筛 ${evtMonth}`;
        chip.style.visibility = 'visible';
    } else {
        chip.style.visibility = 'hidden';   // 用 visibility 占位，避免筛选时工具栏变高把图表顶走
    }
}

function evtSetStatus(text, color) {
    const el = document.getElementById('status-events');
    if (el) { el.textContent = text || ''; el.style.color = color || '#ffb74d'; }
}

function evtStatCard(label, value, color) {
    return `<div style="flex:1;min-width:130px;background:#0f1729;border:1px solid #1b2740;
                border-radius:8px;padding:10px 12px;">
                <div style="color:#8892b0;font-size:11px;">${label}</div>
                <div style="color:${color || '#e6f1ff'};font-size:22px;font-weight:600;line-height:1.3;">${value}</div>
            </div>`;
}

function evtDirectionBadge(d) {
    const map = { '利好': '#ef5350', '利空': '#26a69a', '中性': '#8892b0' };
    const c = map[d] || '#8892b0';
    return `<span style="color:${c};border:1px solid ${c};border-radius:4px;padding:0 5px;font-size:11px;">${d}</span>`;
}

function evtTargets(targets) {
    if (!targets) return '<span style="color:#5c6b8a;">-</span>';
    return targets.split(',').map(t => {
        const [type, key] = t.split(':');
        const label = type === 'stock' ? key : key + '(行业)';
        return `<span style="background:#16233d;border:1px solid #253656;border-radius:4px;
                      padding:0 5px;margin-right:4px;font-size:11px;color:#a8b8d8;">${label}</span>`;
    }).join('');
}

function evtRenderStats(data) {
    const box = document.getElementById('events-stats');
    if (!box) return;
    box.innerHTML = [
        evtStatCard('事件总数', data.total),
        evtStatCard('利好', data.directions['利好'] || 0, '#ef5350'),
        evtStatCard('利空', data.directions['利空'] || 0, '#26a69a'),
        evtStatCard('中性', data.directions['中性'] || 0, '#8892b0'),
        evtStatCard('覆盖行业', data.industries.filter(i => i.name !== '未分类').length),
    ].join('');
}

function evtRenderTimeline(data) {
    const dom = document.getElementById('chart-events-timeline');
    if (!dom) return;
    evtTimelineChart = evtTimelineChart || echarts.init(dom);
    evtMonths = data.months || [];
    const series = data.series.map((s, i) => ({
        name: s.name,
        type: 'bar',
        stack: 'total',
        barMaxWidth: 26,
        emphasis: { focus: 'series' },
        itemStyle: { color: EVT_COLORS[i % EVT_COLORS.length] },
        // 选中某个月时，其它月份调暗，避免"筛了但看不出来"
        data: evtMonth
            ? s.data.map((v, j) => ({
                value: v,
                itemStyle: { opacity: evtMonths[j] === evtMonth ? 1 : 0.3 },
            }))
            : s.data,
    }));
    evtTimelineChart.setOption({
        backgroundColor: 'transparent',
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        legend: { textStyle: { color: '#8892b0' }, top: 0, type: 'scroll' },
        grid: { left: 40, right: 16, top: 34, bottom: 30 },
        xAxis: {
            type: 'category',
            data: data.months,
            axisLabel: { color: '#8892b0', rotate: data.months.length > 14 ? 45 : 0 },
            axisLine: { lineStyle: { color: '#1b2740' } },
        },
        yAxis: {
            type: 'value',
            minInterval: 1,
            axisLabel: { color: '#8892b0' },
            splitLine: { lineStyle: { color: '#16233d' } },
        },
        series: series.length ? series : [{ type: 'bar', data: [] }],
    }, true);

    // 点柱子 → 筛该月事件；再点同一个月取消
    if (!evtTimelineChart.__evtClickBound) {
        evtTimelineChart.on('click', function (p) {
            const m = evtMonths[p.dataIndex];
            if (!m) return;
            evtMonth = evtMonth === m ? '' : m;
            evtLoad();
        });
        evtTimelineChart.__evtClickBound = true;
    }
}

function evtRenderIndustry(data) {
    const dom = document.getElementById('chart-events-industry');
    if (!dom) return;
    evtIndustryChart = evtIndustryChart || echarts.init(dom);
    const rows = data.industries.slice(0, 10).map((x, i) => ({
        name: x.name,
        value: x.total,
        itemStyle: { color: EVT_COLORS[i % EVT_COLORS.length] },
    }));
    evtIndustryChart.setOption({
        backgroundColor: 'transparent',
        tooltip: { trigger: 'item', formatter: '{b}: {c} 条 ({d}%)' },
        series: [{
            type: 'pie',
            radius: ['42%', '68%'],
            center: ['50%', '52%'],
            label: { color: '#a8b8d8', formatter: '{b}\n{c}' },
            labelLine: { lineStyle: { color: '#253656' } },
            data: rows.length ? rows : [{ name: '暂无数据', value: 1, itemStyle: { color: '#1b2740' } }],
        }],
    }, true);
}

function evtRenderTable(data) {
    const box = document.getElementById('events-table');
    const countEl = document.getElementById('events-count');
    if (countEl) {
        countEl.textContent = data.month
            ? `${data.month} 共 ${data.events.length} 条（点柱子切换月份，或点 × 取消筛选）`
            : `共 ${data.events.length} 条（最近在前 · 点柱子可只看某月）`;
    }
    if (!box) return;
    if (!data.events.length) {
        box.innerHTML = '<div style="color:#8892b0;padding:14px;font-size:13px;">' +
            (data.month ? `${data.month} 没有事件（再点一次柱子可取消筛选）。`
                        : '该范围内没有事件。先用 events_cli.py 录入或 feed 抓取。') + '</div>';
        return;
    }
    const rows = data.events.map(e => `
        <tr style="border-bottom:1px solid #16233d;">
            <td style="padding:8px 6px;color:#a8b8d8;white-space:nowrap;">${e.event_date}</td>
            <td style="padding:8px 6px;white-space:nowrap;">${evtDirectionBadge(e.direction)}</td>
            <td style="padding:8px 6px;color:#e6f1ff;">${e.title}</td>
            <td style="padding:8px 6px;color:#8892b0;font-size:12px;max-width:420px;">${e.summary || ''}</td>
            <td style="padding:8px 6px;white-space:nowrap;">${evtTargets(e.targets)}</td>
            <td style="padding:8px 6px;color:#6f7f9e;font-size:11px;">${(e.source || '').slice(0, 24)}</td>
        </tr>`).join('');
    box.innerHTML = `
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
                <tr style="color:#8892b0;text-align:left;border-bottom:1px solid #1b2740;">
                    <th style="padding:6px;">日期</th><th style="padding:6px;">方向</th>
                    <th style="padding:6px;">标题</th><th style="padding:6px;">影响</th>
                    <th style="padding:6px;">标的</th><th style="padding:6px;">来源</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>`;
}

function evtRenderIndustryOptions(data, keep) {
    const sel = document.getElementById('events-industry');
    if (!sel) return;
    const names = data.industries.map(i => i.name).filter(n => n !== '未分类');
    const current = keep !== undefined ? keep : sel.value;
    sel.innerHTML = '<option value="">全部</option>' +
        names.map(n => `<option value="${n}">${n}</option>`).join('');
    if (names.includes(current)) sel.value = current;
}

function evtLoad() {
    const days = document.getElementById('events-days').value;
    const industry = document.getElementById('events-industry').value;
    evtSetStatus('加载中…');
    const url = `/api/event-timeline?days=${encodeURIComponent(days)}` +
                (industry ? `&industry=${encodeURIComponent(industry)}` : '') +
                (evtMonth ? `&month=${encodeURIComponent(evtMonth)}` : '');
    fetch(url)
        .then(r => r.json())
        .then(res => {
            if (res.code !== 200) throw new Error(res.msg || '接口错误');
            const data = res.data;
            evtRenderStats(data);
            // 图表库没加载也要保证表格能看：分开 try，互不拖累
            try {
                evtRenderTimeline(data);
                evtRenderIndustry(data);
            } catch (e) {
                console.warn('图表渲染失败:', e);
                evtSetStatus('图表库未加载（离线/被墙），表格数据仍可用', '#ffb74d');
            }
            evtRenderTable(data);
            if (!industry) evtRenderIndustryOptions(data);
            evtRenderChip();
            document.getElementById('events-asof').textContent =
                `统计区间 ${data.since} 起 · ${new Date().toLocaleTimeString('zh-CN')}`;
            if (typeof echarts === 'undefined') {
                evtSetStatus('ECharts 未加载：只剩表格（检查 /static/vendor/echarts.min.js）', '#ffb74d');
            } else {
                evtSetStatus('');
            }
        })
        .catch(err => evtSetStatus('加载失败：' + err.message, '#ef5350'));
}

document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('events-refresh').addEventListener('click', evtLoad);
    document.getElementById('events-days').addEventListener('change', evtLoad);
    document.getElementById('events-industry').addEventListener('change', evtLoad);
    document.getElementById('events-month-clear').addEventListener('click', function (ev) {
        ev.preventDefault();
        evtMonth = '';
        evtLoad();
    });
    evtLoad();
    window.addEventListener('resize', function () {
        if (evtTimelineChart) evtTimelineChart.resize();
        if (evtIndustryChart) evtIndustryChart.resize();
    });
});
