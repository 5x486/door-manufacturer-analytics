/* ==========================================================================
   杭博门业经营分析系统 · 动效编排（motion.js）
   ==========================================================================
   职责只有四件，全部是「增强」：任何一件失败，页面照常可用。

   1. 入场编排：把首屏与滚动到视口的区块依次唤起（is-in + 逐级延迟）；
   2. 顶栏状态：滚动后给 .topbar 加 is-scrolled（毛玻璃 + 软阴影）；
   3. 指针光晕：把鼠标位置写进卡片的 --mx / --my，CSS 负责画光；
   4. 兜底：motion-init 若已加上却迟迟没跑完，1.6s 后强制显示全部内容。

   为什么入场不用纯 CSS 滚动时间线：
   · 首屏需要「编舞」而不是「跟随滚动进度」，IntersectionObserver 能精确控制
     「进入视口多少才触发」以及同组元素的先后顺序；
   · 滚动时间线在长页面里会让区块的透明度一直跟着滚动条抖动，
     对数据看板来说反而分心。

   兼容性：IntersectionObserver 不可用时直接显示全部元素，不做动画。
   ========================================================================== */
(function () {
  'use strict';

  var root = document.documentElement;

  /** 减少动效偏好：运行期也可能被用户改，所以保存 mql 以便监听变化 */
  var motionQuery = null;
  try {
    motionQuery = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  } catch (e) { motionQuery = null; }
  var reduced = !!(motionQuery && motionQuery.matches);

  /** 与 motion-layer.css 里的隐藏选择器保持一致：改一处必须改两处 */
  var REVEAL_SELECTOR = [
    '.page-head',
    '.filter-bar',
    '.kpi-grid > .kpi',
    '.grid > .card',
    '.grid > .section',
    '.grid > .callout',
    '.grid > .grid > .card',
    '.grid > .grid > .section',
    '.bg-page > .card'
  ].join(',');
  /* ------------------------------------------------------------------------
     1. 入场编排
     ------------------------------------------------------------------------
     同一组元素（例如一排 KPI 卡）按 DOM 顺序逐级延迟 52ms，形成「依次亮起」；
     不同组的延迟互相独立，因此每张卡片都从 0 开始，不会越往下越慢。
     单张卡片最多等 6 档（约 312ms），避免大表格前面的卡片等太久。
     ------------------------------------------------------------------------ */
  function revealAll() {
    var nodes = document.querySelectorAll(REVEAL_SELECTOR);
    if (!nodes.length) return;

    var reducedNow = isReduced();
    var observer = null;

    /* 节奏从 CSS token 读，保证「改一处生效」；读不到就退回 52ms */
    var stagger = 52;
    try {
      var token = parseFloat(window.getComputedStyle(root).getPropertyValue('--motion-stagger'));
      if (token > 0) stagger = token;
    } catch (e) { /* 用默认值 */ }

    if (!reducedNow && typeof window.IntersectionObserver === 'function') {
      observer = new window.IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          entry.target.classList.add('is-in');
          observer.unobserve(entry.target);
        });
      }, {
        /* 只要露出 1% 就触发。曾经用「越过多余 10% 才触发」，结果屏幕底部
           刚探头的卡片会留一条空白，看起来像少渲染了一块；阈值放到 1%
           之后，任何进入视口的内容都会立刻开始淡入。 */
        rootMargin: '0px 0px 0px 0px',
        threshold: 0.01
      });
    }

    Array.prototype.forEach.call(nodes, function (el) {
      if (el.classList.contains('motion-reveal-done')) return;
      el.classList.add('motion-reveal-done');

      var order = siblingOrder(el, nodes);
      /* 延迟必须写在元素上，而不是写死在 CSS 里：同一组卡片共用一条规则 */
      el.style.setProperty('--reveal-delay', Math.min(order, 6) * stagger + 'ms');

      if (reducedNow || !observer) {
        el.classList.add('is-in');
        return;
      }
      observer.observe(el);
    });

    root.setAttribute('data-motion', 'ready');
  }

  /** 在「同一父节点下、同样属于编舞目标」的兄弟中排第几 */
  function siblingOrder(el, all) {
    var parent = el.parentNode;
    if (!parent) return 0;
    var index = 0;
    for (var i = 0; i < parent.children.length; i++) {
      var child = parent.children[i];
      if (child === el) return index;
      if (child.nodeType === 1 && child.matches && child.matches(REVEAL_SELECTOR)) index++;
    }
    return index;
  }

  function isReduced() {
    if (motionQuery) return motionQuery.matches;
    return reduced;
  }

  /** 用户中途打开「减少动效」时，立刻把还在等 IO 的元素放出来 */
  function onMotionPreferenceChange() {
    if (!isReduced()) return;
    Array.prototype.forEach.call(document.querySelectorAll(REVEAL_SELECTOR), function (el) {
      el.classList.add('is-in');
    });
  }

  /* ------------------------------------------------------------------------
     2. 顶栏：滚动过 8px 后进入「浮起」状态
     ------------------------------------------------------------------------
     scroll 事件里只做 class 切换，真正的重绘交给 rAF，避免滚动时抖动。
     ------------------------------------------------------------------------ */
  function bindTopbar() {
    var topbar = document.querySelector('.topbar');
    if (!topbar) return;

    var ticking = false;
    var apply = function () {
      topbar.classList.toggle('is-scrolled', window.scrollY > 8);
      ticking = false;
    };
    var onScroll = function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(apply);
    };

    window.addEventListener('scroll', onScroll, { passive: true });
    apply();
  }

  /* ------------------------------------------------------------------------
     3. 指针光晕：卡片跟随鼠标的柔光
     ------------------------------------------------------------------------
     只在精确指针设备上开启；用 document 级 pointermove 委托，
     所以数据表格、图表这些后插入的元素也能立即获得光晕，不需要重新绑定。
     ------------------------------------------------------------------------ */
  function bindSpotlight() {
    var finePointer = false;
    try {
      finePointer = window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches;
    } catch (e) { finePointer = false; }
    if (!finePointer || isReduced()) return;

    var ticking = false;
    var lastEvent = null;

    function paint() {
      ticking = false;
      var ev = lastEvent;
      if (!ev || !ev.target || !ev.target.closest) return;
      var card = ev.target.closest('.kpi, .card');
      if (!card) return;
      var rect = card.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      card.style.setProperty('--mx', ((ev.clientX - rect.left) / rect.width * 100).toFixed(2) + '%');
      card.style.setProperty('--my', ((ev.clientY - rect.top) / rect.height * 100).toFixed(2) + '%');
    }

    document.addEventListener('pointermove', function (ev) {
      /* 触控笔与鼠标之外的指针不画光晕 */
      if (ev.pointerType && ev.pointerType !== 'mouse' && ev.pointerType !== 'pen') return;
      lastEvent = ev;
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(paint);
    }, { passive: true });
  }

  /* ------------------------------------------------------------------------
     4. 启动与兜底
     ------------------------------------------------------------------------ */
  function start() {
    /* 标记「JS 已经接管」，<head> 里的兜底计时器看到它就不再强制显示 */
    window.__dshMotionReady = true;
    if (window.__dshMotionTimer) {
      window.clearTimeout(window.__dshMotionTimer);
      window.__dshMotionTimer = null;
    }

    /* 兜底已经触发过（JS 迟到了）：直接把内容放出来，不再补播动画。
       顺序很重要，先加 is-in 再撤 fallback，否则会闪一下隐藏态。 */
    var skipReveal = isReduced() || root.classList.contains('motion-fallback');
    if (skipReveal) {
      Array.prototype.forEach.call(document.querySelectorAll(REVEAL_SELECTOR), function (el) {
        el.classList.add('is-in');
      });
      root.classList.remove('motion-init');
      root.classList.remove('motion-fallback');
      bindTopbar();
      return;
    }
    root.classList.remove('motion-fallback');

    /* 跨页转场进来的页面：入场编排交给转场本身，这里不重播，
       否则新页面的快照会停在「还没有淡入」的状态，转场看起来像闪白。 */
    if (!root.classList.contains('vt-nav')) {
      revealAll();
    } else {
      root.setAttribute('data-motion', 'transition');
    }

    bindTopbar();
    bindSpotlight();

    if (motionQuery && typeof motionQuery.addEventListener === 'function') {
      motionQuery.addEventListener('change', onMotionPreferenceChange);
    } else if (motionQuery && typeof motionQuery.addListener === 'function') {
      motionQuery.addListener(onMotionPreferenceChange);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }

  /* 从往返缓存（bfcache）恢复时，页面已经是「显示过」的状态，
     不需要重播入场；这里只把兜底类清掉，避免标题等元素被再次动画。 */
  window.addEventListener('pageshow', function (ev) {
    if (!ev.persisted) return;
    root.classList.remove('motion-init');
  });
})();
