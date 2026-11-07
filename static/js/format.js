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

    window.fmt = { toNum: toNum, fixed: fixed, money: money, pct: pct, plain: plain };
})();
