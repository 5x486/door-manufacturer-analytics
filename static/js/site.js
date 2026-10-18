/* ==========================================================================
   杭博门业经营分析系统 · 前端交互层
   ==========================================================================
   设计原则（对应 design-system.css 的三个刻度：6 / 4 / 6）
   ---------------------------------------------------------------------------
   1. 图表是主角：去掉 ECharts 默认的边框、渐变、阴影，把注意力留给数据本身。
   2. 但标注要清楚（用户明确选择「细致标注风」）：轴单位、图例、数值直标、
      参考线、数据来源与口径一个都不省 —— 克制的是装饰，不是信息。
   3. 动效只服务于反馈：数字滚动表示「这是一次取数结果」，
      卡片抬升表示「可交互」，抽屉滑出表示「这是从你点的那个点来的」。
      一律尊重 prefers-reduced-motion。
   4. 颜色一律从 CSS 变量读取：这样主题切换（亮/暗）时图表自动跟随，
      不需要在 JS 里维护第二份色板。改色只改 design-system.css 一处。

   对外接口（模板与内联脚本依赖，改动需同步模板）
   ---------------------------------------------------------------------------
   window.DSH.Utils                 数字/百分比/金额格式化
   window.DSH.colorTokens()         读取当前主题的图表色板
   window.DSH.chartTheme()          构造 ECharts 主题基线
   window.DSH.renderChart(id, opt)  渲染单图（含 a11y 摘要与 resize）
   window.DSH.renderAllCharts()     渲染页面上所有 data-chart-option-id 容器
   window.DSH.openDrillDrawer()     下钻抽屉
   window.DSH.setTheme('light|dark'|null)
   window.DSH.exportChartCsv(id)    图表数据导出 CSV（可访问性 + 实用）
   ========================================================================== */
(function () {
  'use strict';

  var DSH = window.DSH || {};
  window.DSH = DSH;

  var reduceMotion = false;
  try {
    reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { /* 老环境忽略 */ }

  /* ------------------------------------------------------------------------
     1. 读取设计 token
     ------------------------------------------------------------------------ */
  function cssVar(name, fallback) {
    try {
      var value = window.getComputedStyle(document.documentElement).getPropertyValue(name);
      value = (value || '').trim();
      return value || fallback || '';
    } catch (e) {
      return fallback || '';
    }
  }

  /**
   * 图表色板：顺序即语义。
   * chart-1 是主序列（成交额 / 主线），其后依次是对比序列、状态色。
   * 绝不在这里写死十六进制 —— 否则深色模式会拿到浅色底上的色值，对比度直接不合格。
   */
  function colorTokens() {
    return {
      series: [
        cssVar('--chart-1', '#2c7a7b'),
        cssVar('--chart-2', '#7b8794'),
        cssVar('--chart-3', '#1d5fb8'),
        cssVar('--chart-4', '#b25e09'),
        cssVar('--chart-5', '#8b5cf6'),
        cssVar('--chart-6', '#157347')
      ],
      grid: cssVar('--chart-grid', '#e6eaee'),
      axis: cssVar('--chart-axis', '#a9b3bf'),
      label: cssVar('--chart-label', '#5b6672'),
      accent: cssVar('--accent', '#2c7a7b'),
      up: cssVar('--up', '#157347'),
      down: cssVar('--down', '#b42318'),
      warn: cssVar('--warn', '#b25e09'),
      surface: cssVar('--bg-surface', '#ffffff'),
      text: cssVar('--text-primary', '#14181d'),
      muted: cssVar('--text-muted', '#7b8794'),
      border: cssVar('--border-subtle', '#e6eaee'),
      font: cssVar('--font-sans', '-apple-system, "Segoe UI", "PingFang SC", sans-serif')
    };
  }

  /* ------------------------------------------------------------------------
     2. 格式化工具（中文习惯：万元 / 千分位 / 一位小数百分比）
     ------------------------------------------------------------------------ */
  var Utils = {
    formatNumber: function (value, digits) {
      if (value === null || value === undefined || value === '') return '—';
      var num = Number(value);
      if (!isFinite(num)) return '—';
      var d = typeof digits === 'number' ? digits : 0;
      return num.toFixed(d).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    },
    formatMoney: function (value, unit) {
      if (value === null || value === undefined || value === '') return '—';
      var num = Number(value);
      if (!isFinite(num)) return '—';
      if (unit === 'wan') return (num / 10000).toFixed(1) + ' 万';
      if (unit === 'yuan') return '¥' + Utils.formatNumber(num, 0);
      if (Math.abs(num) >= 100000000) return (num / 100000000).toFixed(2) + ' 亿';
      if (Math.abs(num) >= 10000) return (num / 10000).toFixed(1) + ' 万';
      return '¥' + Utils.formatNumber(num, 0);
    },
    formatPercent: function (value, digits) {
      if (value === null || value === undefined || value === '') return '—';
      var num = Number(value);
      if (!isFinite(num)) return '—';
      var d = typeof digits === 'number' ? digits : 1;
      var pct = Math.abs(num) <= 1 ? num * 100 : num;
      return pct.toFixed(d) + '%';
    },
    /** 涨跌方向 + 符号：颜色之外必须同时给出方向符号（色盲可用） */
    deltaClass: function (value) {
      var num = Number(value);
      if (!isFinite(num) || num === 0) return 'delta--flat';
      return num > 0 ? 'delta--up' : 'delta--down';
    },
    deltaArrow: function (value) {
      var num = Number(value);
      if (!isFinite(num) || num === 0) return '—';
      return num > 0 ? '▲' : '▼';
    }
  };
  DSH.Utils = Utils;

  /* ------------------------------------------------------------------------
     3. ECharts 主题基线
     ------------------------------------------------------------------------ */
  function chartTheme() {
    var t = colorTokens();
    return {
      color: t.series,
      backgroundColor: 'transparent',
      textStyle: {
        fontFamily: t.font,
        color: t.label,
        fontSize: 12
      },
      // 图例贴图不脱离：紧贴顶部、水平排列，避免用户在图与图例之间来回找
      legend: {
        top: 0,
        left: 0,
        icon: 'roundRect',
        itemWidth: 10,
        itemHeight: 10,
        itemGap: 16,
        textStyle: { color: t.label, fontSize: 12, fontFamily: t.font }
      },
      grid: { left: 8, right: 16, top: 44, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: {
          type: 'line',
          lineStyle: { color: t.axis, width: 1, type: [4, 4] }
        },
        backgroundColor: t.surface,
        borderColor: t.border,
        borderWidth: 1,
        padding: [10, 12],
        textStyle: { color: t.text, fontSize: 12, fontFamily: t.font },
        extraCssText: 'box-shadow: 0 8px 28px rgba(20,24,29,.14); border-radius: 8px;'
      },
      // 细致标注：轴标签齐全、单位写在轴名里，网格线弱到不抢数据
      categoryAxis: {
        axisLine: { lineStyle: { color: t.axis } },
        axisTick: { show: false },
        axisLabel: { color: t.label, fontSize: 11.5, fontFamily: t.font, margin: 10 },
        splitLine: { show: false }
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: t.label, fontSize: 11.5, fontFamily: t.font, margin: 10 },
        splitLine: { show: true, lineStyle: { color: t.grid, width: 1, type: 'solid' } }
      },
      animation: !reduceMotion,
      // 绘制时长刻意比「点击反馈」长：图表是「数据落位」的仪式感，
      // 900ms 的长尾曲线看得到笔尖画过去的过程，420ms 只会觉得一闪。
      animationDuration: reduceMotion ? 0 : 900,
      animationDurationUpdate: reduceMotion ? 0 : 360,
      animationEasing: 'cubicOut',
      animationEasingUpdate: 'cubicInOut'
    };
  }
  DSH.chartTheme = chartTheme;

  /**
   * 把主题基线并入后端下发的 option。
   * 深合并 legend/grid/tooltip 这些对象键，否则后端给的一项配置会整块覆盖主题，
   * 导致「一半图有网格线、一半没有」的割裂观感。
   */
  function applyChartTheme(option, theme) {
    var base = theme || chartTheme();
    var merged = {};
    var key;
    for (key in base) {
      if (Object.prototype.hasOwnProperty.call(base, key)) merged[key] = base[key];
    }
    for (key in (option || {})) {
      if (!Object.prototype.hasOwnProperty.call(option, key)) continue;
      var incoming = option[key];
      if (incoming && typeof incoming === 'object' && !Array.isArray(incoming) &&
          merged[key] && typeof merged[key] === 'object' && !Array.isArray(merged[key])) {
        var deep = {};
        var sub;
        for (sub in merged[key]) {
          if (Object.prototype.hasOwnProperty.call(merged[key], sub)) deep[sub] = merged[key][sub];
        }
        for (sub in incoming) {
          if (Object.prototype.hasOwnProperty.call(incoming, sub)) deep[sub] = incoming[sub];
        }
        merged[key] = deep;
      } else {
        merged[key] = incoming;
      }
    }
    return merged;
  }
  DSH.applyChartTheme = applyChartTheme;

  /* ------------------------------------------------------------------------
     4. 渲染
     ------------------------------------------------------------------------ */
  var instances = {};

  function parseOption(el) {
    var id = el.getAttribute('data-chart-option-id');
    if (!id) return null;
    var node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || node.innerHTML || 'null');
    } catch (e) {
      if (window.console) window.console.warn('[DSH] 图表 option 解析失败：', id, e);
      return null;
    }
  }

  function fallbackMessage(el, text) {
    el.innerHTML = '<div class="empty-state">' +
      '<div class="empty-state__title">图表暂不可用</div>' +
      '<div class="empty-state__desc">' + text + '</div></div>';
  }

  function renderChart(target, option) {
    var el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return null;

    if (typeof window.echarts === 'undefined') {
      fallbackMessage(el, '图表库未就绪（static/vendor/echarts.min.js 缺失）。页面上的表格与结论仍然可用。');
      return null;
    }
    if (!option || typeof option !== 'object') {
      fallbackMessage(el, '当前筛选条件下没有可绘制的数据，请放宽筛选条件后重试。');
      return null;
    }

    var finalOption = applyChartTheme(option);
    var instance = instances[el.id];
    if (!instance) {
      instance = window.echarts.init(el, null, { renderer: 'canvas' });
      instances[el.id] = instance;
    }
    instance.setOption(finalOption, true);

    // 可访问性：图形元素对屏幕阅读器不可读，必须补一段文字摘要 + 表格替代
    var summary = el.getAttribute('data-chart-summary');
    if (!summary) {
      var title = '';
      var head = el.closest('.chart-box');
      if (head) {
        var h = head.querySelector('.chart-box__title');
        if (h) title = h.textContent.trim();
      }
      summary = (title ? title + '：' : '') + '此图展示的数值可在下方数据表格中逐条查看。';
      el.setAttribute('data-chart-summary', summary);
    }
    el.setAttribute('aria-label', summary);

    var resize = function () { try { instance.resize(); } catch (e) {} };
    if (typeof window.ResizeObserver === 'function') {
      if (!el.__dshObserver) {
        el.__dshObserver = new window.ResizeObserver(resize);
        el.__dshObserver.observe(el);
      }
    } else {
      window.addEventListener('resize', resize);
    }
    return instance;
  }
  DSH.renderChart = renderChart;

  function renderAllCharts() {
    var nodes = document.querySelectorAll('[data-chart-option-id]');
    Array.prototype.forEach.call(nodes, function (el) {
      renderChart(el, parseOption(el));
    });
  }
  DSH.renderAllCharts = renderAllCharts;

  /* ------------------------------------------------------------------------
     5. 图表数据导出（可访问性要求：图表必须有非图形的数据出口）
     ------------------------------------------------------------------------ */
  function exportChartCsv(target) {
    var el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return;
    var option = parseOption(el);
    if (!option) return;

    var rows = [];
    var cats = (option.xAxis && (option.xAxis.data || (option.xAxis[0] && option.xAxis[0].data))) || [];
    var series = option.series || [];
    var header = ['维度'];
    series.forEach(function (s, i) { header.push(s.name || ('序列' + (i + 1))); });
    rows.push(header);
    cats.forEach(function (c, idx) {
      var line = [c];
      series.forEach(function (s) {
        var d = s.data || [];
        var v = d[idx];
        if (v && typeof v === 'object') v = (v.value !== undefined ? v.value : '');
        line.push(v === undefined || v === null ? '' : v);
      });
      rows.push(line);
    });

    var csv = rows.map(function (line) {
      return line.map(function (cell) {
        var s = String(cell === null || cell === undefined ? '' : cell);
        return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
      }).join(',');
    }).join('\r\n');

    // 加 BOM：否则 Excel 打开中文会乱码（中文用户最常见的反馈）
    var blob = new window.Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = (el.id || 'chart') + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
  DSH.exportChartCsv = exportChartCsv;

  /* ------------------------------------------------------------------------
     6. 下钻抽屉：从「看到异常」到「看到是谁」只差一次点击
     ------------------------------------------------------------------------ */
  function drawerEls() {
    return {
      drawer: document.getElementById('drillDrawer'),
      body: document.getElementById('drillDrawerBody'),
      title: document.getElementById('drillDrawerTitle'),
      sub: document.getElementById('drillDrawerSubtitle')
    };
  }

  function closeDrawer() {
    var d = drawerEls();
    if (!d.drawer) return;
    d.drawer.classList.remove('is-open');
    d.drawer.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    var trigger = d.drawer.__trigger;
    if (trigger && typeof trigger.focus === 'function') trigger.focus();
  }
  DSH.closeDrawer = closeDrawer;

  function esc(text) {
    return String(text === null || text === undefined ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function openDrillDrawer(params) {
    var d = drawerEls();
    if (!d.drawer || !d.body) return;
    var p = params || {};
    var dimension = p.dimension || '';
    var value = p.value === undefined || p.value === null ? '' : String(p.value);
    var api = p.api || '/api/v1/orders/';

    if (d.title) d.title.textContent = dimension ? dimension + '：' + value : '明细数据';
    if (d.sub) d.sub.textContent = '正在取数…';
    d.body.innerHTML = '<div class="skeleton" style="height:120px"></div>' +
      '<div class="skeleton" style="height:120px;margin-top:12px"></div>' +
      '<div class="skeleton" style="height:120px;margin-top:12px"></div>';

    d.drawer.classList.add('is-open');
    d.drawer.setAttribute('aria-hidden', 'false');
    d.drawer.__trigger = p.trigger || null;
    document.body.style.overflow = 'hidden';
    var closeBtn = d.drawer.querySelector('[data-drawer-close]');
    if (closeBtn && typeof closeBtn.focus === 'function') closeBtn.focus();

    // 筛选参数沿用当前 URL，保证「抽屉里的明细」和「图上的聚合」口径一致
    var query = window.location.search.replace(/^\?/, '');
    var url = api + (query ? (api.indexOf('?') >= 0 ? '&' : '?') + query : '');
    if (value) {
      var key = p.filterKey || dimension;
      url += (url.indexOf('?') >= 0 ? '&' : '?') + encodeURIComponent(key) + '=' + encodeURIComponent(value);
    }

    if (typeof window.fetch !== 'function') {
      d.body.innerHTML = '<div class="callout callout--warn">当前浏览器不支持 fetch，无法加载明细。</div>';
      return;
    }

    window.fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      })
      .then(function (payload) {
        var data = payload && payload.data ? payload.data : payload;
        var items = (data && (data.items || data.results || data)) || [];
        if (!Array.isArray(items) || !items.length) {
          if (d.sub) d.sub.textContent = '';
          d.body.innerHTML = '<div class="empty-state">' +
            '<div class="empty-state__title">该维度下没有匹配的明细</div>' +
            '<div class="empty-state__desc">可能是筛选条件本身已经排除了这些订单，或该维度只用于汇总展示。</div></div>';
          return;
        }
        var total = (data && (data.total || data.count)) || items.length;
        if (d.sub) d.sub.textContent = '共 ' + Utils.formatNumber(total) + ' 条，已展示前 ' + items.length + ' 条';
        d.body.innerHTML = buildDrillTable(items, p.columns);
      })
      .catch(function (err) {
        if (d.sub) d.sub.textContent = '';
        d.body.innerHTML = '<div class="callout callout--error">明细加载失败：' + esc(err.message) +
          '。<br>可以关闭抽屉后重试，或直接到「明细数据」页查看完整清单。</div>';
      });
  }
  DSH.openDrillDrawer = openDrillDrawer;

  function buildDrillTable(items, columns) {
    var keys = columns && columns.length ? columns : Object.keys(items[0] || {});
    var head = keys.map(function (k) { return '<th>' + esc(labelOf(k)) + '</th>'; }).join('');
    var body = items.map(function (row) {
      var tds = keys.map(function (k) {
        var v = row[k];
        var cls = '';
        if (typeof v === 'number' || /^-?\d+(\.\d+)?$/.test(String(v))) cls = ' class="is-number"';
        if (v === null || v === undefined || v === '') v = '—';
        return '<td' + cls + '>' + esc(v) + '</td>';
      }).join('');
      return '<tr>' + tds + '</tr>';
    }).join('');
    return '<div class="table-wrap" style="max-height:none"><table class="data-table">' +
      '<caption class="sr-only">图表下钻明细</caption>' +
      '<thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }

  var LABELS = {
    order_no: '订单号', order_date: '下单日期', customer_name: '客户',
    customer_type: '客户类型', channel: '来源渠道', order_type: '订单类型',
    dominant_category: '主品类', final_amount: '成交额', order_status: '状态',
    sales_rep_name: '业务员', store_name: '门店', gross_profit: '毛利',
    gross_margin: '毛利率', quantity: '数量', unit_price: '单价'
  };
  function labelOf(key) { return LABELS[key] || key; }

  /* ------------------------------------------------------------------------
     7. 主题切换（亮/暗）：记忆在本地，首屏在 <head> 里提前应用避免闪烁
     ------------------------------------------------------------------------ */
  function setTheme(mode) {
    var root = document.documentElement;
    if (mode === 'light' || mode === 'dark') {
      root.setAttribute('data-theme', mode);
      try { window.localStorage.setItem('dsh-theme', mode); } catch (e) {}
    } else {
      root.removeAttribute('data-theme');
      try { window.localStorage.removeItem('dsh-theme'); } catch (e) {}
    }
    updateThemeToggleLabel();
    // 主题变了 → CSS 变量变了 → 图表必须重建（ECharts 不会自己重读变量）
    Object.keys(instances).forEach(function (id) {
      try { instances[id].dispose(); } catch (e) {}
      delete instances[id];
    });
    renderAllCharts();
  }
  DSH.setTheme = setTheme;

  function currentTheme() {
    var attr = document.documentElement.getAttribute('data-theme');
    if (attr) return attr;
    try {
      return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    } catch (e) { return 'light'; }
  }

  function updateThemeToggleLabel() {
    var btn = document.getElementById('themeToggle');
    if (!btn) return;
    var next = currentTheme() === 'dark' ? '切换到浅色模式' : '切换到深色模式';
    btn.setAttribute('aria-label', next);
    btn.setAttribute('title', next);
  }

  /* ------------------------------------------------------------------------
     7.5 多选筛选：点一下即勾选，不需要按住 Ctrl
     ------------------------------------------------------------------------
     为什么不用原生 <select multiple>：
       原生多选的交互是「普通点击 = 单选、Ctrl/Shift + 点击 = 多选」，
       这是一个桌面软件的遗留约定，绝大多数人第一次用会以为选不了多个；
       而「按住 Ctrl」的提示文字写了也没人看。
     做法：
       把 select[multiple] 换成 details + checkbox 列表（点一下即勾选、再点取消），
       原生 select 用 hidden 留在 DOM 里作为「值的事实来源」，
       所有既有逻辑（隐藏 input 同步、表单提交）都不需要改。
   ------------------------------------------------------------------------ */
  function enhanceMultiSelects() {
    var selects = document.querySelectorAll('select[multiple][data-multi]');
    Array.prototype.forEach.call(selects, function (sel) {
      var field = sel.getAttribute('data-filter-field');
      var labelEl = document.querySelector('label[for="' + sel.id + '"]');
      var labelText = labelEl ? labelEl.textContent.replace(/（可多选）/, '') : (field || '筛选项');
      var hintEl = sel.parentNode.querySelector('.field__hint');
      var placeholder = hintEl ? hintEl.textContent.trim() : '点选多项';

      var box = document.createElement('div');
      box.className = 'ms';

      var toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'ms__toggle';
      toggle.setAttribute('aria-haspopup', 'listbox');
      toggle.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-label', labelText + '筛选，可多选');

      var text = document.createElement('span');
      text.className = 'ms__text';
      var caret = document.createElement('span');
      caret.className = 'ms__caret';
      caret.setAttribute('aria-hidden', 'true');
      caret.textContent = '▾';
      toggle.appendChild(text);
      toggle.appendChild(caret);

      var panel = document.createElement('div');
      panel.className = 'ms__panel';
      panel.setAttribute('role', 'listbox');
      panel.setAttribute('aria-multiselectable', 'true');
      panel.hidden = true;

      var options = Array.prototype.slice.call(sel.options);
      options.forEach(function (opt) {
        if (opt.disabled) return;
        var row = document.createElement('label');
        row.className = 'ms__option';

        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = opt.value;
        cb.checked = opt.selected;
        cb.setAttribute('role', 'option');

        var span = document.createElement('span');
        span.textContent = opt.textContent;

        row.appendChild(cb);
        row.appendChild(span);
        panel.appendChild(row);

        cb.addEventListener('change', function () {
          opt.selected = cb.checked;
          sync();
          // 变更即查询：与日期/单选一致，避免用户以为「勾了没用」
          fireChange(sel);
        });
      });

      function sync() {
        var picked = options.filter(function (o) { return o.selected; }).map(function (o) { return o.textContent; });
        if (picked.length === 0) {
          text.textContent = placeholder + '（不限）';
          text.classList.add('is-placeholder');
        } else if (picked.length <= 2) {
          text.textContent = picked.join('、');
          text.classList.remove('is-placeholder');
        } else {
          text.textContent = picked[0] + ' 等 ' + picked.length + ' 项';
          text.classList.remove('is-placeholder');
        }
      }

      function close() {
        panel.hidden = true;
        toggle.setAttribute('aria-expanded', 'false');
        box.classList.remove('is-open');
      }
      function open() {
        panel.hidden = false;
        toggle.setAttribute('aria-expanded', 'true');
        box.classList.add('is-open');
      }

      toggle.addEventListener('click', function (ev) {
        ev.preventDefault();
        if (panel.hidden) open(); else close();
      });
      // 点击外部关闭；Esc 关闭（键盘可达）
      document.addEventListener('click', function (ev) {
        if (panel.hidden) return;
        if (box.contains(ev.target)) return;
        close();
      });
      box.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') { close(); toggle.focus(); }
      });

      // 全选 / 清空：多选场景下这两个动作很常用，省掉逐项点击
      var actions = document.createElement('div');
      actions.className = 'ms__actions';
      var allBtn = document.createElement('button');
      allBtn.type = 'button';
      allBtn.className = 'btn btn--quiet btn--small';
      allBtn.textContent = '全选';
      var noneBtn = document.createElement('button');
      noneBtn.type = 'button';
      noneBtn.className = 'btn btn--quiet btn--small';
      noneBtn.textContent = '清空';
      actions.appendChild(allBtn);
      actions.appendChild(noneBtn);
      panel.appendChild(actions);

      allBtn.addEventListener('click', function () {
        Array.prototype.forEach.call(panel.querySelectorAll('input[type=checkbox]'), function (cb) {
          cb.checked = true;
        });
        options.forEach(function (o) { o.selected = true; });
        sync(); fireChange(sel);
      });
      noneBtn.addEventListener('click', function () {
        Array.prototype.forEach.call(panel.querySelectorAll('input[type=checkbox]'), function (cb) {
          cb.checked = false;
        });
        options.forEach(function (o) { o.selected = false; });
        sync(); fireChange(sel);
      });

      box.appendChild(toggle);
      box.appendChild(panel);
      sel.parentNode.insertBefore(box, sel);
      // 原生 select 保留在 DOM 中作为值的来源，但视觉上隐藏
      sel.classList.add('ms__native');
      if (labelEl) labelEl.textContent = labelText + '（可多选）';
      sync();
    });
  }

  /** 触发 change 事件，让既有的「多选同步到隐藏 input」逻辑接管 */
  function fireChange(el) {
    var ev;
    try {
      ev = new Event('change', { bubbles: true });
    } catch (e) {
      ev = document.createEvent('HTMLEvents');
      ev.initEvent('change', true, false);
    }
    el.dispatchEvent(ev);
  }
  DSH.enhanceMultiSelects = enhanceMultiSelects;


  /* ------------------------------------------------------------------------
     8. 初始化：图表、下钻、抽屉、筛选条、侧栏、数字滚动、tabs
     ------------------------------------------------------------------------ */
  function bindDrilldown() {
    document.addEventListener('click', function (ev) {
      var el = ev.target;
      if (!el || typeof el.closest !== 'function') return;
      var chartEl = el.closest('[data-drill-dimension]');
      if (chartEl) {
        var instance = instances[chartEl.id];
        if (instance && typeof instance.getZr === 'function') {
          var point = [ev.clientX - chartEl.getBoundingClientRect().left,
                       ev.clientY - chartEl.getBoundingClientRect().top];
          var found = instance.convertFromPixel({ seriesIndex: 0 }, point);
          var option = parseOption(chartEl);
          var cats = (option && option.xAxis && option.xAxis.data) || [];
          var value = Array.isArray(found) ? cats[found[0]] : null;
          if (value) {
            openDrillDrawer({
              dimension: chartEl.getAttribute('data-drill-dimension'),
              filterKey: chartEl.getAttribute('data-drill-filter') || '',
              value: value,
              api: chartEl.getAttribute('data-drill-api') || '/api/v1/orders/',
              trigger: chartEl
            });
          }
        }
      }
    });
  }

  function bindDrawer() {
    var drawer = document.getElementById('drillDrawer');
    if (!drawer) return;
    drawer.addEventListener('click', function (ev) {
      if (ev.target.closest && (ev.target.closest('[data-drawer-close]') || ev.target.classList.contains('drawer__scrim'))) {
        closeDrawer();
      }
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && drawer.classList.contains('is-open')) closeDrawer();
    });
  }

  function bindSidebar() {
    var toggle = document.getElementById('sidebarToggle');
    var sidebar = document.getElementById('appSidebar');
    if (!toggle || !sidebar) return;
    toggle.addEventListener('click', function () {
      var open = sidebar.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('click', function (ev) {
      if (window.innerWidth >= 980) return;
      if (!sidebar.classList.contains('is-open')) return;
      if (sidebar.contains(ev.target) || toggle.contains(ev.target)) return;
      sidebar.classList.remove('is-open');
      toggle.setAttribute('aria-expanded', 'false');
    });
  }

  function bindThemeToggle() {
    var btn = document.getElementById('themeToggle');
    if (!btn) return;
    updateThemeToggleLabel();
    btn.addEventListener('click', function () {
      setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
    });
  }

  /** 筛选条：改变即提交（防抖），并把已选条件同步成可读标签 */
  function bindFilters() {
    var forms = document.querySelectorAll('[data-filter-form]');
    Array.prototype.forEach.call(forms, function (form) {
      var timer = null;
      form.addEventListener('change', function () {
        if (timer) window.clearTimeout(timer);
        timer = window.setTimeout(function () { form.submit(); }, 260);
      });
      // 多选列表：选中项同步到隐藏 input（后端只解析一个逗号分隔参数）
      var multi = form.querySelectorAll('select[multiple][data-filter-field]');
      Array.prototype.forEach.call(multi, function (sel) {
        var sync = function () {
          var hidden = form.querySelector('input[type="hidden"][name="' + sel.getAttribute('data-filter-field') + '"]');
          if (!hidden) return;
          hidden.value = Array.prototype.map.call(sel.selectedOptions, function (o) { return o.value; }).join(',');
        };
        sel.addEventListener('change', sync);
        sync();
      });
    });

    document.addEventListener('click', function (ev) {
      var btn = ev.target.closest && ev.target.closest('[data-filter-remove]');
      if (!btn) return;
      var field = btn.getAttribute('data-filter-remove');
      var value = btn.getAttribute('data-filter-value');
      var url = new URL(window.location.href);
      var current = (url.searchParams.get(field) || '').split(',').filter(Boolean);
      var next = current.filter(function (v) { return v !== value; });
      if (next.length) url.searchParams.set(field, next.join(','));
      else url.searchParams.delete(field);
      url.searchParams.delete('page');
      window.location.href = url.toString();
    });
  }

  /**
   * KPI 数字滚动：让「取数结果」这件事有视觉反馈。
   * 用 requestAnimationFrame 而不是 setInterval，避免掉帧与后台标签页空转；
   * 减少动效偏好下直接跳过（数值立刻到位，不做动画）。
   *
   * 节奏说明（与 motion-layer.css 的入场编排对齐）：
   * · 卡片本身是「依次亮起」的，数字若同时起步，后出现的卡片会显示半截数字；
   *   所以这里给每张卡片加 60ms 的递延，和卡片入场共用同一条时间线。
   * · 缓动改成 easeOutExpo（长尾减速）：前三帧就冲到 90%，最后一段慢慢落定，
   *   这正是苹果官网数字滚动的手感，比线性或 cubic 更像「有惯性」。
   */

  /**
   * 从一张 KPI 卡上读出「数字 + 前后缀」。
   *
   * 后端 kpi() 只给了已经格式化好的字符串（"5,981.82 万元" / "20.3%"），
   * 模板里那条 data-kpi-value 分支从来没有被渲染过，于是数字滚动一直是死代码。
   * 与其去改上百个 kpi() 调用点，不如在这里把显示文本反解成数字：
   * 数字部分动，前缀（¥、+）与后缀（万元、%、单）原样保留。
   * 解析不出来（"-"、"铜门"、"未提交推演"）就返回 null，卡片保持静态。
   */
  function readKpiNumber(el) {
    var raw = el.getAttribute('data-kpi-value');
    if (raw !== null && raw !== '' && isFinite(Number(raw))) {
      return {
        target: Number(raw),
        digits: Number(el.getAttribute('data-kpi-digits') || 0),
        prefix: el.getAttribute('data-kpi-prefix') || '',
        suffix: el.getAttribute('data-kpi-suffix') || ''
      };
    }
    /* 带嵌套标签（例如 <span class="kpi__unit">）的卡片不碰：
       写回 textContent 会把内部的单位节点一起抹掉。 */
    if (el.children && el.children.length) return null;

    var text = (el.textContent || '').trim();
    var match = text.match(/^([^\d]*?)([+\-]?[\d,]+(?:\.\d+)?)(.*)$/);
    if (!match) return null;
    var numberText = match[2].replace(/,/g, '');
    var target = Number(numberText);
    if (!isFinite(target)) return null;
    return {
      target: target,
      digits: (numberText.split('.')[1] || '').length,
      prefix: match[1],
      suffix: match[3]
    };
  }

  function animateNumbers() {
    var nodes = document.querySelectorAll('[data-kpi-value]');
    if (!nodes.length) nodes = document.querySelectorAll('.kpi__value');
    if (!nodes.length) return;
    if (reduceMotion) return;

    Array.prototype.forEach.call(nodes, function (el, index) {
      var spec = readKpiNumber(el);
      if (!spec) return;
      var target = spec.target;
      if (!isFinite(target) || target === 0) return;
      var digits = spec.digits;
      var prefix = spec.prefix;
      var suffix = spec.suffix;
      var start = null;
      var dur = 900;
      var delay = Math.min(index, 8) * 60;

      var step = function (ts) {
        if (start === null) start = ts;
        var p = Math.min(1, (ts - start) / dur);
        var eased = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
        var value = target * eased;
        el.textContent = prefix + Utils.formatNumber(value, digits) + suffix;
        if (p < 1) window.requestAnimationFrame(step);
        else el.textContent = prefix + Utils.formatNumber(target, digits) + suffix;
      };

      window.setTimeout(function () {
        window.requestAnimationFrame(step);
      }, delay);
    });
  }

  function bindTabs() {
    document.addEventListener('click', function (ev) {
      var btn = ev.target.closest && ev.target.closest('[data-tab-target]');
      if (!btn) return;
      ev.preventDefault();
      var group = btn.getAttribute('data-tab-group') || '';
      var target = btn.getAttribute('data-tab-target');
      document.querySelectorAll('[data-tab-target]').forEach(function (b) {
        if ((b.getAttribute('data-tab-group') || '') !== group) return;
        var on = b === btn;
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      document.querySelectorAll('[data-tab-panel]').forEach(function (p) {
        if (group && (p.getAttribute('data-tab-group') || '') !== group) return;
        p.hidden = p.getAttribute('data-tab-panel') !== target;
      });
    });
  }

  /* ------------------------------------------------------------------------
     7.6 滑块 ↔ 数值输入：双向绑定（决策模拟页）
     ------------------------------------------------------------------------
     需求：拖动滑块时右侧数值实时跟随；直接在数值框里输入时滑块同步移动。
     要点：
       1. **实时**：用 input 事件而不是 change。change 只在失焦/松手时触发，
          拖动过程中数字不动，是最常见的「感觉有延迟」来源。
       2. **防回声**：A 改 B 时，B 会再抛一次 input 事件回头改 A，形成循环。
          这里用一个 binding 上的 busy 标记把它掐断，而不是靠比较值是否相等
          （浮点相等判断在 step 为 0.1 时并不可靠）。
       3. **格式化按字段而定**：百分比 1 位小数、天数 1 位小数、人数 1 位小数。
          格式只作用于「显示」，写回滑块的值始终是 Number，避免把 "2.5%" 当成数值。
       4. **夹取与吸附**：输入越界时夹到 min/max，并按 step 对齐，保证滑块位置有解。
       5. 数值框用 type="number"，于是键盘上下键、移动端数字键盘都天然可用；
         配合 aria-label，读屏也能读出「某某参数的数值输入」。
   ------------------------------------------------------------------------ */

  /** 把原始数值按单位格式化成展示文本 */
  function formatParamValue(value, unit, step) {
    var num = Number(value);
    if (!isFinite(num)) return '';
    // 步长小于 1 时才显示一位小数：步长为 1 的参数显示 3.0 天只会显得啰嗦
    var decimals = (Number(step) > 0 && Number(step) < 1) ? 1 : 0;
    var text = num.toFixed(decimals);
    if (unit === '%') return text + '%';
    if (unit === '天') return text + '天';
    if (unit === '人') return text + '人';
    if (unit === '元') return '¥' + text;
    if (unit) return text + unit;
    return text;
  }
  DSH.formatParamValue = formatParamValue;

  /** 按 step 对齐并夹取到 [min, max] */
  function clampToStep(value, min, max, step) {
    var num = Number(value);
    if (!isFinite(num)) num = min;
    var s = Number(step);
    if (!(s > 0)) s = 1;
    // 用「相对 min 的偏移」对齐，而不是直接用 0 对齐：
    // 像 -15 到 15、步长 0.5 这种区间，按 0 对齐与按 min 对齐结果一致；
    // 但像 0 到 300、步长 5 这类区间，只有相对 min 对齐才不会有半格误差。
    var aligned = min + Math.round((num - min) / s) * s;
    // 浮点乘法会引入 1e-16 级别的尾巴，按 step 的精度收一下
    var digits = (String(s).split('.')[1] || '').length;
    aligned = Number(aligned.toFixed(Math.min(digits + 2, 6)));
    if (aligned < min) aligned = min;
    if (aligned > max) aligned = max;
    return aligned;
  }
  DSH.clampToStep = clampToStep;

  function init() {
    bindDrawer();
    bindSidebar();
    bindThemeToggle();
    // 多选增强必须在 bindFilters 之前：它会把 select[multiple] 换成可点选的列表，
    // 之后 bindFilters 绑定到原生 select 上的 change 同步逻辑依然生效（值来源没变）。
    enhanceMultiSelects();
    bindFilters();
    bindTabs();
    bindDrilldown();
    renderAllCharts();
    animateNumbers();

    // 决策模拟页的滑块双向绑定在独立文件 static/js/simulator.js 里，
    // 它自己会在 DOM 就绪时初始化。这里只在它已加载但尚未初始化时补一次调用，
    // 保证「先加载 site.js 再加载 simulator.js」与相反的加载顺序都能正常工作。
    if (window.DSHSimulator && typeof window.DSHSimulator.bindSliders === 'function' && !window.DSHSimulator.__bound) {
      window.DSHSimulator.bindSliders();
      window.DSHSimulator.__bound = true;
    }

    // 图表导出按钮：委托绑定，页面后续插入的图表也能用
    document.addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest && ev.target.closest('[data-chart-export]');
      if (!btn) return;
      ev.preventDefault();
      exportChartCsv(btn.getAttribute('data-chart-export'));
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
