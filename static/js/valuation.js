// 估值偏离页 专属脚本（从 valuation.html 抽出，2026-08）
// 依赖: base.html 中的 echarts CDN + app.js（getChart 等共享函数）

// ========== XSS 防护 ==========
// 股票名、政策事件标题/摘要/来源都可能含 < > " '，直接拼 innerHTML 会被当成标签执行。
// 模板里也不再拼 onclick="fn('名字')"（名字里的引号能直接闭合字符串），改用 data-* + 事件委托。
function valEsc(v) {
    return String(v === null || v === undefined ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

document.addEventListener('click', function (ev) {
    var rm = ev.target.closest && ev.target.closest('[data-val-remove]');
    if (rm) {
        ev.stopPropagation();
        removeValWatchlist(rm.getAttribute('data-val-remove'));
        return;
    }
    var tg = ev.target.closest && ev.target.closest('[data-val-toggle]');
    if (tg) {
        toggleValWatchlist(tg.getAttribute('data-val-toggle'), tg.getAttribute('data-val-name') || '');
        return;
    }
    var el = ev.target.closest && ev.target.closest('[data-val-code]');
    if (el) {
        var c = el.getAttribute('data-val-code'), n = el.getAttribute('data-val-name') || '';
        if (el.hasAttribute('data-val-search')) loadStockValBySearch(c, n);
        else loadStockVal(c, n);
    }
});

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
        var code = valEsc(i.code), name = valEsc(i.name);
        return '<button class="chip" data-val-code="' + code + '" data-val-name="' + name + '">' + name +
               ' <span data-val-remove="' + code + '" style="margin-left:4px;cursor:pointer;">✕</span></button>';
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
        var code = valEsc(i.code), name = valEsc(i.name);
        return '<button class="chip" data-val-code="' + code + '" data-val-name="' + name + '">' + name + '</button>';
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
                var code = valEsc(s.code), name = valEsc(s.name);
                return '<button class="chip" data-val-code="' + code + '" data-val-name="' + name + '" ' +
                       'data-val-search="1">' + name + ' (' + code + ')</button>';
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
function finStatusToneColor(tone) {
    return ({ok: '#26a69a', info: '#42a5f5', warn: '#ffa726',
             error: '#ef5350', muted: '#8892b0'})[tone] || '#8892b0';
}

// 财报状态徽标 + 说明行（S3-4B）。status 来自 /api/valuation 的 financials_status。
function finStatusBlock(status, withTitle) {
    var view = (window.financialsStatus && window.financialsStatus.view)
        ? window.financialsStatus.view(status)
        : {kind: 'unknown', tone: 'muted', badge: '', message: '财报状态未知'};
    var metaLine = (window.financialsStatus && window.financialsStatus.meta)
        ? window.financialsStatus.meta(status) : '';
    var color = finStatusToneColor(view.tone);
    var badge = view.badge
        ? '<span style="display:inline-block;border:1px solid ' + color + ';color:' + color +
          ';font-size:10px;padding:0 5px;border-radius:3px;margin-left:6px;font-weight:normal;">' +
          valEsc(view.badge) + '</span>'
        : '';
    var html = '';
    if (withTitle) html += '<hr><p style="color:#e0e0e0;font-weight:bold;margin:6px 0;">📊 最新财报</p>';
    html += '<p style="color:' + color + ';font-size:12px;margin:2px 0;">' + valEsc(view.message) + badge + '</p>';
    if (metaLine) {
        html += '<p style="color:#8892b0;font-size:11px;margin:2px 0;">' + valEsc(metaLine) + '</p>';
    }
    if (status && status.reason && view.kind !== 'live' && view.kind !== 'cache') {
        html += '<p style="color:#5c6b8a;font-size:11px;margin:2px 0;">原因：' +
                valEsc(status.reason) + '</p>';
    }
    return html;
}

// ========== 财报快照 + 政策事件（2026-08 新增） ==========
// 财报数据来自 data_service.get_latest_financials（东财F10，动态拉取+6小时缓存）
// S3-4B：没有可用财报时不再返回空字符串 —— 必须明确写出"没有财报/获取失败/缓存无效/旧数据"。
// 状态对象由后端随同一次请求返回（financials_status），不读全局最新状态。
function buildFinancialsHtml(fin, status) {
    if (!fin || !fin.report_date) {
        return finStatusBlock(status, true);
    }
    function fmtYi(v) { return (v === null || v === undefined) ? '-' : (v / 1e8).toFixed(2) + '亿'; }
    function fmtPct(v) { return (v === null || v === undefined) ? '-' : ((v > 0 ? '+' : '') + Number(v).toFixed(1) + '%'); }
    function pctColor(v) { return (v || 0) > 0 ? '#ef5350' : ((v || 0) < 0 ? '#26a69a' : '#8892b0'); }
    var eps = (fin.eps === null || fin.eps === undefined) ? '-' : fin.eps;
    var roe = (fin.roe === null || fin.roe === undefined) ? '-' : Number(fin.roe).toFixed(2) + '%';
    return '<hr>' +
        '<p style="color:#e0e0e0;font-weight:bold;margin:6px 0;">📊 最新财报 ' +
        '<span style="color:#8892b0;font-size:11px;font-weight:normal;">' + valEsc(fin.report_name || '') +
        ' · 披露 ' + valEsc(fin.notice_date || '-') + '</span></p>' +
        finStatusBlock(status, false) +
        '<div class="summary-row"><span class="label">EPS（基本）</span><span class="value">' + eps + ' 元</span></div>' +
        '<div class="summary-row"><span class="label">净利润同比</span><span class="value" style="color:' + pctColor(fin.profit_yoy) + ';">' + fmtPct(fin.profit_yoy) + '</span></div>' +
        '<div class="summary-row"><span class="label">营收同比</span><span class="value" style="color:' + pctColor(fin.revenue_yoy) + ';">' + fmtPct(fin.revenue_yoy) + '</span></div>' +
        '<div class="summary-row"><span class="label">ROE</span><span class="value">' + roe + '</span></div>' +
        '<p style="color:#8892b0;font-size:11px;margin-top:4px;">净利润 ' + fmtYi(fin.net_profit) +
        ' / 营收 ' + fmtYi(fin.revenue) + '（东财F10，缓存6小时）</p>';
}

// 只放行 http/https：source_url 来自外部新闻，javascript:/data: 能直接执行
function policyUrl(u) {
    if (!u) return '';
    try {
        var x = new URL(String(u), window.location.origin);
        return (x.protocol === 'http:' || x.protocol === 'https:') ? x.href : '';
    } catch (e) { return ''; }
}

function policyBadge(text, color) {
    if (!text) return '';
    return '<span style="display:inline-block;border:1px solid ' + color + ';color:' + color +
        ';border-radius:4px;padding:0 5px;font-size:11px;margin:0 4px 2px 0;">' + valEsc(text) + '</span>';
}

// 关联类型 → 颜色 / 文案（与后端 policy_live.RELATION_LABEL 保持一致）
var POLICY_REL_STYLE = {
    'direct': { color: '#ef5350', label: '直接影响（公司公告）' },
    'business_related': { color: '#ffb74d', label: '主营业务相关' },
    'industry_indirect': { color: '#42a5f5', label: '产业链/行业间接' },
    'macro_related': { color: '#8892b0', label: '宏观政策影响' },
    'unconfirmed': { color: '#5c6b8a', label: '关联待确认' }
};

// 一条政策事件：关系类型 / 方向 / 影响程度 / 审核状态 / 三个时间 / 机制 / 证据 / 原文链接
function policyItemHtml(n) {
    var rel = POLICY_REL_STYLE[n.relation_type] || POLICY_REL_STYLE['unconfirmed'];
    var relColor = rel.color;
    var dirColor = { '利好': '#ef5350', '利空': '#26a69a', '中性': '#ffb74d', '待确认': '#8892b0' }[n.direction] || '#8892b0';

    var when = n.occurred_at
        ? valEsc(String(n.occurred_at).slice(0, 10))
        : (n.published_at
            ? '发布 ' + valEsc(String(n.published_at).slice(0, 10)) + '（发生日期未知）'
            : '发生日期未知');
    if (n.collected_at) when += ' · 采集 ' + valEsc(String(n.collected_at).slice(0, 10));

    var url = policyUrl(n.source_url);
    var link = url
        ? '<a href="' + valEsc(url) + '" target="_blank" rel="noopener noreferrer" style="color:#42a5f5;text-decoration:none;">原文 ↗</a>'
        : '<span style="color:#5c6b8a;">无原文链接</span>';

    return '<div style="border-left:3px solid ' + relColor + ';padding:6px 9px;margin:8px 0;background:rgba(255,255,255,0.03);">' +
        '<div style="margin-bottom:3px;">' +
            policyBadge(n.relation_label || rel.label, relColor) +
            policyBadge(n.direction, dirColor) +
            policyBadge('影响程度：' + (n.impact_level || '待确认'), '#8892b0') +
            (n.review_status === 'approved' ? policyBadge('已审核', '#26a69a') : policyBadge('未审核', '#ffb74d')) +
        '</div>' +
        '<div style="font-size:13px;color:#e6f1ff;">' + valEsc(n.title) + '</div>' +
        '<div style="color:#8892b0;font-size:11px;margin-top:3px;">' + when + ' · ' + link + '</div>' +
        (n.mechanism ? '<div style="color:#a8b8d8;font-size:12px;margin-top:4px;line-height:1.6;">影响机制：' + valEsc(n.mechanism) + '</div>' : '') +
        (n.relation_basis ? '<div style="color:#5a6a8a;font-size:11px;margin-top:2px;">关联依据：' + valEsc(n.relation_basis) + '</div>' : '') +
        (n.evidence ? '<div style="color:#5a6a8a;font-size:11px;margin-top:2px;">' + valEsc(n.evidence) + '</div>' : '') +
        '</div>';
}

// 个股画像抬头：行业 / 产业链 / 主营业务 / 关键词。资料缺失时明说"缺失"，不补内容。
function policyProfileHtml(profile, industry) {
    if (!profile || (!profile.name && !profile.industry && !profile.business && !industry)) return '';
    var bits = [];
    var ind = profile.industry || industry || '';
    if (ind) bits.push('行业：' + valEsc(ind));
    if (profile.industry_chain && profile.industry_chain.length) {
        bits.push('产业链：' + valEsc(profile.industry_chain.join(' > ')));
    }
    var role = '行业：' + valEsc(ind || '未知');
    var biz = profile.business_short
        ? '主营业务：' + valEsc(profile.business_short) +
          '<span style="color:#5c6b8a;font-size:10px;">（' + valEsc(profile.business_source || '来源未标注') + '）</span>'
        : '<span style="color:#ffb74d;">主营业务资料缺失（未获取到公司概况，不使用推测内容）</span>';
    var kw = (profile.keywords && profile.keywords.length)
        ? '检索关键词：' + valEsc(profile.keywords.slice(0, 8).join('、'))
        : '';
    var src = profile.source_url
        ? '<a href="' + valEsc(profile.source_url) + '" target="_blank" rel="noopener noreferrer" style="color:#42a5f5;text-decoration:none;">' + valEsc(profile.source || '来源') + ' ↗</a>'
        : '';
    return '<div style="border-left:3px solid #42a5f5;padding:5px 9px;margin-bottom:6px;background:rgba(66,165,245,0.06);font-size:12px;color:#a8b8d8;">' +
        '<div>' + (bits.length ? bits.join(' · ') : role) + '</div>' +
        '<div style="margin-top:2px;">' + biz + '</div>' +
        (kw ? '<div style="margin-top:2px;color:#5c6b8a;font-size:11px;">' + kw + '</div>' : '') +
        (src ? '<div style="margin-top:2px;font-size:10px;">' + src + '</div>' : '') +
        '</div>';
}

function renderPolicy(events, meta, industry, profile) {
    var box = document.getElementById('valuation-policy');
    if (!box) return;
    events = events || [];
    var head = document.getElementById('val-policy-meta');
    if (head) head.textContent = events.length ? ('共 ' + events.length + ' 条' + (industry ? ' · ' + industry : '')) : (industry || '');
    var profileHtml = policyProfileHtml(profile, industry);

    if (!events.length) {
        // 2.4：success / empty / partial / failed 必须分开说，不能把"取不到"说成"没有"
        var ls = meta && meta.live_status ? String(meta.live_status) : '';
        var msg = '库里暂无已确认的相关政策与事件';
        var reason = '';
        if (meta && meta.data_status === 'unavailable') reason = '事件库不可用（不等于"没有政策"）';
        else if (meta && meta.status === 'error') reason = '请求失败';
        if (ls === 'failed') {
            msg = '实时检索失败，所有数据源都没返回结果';
            reason = '这是"没取到"，不等于"没有相关政策"';
        } else if (ls === 'partial') {
            msg = '部分数据源检索失败，结果可能不完整';
            var det = meta.live_status_detail || {};
            reason = Object.keys(det).map(function (k) { return k + '：' + det[k]; }).join('；');
        } else if (ls === 'empty') {
            msg = '检索已完成，公告与快讯中确无相关条目';
        } else if (ls === 'success') {
            msg = '检索成功，但未命中与该公司业务相关的条目';
        }
        box.innerHTML = profileHtml + '<p style="color:#8892b0;font-size:12px;">' + valEsc(msg) +
            (reason ? '（' + valEsc(reason) + '）' : '') + '。</p>' +
            '<p style="color:#5c6b8a;font-size:11px;">可点右上角「🔍 实时检索公告」强制重新拉取（结果未审核，只作线索）。</p>';
        return;
    }
    var counts = {};
    events.forEach(function (e) {
        var k = e.relation_type || 'unconfirmed';
        counts[k] = (counts[k] || 0) + 1;
    });
    var summary = Object.keys(counts).map(function (k) {
        var st = POLICY_REL_STYLE[k] || POLICY_REL_STYLE['unconfirmed'];
        return '<span style="color:' + st.color + ';">' + valEsc(st.label) + ' ' + counts[k] + '</span>';
    }).join(' · ');
    box.innerHTML = profileHtml +
        (summary ? '<div style="font-size:11px;color:#5c6b8a;margin:2px 0 4px;">' + summary + '</div>' : '') +
        events.map(policyItemHtml).join('');
}

// ========== 个股政策与事件：自动加载 + 竞态防护 ==========
//
// 2026-11 修复（P1）：
//   1. 切股票必须让在飞的政策请求立刻失效 —— 旧实现只在手动检索里 ++policySeq，
//      loadStockVal 不同步，于是"贵州茅台的慢响应"能落到紫金矿业页面上。
//   2. 响应回来要同时校验三件事：序号仍有效 + 页面当前股票没变 + 响应里的股票码没变。
//   3. 加载 / 空 / 失败 三种状态分开显示，失败不许伪装成"没有政策"。
//   4. 切股票后自动拉取，不再要求用户点按钮（按钮保留，用于强制刷新）。

var policySeq = 0;
var policyAbort = null;

function invalidatePolicyRequest() {
    policySeq += 1;
    if (policyAbort) {
        try { policyAbort.abort(); } catch (e) { /* 已结束的请求 abort 会抛，忽略 */ }
        policyAbort = null;
    }
    var btn = document.getElementById('val-live-btn');
    if (btn) { btn.disabled = false; btn.textContent = '🔍 实时检索公告'; }
}

function policyStatusHtml(kind, text) {
    var color = kind === 'error' ? '#ef5350' : '#8892b0';
    return '<p class="loading-hint" style="color:' + color + ';font-size:12px;">' +
        valEsc(text) + '</p>';
}

function loadPolicy(code, name, opts) {
    opts = opts || {};
    invalidatePolicyRequest();          // 同一股票连续刷新也不许旧结果覆盖新结果
    var seq = policySeq;
    var signal;
    if (typeof AbortController !== 'undefined') {
        policyAbort = new AbortController();
        signal = policyAbort.signal;
    }
    var btn = document.getElementById('val-live-btn');
    if (btn) { btn.disabled = true; btn.textContent = '检索中...'; }

    var box = document.getElementById('valuation-policy');
    if (box) {
        box.innerHTML = policyStatusHtml('loading',
            '正在加载 ' + (name || code) + ' 的政策与事件...');
    }

    var url = '/api/policy-events?stock_code=' + encodeURIComponent(code) +
              '&stock_name=' + encodeURIComponent(name || '') + '&live=1' +
              (opts.force ? '&refresh=1' : '');
    fetch(url, signal ? { signal: signal } : {})
        .then(function (r) { return r.json(); })
        .then(function (res) {
            if (seq !== policySeq) return;                        // 已有更新的请求
            if (String(valCurrentCode) !== String(code)) return;   // 页面已经切走
            if (btn) { btn.disabled = false; btn.textContent = '🔍 实时检索公告'; }
            if (!res || res.code !== 200) {
                if (box) box.innerHTML = policyStatusHtml('error',
                    '政策事件请求失败，请稍后重试（这不等于"没有相关政策"）。');
                return;
            }
            var d = res.data || {};
            if (String(d.stock_code) !== String(code)) return;      // 响应自带的股票码也不对
            renderPolicy(d.events || [], {
                status: d.meta && d.meta.data_status,
                live_status: d.meta && d.meta.live_status
            }, d.industry || '', d.profile || {});
        })
        .catch(function (err) {
            if (seq !== policySeq) return;                         // 被新请求取代/取消，静默
            if (err && err.name === 'AbortError') return;
            if (btn) { btn.disabled = false; btn.textContent = '🔍 实时检索公告'; }
            if (box) box.innerHTML = policyStatusHtml('error',
                '政策事件请求失败（网络或服务异常），这不等于"没有相关政策"。');
        });
}

// 手动强制刷新：绕过普通缓存重新检索
function refreshPolicyLive() {
    loadPolicy(valCurrentCode, valCurrentName, { force: true });
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
var valSeq = 0;             // 请求序号，防止旧请求覆盖新股票

function loadStockVal(code, name, refresh) {
    valCurrentCode = code;
    valCurrentName = name;
    addValHistory(code, name);
    renderValWatchlist();

    // 快速连点不同股票时，慢响应可能后到并盖掉新股票的内容 → 用序号丢弃过期响应
    var seq = ++valSeq;

    // 在估值详情卡片加"加入自选"按钮
    var inWl = isInWatchlist(code);
    var wlBtn = inWl ? '<span style="color:#ef5350;font-weight:bold;">★ 已自选</span>' :
        '<button data-val-toggle="' + valEsc(code) + '" data-val-name="' + valEsc(name) + '" style="background:#ffb74d;border:none;border-radius:4px;padding:4px 10px;color:#fff;cursor:pointer;font-size:12px;">☆ 加入自选</button>';
    // 手动更新按钮：强制穿透行业PE/财报缓存重新拉取
    var refreshBtn = '<button id="val-refresh-btn" onclick="refreshStockVal()" style="background:#42a5f5;border:none;border-radius:4px;padding:4px 10px;color:#fff;cursor:pointer;font-size:12px;margin-left:6px;" title="强制重新拉取行情/行业PE/财报并更新">🔄 更新数据</button>';

    // 切股票：先让在飞的政策请求失效（序号+取消），再自动加载当前股票的政策与事件
    invalidatePolicyRequest();
    loadPolicy(code, name, {});

    Promise.all([
        fetch('/api/valuation?code=' + code + '&name=' + name + (refresh ? '&refresh=1' : '')).then(function(r) { return r.json(); }),
        fetch('/api/intrinsic-value?code=' + code).then(function(r) { return r.json(); }),
        fetch('/api/fibonacci?code=' + code + '&name=' + name).then(function(r) { return r.json(); })
    ]).then(function(results) {
        var valRes = results[0];
        var ivRes = results[1];
        var fibRes = results[2];
        if (seq !== valSeq) return;      // 已经切到别的股票，丢弃这次结果

        // 摆动点/支撑压力/趋势线（独立请求，失败不影响上面三个）
        loadSwingAnalysis(code, name, swingLevel);

        if (valRes.code === 200) {
            var d = valRes.data;
            renderValuationGauge('chart-valuation', d);
            // 2026-11: PE 缺失（亏损/停牌）时后端给 null，这里必须显示"不可用"而不是 null/0
            var NA = '<span style="color:#8892b0;">不可用</span>';
            var isNum = function(v) { return v !== null && v !== undefined; };
            var levelColor = d.level === '偏高' ? '#ef5350' : (d.level === '偏低' ? '#26a69a' : (d.level === '数据不可用' ? '#8892b0' : '#ffb74d'));
            var devTxt = isNum(d.deviation_pe)
                ? '<span style="color:' + levelColor + ';font-weight:bold;">' + (d.deviation_pe > 0 ? '+' : '') + d.deviation_pe + '%</span>'
                : NA;
            var peSrcTxt = d.industry_pe_is_static
                ? '⚠ ' + valEsc(d.industry_pe_source_label || '静态行业PE（人工估算，非实时）')
                : (d.industry_pe_source === 'eastmoney' ? '东财实时板块' : '不可用（接口未返回且不在静态表内）');
            var actBox = document.getElementById('val-main-actions');
            if (actBox) actBox.innerHTML = wlBtn + refreshBtn;
            var titleBox = document.getElementById('val-main-title');
            if (titleBox) titleBox.textContent = name + ' (' + code + ')';
            document.getElementById('valuation-detail').innerHTML =
                '<div class="summary-row"><span class="label">PE (市盈率)</span><span class="value">' + (isNum(d.pe) ? d.pe : NA) + '</span></div>' +
                '<div class="summary-row"><span class="label">PB (市净率)</span><span class="value">' + (isNum(d.pb) ? d.pb : NA) + '</span></div>' +
                '<div class="summary-row"><span class="label">PE-TTM</span><span class="value">' + (isNum(d.pe_ttm) ? d.pe_ttm : NA) + '</span></div>' +
                '<div class="summary-row"><span class="label">行业平均 PE（' + valEsc(d.industry_name || '未知') + '）</span><span class="value">' + (isNum(d.industry_pe) ? d.industry_pe : NA) + '</span></div>' +
                '<hr>' +
                '<div class="summary-row"><span class="label">偏离度</span><span class="value">' + devTxt + '</span></div>' +
                '<div class="summary-row"><span class="label">结论</span><span class="value" style="color:' + levelColor + ';font-weight:bold;">' + valEsc(d.level) + '</span></div>' +
                (d.level_reason ? '<p style="color:#ffb74d;font-size:12px;margin-top:6px;">' + valEsc(d.level_reason) + '</p>' : '') +
                '<p style="color:#8892b0;font-size:12px;margin-top:6px;">行业PE来源：' + peSrcTxt + '</p>' +
                '<p style="color:#8892b0;font-size:11px;margin-top:2px;">' +
                    '行情时间：' + (d.quote_asof ? new Date(d.quote_asof * 1000).toLocaleString() : '未知') +
                    (d.quote_source ? ' · 来源 ' + valEsc(d.quote_source) : '') +
                    (d.quote_stale ? ' · <span style="color:#ffb74d;">' + valEsc(d.quote_stale_reason || '数据可能过期') + '</span>' : '') +
                '</p>' +
                '<p style="color:#5c6b8a;font-size:11px;margin-top:2px;">' + valEsc(d.deviation_basis || '偏离度仅作横向参考') +
                    '。历史估值分位：未接入真实历史 PE 序列，本页不显示该指标。</p>';

            // 财报快照留在"一、基础估值"；政策与事件已拆到"四、政策与事件"
            document.getElementById('valuation-detail').innerHTML += buildFinancialsHtml(d.financials, d.financials_status);
            renderPolicy(d.policy_notes || [],
                         { status: (d.policy_notes && d.policy_notes.length) ? 'real' : 'empty' },
                         d.industry_name || '');
        } else {
            // 估值接口失败：政策区不能一直停在"加载中"，那是伪装成"还在查"
            renderPolicy([], { status: 'error' });
        }

        if (ivRes.code === 200) {
            var d = ivRes.data;
            renderIntrinsicValue('chart-intrinsic', d);
            var priceTxt = (d.price || 0).toFixed(2);
            var mp = d.model_params || {};
            var basisHtml = mp.basis
                ? '<p style="color:#8892b0;font-size:11px;margin-top:2px;">估值依据：' + valEsc(mp.basis) +
                  '；安全边际 ' + (mp.safety_margin != null ? mp.safety_margin : '--') + '</p>'
                : '';
            var refHtml = basisHtml;
            if (d.pe_reference && d.pe_reference.available) {
                refHtml = basisHtml + '<hr>' +
                    '<div class="summary-row"><span class="label">相对PE参考估值</span><span class="value">' +
                    Number(d.pe_reference.value).toFixed(2) + '</span></div>' +
                    '<p style="color:#8892b0;font-size:11px;margin-top:2px;">' +
                    valEsc(d.pe_reference.label) + '；反推 EPS=' + d.pe_reference.eps_derived +
                    '（由股价/PE 得到，不是财报值，仅供参考）</p>';
            }
            if (d.available === false || d.intrinsic_value === null || d.intrinsic_value === undefined) {
                // 不再用股价顶替内在价值（否则偏离恒为 0，等于自证"合理"）
                document.getElementById('intrinsic-detail').innerHTML =
                    '<div class="summary-row"><span class="label">当前股价</span><span class="value">' + priceTxt + '</span></div>' +
                    '<p style="color:#ffb74d;font-size:12px;margin-top:6px;">' + valEsc(d.reason || '缺少可用的 EPS / 行业 PE，无法估算内在价值') + '</p>' +
                    refHtml;
            } else {
                var gapColor = d.gap > 0 ? '#ef5350' : '#26a69a';
                document.getElementById('intrinsic-detail').innerHTML =
                    '<div class="summary-row"><span class="label">当前股价</span><span class="value">' + priceTxt + '</span></div>' +
                    '<div class="summary-row"><span class="label">内在价值（估算）</span><span class="value">' + d.intrinsic_value.toFixed(2) + '</span></div>' +
                    '<div class="summary-row"><span class="label">每股收益 EPS</span><span class="value">' + d.eps + '</span></div>' +
                    '<hr>' +
                    '<div class="summary-row"><span class="label">偏离</span><span class="value" style="color:'+gapColor+';font-weight:bold;">' + (d.gap > 0 ? '+' : '') + d.gap + '%</span></div>' +
                    '<div class="summary-row"><span class="label">判断</span><span class="value" style="color:'+gapColor+';font-weight:bold;">' + (d.gap < -10 ? '低估' : (d.gap > 10 ? '高估' : '合理')) + '</span></div>' +
                    '<p style="color:#8892b0;font-size:11px;margin-top:6px;">模型：年化EPS × 行业PE(×' +
                    valEsc(d.industry_pe) + ') × 安全边际 ' + valEsc(mp.safety_margin) +
                    '；EPS 来源：' + valEsc(d.eps_source || '-') + '</p>' +
                    refHtml;
            }
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
var _sectorLoader = null;

function _sectorNotes(msg) {
    ['sector-scatter-note', 'sector-rank-note'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.textContent = msg;
    });
}

function loadSectorValuation() {
    var sv = window.sectorView;
    if (!_sectorLoader) {
        _sectorLoader = sv.sectorLoader({
            url: '/api/sector-valuation',
            render: function (res, meta) {
                var st = document.getElementById('sector-status');
                if (st) st.textContent = sv.freshnessText(meta);
                renderSectorValuation('chart-sector-scatter', res.data, meta);
                renderSectorPERank('chart-sector-pe-rank', res.data, meta);
            },
            fail: function (info, hadData, text) {
                var st = document.getElementById('sector-status');
                if (st) st.textContent = text;
                var detail = (info && info.body && info.body.msg) ? '（' + info.body.msg + '）' : '';
                _sectorNotes(text + detail + (hadData ? '' : ' 可在切换 Tab 后重试。'));
            }
        });
    }
    // 成功后 loader 内部 loaded=true，重复打开 Tab 不会重复抓取；失败会自动解除，允许重试
    window._sectorLoaded = _sectorLoader.state.loaded;
    return _sectorLoader.load().then(function (r) {
        window._sectorLoaded = _sectorLoader.state.loaded;
        return r;
    });
}
window.loadSectorValuation = loadSectorValuation;

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
            var mockBadge = d.is_mock
                ? '<p style="color:#ef5350;font-weight:bold;font-size:12px;">⚠ 模拟数据：历史K线接口未返回，以下指标由随机数据生成，不可用于决策</p>'
                : '';
            document.getElementById('tech-analysis-summary').innerHTML = mockBadge +
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