/* 统一数字格式化（2026-11）
 *
 * 起因：资金流向页各处的精度不一致 —— 有的 toFixed(2)，有的直接拼原值，
 * 更有 f.v 为 null 时调 .toFixed 抛异常、整个 forEach 中断，卡片永远停在 "--"。
 *
 * 规则：
 *   1. 所有金额保留两位小数（显示统一两位，不是"最多两位"）
 *   2. null / undefined / 空串 / NaN / Infinity 一律显示 "--"，绝不让 NaN 漏到页面
 *   3. 正数带 +，负数自带 -，0 不带符号（0 → "0.00"）
 *   4. 只改显示，不碰原始数据精度，也不在调用处四舍五入
 *   5. 字符串数字（接口偶尔给 "43845.000"）也能吃
 */
(function () {
    function toNum(v) {
        if (v === null || v === undefined || v === '') return null;
        var n = (typeof v === 'number') ? v : parseFloat(String(v).replace(/,/g, ''));
        return isFinite(n) ? n : null;
    }

    function fixed(v, d) {
        var n = toNum(v);
        if (n === null) return '--';
        return n.toFixed(d === undefined ? 2 : d);
    }

    // 金额：正数带 +，0 不带符号，单位原样保留（亿 / 万 / 元）
    function money(v, unit) {
        var n = toNum(v);
        if (n === null) return '--';
        var sign = n > 0 ? '+' : '';
        return sign + n.toFixed(2) + (unit || '');
    }

    // 百分比：和金额同规则，默认也两位
    function pct(v, d) {
        var n = toNum(v);
        if (n === null) return '--';
        var sign = n > 0 ? '+' : '';
        return sign + n.toFixed(d === undefined ? 2 : d) + '%';
    }

    // 不带符号的普通数字（价格、PE 等）
    function plain(v, d) {
        return fixed(v, d);
    }

    // 交易日：接口原样给 8 位（20261106）→ 显示成 2026-11-06。
    // 已是 2026-11-06 就原样返回；取不到 / 不是合法日期 → 空串。
    // 之所以返回空串而不是 '--'：调用处据此直接不显示，页面上不该出现
    // "上证指数 undefined" 或 "上证指数 --" 这种像坏了一半的样子。
    function date8(v) {
        if (v === null || v === undefined) return '';
        var s = String(v).trim().replace(/\//g, '-');
        var m;
        if ((m = /^(\d{4})(\d{2})(\d{2})$/.exec(s)) || (m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s))) {
            var mm = parseInt(m[2], 10), dd = parseInt(m[3], 10);
            if (mm < 1 || mm > 12 || dd < 1 || dd > 31) return '';   // 20261399 这种直接不显示
            return m[1] + '-' + m[2] + '-' + m[3];
        }
        return '';
    }

    // ---------- 图表空值 ----------
    // Math.abs(null) === 0 —— 直接把 null 丢进 Math.abs 会把"没有数据"画成真实的 0。
    // 约定：null/undefined/空串/NaN 一律保留为 null（ECharts 不画该扇区），0 仍是 0。
    function chartValue(v) {
        var n = toNum(v);
        return n === null ? null : Math.abs(n);
    }

    // 饼图数据集：只做绝对值，null 原样保留
    function pieItems(items) {
        return (items || []).map(function (i) {
            return { name: i && i.name, value: chartValue(i && i.value) };
        });
    }

    // 饼图 tooltip：null 显示"暂无数据"，绝不显示 0.00 亿
    function pieTip(name, value, percent) {
        var n = toNum(value);
        if (n === null) return name + ': 暂无数据';
        var tail = (percent === null || percent === undefined) ? '' : ' (' + fixed(percent) + '%)';
        return name + ': ' + fixed(n) + '亿' + tail;
    }

    // 缺失提示：按实际缺失的类别动态生成，不写死
    function gapHint(series) {
        var list = series || [];
        var missing = [];
        for (var i = 0; i < list.length; i++) {
            if (toNum(list[i].value) === null) missing.push(list[i].name);
        }
        if (missing.length === 0) return '';
        var head = missing.join('、') + '数据暂缺';
        return missing.length === list.length
            ? head + '，暂无可用资金数据。'
            : head + '，当前图表仅展示可用资金类别。';
    }

    window.fmt = { toNum: toNum, fixed: fixed, money: money, pct: pct, plain: plain,
                   date8: date8, chartValue: chartValue, pieItems: pieItems,
                   pieTip: pieTip, gapHint: gapHint };
})();
