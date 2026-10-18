# -*- coding: utf-8 -*-
"""
杭博门业 · 经营分析交互看板（Streamlit）
==========================================
苹果官网质感：近黑 + 单点金、大留白、双层卡（Double-Bezel）、克制动效。
新增交互：分段控件切换指标/视图/排序、明细展开、更多筛选。
跟随系统明暗（浅色=苹果近白，深色=苹果近黑）。

启动：
    python -m streamlit run dashboard/app.py   （用项目 venv 的 python）
"""
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

PROJECT = Path(__file__).resolve().parent.parent
TODAY = pd.Timestamp("2026-08-09")

# ---------------------------------------------------------------------------
# 品牌与配色（单一强调色·金；可浅深通用）
# ---------------------------------------------------------------------------
COPPER = "#D9A13C"                       # 品牌金·强调
GOLD = "#C98A3C"
GOLD_SEQ = ["#F1DFB4", "#D9A13C", "#9C6B2E"]   # 金·顺序色（浅→深）
SERIES = ["#D9A13C", "#8FA8C9", "#C88D7A", "#8FB58F",
          "#B39BBF", "#C9B78F", "#7FA8A0", "#A9A59B"]
INK = "#8E8E93"                           # 图表中性墨（浅深通用，接近苹果系统灰）
GRID = "rgba(128,128,130,0.14)"


def style_fig(fig, height=360):
    """统一图表样式：透明底、中性墨、弱网格，浅深主题通用（保留图表标题）。"""
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK, size=12),
        margin=dict(l=8, r=8, t=46, b=8), height=height,
        legend=dict(orientation="h", y=1.12, x=0, font=dict(color=INK, size=11)),
        title=dict(font=dict(color=INK, size=14), x=0, xanchor="left"),
        hoverlabel=dict(bgcolor="#2A2A30", font=dict(color="#F5F5F2", size=12)),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID)
    return fig


def metric_scale(metric):
    """指标 → (缩放除数, y 轴后缀)。"""
    return {"成交额": (1e4, " 万"), "订单量": (1, " 单"), "客单价": (1, " 元")}[metric]


st.set_page_config(page_title="杭博门业 · 经营分析看板", page_icon="🚪", layout="wide")

# ---------------------------------------------------------------------------
# 样式：苹果质感（近黑/近白双层卡、大留白、克制动效）
# ---------------------------------------------------------------------------
st.markdown("""
<style>
:root {
  --page:#F5F5F7; --shell:rgba(0,0,0,.035); --card:#FFFFFF; --card2:#F0F0F2;
  --ink:#1D1D1F; --muted:#6E6E73; --line:rgba(0,0,0,.09);
  --gold-fill:#B0703C; --gold:#9C5B2B; --shadow:0 12px 34px rgba(0,0,0,.06);
}
html, body, .stApp {
  font-family:"SF Pro Text","SF Pro Display",-apple-system,"Segoe UI",
    "PingFang SC","Microsoft YaHei",sans-serif;
  font-feature-settings:"tnum" 1;
}
.block-container { padding-top:2.6rem; padding-bottom:5rem; max-width:1180px; }
.stApp, [data-testid="stAppViewContainer"] { background:var(--page); }
[data-testid="stHeader"] { background:transparent; }

/* ---- 顶部：eyebrow + 标题 + 元信息（纯排版，苹果式） ---- */
.eyebrow { font-size:11px; letter-spacing:.22em; text-transform:uppercase;
  font-weight:700; color:var(--gold); margin-bottom:10px; }
.hd { display:flex; justify-content:space-between; align-items:flex-end; gap:28px;
  padding:2px 2px 26px; border-bottom:1px solid var(--line); margin-bottom:28px; }
.hd-brand .t { font-size:30px; font-weight:800; color:var(--ink); letter-spacing:-.01em; line-height:1.1; }
.hd-brand small { color:var(--muted); font-weight:600; margin-left:10px; font-size:14px; }
.hd-title { margin-top:10px; color:var(--muted); font-size:14.5px; max-width:640px; line-height:1.6; }
.hd-meta { text-align:right; color:var(--muted); font-size:12.5px; line-height:1.9; white-space:nowrap; }
.hd-meta b { color:var(--gold); font-weight:700; }

/* ---- KPI 卡片：双层嵌套（Double-Bezel） ---- */
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(196px,1fr)); gap:16px;
  margin:4px 0 10px; }
.kpi-shell { padding:7px; border-radius:22px; background:var(--shell);
  border:1px solid var(--line);
  transition:transform .55s cubic-bezier(.32,.72,0,1), border-color .35s ease, box-shadow .55s ease; }
.kpi { position:relative; background:var(--card); height:100%;
  border-radius:calc(22px - 7px); padding:20px 22px 18px; overflow:hidden;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.5), var(--shadow); }
.kpi .kl { color:var(--muted); font-size:12.5px; font-weight:600; letter-spacing:.06em; }
.kpi .kv { color:var(--ink); font-size:27px; font-weight:700; margin-top:8px;
  letter-spacing:-.015em; font-variant-numeric:tabular-nums; }
.kpi .ks { color:var(--muted); font-size:11.5px; margin-top:5px; }
.kpi-shell:hover { transform:translateY(-3px); border-color:var(--gold-fill); box-shadow:var(--shadow); }
.kpi-shell.lead .kpi { box-shadow:inset 0 2px 0 var(--gold-fill); }
.kpi-shell.lead .kv { color:var(--gold); }

/* ---- 分段控件 / Toggle / Expander（苹果式） ---- */
[data-testid="stSegmentedControl"] { background:var(--card2); border:1px solid var(--line);
  border-radius:999px; padding:3px; }
[data-testid="stSegmentedControl"] button { border:none; background:transparent; color:var(--muted);
  font-size:13px; font-weight:600; border-radius:999px; padding:6px 16px;
  transition:all .35s cubic-bezier(.32,.72,0,1); }
[data-testid="stSegmentedControl"] button[aria-checked="true"] { background:var(--card);
  color:var(--ink); box-shadow:0 1px 4px rgba(0,0,0,.14); }
[data-testid="stExpander"] { border:1px solid var(--line); border-radius:16px; overflow:hidden;
  background:var(--card); box-shadow:var(--shadow); }
[data-testid="stExpander"] summary { font-weight:600; color:var(--ink); }
[data-testid="stExpander"] [data-testid="stMarkdownContainer"], [data-testid="stExpander"] .stCaption { color:var(--muted); }

/* ---- Tabs：文字 + 金下划线 ---- */
[data-testid="stTabs"] [data-baseweb="tab-list"] { gap:30px; border-bottom:1px solid var(--line);
  margin-bottom:20px; }
[data-testid="stTabs"] button[data-baseweb="tab"] { padding:10px 2px; color:var(--muted);
  font-weight:600; font-size:14px; background:transparent; border-radius:0;
  transition:color .3s ease; }
[data-testid="stTabs"] button[data-baseweb="tab"]:hover { color:var(--ink); }
[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] { color:var(--ink); }
[data-testid="stTabs"] [data-baseweb="tab-highlight"] { background-color:var(--gold-fill); }

/* ---- 侧边栏 ---- */
[data-testid="stSidebar"] { background:var(--page); border-right:1px solid var(--line); }
[data-testid="stSidebar"] > div { padding-top:2.2rem; }
.sb-brand { display:flex; align-items:center; gap:10px; margin:2px 0 6px; }
.sb-brand .a { font-size:16px; font-weight:800; color:var(--ink); letter-spacing:-.01em; }
.sb-note { color:var(--muted); font-size:12px; line-height:1.7; margin:0 0 6px; }
.sb-label { color:var(--muted); font-size:11px; font-weight:700; letter-spacing:.16em;
  margin:16px 0 8px; text-transform:uppercase; }
[data-testid="stWidgetLabel"] { color:var(--muted); font-size:12.5px; font-weight:600; }
[data-baseweb="select"] > div { background:var(--card); border:1px solid var(--line);
  border-radius:12px; }
[data-testid="stSidebar"] .stCaption, [data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:var(--muted); }

/* ---- 表格容器 / 原生指标卡 ---- */
[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:14px; overflow:hidden;
  box-shadow:var(--shadow); }
[data-testid="stMetric"] { background:var(--card); border:1px solid var(--line); border-radius:16px;
  padding:14px 18px; box-shadow:var(--shadow); }
[data-testid="stMetricLabel"] { color:var(--muted) !important; font-size:12px; }
[data-testid="stMetricValue"] { color:var(--ink) !important; font-size:22px; font-weight:700;
  letter-spacing:-.01em; }

/* ---- 洞察 callout / 结论 ---- */
.callout { border-left:3px solid var(--gold-fill); background:var(--card); border-radius:0 14px 14px 0;
  padding:14px 18px; margin:18px 0 4px; color:var(--ink); font-size:13.5px; line-height:1.7;
  box-shadow:var(--shadow); }
.callout b { color:var(--gold); }
.concl { display:flex; flex-direction:column; gap:14px; margin-top:8px; }
.concl-i { border-left:3px solid var(--gold-fill); padding:1px 0 1px 16px; }
.concl-i b { color:var(--ink); font-size:14px; letter-spacing:-.01em; }
.concl-i div { color:var(--muted); font-size:13px; line-height:1.7; margin-top:4px; }
.sec-title { color:var(--ink); font-size:17px; font-weight:800; margin:30px 0 14px;
  letter-spacing:-.01em; }
.sec-title::after { content:""; display:block; height:1px; background:var(--line); margin-top:10px; }

/* ---- 页脚 ---- */
.dash-foot { color:var(--muted); font-size:12px; text-align:center; margin-top:46px; padding-top:18px;
  border-top:1px solid var(--line); }

/* ---- 深色模式（跟随系统） ---- */
@media (prefers-color-scheme: dark) {
  :root {
    --page:#0B0B0D; --shell:rgba(255,255,255,.045); --card:#141417; --card2:#1C1C20;
    --ink:#F5F5F2; --muted:#8E8E93; --line:rgba(255,255,255,.10);
    --gold-fill:#D9A13C; --gold:#D9A13C; --shadow:none;
  }
  .stApp, [data-testid="stAppViewContainer"] {
    background:radial-gradient(900px 420px at 85% -8%, rgba(217,161,60,.07), transparent 62%), var(--page);
  }
  .kpi { box-shadow:inset 0 1px 0 rgba(255,255,255,.04); }
  [data-testid="stAlert"] { border-color:var(--line); }
  [data-testid="stAlert"] [data-testid="stMarkdownContainer"] { color:var(--ink); }
  .stApp h1,.stApp h2,.stApp h3,.stApp p,.stApp label,.stApp span,
  .stApp .stMarkdown,.stApp .stCaption { color:var(--ink); }
  [data-testid="stSidebar"] * { color:var(--ink) !important; }
  [data-testid="stSidebar"] [data-testid="stWidgetLabel"], [data-testid="stSidebar"] .stCaption { color:var(--muted) !important; }
  [data-testid="stSegmentedControl"] button[aria-checked="true"] { background:var(--card2); color:var(--ink); box-shadow:none; }
}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
@st.cache_data
def load_data():
    orders = pd.read_csv(PROJECT / "data/processed/orders_clean.csv",
                         parse_dates=["order_date"])
    items = pd.read_csv(PROJECT / "data/processed/item_flat.csv")
    rfm = pd.read_csv(PROJECT / "data/processed/customer_rfm.csv")
    customers = pd.read_csv(PROJECT / "data/raw/customers.csv")
    after = pd.read_csv(PROJECT / "data/raw/after_sales.csv", parse_dates=["report_date"])
    return orders, items, rfm, customers, after


orders, items, rfm, customers, after = load_data()

# ---------------------------------------------------------------------------
# 侧边栏筛选
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("""
    <div class="sb-brand"><span class="hd-mark" style="width:9px;height:9px;border-radius:2px;background:#D9A13C;display:inline-block"></span><span class="a">杭博门业</span></div>
    <div class="sb-note">经营分析看板<br>浙江嘉兴桐乡 · 小型定制门厂（2023 起）</div>
    <div class="sb-label">数据筛选</div>
    """, unsafe_allow_html=True)
    years = sorted(orders["order_year"].unique().tolist())
    channels = sorted(orders["channel"].unique().tolist())
    types = sorted(orders["customer_type"].unique().tolist())
    otypes = sorted(orders["order_type"].unique().tolist())
    cats = sorted(items["category"].unique().tolist())

    sel_years = st.multiselect("年份", years, default=years)
    sel_ch = st.multiselect("来源渠道", channels, default=channels)
    sel_type = st.multiselect("客户类型", types, default=types)
    sel_otype = st.multiselect("订单类型", otypes, default=otypes)
    sel_cat = st.multiselect("品类", cats, default=cats)
    st.caption("筛选联动全部视图；品类额外作用于品类图。")

# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------
mask = (orders["order_date"].dt.year.isin(sel_years)
        & orders["channel"].isin(sel_ch)
        & orders["customer_type"].isin(sel_type)
        & orders["order_type"].isin(sel_otype))
valid = orders[mask & (orders["is_valid"] == 1)].copy()
done = valid[valid["order_status"] == "已完成"].copy()
items_f = items[items["order_id"].isin(valid["order_id"])]
if sel_cat:
    items_f = items_f[items_f["category"].isin(sel_cat)]
    valid_cat = valid[valid["dominant_category"].isin(sel_cat)]
else:
    valid_cat = valid
rfm_f = rfm[rfm["customer_id"].isin(valid["customer_id"])]
after_f = after[after["order_id"].isin(valid["order_id"])]

# ---------------------------------------------------------------------------
# KPI
# ---------------------------------------------------------------------------
gmv = valid["final_amount"].sum()
n_orders = len(valid)
aov = gmv / n_orders if n_orders else 0
n_cust = valid["customer_id"].nunique()
on_time = done["is_on_time"].mean() * 100 if len(done) else np.nan

period = f"{min(sel_years)} 年 ~ {max(sel_years)} 年"
yoy = ""
if 2025 in sel_years and 2024 in sel_years:
    g24 = valid[valid["order_year"] == 2024]["final_amount"].sum()
    g25 = valid[valid["order_year"] == 2025]["final_amount"].sum()
    if g24 > 0:
        yoy = f"2025 同比 +{(g25 / g24 - 1) * 100:.0f}%"

st.markdown(f"""
<div class="hd">
  <div>
    <div class="eyebrow">数据分析 · 经营决策</div>
    <div class="hd-brand"><span class="t">杭博门业</span><small>经营分析看板</small></div>
    <div class="hd-title">一个小厂，如何用最低的成本把单子做起来、把钱赚稳</div>
  </div>
  <div class="hd-meta">
    <div>数据范围 <b>{period}</b></div>
    <div>浙江嘉兴桐乡 · 模拟数据</div>
    {f'<div>{yoy}</div>' if yoy else ''}
  </div>
</div>
""", unsafe_allow_html=True)

kpis = [
    ("成交额", f"¥{gmv / 1e4:,.0f} 万", "合计（剔除取消）", True),
    ("有效订单", f"{n_orders:,}", "单 · 全部渠道", False),
    ("客单价", f"¥{aov:,.0f}", "成交额 / 订单数", False),
    ("成交客户", f"{n_cust:,}", "有订单的客户", False),
    ("准时交付率", f"{on_time:.1f}%" if not np.isnan(on_time) else "-", "已完成订单", False),
]
cards = "".join(
    f'<div class="kpi-shell{" lead" if lead else ""}"><div class="kpi">'
    f'<div class="kl">{k}</div><div class="kv">{v}</div><div class="ks">{s}</div></div></div>'
    for k, v, s, lead in kpis)
st.markdown(f'<div class="kpis">{cards}</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Tab 面板
# ---------------------------------------------------------------------------
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["增长与趋势", "品类结构", "渠道效能", "客户价值", "交付与售后"])

# ---------- Tab1 增长与趋势（可切换指标 / 视图） ----------
with tab1:
    c_metric, c_kind, _ = st.columns([1, 1, 2])
    with c_metric:
        metric = st.segmented_control("指标", ["成交额", "订单量", "客单价"], default="成交额")
    with c_kind:
        chart_kind = st.segmented_control("视图", ["折线", "柱状", "面积"], default="折线")

    monthly = valid.groupby(valid["order_date"].dt.to_period("M"))["final_amount"].agg(gmv="sum", n="count")
    monthly["aov"] = monthly["gmv"] / monthly["n"]
    monthly["ym"] = monthly.index.astype(str)
    scale, suffix = metric_scale(metric)
    ycol = {"成交额": "gmv", "订单量": "n", "客单价": "aov"}[metric]
    yv = monthly[ycol] / scale

    if chart_kind == "柱状":
        trace = go.Bar(x=monthly["ym"], y=yv, marker_color=COPPER, opacity=.88,
                       hovertemplate="%{y:.0f}" + suffix.strip() + "<extra></extra>")
    elif chart_kind == "面积":
        trace = go.Scatter(x=monthly["ym"], y=yv, mode="lines", line=dict(color=COPPER, width=2),
                           fill="tozeroy", fillcolor="rgba(217,161,60,0.16)",
                           hovertemplate="%{y:.0f}" + suffix.strip() + "<extra></extra>")
    else:
        trace = go.Scatter(x=monthly["ym"], y=yv, mode="lines+markers",
                           line=dict(color=COPPER, width=2.2), marker=dict(size=4, color=COPPER),
                           hovertemplate="%{y:.0f}" + suffix.strip() + "<extra></extra>")
    fig = go.Figure(trace)
    fig.update_yaxes(tickformat=".0f", ticksuffix=suffix)
    fig.update_layout(title=f"月度{metric}", hovermode="x unified")
    st.plotly_chart(style_fig(fig), width="stretch")

    yearly = valid.groupby("order_year")["final_amount"].agg(gmv="sum", n="count")
    yearly["aov"] = yearly["gmv"] / yearly["n"]
    yearly["成交额"] = yearly["gmv"] / 1e4
    yearly["订单量"] = yearly["n"]
    yearly["客单价"] = yearly["aov"]
    yearly = yearly.reset_index()
    yscale, ysuffix = metric_scale(metric)
    fig2 = px.bar(yearly, x="order_year", y=metric, text_auto=".0f",
                  color="order_year", color_discrete_sequence=[GOLD, COPPER, "#9C6B2E", "#B5763A"])
    fig2.update_layout(title=f"按年度{metric}", showlegend=False)
    fig2.update_yaxes(tickformat=".0f", ticksuffix=ysuffix)
    st.plotly_chart(style_fig(fig2, height=300), width="stretch")
    note = "2026 年为前 7 个月。" if 2026 in sel_years else ""
    st.caption(f"2023 建厂爬坡，之后逐年放量。{note}")

# ---------- Tab2 品类结构（可切换视角 / 范围） ----------
with tab2:
    if len(items_f):
        c_view, c_n, _ = st.columns([1, 1, 2])
        with c_view:
            view = st.segmented_control("视角", ["收入占比", "订单量"], default="收入占比")
        with c_n:
            topn = st.segmented_control("范围", ["全部", "前 5"], default="全部")

        if view == "收入占比":
            cat = items_f.groupby("category")["line_amount"].sum().sort_values(ascending=False)
        else:
            cat = items_f.groupby("category")["quantity"].sum().sort_values(ascending=False)
        if topn == "前 5":
            cat = cat.nlargest(5)
        share = cat / cat.sum() * 100
        cum = share.cumsum()

        fig3 = go.Figure()
        fig3.add_bar(x=cat.index, y=share.values, name="占比%",
                     marker_color=[SERIES[i % len(SERIES)] for i in range(len(cat))],
                     hovertemplate="%{x} · %{y:.1f}%<extra></extra>")
        fig3.add_trace(go.Scatter(x=cat.index, y=cum.values, name="累计%",
                                  mode="lines+markers", line=dict(color=COPPER, width=2),
                                  marker=dict(size=5, color=COPPER),
                                  hovertemplate="累计 %{y:.1f}%<extra></extra>"))
        fig3.update_layout(title=f"品类结构 · 按{view}", yaxis=dict(range=[0, 105], title="占比%"),
                           barmode="group", hovermode="x unified")
        st.plotly_chart(style_fig(fig3, height=380), width="stretch")

        aov_cat = items_f.groupby("category")["unit_price"].mean().round(0)
        tbl = pd.concat([cat.rename("规模"), share.rename("占比%"),
                         aov_cat.rename("均价")], axis=1).sort_values("占比%", ascending=False)
        tbl["占比%"] = tbl["占比%"].round(1)
        if view == "收入占比":
            tbl["规模"] = tbl["规模"].map(lambda v: f"{v / 1e4:.0f} 万")
        tbl["均价"] = tbl["均价"].map(lambda v: f"{v:,.0f} 元")
        st.dataframe(tbl, width="stretch", column_config={"占比%": st.column_config.NumberColumn(format="%.1f%%")})
        st.markdown(f'<div class="callout"><b>洞察</b>　按「{view}」看，{cat.index[0]} 居首'
                    f'（占 <b>{share.iloc[0]:.0f}%</b>），前 3 品类合计 <b>{share.head(3).sum():.0f}%</b>，集中度明显。</div>',
                    unsafe_allow_html=True)
    else:
        st.warning("当前筛选下无明细数据")

# ---------- Tab3 渠道效能（可排序 / 切换转化率） ----------
with tab3:
    c_sort, c_conv, _ = st.columns([1, 1, 2])
    with c_sort:
        sortby = st.segmented_control("排序依据", ["成交额", "订单数", "客单价"], default="成交额")
    with c_conv:
        show_conv = st.toggle("显示注册 → 成交转化率", value=True)

    ch = (valid.groupby("channel").agg(订单数=("order_id", "count"),
                                       成交额=("final_amount", "sum"),
                                       客单价=("final_amount", "mean")).reset_index())
    ch["收入占比%"] = ch["成交额"] / ch["成交额"].sum() * 100
    ch = ch.sort_values(sortby, ascending=False)

    fig4 = px.bar(ch, x="channel", y=ch["成交额"] / 1e4, text_auto=".0f", color="收入占比%",
                  color_continuous_scale=GOLD_SEQ,
                  labels={"channel": "渠道", "y": "成交额（万元）"})
    fig4.update_layout(title="各渠道成交额（万元 · 颜色=收入占比）")
    fig4.update_yaxes(tickformat=".0f", ticksuffix=" 万")
    st.plotly_chart(style_fig(fig4, height=330), width="stretch")
    ch_show = ch.copy()
    ch_show["成交额"] = ch_show["成交额"].map(lambda v: f"{v / 1e4:.0f} 万")
    ch_show["客单价"] = ch_show["客单价"].map(lambda v: f"¥{v:,.0f}")
    ch_show["收入占比%"] = ch_show["收入占比%"].round(1)
    st.dataframe(ch_show, width="stretch")

    if show_conv:
        conv = customers.groupby("channel")["is_lead"].apply(lambda s: (s == 0).mean() * 100)
        conv = conv.sort_values(ascending=False)
        fig_c = px.bar(conv.reset_index(), x="channel", y="is_lead", text_auto=".1f",
                       color="is_lead", color_continuous_scale=GOLD_SEQ,
                       labels={"channel": "渠道", "is_lead": "转化率（%）"})
        fig_c.update_layout(title="各渠道「注册 → 成交」转化率", yaxis_tickformat=".0f")
        st.plotly_chart(style_fig(fig_c, height=280), width="stretch")
        st.caption("转化率 = 有成交客户数 / 该渠道注册客户总数（含未成交线索）")

# ---------- Tab4 客户价值 ----------
with tab4:
    if len(rfm_f):
        seg = (rfm_f.groupby("segment").agg(客户数=("customer_id", "count"),
                                            累计消费=("monetary", "sum"),
                                            人均消费=("monetary", "mean")).reset_index())
        seg["收入占比%"] = seg["累计消费"] / seg["累计消费"].sum() * 100
        seg = seg.sort_values("累计消费", ascending=False)

        col_a, col_b = st.columns(2)
        with col_a:
            fig5 = px.bar(seg, x="segment", y="客户数", text_auto=True,
                          color="segment", color_discrete_sequence=SERIES[:5],
                          labels={"segment": "分群", "客户数": "客户数"})
            fig5.update_layout(title="RFM 分群 · 客户数", showlegend=False)
            st.plotly_chart(style_fig(fig5, height=310), width="stretch")
        with col_b:
            fig6 = px.pie(seg, names="segment", values="收入占比%", hole=0.45,
                          color_discrete_sequence=SERIES[:5])
            fig6.update_layout(title="RFM 分群 · 收入占比")
            fig6.update_traces(textposition="outside", textinfo="label+percent",
                               textfont=dict(color=INK, size=11))
            st.plotly_chart(style_fig(fig6, height=310), width="stretch")

        seg_show = seg.copy()
        seg_show["累计消费"] = seg_show["累计消费"].map(lambda v: f"{v / 1e4:.0f} 万")
        seg_show["人均消费"] = seg_show["人均消费"].map(lambda v: f"¥{v:,.0f}")
        seg_show["收入占比%"] = seg_show["收入占比%"].round(1)
        st.dataframe(seg_show, width="stretch")

        hv = rfm_f[rfm_f["segment"] == "高价值客户"]
        if len(hv):
            st.markdown('<div class="sec-title">高价值客户画像</div>', unsafe_allow_html=True)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("人数", f"{len(hv)}")
            m2.metric("人均消费", f"¥{hv['monetary'].mean():,.0f}")
            m3.metric("平均复购", f"{hv['frequency'].mean():.1f} 次")
            m4.metric("主要来源", hv["channel"].mode()[0])
            c1, c2 = st.columns(2)
            c1.markdown(f"**主要类型**：{dict(hv['customer_type'].value_counts(normalize=True).round(2).head(3))}")
            c2.markdown(f"**主要城市**：{dict(hv['city'].value_counts().head(5))}")
            with st.expander("查看高价值客户清单"):
                hv_tbl = hv[["customer_name", "customer_type", "channel", "city",
                             "frequency", "monetary"]].sort_values("monetary", ascending=False)
                hv_tbl["monetary"] = hv_tbl["monetary"].map(lambda v: f"¥{v:,.0f}")
                st.dataframe(hv_tbl.rename(columns={"customer_name": "客户", "customer_type": "类型",
                                                    "channel": "来源", "city": "城市",
                                                    "frequency": "下单次数", "monetary": "累计消费"}),
                             width="stretch")
    else:
        st.warning("当前筛选下无客户分群数据")

# ---------- Tab5 交付与售后 ----------
with tab5:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown('<div class="sec-title">交付</div>', unsafe_allow_html=True)
        st.metric("整体准时率", f"{on_time:.1f}%")
        st.metric("交付周期中位（天）", f"{done['actual_lead_days'].median():.0f}" if len(done) else "-")
        if len(done):
            lead_cat = done.groupby("dominant_category")["actual_lead_days"].median().sort_values()
            fig7 = px.bar(lead_cat.reset_index(), x="dominant_category", y="actual_lead_days",
                          color="actual_lead_days", color_continuous_scale=GOLD_SEQ,
                          labels={"dominant_category": "品类", "actual_lead_days": "交付周期（天）"})
            fig7.update_layout(title="各品类交付周期（中位天数）", yaxis_tickformat=".0f")
            st.plotly_chart(style_fig(fig7, height=300), width="stretch")
    with c2:
        st.markdown('<div class="sec-title">售后</div>', unsafe_allow_html=True)
        if len(after_f):
            t = after_f["type"].value_counts().reset_index()
            t.columns = ["类型", "次数"]
            fig8 = px.bar(t, x="类型", y="次数", text_auto=True,
                          color="次数", color_continuous_scale=GOLD_SEQ,
                          labels={"类型": "问题类型", "次数": "次数"})
            fig8.update_layout(title="售后问题分布", showlegend=False, yaxis_tickformat=".0f")
            st.plotly_chart(style_fig(fig8, height=240), width="stretch")
            as_cat = after_f[after_f["type"] != "退款"].groupby("product_id").size()
            dn_cat = valid_cat.groupby("dominant_category").size()
            rate = (as_cat / dn_cat.reindex(as_cat.index) * 100).sort_values(ascending=False)
            st.dataframe(pd.DataFrame({"售后率%": rate.round(1)}), width="stretch")
            with st.expander("查看售后明细"):
                aft = after_f[["report_date", "order_id", "type", "product_id",
                               "cost", "status", "handling_days"]].sort_values("report_date", ascending=False).copy()
                aft["report_date"] = aft["report_date"].dt.strftime("%Y-%m-%d")
                aft["cost"] = aft["cost"].map(lambda v: f"¥{v:,.0f}")
                st.dataframe(aft.rename(columns={"report_date": "报修日期", "order_id": "订单号",
                                                 "type": "问题类型", "product_id": "品类",
                                                 "cost": "处理成本", "status": "状态",
                                                 "handling_days": "处理天数"}),
                             width="stretch")
        else:
            st.warning("当前筛选下无售后记录")

# ---------------------------------------------------------------------------
# 结论
# ---------------------------------------------------------------------------
st.markdown('<div class="sec-title">核心结论（随筛选更新）</div>', unsafe_allow_html=True)

if n_orders and len(ch):
    top_channel = ch.iloc[0]["channel"]
    hv_share = seg[seg["segment"] == "高价值客户"]["收入占比%"].iloc[0] if len(rfm_f) and len(seg) else np.nan
    lead_cat_max = done.groupby("dominant_category")["actual_lead_days"].median().max() if len(done) else np.nan
    lead_cat_name = done.groupby("dominant_category")["actual_lead_days"].median().idxmax() if len(done) else "-"

    concl = [
        ("增长引擎：抖音 + 老客户转介",
         f"所选范围成交额 ¥{gmv / 1e4:,.0f} 万、{n_orders:,} 单。抖音与转介是零成本获客主力，"
         f"客单价最高渠道为 {ch.loc[ch['客单价'].idxmax(), 'channel']}（¥{ch['客单价'].max():,.0f}）。"),
        ("品类集中",
         f"按当前口径，收入最高品类贡献 {share.iloc[0]:.0f}%，前 3 品类合计约 {share.head(3).sum():.0f}%。"
         f"建议「不锈钢跑量 + 铜门做利润」的组合方案。"),
        ("高客单渠道",
         f"{top_channel} 是当前筛选下成交额最高的渠道（¥{ch.iloc[0]['成交额'] / 1e4:,.0f} 万），值得重点经营。"),
        ("交付短板",
         f"准时率 {on_time:.1f}%，交付周期最长的是 {lead_cat_name}（中位 {lead_cat_max:.0f} 天），需管理交付预期。"),
        ("高价值客户",
         f"高价值客户 {len(hv)} 名、贡献约 {hv_share:.0f}% 收入，人均 ¥{hv['monetary'].mean():,.0f}，重点维护并引导转介绍。"),
    ]
    items_html = "".join(f'<div class="concl-i"><b>{t}</b><div>{x}</div></div>' for t, x in concl)
    st.markdown(f'<div class="concl">{items_html}</div>', unsafe_allow_html=True)

with st.expander("查看筛选后的订单明细"):
    if n_orders:
        cols = ["order_date", "order_id", "customer_type", "channel", "order_type",
                "dominant_category", "item_count", "final_amount", "order_status"]
        detail = valid[cols].sort_values("order_date", ascending=False)
        detail["final_amount"] = detail["final_amount"].map(lambda v: f"¥{v:,.0f}")
        detail["order_date"] = detail["order_date"].dt.strftime("%Y-%m-%d")
        st.dataframe(detail.rename(columns={"order_date": "下单日期", "order_id": "订单号",
                                            "customer_type": "客户类型", "channel": "渠道",
                                            "order_type": "订单类型", "dominant_category": "主品类",
                                            "item_count": "明细数", "final_amount": "成交额",
                                            "order_status": "状态"}),
                     width="stretch")
    else:
        st.info("当前筛选下无有效订单")

st.markdown('<div class="dash-foot">数据为模拟数据，口径见 docs/data_dictionary.md</div>',
            unsafe_allow_html=True)
