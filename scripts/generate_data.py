# -*- coding: utf-8 -*-
"""
门业定制企业 ，， 模拟业务数据生成脚本
=========================================
用于数据分析求职项目：生成一套贴近现实、规模较大、字段完整的「高端定制门业企业」业务数据。

业务画像（依据公开信息校准，数据本身纯虚构）：
    浙江嘉兴桐乡的一家三人合伙、2023 年起步的小型定制门厂，主营铜门、不锈钢门、
    不锈钢铜门、铸铝门、甲级防盗门、防火门；桐乡工厂展厅 + 抖音/线上接单 + 装企工程。

合规声明：
    本脚本生成的所有数据均为程序随机模拟，不包含任何真实个人或企业的信息。
    人名、电话、地址、公司名等均为随机拼接的虚构内容。

生成范围（2023-01-01 ~ 2026-07-31，约 3.5 年）：
    products.csv        产品目录（48 个 SKU）
    stores.csv          门店/渠道（3 个）
    sales_reps.csv      业务员（6 人）
    customers.csv       客户（约 1,500 名，含未成交线索）
    orders.csv          订单主表（2,500+ 单）
    order_items.csv     订单明细（3,000+ 行）
    payments.csv        付款记录（6,000+ 条）
    after_sales.csv     售后/退换货记录（约 200 条）

用法：
    python scripts/generate_data.py
    输出到 data/raw/ 目录。
"""
import os

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
SEED = 20260809
rng = np.random.default_rng(SEED)

START = pd.Timestamp("2023-01-01")
END = pd.Timestamp("2026-07-31")
TODAY = pd.Timestamp("2026-08-09")  # 用于订单状态的“当前时间”

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(BASE_DIR, "data", "raw")
os.makedirs(RAW_DIR, exist_ok=True)


def save(df, name):
    path = os.path.join(RAW_DIR, name)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  [OK] {name:.<26s} {len(df):>7,} 行  ->  {path}")


# ---------------------------------------------------------------------------
# 1. 基础工具：中文姓名 / 公司名 / 电话
# ---------------------------------------------------------------------------
SURNAMES = list("王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石戴贾韦夏邱方侯邹熊孟秦白江阎薛尹段雷黎史龙陶贺顾毛郝龚邵万钱严覃武莫孔向汤")
MALE_SINGLE = list("伟磊军洋勇杰涛明超强斌鹏波飞凯锋东辉刚国伟建平")
FEMALE_SINGLE = list("静丽敏芳娜娟艳杰霞婷欣丹琪慧梅雪莹秀兰英华")
CLASSIC_DOUBLE = ["志强", "建国", "卫东", "春梅", "秀英", "桂芳", "建华", "国平",
                  "丽华", "文娟", "德林", "永康", "庆芳", "淑珍", "秀兰", "春华"]
MODERN_DOUBLE = ["子涵", "欣怡", "雨桐", "思远", "浩宇", "泽宇", "佳琪", "诗涵",
                 "一诺", "若曦", "梓豪", "梦琪", "宇轩", "嘉怡", "子轩", "欣悦",
                 "天佑", "梓萱", "雨辰", "语嫣", "心怡", "俊杰", "志远", "沐宸"]


def gen_name(age, gender):
    if rng.random() < 0.42:
        if age < 30:
            given = rng.choice(MODERN_DOUBLE)
        elif age < 48:
            given = rng.choice(MODERN_DOUBLE + CLASSIC_DOUBLE)
        else:
            given = rng.choice(CLASSIC_DOUBLE)
    else:
        pool = MALE_SINGLE if gender == "男" else FEMALE_SINGLE
        given = rng.choice(pool)
    return rng.choice(SURNAMES) + given


def gen_company_name():
    prefix = rng.choice(list("恒宏建金鼎中华远盛新德瑞广联嘉美尚和顺信"))
    core = rng.choice(list("艺信和邦达通泰泽诚安顺锦翔匠筑峰源拓力"))
    suffix = rng.choice(["装饰", "装修", "建筑", "建设工程", "置业", "家居",
                         "建材", "安装", "工程", "房产"])
    return f"{prefix}{core}{suffix}有限公司"


def gen_phone():
    head = rng.choice(["13", "15", "18", "17", "19"])
    tail = "".join(str(rng.integers(0, 10)) for _ in range(9))
    return f"{head}{tail}"


# ---------------------------------------------------------------------------
# 2. 城市（浙江嘉兴及长三角为主，带权重）
# ---------------------------------------------------------------------------
CITIES = []
def _add(prov, city, w):
    CITIES.append((prov, city, w))

for prov, cities in {
    # 本地核心市场（嘉兴/桐乡/海宁，约 7 成）
    "浙江": [("嘉兴", 10), ("桐乡", 9), ("海宁", 9), ("嘉善", 2), ("平湖", 1.5),
             ("杭州", 3.5), ("湖州", 1.5), ("绍兴", 1.5), ("宁波", 1.5),
             ("金华", 1), ("温州", 1), ("台州", 1), ("丽水", 0.8), ("衢州", 0.8)],
    # 周边辐射
    "江苏": [("苏州", 2), ("无锡", 1.5), ("常州", 1), ("南京", 1), ("南通", 1),
             ("扬州", 0.8), ("泰州", 0.8), ("镇江", 0.8)],
    "上海": [("上海", 2)],
    "安徽": [("合肥", 1), ("芜湖", 1), ("宣城", 1), ("马鞍山", 0.8), ("黄山", 0.6)],
    "江西": [("南昌", 0.8), ("上饶", 0.8), ("九江", 0.7), ("景德镇", 0.6)],
    "福建": [("福州", 0.8), ("厦门", 0.8), ("泉州", 0.7)],
    # 其他（少量，装企/工程辐射）
    "山东": [("青岛", 0.8), ("济南", 0.7)],
    "广东": [("广州", 0.8), ("深圳", 0.7)],
    "湖北": [("武汉", 0.8)], "湖南": [("长沙", 0.7)], "河南": [("郑州", 0.7)],
    "四川": [("成都", 0.8)], "北京": [("北京", 0.8)],
}.items():
    for c, w in cities:
        _add(prov, c, w)

CITY_DF = pd.DataFrame(CITIES, columns=["province", "city", "w"])
CITY_WEIGHTS = CITY_DF["w"].values / CITY_DF["w"].sum()

ZONES = ["中心", "城东", "城西", "城南", "城北", "开发区", "老城", "滨江新区"]
ROADS = ["中山", "人民", "建设", "解放", "朝阳", "长江", "和平", "新华",
         "胜利", "复兴", "文化", "东湖", "望江", "天宁", "锦绣"]
TOWNS = ["崇福", "濮院", "屠甸", "洲泉", "大麻", "石门", "河山", "乌镇",
         "长安", "许村", "盐官", "斜桥", "周王庙", "袁花", "丁桥"]


def gen_address(prov, city):
    road = rng.choice(ROADS)
    num = rng.integers(1, 200)
    if city in ("桐乡", "海宁"):
        town = rng.choice(TOWNS)
        return f"{city}市{town}镇{road}路{num}号"
    zone = rng.choice(ZONES)
    return f"{prov}省{city}市{zone}{road}路{num}号"


def sample_city(n):
    idx = rng.choice(len(CITY_DF), size=n, p=CITY_WEIGHTS)
    row = CITY_DF.iloc[idx]
    return row["province"].values, row["city"].values


# ---------------------------------------------------------------------------
# 3. 带季节 + 增长趋势的日期采样
# ---------------------------------------------------------------------------
MONTH_WEIGHT = {1: 0.55, 2: 0.50, 3: 1.15, 4: 1.20, 5: 1.10, 6: 0.95,
                7: 0.90, 8: 0.90, 9: 1.15, 10: 1.20, 11: 1.05, 12: 1.00}
YEAR_WEIGHT = {2023: 0.55, 2024: 1.00, 2025: 1.35, 2026: 1.55}

_day_pool = pd.date_range(START, END, freq="D")
_day_w = np.array([MONTH_WEIGHT[d.month] * YEAR_WEIGHT[d.year] for d in _day_pool])
_day_w = _day_w / _day_w.sum()


def sample_order_dates(n):
    idx = rng.choice(len(_day_pool), size=n, p=_day_w)
    return pd.to_datetime(pd.Series(_day_pool[idx].to_numpy()))


# ---------------------------------------------------------------------------
# 4. 产品目录（铜门 / 不锈钢门 / 铜艺门 / 防火门等）
# ---------------------------------------------------------------------------
PRODUCT_TYPES = [
    dict(type="铜门", mats=["紫铜", "黄铜", "铜合金"], surf=["氟碳漆", "铜雕工艺", "拉丝铜", "仿古做旧"],
         base=(5000, 16000), lead=(45, 70), std=(1100, 2200), thick=(70, 90),
         defect=0.060, styles=["新中式", "欧式", "轻奢", "简约"]),
    dict(type="不锈钢门", mats=["304不锈钢", "201不锈钢"], surf=["拉丝", "烤漆", "真空镀钛"],
         base=(3000, 15000), lead=(20, 40), std=(1000, 2200), thick=(60, 80),
         defect=0.045, styles=["现代", "简约", "轻奢"]),
    dict(type="不锈钢铜门", mats=["304不锈钢", "铜合金"], surf=["镀铜", "铜雕", "仿铜"],
         base=(5000, 15000), lead=(30, 50), std=(1050, 2200), thick=(65, 85),
         defect=0.050, styles=["新中式", "欧式", "轻奢"]),
    dict(type="铸铝门", mats=["铸铝", "铝镁合金"], surf=["粉末喷涂", "铸铝雕花"],
         base=(4000, 15000), lead=(35, 55), std=(1000, 2200), thick=(70, 90),
         defect=0.055, styles=["欧式", "新中式", "现代"]),
    dict(type="甲级防盗门", mats=["镀锌钢板", "冷轧钢板"], surf=["氟碳漆", "防指纹"],
         base=(2000, 6000), lead=(15, 30), std=(960, 2050), thick=(70, 95),
         defect=0.035, styles=["现代", "简约"]),
    dict(type="防火门", mats=["钢质防火", "木质防火"], surf=["防火漆", "饰面防火板"],
         base=(1000, 4500), lead=(15, 30), std=(1000, 2100), thick=(50, 70),
         defect=0.030, styles=["甲级", "乙级", "工程款"]),
]

FEATURES = [("标准", 1.0), ("静音", 1.05), ("带可视猫眼", 1.08), ("智能锁配套", 1.10)]


def _product_name(cat, material, style, feature):
    if cat == "铜门":
        n = f"{style}{material}门"
    elif cat == "不锈钢门":
        n = f"{style}{material}门"
    elif cat == "不锈钢铜门":
        n = f"{style}不锈钢铜门"
    elif cat == "铸铝门":
        n = f"{style}{material}门"
    elif cat == "甲级防盗门":
        n = f"{style}甲级防盗门"
    else:  # 防火门，style 即甲级/乙级/工程款
        n = f"{style}防火门"
    return n + (f"·{feature}" if feature != "标准" else "")


def build_products():
    rows, pid = [], 0
    for spec in PRODUCT_TYPES:
        n_variants = 8
        for _ in range(n_variants):
            pid += 1
            material = rng.choice(spec["mats"])
            surface = rng.choice(spec["surf"])
            style = rng.choice(spec["styles"])
            feature, feat_mult = FEATURES[rng.integers(0, len(FEATURES))]
            base_price = int(round(rng.uniform(*spec["base"]) / 10) * 10)
            rows.append({
                "product_id": f"P{pid:03d}",
                "product_name": _product_name(spec["type"], material, style, feature),
                "category": spec["type"],
                "material": material,
                "surface": surface,
                "style": style,
                "feature": feature,
                "price_multiplier": feat_mult,
                "base_price": base_price,
                "std_width": spec["std"][0],
                "std_height": spec["std"][1],
                "std_area": spec["std"][0] * spec["std"][1],
                "lead_min": spec["lead"][0],
                "lead_max": spec["lead"][1],
                "defect_rate": spec["defect"],
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 5. 门店 / 业务员（浙江嘉兴为核心）
# ---------------------------------------------------------------------------
STORES = [
    ("S01", "桐乡工厂展厅", "浙江", "嘉兴桐乡", "直营门店"),
    ("S02", "线上/抖音接单", "", "", "线上"),
    ("S03", "装企工程部", "", "", "工程"),
]


def build_stores():
    return pd.DataFrame([{
        "store_id": s[0], "store_name": s[1], "province": s[2],
        "city": s[3], "store_type": s[4], "status": "营业中",
    } for s in STORES])


def build_reps(stores):
    rows, rid = [], 0
    for _, st in stores.iterrows():
        n = {"直营门店": 3, "线上": 1, "工程": 2}.get(st["store_type"], 1)
        for _ in range(n):
            rid += 1
            gender = rng.choice(["男", "女"])
            age = int(rng.integers(24, 48))
            hire = pd.Timestamp("2023-08-09") - pd.Timedelta(days=int(rng.integers(180, 2600)))
            tenure_y = max(0, (TODAY - hire).days / 365)
            level = "资深" if tenure_y >= 5 else ("高级" if tenure_y >= 3 else ("中级" if tenure_y >= 1 else "初级"))
            rows.append({
                "rep_id": f"R{rid:03d}", "rep_name": gen_name(age, gender),
                "gender": gender, "store_id": st["store_id"],
                "hire_date": hire.strftime("%Y-%m-%d"), "level": level,
                "status": "在职",
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 6. 客户
# ---------------------------------------------------------------------------
CUST_TYPES = ["个人家装", "装修公司", "工程客户"]
CUST_TYPE_P = [0.82, 0.14, 0.04]


def pick_channel(cust_type):
    if cust_type == "个人家装":
        return rng.choice(["抖音短视频", "老客户转介", "门店到访", "线上渠道", "展会营销"],
                          p=[0.35, 0.28, 0.20, 0.10, 0.07])
    return rng.choice(["装修公司合作", "老客户转介", "展会营销"],
                      p=[0.55, 0.30, 0.15])


def build_customers(n=1500):
    age = np.clip(rng.normal(40, 10, n), 25, 75).astype(int)
    gender = rng.choice(["男", "女"], size=n, p=[0.52, 0.48])
    ctype = rng.choice(CUST_TYPES, size=n, p=CUST_TYPE_P)
    prov, city = sample_city(n)

    rows = []
    for i in range(n):
        t = ctype[i]
        if t == "个人家装":
            name = gen_name(age[i], gender[i])
        else:
            name = gen_company_name()
        rows.append({
            "customer_id": f"C{i + 1:05d}",
            "customer_name": name,
            "customer_type": t,
            "gender": gender[i] if t == "个人家装" else "，",
            "age": age[i] if t == "个人家装" else np.nan,
            "phone": gen_phone(),
            "province": prov[i], "city": city[i],
            "address": gen_address(prov[i], city[i]),
            "channel": pick_channel(t),
            "register_date": None,  # 下单后回填
            "member_level": "普通",
            "total_orders": 0, "total_spend": 0.0,
            "first_order_date": None, "last_order_date": None,
            "is_lead": 1 if rng.random() < 0.18 else 0,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 7. 订单主表 + 明细
# ---------------------------------------------------------------------------
ORDER_TYPE_BY_CUST = {"个人家装": "零售", "装修公司": "装企合作", "工程客户": "工程"}

CHANNEL_TO_STORE = {
    "线上渠道": ["S02"],
    "门店到访": ["S01"],
    "老客户转介": ["S01"],
    "抖音短视频": ["S01"],
    "展会营销": ["S01"],
    "装修公司合作": ["S03"],
}

CAT_W = {"铜门": 0.14, "不锈钢门": 0.42, "不锈钢铜门": 0.10, "铸铝门": 0.08,
         "甲级防盗门": 0.18, "防火门": 0.08}
CAT_W_BIZ = {"铜门": 0.04, "不锈钢门": 0.26, "不锈钢铜门": 0.05, "铸铝门": 0.03,
             "甲级防盗门": 0.27, "防火门": 0.35}
CAT_W_ENG = {"铜门": 0.02, "不锈钢门": 0.22, "不锈钢铜门": 0.03, "铸铝门": 0.01,
             "甲级防盗门": 0.32, "防火门": 0.40}

CATS = list(CAT_W.keys())


def pick_qty(cust_type):
    if cust_type == "个人家装":
        return int(rng.choice([1, 2, 3], p=[0.55, 0.32, 0.13]))
    if cust_type == "装修公司":
        return int(rng.choice([2, 3, 4, 5],
                              p=[0.35, 0.27, 0.22, 0.16]))
    return int(rng.integers(8, 18))


def pick_size(cat, custom):
    if cat == "防火门":
        w, h = int(rng.integers(800, 1250)), int(rng.integers(2000, 2300))
    else:  # 入户门（铜门/不锈钢门/铸铝/防盗）
        w, h = int(rng.integers(900, 1200)), int(rng.integers(2050, 2400))
    if custom:
        w, h = int(rng.integers(800, 1300)), int(rng.integers(1950, 2500))
    return w, h


def pick_thick(cat):
    for spec in PRODUCT_TYPES:
        if spec["type"] == cat:
            return int(rng.integers(*spec["thick"]))
    return 45


def gen_orders(customers, products, reps):
    cust = customers[customers["is_lead"] == 0].reset_index(drop=True)
    n_orders_per_cust = []
    for t in cust["customer_type"]:
        if t == "个人家装":
            n_orders_per_cust.append(int(rng.choice([1, 2, 3], p=[0.60, 0.30, 0.10])))
        elif t == "装修公司":
            n_orders_per_cust.append(int(rng.integers(3, 6)))
        else:
            n_orders_per_cust.append(int(rng.integers(2, 4)))
    n_orders_per_cust = np.array(n_orders_per_cust)

    # 生成订单头部
    order_rows = []
    for ci, (_, c) in enumerate(cust.iterrows()):
        for k in range(n_orders_per_cust[ci]):
            order_rows.append({"customer_id": c["customer_id"], "customer_type": c["customer_type"],
                               "channel": c["channel"]})
    n = len(order_rows)
    dates = sample_order_dates(n)

    offline_store_ids = [s[0] for s in STORES if s[4] == "直营门店"]
    eng_store_ids = [s[0] for s in STORES if s[4] == "工程"]

    for i, row in enumerate(order_rows):
        t = row["customer_type"]
        order_type = ORDER_TYPE_BY_CUST[t]
        if order_type in ("工程", "装企合作"):
            store = "S03"
        else:
            pool = CHANNEL_TO_STORE.get(row["channel"], offline_store_ids)
            store = rng.choice(pool)
        rep_pool = reps[reps["store_id"] == store]["rep_id"].tolist() or ["R001"]
        rep = rng.choice(rep_pool)
        delivery = rng.choice(["包安装", "自提"], p=[0.85, 0.15]) if t == "个人家装" \
            else rng.choice(["包安装", "自提"], p=[0.4, 0.6])
        order_rows[i].update({
            "store_id": store, "sales_rep_id": rep, "order_type": order_type,
            "delivery_type": delivery,
        })

    orders = pd.DataFrame(order_rows)
    orders["order_date"] = dates.reset_index(drop=True).values
    orders = orders.sort_values(["customer_id", "order_date"]).reset_index(drop=True)
    orders["order_id"] = [
        "DD" + pd.Timestamp(d).strftime("%Y%m%d") + f"{i + 1:05d}"
        for i, d in enumerate(orders["order_date"])
    ]

    # 生成订单明细并回填金额
    prod_by_id = products.set_index("product_id")
    cat_to_spec = {s["type"]: s for s in PRODUCT_TYPES}
    item_rows = []
    order_meta = {}

    for _, o in orders.iterrows():
        oid = o["order_id"]
        t = o["customer_type"]
        if o["order_type"] == "工程":
            n_items = 1  # 工程批量单通常为单一规格
        else:
            n_items = rng.choice([1, 2], p=[0.70, 0.30])
        cat_w = CAT_W if t == "个人家装" else (CAT_W_BIZ if t == "装修公司" else CAT_W_ENG)
        cats = rng.choice(CATS, size=n_items, p=[cat_w[c] for c in CATS])
        custom = rng.random() < (0.30 if t == "个人家装" else 0.55)

        total = 0.0
        max_price_item, dominant_cat = 0.0, cats[0]
        for j, cat in enumerate(cats):
            pid = rng.choice(prod_by_id[prod_by_id["category"] == cat].index)
            prod = prod_by_id.loc[pid]
            qty = pick_qty(t)
            w, h = pick_size(cat, custom)
            thick = pick_thick(cat)
            area = w * h
            size_factor = area / prod["std_area"]
            unit_price = prod["base_price"] * prod["price_multiplier"] * size_factor
            if custom:
                unit_price *= 1.10
            unit_price *= rng.uniform(0.97, 1.03)
            unit_price = int(round(unit_price / 10) * 10)
            line = round(unit_price * qty, 2)
            total += line
            if unit_price * qty > max_price_item:
                max_price_item, dominant_cat = unit_price * qty, cat
            item_rows.append({
                "item_id": f"I{len(item_rows) + 1:06d}",
                "order_id": oid, "product_id": pid,
                "product_name": prod["product_name"], "category": cat,
                "quantity": qty,
                "width_mm": w, "height_mm": h, "thickness_mm": thick,
                "is_custom": 1 if custom else 0,
                "unit_price": unit_price, "line_amount": line,
                "area_m2": round(area / 1e6, 3),
            })

        # 折扣 / 运费
        if t == "个人家装":
            disc = rng.uniform(0, 0.03)
        elif t == "装修公司":
            disc = rng.uniform(0.04, 0.08)
        else:
            disc = rng.uniform(0.06, 0.12)
        freight = 0.0
        if o["delivery_type"] == "包安装" and rng.random() < 0.18:
            freight = float(rng.integers(80, 350))
        final = round(total * (1 - disc) + freight, 2)

        lead_spec = cat_to_spec[dominant_cat]
        lead = int(rng.integers(lead_spec["lead"][0], lead_spec["lead"][1] + 1))
        order_meta[oid] = {"total_amount": round(total, 2), "discount": disc,
                           "freight": freight, "final_amount": final,
                           "lead_days": lead, "dominant_cat": dominant_cat}

    items = pd.DataFrame(item_rows)

    # 订单状态（结合当前时间）
    status = []
    actual_delivery, planned_delivery, is_on_time = [], [], []
    for _, o in orders.iterrows():
        meta = order_meta[o["order_id"]]
        d = pd.Timestamp(o["order_date"])
        planned = d + pd.Timedelta(days=meta["lead_days"])
        days_since = (TODAY - d).days
        if days_since > 10 and rng.random() < 0.07:
            st = "已取消"
        elif planned <= TODAY:
            st = "已完成"
        elif days_since >= 7:
            st = rng.choice(["待发货", "生产中"], p=[0.55, 0.45])
        else:
            st = rng.choice(["生产中", "待发货"], p=[0.70, 0.30])
        status.append(st)

        if st == "已取消":
            actual_delivery.append(pd.NaT)
            planned_delivery.append(planned)
            is_on_time.append(np.nan)
        elif st == "已完成":
            delay = int(np.clip(rng.normal(-1.5, 3), -5, 10))
            act = min(planned + pd.Timedelta(days=delay), TODAY)
            actual_delivery.append(act)
            planned_delivery.append(planned)
            is_on_time.append(1 if act <= planned else 0)
        else:
            actual_delivery.append(pd.NaT)
            planned_delivery.append(planned)
            is_on_time.append(np.nan)

    orders["order_status"] = status
    orders["planned_delivery_date"] = pd.to_datetime(planned_delivery)
    orders["actual_delivery_date"] = pd.to_datetime(actual_delivery)
    orders["is_on_time"] = is_on_time
    orders["total_amount"] = [order_meta[o]["total_amount"] for o in orders["order_id"]]
    orders["discount"] = [order_meta[o]["discount"] for o in orders["order_id"]]
    orders["freight"] = [order_meta[o]["freight"] for o in orders["order_id"]]
    orders["final_amount"] = [order_meta[o]["final_amount"] for o in orders["order_id"]]
    orders["lead_days"] = [order_meta[o]["lead_days"] for o in orders["order_id"]]
    orders["dominant_category"] = [order_meta[o]["dominant_cat"] for o in orders["order_id"]]
    orders["item_count"] = items.groupby("order_id")["item_id"].count().reindex(orders["order_id"]).fillna(0).astype(int).values
    orders["order_year"] = pd.to_datetime(orders["order_date"]).dt.year
    orders["order_month"] = pd.to_datetime(orders["order_date"]).dt.month
    orders["quarter"] = pd.to_datetime(orders["order_date"]).dt.quarter
    orders["weekday"] = pd.to_datetime(orders["order_date"]).dt.dayofweek

    return orders, items


# ---------------------------------------------------------------------------
# 8. 付款记录
# ---------------------------------------------------------------------------
PM_METHOD = {"定金": ["微信支付", "支付宝", "POS刷卡"],
             "中期款": ["银行转账", "POS刷卡", "对公转账"],
             "尾款": ["银行转账", "POS刷卡", "对公转账", "微信支付"]}


def gen_payments(orders):
    rows = []
    for _, o in orders.iterrows():
        oid, final, d, act = o["order_id"], o["final_amount"], pd.Timestamp(o["order_date"]), o["actual_delivery_date"]
        st = o["order_status"]
        if st == "已取消":
            rows.append({"payment_id": f"PM{len(rows) + 1:06d}", "order_id": oid,
                         "payment_date": d, "stage": "定金", "amount": round(final * 0.30, 2),
                         "payment_method": rng.choice(PM_METHOD["定金"]), "is_refund": 1})
            continue
        rows.append({"payment_id": f"PM{len(rows) + 1:06d}", "order_id": oid,
                     "payment_date": d, "stage": "定金", "amount": round(final * 0.30, 2),
                     "payment_method": rng.choice(PM_METHOD["定金"]), "is_refund": 0})
        need_mid = final > 20000 or o["order_type"] != "零售"
        if need_mid:
            mid_date = d + pd.Timedelta(days=int(rng.integers(12, 25)))
            if mid_date <= TODAY:
                rows.append({"payment_id": f"PM{len(rows) + 1:06d}", "order_id": oid,
                             "payment_date": mid_date, "stage": "中期款",
                             "amount": round(final * 0.35, 2),
                             "payment_method": rng.choice(PM_METHOD["中期款"]), "is_refund": 0})
        if pd.notna(act):
            tail_date = min(pd.Timestamp(act), TODAY)
            rows.append({"payment_id": f"PM{len(rows) + 1:06d}", "order_id": oid,
                         "payment_date": tail_date, "stage": "尾款",
                         "amount": round(final * 0.35, 2) if need_mid else round(final * 0.70, 2),
                         "payment_method": rng.choice(PM_METHOD["尾款"]), "is_refund": 0})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 9. 售后 / 退换货
# ---------------------------------------------------------------------------
AS_TYPES = ["安装问题", "尺寸误差", "色差", "五金损坏", "门体变形", "物流破损", "售后咨询", "退款"]


def pick_as_type(cat):
    p = np.array([0.22, 0.10, 0.18, 0.12, 0.15, 0.08, 0.08, 0.07], dtype=float)
    if cat in ("铜门", "不锈钢铜门", "铸铝门"):
        p[2] *= 1.6   # 色差/做旧差异
    if cat == "铜门":
        p[4] *= 1.4   # 门体变形（大板面）
    if cat in ("防火门", "甲级防盗门"):
        p[1] *= 1.5   # 尺寸误差（工程单标准件）
    if cat == "甲级防盗门":
        p[3] *= 1.4   # 五金/锁体
    p = p / p.sum()
    return rng.choice(AS_TYPES, p=p)


def gen_after_sales(orders, items, products):
    rows = []
    completed = orders[orders["order_status"].isin(["已完成", "生产中"])]
    prod_defect = items.merge(products[["product_id", "defect_rate"]], on="product_id", how="left")
    order_defect = completed.merge(
        prod_defect.groupby("order_id")["defect_rate"].max().reset_index(), on="order_id", how="left")
    order_defect["defect_rate"] = order_defect["defect_rate"].fillna(0.05)

    for _, o in order_defect.iterrows():
        p = o["defect_rate"] * 1.35
        if rng.random() > p:
            continue
        oid = o["order_id"]
        d = pd.Timestamp(o["order_date"])
        act = o["actual_delivery_date"]
        cat = o["dominant_category"]
        as_type = pick_as_type(cat)
        if as_type in ("安装问题", "尺寸误差", "五金损坏", "物流破损"):
            rpt = act + pd.Timedelta(days=int(rng.integers(0, 8))) if pd.notna(act) else d
        elif as_type in ("色差", "门体变形"):
            rpt = act + pd.Timedelta(days=int(rng.integers(5, 60))) if pd.notna(act) else d
        else:
            rpt = d + pd.Timedelta(days=int(rng.integers(3, 20)))
        rpt = min(pd.Timestamp(rpt), TODAY)
        cost = float(rng.integers(80, 600))
        if as_type == "色差":
            cost = float(rng.integers(600, 2500))
        elif as_type == "门体变形":
            cost = float(rng.integers(800, 3500))
        elif as_type == "退款":
            cost = round(o["final_amount"] * rng.uniform(0.6, 1.0), 2)
        elif as_type == "售后咨询":
            cost = 0.0
        rows.append({
            "after_id": f"AF{len(rows) + 1:06d}", "order_id": oid,
            "customer_id": o["customer_id"], "product_id": o["dominant_category"],
            "report_date": rpt, "type": as_type, "cost": cost,
            "status": rng.choice(["已处理", "处理中"], p=[0.9, 0.1]),
            "handling_days": int(rng.integers(1, 13)),
        })

    canceled = orders[orders["order_status"] == "已取消"]
    for _, o in canceled.iterrows():
        if rng.random() < 0.30:
            d = pd.Timestamp(o["order_date"])
            rows.append({
                "after_id": f"AF{len(rows) + 1:06d}", "order_id": o["order_id"],
                "customer_id": o["customer_id"], "product_id": o["dominant_category"],
                "report_date": d + pd.Timedelta(days=int(rng.integers(2, 10))),
                "type": "退款", "cost": round(o["final_amount"] * 0.30, 2),
                "status": "已处理", "handling_days": int(rng.integers(2, 8)),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 10. 客户聚合回填（成交状态 / 会员等级）
# ---------------------------------------------------------------------------
def finalize_customers(customers, orders):
    done = orders[orders["order_status"] != "已取消"]
    agg = done.groupby("customer_id").agg(
        total_orders=("order_id", "count"),
        total_spend=("final_amount", "sum"),
        first_order=("order_date", "min"),
        last_order=("order_date", "max"),
    ).reset_index()
    cust_map = customers.set_index("customer_id")

    def level_of(spend):
        if spend >= 80000: return "钻石"
        if spend >= 35000: return "金卡"
        if spend >= 12000: return "银卡"
        return "普通"

    for _, r in agg.iterrows():
        cid = r["customer_id"]
        reg = pd.Timestamp(r["first_order"]) - pd.Timedelta(days=int(rng.integers(20, 400)))
        if reg < START:
            reg = pd.Timestamp(r["first_order"]) - pd.Timedelta(days=1)
        cust_map.at[cid, "register_date"] = reg.strftime("%Y-%m-%d")
        cust_map.at[cid, "total_orders"] = int(r["total_orders"])
        cust_map.at[cid, "total_spend"] = round(r["total_spend"], 2)
        cust_map.at[cid, "first_order_date"] = str(pd.Timestamp(r["first_order"]).date())
        cust_map.at[cid, "last_order_date"] = str(pd.Timestamp(r["last_order"]).date())
        cust_map.at[cid, "member_level"] = level_of(r["total_spend"])
        cust_map.at[cid, "is_lead"] = 0

    leads = customers[customers["is_lead"] == 1]
    for cid in leads["customer_id"]:
        rd = START + pd.Timedelta(days=int(rng.integers(0, (END - START).days)))
        cust_map.at[cid, "register_date"] = rd.strftime("%Y-%m-%d")
    return cust_map.reset_index()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("生成「高端定制门业企业」模拟业务数据 ...")
    print("=" * 60)

    products = build_products()
    stores = build_stores()
    reps = build_reps(stores)
    customers = build_customers(1500)
    print(f"  产品 {len(products)} 个 / 门店 {len(stores)} 家 / 业务员 {len(reps)} 人 / "
          f"客户 {len(customers)} 人（其中线索 {customers['is_lead'].sum()}）")

    orders, items = gen_orders(customers, products, reps)
    print(f"  订单 {len(orders)} 单 / 明细 {len(items)} 行")

    customers = finalize_customers(customers, orders)
    payments = gen_payments(orders)
    after_sales = gen_after_sales(orders, items, products)
    print(f"  付款 {len(payments)} 条 / 售后 {len(after_sales)} 条")

    print("-" * 60)
    save(products, "products.csv")
    save(stores, "stores.csv")
    save(reps, "sales_reps.csv")
    save(customers, "customers.csv")
    save(orders, "orders.csv")
    save(items, "order_items.csv")
    save(payments, "payments.csv")
    save(after_sales, "after_sales.csv")

    print("-" * 60)
    valid = orders[orders["order_status"] != "已取消"]
    total_revenue = valid["final_amount"].sum()
    print(f"数据生成完成。近 3.5 年有效订单收入合计约 ¥{total_revenue:,.0f}，"
          f"订单均价约 ¥{valid['final_amount'].mean():,.0f}")
    print("按订单类型收入占比：")
    print(valid.groupby("order_type")["final_amount"].agg(["sum", "count", "mean"]).round(0).to_string())
    print("输出目录：", RAW_DIR)
