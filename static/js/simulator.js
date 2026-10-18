/* ==========================================================================
   决策模拟页：滑块与数值的双向绑定
   ==========================================================================
   为什么单独一个文件而不是塞进 site.js：
     1. 这段逻辑只服务一个页面，混进公共脚本会让 site.js 越来越难读；
     2. 它有明确的不变量（双向同步不回声、绝不自动提交），独立成文件后
        可以直接被测试脚本加载验证，不必启动浏览器。

   对外暴露 window.DSHSimulator = { bindSliders, bindReset, formatParamValue,
   clampToStep }；site.js 在初始化时会发现它并兜底调用一次，
   因此两个脚本的加载顺序不影响结果。

   交互约定（按用户明确要求）
   --------------------------------------------------------------------------
     · 拖动滑块  → 右侧数字实时跟随（用 input 事件，不是 change）；
     · 直接改数字 → 滑块同步移动（保留手改能力：它不是只读文字）；
     · 绝不自动提交：必须用户自己点「运行推演」才出结果。
       看板类工具最忌讳「动一下就重算」：一次探索要比较多个参数组合，
       自动提交会把结果刷掉，用户没法前后对照。
   ========================================================================== */
(function () {
  'use strict';

  /** 把原始数值按单位格式化成展示文本 */
  function formatParamValue(value, unit, step) {
    var num = Number(value);
    if (!isFinite(num)) return '';
    // 步长小于 1 才显示一位小数：步长为 1 的参数显示「3.0 天」只是啰嗦
    var decimals = (Number(step) > 0 && Number(step) < 1) ? 1 : 0;
    var text = num.toFixed(decimals);
    if (unit === '%') return text + '%';
    if (unit === '天') return text + '天';
    if (unit === '人') return text + '人';
    if (unit === '元') return '¥' + text;
    if (unit) return text + unit;
    return text;
  }

  /**
   * 写进 <input type="number"> 的文本。
   * 关键约束：number 输入框只接受纯数字。
   * 直接把 formatParamValue 的结果（"2.5%" / "3 天"）写进去，
   * 浏览器会判定为非法值并把框清空，页面上就只剩一个空白色块。
   * 所以这里只返回数字，单位交给旁边的 .slider-row__unit 文本。
   */
  function formatForNumberInput(value, step) {
    var num = Number(value);
    if (!isFinite(num)) return '';
    var decimals = (Number(step) > 0 && Number(step) < 1) ? 1 : 0;
    /* 整数不显示小数点后的 0：基线下显示「0」比「0.0」更像一句陈述，
       也让默认状态看起来是「没做任何调整」而不是「有个精确到小数点的值」。 */
    if (Number.isInteger(num)) return String(num);
    return num.toFixed(decimals);
  }

  /** 按 step 对齐并夹取到 [min, max]，保证滑块位置永远有解 */
  function clampToStep(value, min, max, step) {
    var num = Number(value);
    if (!isFinite(num)) num = min;
    var s = Number(step);
    if (!(s > 0)) s = 1;
    // 相对 min 对齐（而不是相对 0）：0~300、步长 5 这类区间才不会出现半格误差
    var aligned = min + Math.round((num - min) / s) * s;
    var digits = (String(s).split('.')[1] || '').length;
    aligned = Number(aligned.toFixed(Math.min(digits + 2, 6)));
    if (aligned < min) aligned = min;
    if (aligned > max) aligned = max;
    return aligned;
  }

  /** 从输入框文本里取出纯数字（容忍用户贴进来的 "2.5%" 这类写法） */
  function parseInput(raw) {
    return String(raw == null ? '' : raw)
      .replace(/[%天人元¥\s]/g, '')
      .trim();
  }

  function bindSliders() {
    var rows = document.querySelectorAll('[data-param-row]');
    Array.prototype.forEach.call(rows, function (row) {
      var key = row.getAttribute('data-param-row');
      var unit = row.getAttribute('data-unit') || '';
      var stepAttr = row.getAttribute('data-step') || '1';
      var range = row.querySelector('[data-slider-range="' + key + '"]');
      var input = row.querySelector('[data-slider-input="' + key + '"]');
      if (!range || !input) return;

      var min = Number(range.min);
      var max = Number(range.max);
      var s = Number(range.step) || Number(stepAttr) || 1;

      // 前后端共用一份区间定义：把后端的 min/max/step 同步到输入框上
      input.min = String(min);
      input.max = String(max);
      input.step = String(s);

      // busy 用于掐断「A 改 B → B 又改 A」的回声循环。
      // 不用「比较值是否相等」来防循环：step 为 0.1/0.5 时浮点相等判断并不可靠。
      var busy = false;

      function updateFill() {
        var pct = max > min ? ((Number(range.value) - min) / (max - min)) * 100 : 0;
        range.style.setProperty('--fill', pct.toFixed(2) + '%');
      }

      /** 滑块 → 输入框（拖动过程中实时） */
      function fromRange() {
        if (busy) return;
        busy = true;
        var v = clampToStep(range.value, min, max, s);
        input.value = formatForNumberInput(v, s);
        updateFill();
        busy = false;
      }

      /** 输入框 → 滑块（输入过程中实时，但不改写用户正在敲的文本） */
      function fromInput() {
        if (busy) return;
        var raw = parseInput(input.value);
        // 输入中间态（"2."、"-"、"" 等）不改写文本，否则光标会跳、没法继续输入
        if (raw === '' || raw === '-' || raw === '.' || raw === '-.') return;
        var v = clampToStep(raw, min, max, s);
        busy = true;
        range.value = String(v);
        updateFill();
        busy = false;
      }

      /** 失焦/回车时把文本规范化：吸附到步长、夹取到区间，并补上单位 */
      function normalize() {
        var raw = parseInput(input.value);
        var v = clampToStep(raw === '' ? min : raw, min, max, s);
        range.value = String(v);
        busy = true;
        input.value = formatForNumberInput(v, s);
        updateFill();
        busy = false;
      }

      function onKeydown(ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          normalize();
          input.blur();
        } else if (ev.key === 'ArrowUp' || ev.key === 'ArrowDown') {
          // 上下键微调：比拖滑块更容易落在一个精确值上
          ev.preventDefault();
          var delta = (ev.key === 'ArrowUp' ? s : -s) * (ev.shiftKey ? 10 : 1);
          var v = clampToStep(Number(range.value) + delta, min, max, s);
          range.value = String(v);
          input.value = formatForNumberInput(v, s);
          updateFill();
        } else if (ev.key === 'Escape') {
          // Esc 放弃本次输入，恢复成滑块当前值
          input.value = formatForNumberInput(range.value, s);
          input.blur();
        }
      }

      // range 的 input 在拖动过程中持续触发，这是「实时」的关键
      range.addEventListener('input', fromRange);
      input.addEventListener('input', fromInput);
      input.addEventListener('change', normalize);
      input.addEventListener('blur', normalize);
      input.addEventListener('keydown', onKeydown);

      // 首屏：以滑块值为准，把输入框格式化成「2.5%」这样的文本
      input.value = formatForNumberInput(clampToStep(range.value, min, max, s), s);
      updateFill();
    });

    // 双保险：这个表单里绝不允许「改一个参数就自动提交」。
    // 之前的多选筛选为了即时过滤是自动提交的，但模拟器是探索工具，必须手动提交。
    var form = document.querySelector('[data-simulator-form]');
    if (form) {
      form.addEventListener('change', function (ev) {
        if (ev.target && ev.target.getAttribute && ev.target.getAttribute('type') === 'number') {
          ev.stopPropagation();
        }
      }, true);
    }
  }

  /** 重置参数：所有参数恢复为 0（业务含义是「不调整」），只刷新显示、不提交 */
  function bindReset() {
    var btn = document.querySelector('[data-simulator-reset]');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var rows = document.querySelectorAll('[data-param-row]');
      Array.prototype.forEach.call(rows, function (row) {
        var key = row.getAttribute('data-param-row');
        var range = row.querySelector('[data-slider-range="' + key + '"]');
        if (!range) return;
        var min = Number(range.min);
        var max = Number(range.max);
        var s = Number(range.step) || 1;
        var def = (min <= 0 && max >= 0) ? clampToStep(0, min, max, s) : min;
        range.value = String(def);
        range.dispatchEvent(new Event('input', { bubbles: true }));
      });
    });
  }

  function init() {
    if (window.DSHSimulator && window.DSHSimulator.__bound) return;
    bindSliders();
    bindReset();
    if (window.DSHSimulator) window.DSHSimulator.__bound = true;
  }

  window.DSHSimulator = {
    bindSliders: bindSliders,
    bindReset: bindReset,
    formatParamValue: formatParamValue,
    clampToStep: clampToStep,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
