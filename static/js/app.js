/* ============================================================================
 * 杭博门业经营分析系统 · 前端交互（app.js）
 * ----------------------------------------------------------------------------
 * 设计约束（很重要，决定了这个文件的写法）：
 *
 * 1. **不引入任何外部 CDN 库**。ECharts 走本地 static 文件（vendor/echarts.min.js），
 *    其余全部为原生 JS。理由是内网/离线环境下 CDN 必然不可用，
 *    而经营分析系统常常部署在厂区局域网里。
 * 2. **不依赖任何框架**（无 jQuery / Vue）。页面是「服务端渲染 + 局部增强」，
 *    交互只有四件事：筛选条提交、图表初始化、下钻抽屉、Tab/手风琴。
 * 3. **渐进增强**：JS 全部失效时，表单仍能用「查询」按钮提交、
 *    表格仍能通过普通链接翻页与排序，页面不会变成白屏。
 * 4. **图表缺失要降级**：ECharts 没加载成功时，显示「图表库未就绪」提示，
 *    而不是让页面控制台报一堆错。
 * ========================================================================== */

(function () {
    'use strict';

    /* ------------------------------------------------------------------------
     * 0. 命名空间与工具函数
     * ---------------------------------------------------------------------- */
    var DSH = window.DSH || {};
    window.DSH = DSH;

    var Utils = {
        /** 千分位格式化：12874.5 -> "12,874.50" */
        formatNumber: function (value, digits) {
            if (value === null || value === undefined || value === '') return '—';
            var num = Number(value);
            if (isNaN(num)) return '—';
            var d = typeof digits === 'number' ? digits : 0;
            var parts = num.toFixed(d).split('.');
            parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
            return parts.join('.');
        },

        /** 比率（0~1）转百分比文本：0.9123 -> "91.2%"（全项目统一口径） */
        formatPercent: function (value, digits) {
            if (value === null || value === undefined || value === '') return '—';
            var num = Number(value);
            if (isNaN(num)) return '—';
            var d = typeof digits === 'number' ? digits : 1;
            return (num * 100).toFixed(d) + '%';
        },

        /** 金额自适应单位：12345678 -> "1,234.57 万元" */
        formatMoney: function (value) {
            if (value === null || value === undefined || value === '') return '—';
            var num = Number(value);
            if (isNaN(num)) return '—';
            var sign = num < 0 ? '-' : '';
            var abs = Math.abs(num);
            if (abs >= 100000000) return sign + Utils.formatNumber(abs / 100000000, 2) + ' 亿元';
            if (abs >= 10000) return sign + Utils.formatNumber(abs / 10000, 2) + ' 万元';
            return sign + Utils.formatNumber(abs, 2) + ' 元';
        },

        /** 防抖：筛选条变更即提交时用它，避免拖滑块式地狂发请求 */
        debounce: function (fn, wait) {
            var timer = null;
            return function () {
                var args = arguments;
                var self = this;
                if (timer) clearTimeout(timer);
                timer = setTimeout(function () {
                    timer = null;
                    fn.apply(self, args);
                }, wait || 300);
            };
        },

        /** 读取查询参数 */
        getParams: function () {
            var params = {};
            var search = window.location.search.replace(/^\?/, '');
            if (!search) return params;
            search.split('&').forEach(function (pair) {
                if (!pair) return;
                var idx = pair.indexOf('=');
                var key = idx >= 0 ? pair.slice(0, idx) : pair;
                var value = idx >= 0 ? pair.slice(idx + 1) : '';
                params[decodeURIComponent(key)] = decodeURIComponent(value.replace(/\+/g, ' '));
            });
            return params;
        },

        /** 简易查询串构造（跳过空值） */
        buildQuery: function (obj) {
            var parts = [];
            Object.keys(obj).forEach(function (key) {
                var value = obj[key];
                if (value === null || value === undefined || value === '') return;
                if (Array.isArray(value)) {
                    if (!value.length) return;
                    parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value.join(',')));
                    return;
                }
                parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
            });
            return parts.join('&');
        },

        /** 安全取 DOM（取第一个） */
        one: function (selector, scope) {
            return (scope || document).querySelector(selector);
        },

        /** 安全取 DOM（取全部，返回数组） */
        $$: function (selector, scope) {
            return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
        },

        /** 转义 HTML：下钻表格是拼接出来的，客户名/备注里可能有尖括号 */
        escapeHtml: function (text) {
            return String(text === null || text === undefined ? '' : text)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        },

        fetchJson: function (url) {
            if (typeof window.fetch !== 'function') {
                return Promise.reject(new Error('浏览器不支持 fetch'));
            }
            return window
                .fetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
                .then(function (response) {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.json();
                });
        }
    };

    DSH.Utils = Utils;

    /* ------------------------------------------------------------------------
     * 1. 颜色与主题：从 CSS 变量读取，保证图表与页面用的是同一份色值
     * ---------------------------------------------------------------------- */
    var COLOR_FALLBACK = ['#2F6FED', '#0F9D8C', '#D97706', '#C2410C', '#B42318', '#6B7280'];

    function cssVar(name, fallback) {
        var value = window.getComputedStyle(document.documentElement).getPropertyValue(name);
        value = (value || '').trim();
        return value || fallback;
    }

    /** 图表色板（与 app.css 的 --chart-* / --color-* 保持一致） */
    function colorTokens() {
        return {
            primary: cssVar('--color-primary', COLOR_FALLBACK[0]),
            accent: cssVar('--color-accent', COLOR_FALLBACK[1]),
            success: cssVar('--color-success', '#1C8C5B'),
            warning: cssVar('--color-warning', '#B7791F'),
            danger: cssVar('--color-danger', '#C0392B'),
            info: cssVar('--color-info', '#4A5568'),
            series: COLOR_FALLBACK.slice(),
            axisLine: cssVar('--border-strong', '#C9D0D9'),
            splitLine: cssVar('--border-color', '#E2E6EC'),
            textPrimary: cssVar('--text-primary', '#0F1419'),
            textMuted: cssVar('--text-muted', '#6B7683'),
            surface: cssVar('--bg-surface', '#FFFFFF')
        };
    }

    DSH.colorTokens = colorTokens;

    /** ECharts 基础主题：字体族、字号、网格线只在这里定义一次 */
    function chartTheme() {
        var tokens = colorTokens();
        var fontFamily = cssVar('--font-sans', '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif');
        return {
            tokens: tokens,
            textStyle: {
                fontFamily: fontFamily,
                fontSize: 12,
                color: tokens.textPrimary
            },
            tooltip: {
                backgroundColor: tokens.surface,
                borderColor: tokens.splitLine,
                borderWidth: 1,
                textStyle: { color: tokens.textPrimary, fontSize: 12, fontFamily: fontFamily }
            },
            legend: {
                textStyle: { color: tokens.textMuted, fontSize: 12, fontFamily: fontFamily }
            },
            axis: {
                axisLine: { lineStyle: { color: tokens.axisLine } },
                axisLabel: { color: tokens.textMuted, fontSize: 12, fontFamily: fontFamily },
                splitLine: { lineStyle: { color: tokens.splitLine, type: 'dashed' } },
                nameTextStyle: { color: tokens.textMuted, fontSize: 11, fontFamily: fontFamily }
            }
        };
    }

    DSH.chartTheme = chartTheme;

    /** 把主题默认值合并进后端下发的 option（后端只需管业务数据，不用管字号） */
    function applyTheme(option) {
        var theme = chartTheme();
        var merged = JSON.parse(JSON.stringify(option || {}));

        merged.textStyle = Object.assign({}, theme.textStyle, merged.textStyle || {});
        merged.color = merged.color || theme.tokens.series;
        merged.tooltip = Object.assign({}, theme.tooltip, merged.tooltip || {});

        if (merged.legend) {
            merged.legend = Object.assign({ textStyle: theme.legend.textStyle }, merged.legend);
        }

        var axes = [];
        if (merged.xAxis) axes = axes.concat(merged.xAxis);
        if (merged.yAxis) axes = axes.concat(merged.yAxis);
        axes.forEach(function (axis) {
            if (!axis || !axis.type) return;
            axis.axisLine = Object.assign({}, theme.axis.axisLine, axis.axisLine || {});
            axis.axisLabel = Object.assign({}, theme.axis.axisLabel, axis.axisLabel || {});
            axis.nameTextStyle = Object.assign({}, theme.axis.nameTextStyle, axis.nameTextStyle || {});
            if (axis.type === 'value' || axis.type === 'log') {
                axis.splitLine = Object.assign({ show: true }, theme.axis.splitLine, axis.splitLine || {});
            } else if (axis.splitLine === undefined) {
                axis.splitLine = { show: false };
            }
        });

        return merged;
    }

    DSH.applyChartTheme = applyTheme;

    /* ------------------------------------------------------------------------
     * 2. 图表统一封装
     * ---------------------------------------------------------------------- */
    var chartRegistry = {};

    function echartsReady() {
        return typeof window.echarts !== 'undefined';
    }

    /** 在容器里显示「图表库未就绪」的降级提示（而不是静默留白） */
    function showChartFallback(el, message) {
        if (!el) return;
        el.innerHTML = '';
        var box = document.createElement('div');
        box.className = 'chart-box__fallback';
        box.textContent = message || '图表库未就绪：请确认 static/vendor/echarts.min.js 已随静态文件一起部署。';
        el.appendChild(box);
    }

    /**
     * 渲染图表（幂等：同一容器重复调用会先 dispose，避免内存泄漏）
     * @param {string|HTMLElement} target 容器 id 或元素
     * @param {Object} option ECharts option
     * @returns {Object|null} ECharts 实例
     */
    function renderChart(target, option) {
        var el = typeof target === 'string' ? document.getElementById(target) : target;
        if (!el) return null;

        if (!echartsReady()) {
            showChartFallback(el);
            return null;
        }

        var existing = chartRegistry[el.id];
        if (existing && !existing.isDisposed()) {
            existing.dispose();
        }

        var instance = window.echarts.init(el, null, { renderer: 'canvas' });
        var themedOption = applyTheme(option);

        // 下钻：点击柱子 / 扇区 / 散点后按维度值拉明细
        themedOption = attachDrilldown(themedOption, el);

        instance.setOption(themedOption, true);
        chartRegistry[el.id] = instance;
        return instance;
    }

    DSH.renderChart = renderChart;

    /** 容器尺寸变化时 resize（用 ResizeObserver，比 window.resize 更准：侧边栏折叠也会触发） */
    function observeResize() {
        var charts = function () {
            return Object.keys(chartRegistry).map(function (id) {
                return chartRegistry[id];
            });
        };
        var resizeAll = Utils.debounce(function () {
            charts().forEach(function (chart) {
                if (chart && !chart.isDisposed()) chart.resize();
            });
        }, 120);

        window.addEventListener('resize', resizeAll);

        if (typeof window.ResizeObserver === 'function') {
            var observer = new window.ResizeObserver(resizeAll);
            Utils.$$('.chart-box__canvas').forEach(function (el) {
                observer.observe(el);
            });
        }
    }

    /** 一次性初始化页面上所有图表（option 由模板用 json_script 注入到 script 标签） */
    function renderAllCharts() {
        Utils.$$('[data-chart-option-id]').forEach(function (el) {
            var holder = document.getElementById(el.getAttribute('data-chart-option-id'));
            if (!holder) return;
            var option = null;
            try {
                option = JSON.parse(holder.textContent);
            } catch (err) {
                showChartFallback(el, '图表配置解析失败（后端下发的 JSON 不合法）。');
                return;
            }
            renderChart(el, option);
        });
    }

    DSH.renderAllCharts = renderAllCharts;

    /* ------------------------------------------------------------------------
     * 3. 图表下钻（点击柱子/扇区 → 拉明细 → 打开右侧抽屉）
     * ---------------------------------------------------------------------- */
    function attachDrilldown(option, el) {
        var dimension = el.getAttribute('data-drill-dimension');
        var apiTemplate = el.getAttribute('data-drill-api') || '/api/v1/orders/';
        if (!dimension) return option;

        var seriesList = option.series;
        if (!seriesList) return option;
        if (!Array.isArray(seriesList)) seriesList = [seriesList];

        seriesList.forEach(function (series) {
            if (!series || !series.type) return;
            if (['bar', 'line', 'pie', 'scatter'].indexOf(series.type) < 0) return;
            if (typeof series.onClick === 'function') return;

            series.cursor = 'pointer';
            series.onClick = function (params) {
                var value = extractDimensionValue(params, series.type);
                if (!value) return;
                openDrillDrawer({
                    title: '下钻明细：' + dimension + ' = ' + value,
                    params: buildDrillParams(dimension, value),
                    api: apiTemplate
                });
            };
        });

        return option;
    }

    /** 从 ECharts 事件参数里取出「维度值」 */
    function extractDimensionValue(params, seriesType) {
        if (seriesType === 'pie') {
            return params.name || null;
        }
        if (seriesType === 'scatter') {
            var data = params.data;
            if (Array.isArray(data) && data.length >= 3) return data[2];
            return params.name || null;
        }
        return params.name || null;
    }

    /**
     * 维度 → 接口查询参数
     * 只有明确支持的维度才下钻；未知维度返回空对象并提示，
     * 避免把任意字符串当成字段名拼进请求（那会变成一次静默的无效查询）。
     */
    var DIMENSION_PARAM_MAP = {
        '渠道': 'channels',
        '渠道名称': 'channels',
        '品类': 'categories',
        '品类名称': 'categories',
        '客户类型': 'customer_types',
        '订单类型': 'order_types',
        '门店': 'stores',
        '客群': 'segments',
        '期间': 'period',
        '月份': 'period'
    };

    function buildDrillParams(dimension, value) {
        var key = DIMENSION_PARAM_MAP[dimension];
        var params = {};
        if (key) {
            params[key] = value;
        } else {
            params.q = value;
        }
        // 继承当前页面的筛选条件（年份等），保证下钻与图表口径一致
        var current = Utils.getParams();
        ['period', 'years', 'date_from', 'date_to', 'channels', 'categories',
            'customer_types', 'order_types', 'stores', 'segments'].forEach(function (name) {
            if (current[name] && !params[name]) params[name] = current[name];
        });
        return params;
    }

    /* ---- 抽屉 ---- */
    function ensureDrawer() {
        var drawer = document.getElementById('drillDrawer');
        if (drawer) return drawer;

        drawer = document.createElement('aside');
        drawer.id = 'drillDrawer';
        drawer.className = 'drawer';
        drawer.setAttribute('role', 'dialog');
        drawer.setAttribute('aria-modal', 'true');
        drawer.setAttribute('aria-labelledby', 'drillDrawerTitle');
        drawer.innerHTML =
            '<div class="drawer__head">' +
            '  <span class="drawer__title" id="drillDrawerTitle">下钻明细</span>' +
            '  <button type="button" class="drawer__close" data-drawer-close aria-label="关闭抽屉">✕</button>' +
            '</div>' +
            '<div class="drawer__body" id="drillDrawerBody"></div>' +
            '<div class="drawer__meta" id="drillDrawerMeta"></div>';
        document.body.appendChild(drawer);

        var backdrop = document.createElement('div');
        backdrop.className = 'drawer-backdrop';
        backdrop.id = 'drillDrawerBackdrop';
        document.body.appendChild(backdrop);

        Utils.$$('[data-drawer-close]').forEach(function (btn) {
            btn.addEventListener('click', closeDrawer);
        });
        backdrop.addEventListener('click', closeDrawer);
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') closeDrawer();
        });

        return drawer;
    }

    function openDrillDrawer(config) {
        var drawer = ensureDrawer();
        var body = document.getElementById('drillDrawerBody');
        var meta = document.getElementById('drillDrawerMeta');
        var title = document.getElementById('drillDrawerTitle');
        var backdrop = document.getElementById('drillDrawerBackdrop');

        title.textContent = config.title || '下钻明细';
        body.innerHTML = '<div class="empty-state"><div class="empty-state__title">正在加载…</div></div>';
        meta.textContent = '';
        drawer.classList.add('is-open');
        if (backdrop) backdrop.classList.add('is-open');
        drawer.setAttribute('aria-hidden', 'false');

        var url = config.api + '?' + Utils.buildQuery(config.params);
        Utils.fetchJson(url)
            .then(function (payload) {
                if (!payload || payload.code !== 0) {
                    var message = (payload && payload.message) || '接口返回异常';
                    body.innerHTML = emptyState('无法加载明细', message);
                    return;
                }
                var data = payload.data || {};
                var items = data.items || [];
                if (!items.length) {
                    body.innerHTML = emptyState('没有符合条件的明细', '可以放宽筛选条件后重试。');
                } else {
                    body.innerHTML = renderDrillTable(items);
                }
                var version = (payload.meta && payload.meta.data_version) || '—';
                var total = data.total === undefined ? items.length : data.total;
                meta.textContent = '共 ' + total + ' 条 · 数据版本 ' + version +
                    ' · 口径与看板一致（均取自同一取数层）';
            })
            .catch(function (err) {
                body.innerHTML = emptyState('明细加载失败', String(err && err.message ? err.message : err));
            });
    }

    function closeDrawer() {
        var drawer = document.getElementById('drillDrawer');
        var backdrop = document.getElementById('drillDrawerBackdrop');
        if (drawer) {
            drawer.classList.remove('is-open');
            drawer.setAttribute('aria-hidden', 'true');
        }
        if (backdrop) backdrop.classList.remove('is-open');
    }

    DSH.openDrillDrawer = openDrillDrawer;
    DSH.closeDrawer = closeDrawer;

    function emptyState(title, hint) {
        return '<div class="empty-state">' +
            '<div class="empty-state__icon" aria-hidden="true">📭</div>' +
            '<div class="empty-state__title">' + Utils.escapeHtml(title) + '</div>' +
            '<div class="empty-state__hint">' + Utils.escapeHtml(hint || '') + '</div>' +
            '</div>';
    }

    /** 下钻明细表格：列与 /api/v1/orders/ 的字段一一对应 */
    var DRILL_COLUMNS = [
        { key: 'order_no', label: '订单号' },
        { key: 'order_date', label: '下单日期' },
        { key: 'customer_name', label: '客户' },
        { key: 'channel', label: '渠道' },
        { key: 'dominant_category', label: '品类' },
        { key: 'store_name', label: '门店' },
        { key: 'sales_rep_name', label: '业务员' },
        { key: 'final_amount', label: '成交额', numeric: true, money: true },
        { key: 'order_status', label: '状态' },
        { key: 'lead_days', label: '交付周期(天)', numeric: true }
    ];

    function renderDrillTable(items) {
        var head = DRILL_COLUMNS.map(function (col) {
            return '<th' + (col.numeric ? ' class="is-numeric"' : '') + '>' + Utils.escapeHtml(col.label) + '</th>';
        }).join('');

        var rows = items.map(function (row) {
            var cells = DRILL_COLUMNS.map(function (col) {
                var value = row[col.key];
                var text = col.money ? Utils.formatMoney(value) : Utils.formatNumber(value, 0);
                if (!col.money && !col.numeric) {
                    text = value === null || value === undefined || value === '' ? '—' : Utils.escapeHtml(value);
                }
                return '<td' + (col.numeric ? ' class="is-numeric"' : '') + '>' + text + '</td>';
            }).join('');
            return '<tr>' + cells + '</tr>';
        }).join('');

        return '<div class="table-wrap"><table class="data-table">' +
            '<caption class="sr-only">下钻明细：按当前图表维度筛选出的订单记录</caption>' +
            '<thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody></table></div>';
    }

    /* ------------------------------------------------------------------------
     * 4. 筛选条：变更即提交 + 多选标签 + URL 同步 + 清空
     * ---------------------------------------------------------------------- */
    function formToParams(form) {
        var params = {};
        var formData = new window.FormData(form);
        formData.forEach(function (value, key) {
            if (value === '' || value === null || value === undefined) return;
            if (key === 'page') return; // 改条件后回到第一页
            if (params[key] === undefined) {
                params[key] = value;
                return;
            }
            params[key] = params[key] + ',' + value;
        });
        // 合并多选项的重复值（同一 key 出现多次时逗号拼接）
        Object.keys(params).forEach(function (key) {
            var seen = {};
            params[key] = String(params[key]).split(',').filter(function (item) {
                if (seen[item]) return false;
                seen[item] = true;
                return item !== '';
            }).join(',');
        });
        return params;
    }

    function initFilterBar() {
        var form = document.getElementById('filterForm');
        if (!form) return;

        var autoSubmit = Utils.debounce(function () {
            submitFilters(form);
        }, 400);

        Utils.$$('select, input[type="text"], input[type="date"], input[type="number"]', form).forEach(function (field) {
            field.addEventListener('change', autoSubmit);
            // 文本输入用 input 事件 + 更长防抖，避免每敲一个字符发一次请求
            if (field.type === 'text' || field.type === 'number') {
                field.addEventListener('input', Utils.debounce(function () {
                    if (field.value.length >= 2 || field.value === '') autoSubmit();
                }, 700));
            }
        });

        // 多选（复选框组）：变化后立即提交
        Utils.$$('input[type="checkbox"][data-filter-field]', form).forEach(function (box) {
            box.addEventListener('change', function () {
                syncMultiSelectInput(form, box.getAttribute('data-filter-field'));
                autoSubmit();
            });
        });

        // 清空筛选
        Utils.$$('[data-filter-clear]', form).forEach(function (btn) {
            btn.addEventListener('click', function (event) {
                event.preventDefault();
                clearFilters(form);
            });
        });

        // 已选标签上的 ✕：取消该条件后重新提交
        Utils.$$('[data-filter-remove]', form.parentNode || document).forEach(function (btn) {
            btn.addEventListener('click', function (event) {
                event.preventDefault();
                var field = btn.getAttribute('data-filter-remove');
                var value = btn.getAttribute('data-filter-value');
                removeFilterValue(form, field, value);
                submitFilters(form);
            });
        });

        // 多选下拉面板开关
        Utils.$$('.multiselect', form).forEach(function (box) {
            var trigger = Utils.one('.multiselect__trigger', box);
            if (!trigger) return;
            trigger.addEventListener('click', function (event) {
                event.preventDefault();
                var open = box.classList.toggle('is-open');
                trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
            });
            document.addEventListener('click', function (event) {
                if (!box.contains(event.target)) {
                    box.classList.remove('is-open');
                    trigger.setAttribute('aria-expanded', 'false');
                }
            });
        });
    }

    /** 复选框组 → 隐藏 input（逗号串） */
    function syncMultiSelectInput(form, field) {
        var hidden = form.querySelector('input[type="hidden"][name="' + field + '"]');
        var boxes = Utils.$$('input[type="checkbox"][data-filter-field="' + field + '"]', form);
        var values = boxes.filter(function (box) { return box.checked; }).map(function (box) { return box.value; });
        if (hidden) hidden.value = values.join(',');
    }

    /** 提交筛选：更新 URL（replaceState，不污染后退历史）后整页重新渲染 */
    function submitFilters(form) {
        var params = formToParams(form);
        var query = Utils.buildQuery(params);
        var url = window.location.pathname + (query ? '?' + query : '');
        try {
            window.history.replaceState(null, '', url);
        } catch (err) {
            /* file:// 或受限环境下 replaceState 可能失败，忽略即可 */
        }
        form.submit();
    }

    function clearFilters(form) {
        Utils.$$('input, select', form).forEach(function (field) {
            if (field.type === 'checkbox' || field.type === 'radio') {
                field.checked = false;
            } else if (field.type === 'hidden' && field.getAttribute('data-filter-field')) {
                field.value = '';
            } else if (field.tagName === 'SELECT') {
                field.selectedIndex = 0;
            } else {
                field.value = '';
            }
        });
        try {
            window.history.replaceState(null, '', window.location.pathname);
        } catch (err) {
            /* 同上 */
        }
        form.submit();
    }

    function removeFilterValue(form, field, value) {
        var multi = Utils.$$('input[type="checkbox"][data-filter-field="' + field + '"]', form);
        if (multi.length) {
            multi.forEach(function (box) {
                if (box.value === value) box.checked = false;
            });
            syncMultiSelectInput(form, field);
            return;
        }
        var single = form.querySelector('[name="' + field + '"]');
        if (single) single.value = '';
    }

    /* ------------------------------------------------------------------------
     * 5. 侧边栏汉堡菜单 / Tab / 手风琴
     * ---------------------------------------------------------------------- */
    function initShell() {
        var sidebar = document.getElementById('appSidebar');
        var toggle = document.getElementById('sidebarToggle');
        if (sidebar && toggle) {
            toggle.addEventListener('click', function () {
                var open = sidebar.classList.toggle('is-open');
                toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            });
            document.addEventListener('click', function (event) {
                if (window.innerWidth >= 768) return;
                if (sidebar.contains(event.target) || toggle.contains(event.target)) return;
                sidebar.classList.remove('is-open');
                toggle.setAttribute('aria-expanded', 'false');
            });
        }
    }

    function initTabs() {
        Utils.$$('[data-tabs]').forEach(function (group) {
            var buttons = Utils.$$('[data-tab-target]', group);
            buttons.forEach(function (button) {
                button.addEventListener('click', function () {
                    var target = button.getAttribute('data-tab-target');
                    buttons.forEach(function (item) {
                        item.classList.toggle('is-active', item === button);
                        item.setAttribute('aria-selected', item === button ? 'true' : 'false');
                    });
                    var container = document.querySelector('[data-tab-panels="' + target + '"]') ||
                        document.getElementById(target);
                    if (!container) return;
                    Utils.$$('.tab-panel', container).forEach(function (panel) {
                        panel.classList.toggle('is-active', panel.id === target);
                    });
                    // Tab 切换后图表容器尺寸可能变化，主动 resize
                    Object.keys(chartRegistry).forEach(function (id) {
                        var chart = chartRegistry[id];
                        if (chart && !chart.isDisposed()) chart.resize();
                    });
                });
            });
        });
    }

    function initAccordion() {
        Utils.$$('[data-accordion-head]').forEach(function (head) {
            head.addEventListener('click', function () {
                var expanded = head.getAttribute('aria-expanded') === 'true';
                head.setAttribute('aria-expanded', expanded ? 'false' : 'true');
                var panel = document.getElementById(head.getAttribute('aria-controls'));
                if (panel) panel.classList.toggle('is-open', !expanded);
            });
        });
    }

    /* ------------------------------------------------------------------------
     * 6. 决策模拟器：滑块数值联动
     * ---------------------------------------------------------------------- */
    function initSimulator() {
        Utils.$$('.slider-row__range').forEach(function (input) {
            var output = input.parentNode.querySelector('.slider-row__value');
            var sync = function () {
                if (output) output.textContent = input.value + (input.getAttribute('data-unit') || '');
            };
            input.addEventListener('input', sync);
            sync();
        });
    }

    /* ------------------------------------------------------------------------
     * 7. 深色模式切换时重建图表（颜色取自 CSS 变量，不重建会残留旧色）
     * ---------------------------------------------------------------------- */
    function watchColorScheme() {
        if (typeof window.matchMedia !== 'function') return;
        var query = window.matchMedia('(prefers-color-scheme: dark)');
        var handler = Utils.debounce(function () {
            renderAllCharts();
        }, 200);
        if (typeof query.addEventListener === 'function') {
            query.addEventListener('change', handler);
        } else if (typeof query.addListener === 'function') {
            query.addListener(handler);
        }
    }

    /* ------------------------------------------------------------------------
     * 8. 启动
     * ---------------------------------------------------------------------- */
    function boot() {
        initShell();
        initFilterBar();
        initTabs();
        initAccordion();
        initSimulator();
        renderAllCharts();
        observeResize();
        watchColorScheme();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
