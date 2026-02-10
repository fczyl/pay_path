import io
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from pathlib import Path


# ===== 基本配置 =====
SHEET_NAME = "原始数据"

st.set_page_config(
    page_title="Eden 商业化礼包 - 付费路径分析",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 全局样式
st.markdown(
    """
    <style>
    .main {
        background: radial-gradient(circle at top left, #f3f4f6 0, #e5e7eb 45%, #ffffff 100%);
        color: #111827;
    }
    section[data-testid="stSidebar"] {
        background: #e5e7eb;
        border-right: 1px solid #d1d5db;
        color: #111827;
    }
    h1, h2, h3 {
        color: #111827 !important;
    }
    .metric-card {
        padding: 16px 20px;
        border-radius: 16px;
        background: linear-gradient(135deg, #e0f2fe, #c7d2fe);
        border: 1px solid rgba(148, 163, 184, 0.7);
        box-shadow: 0 12px 30px rgba(15, 23, 42, 0.16);
    }
    .metric-label {
        font-size: 13px;
        color: #6b7280;
    }
    .metric-value {
        font-size: 24px;
        font-weight: 700;
        color: #111827;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data
def load_data(excel_bytes: bytes):
    # 只读取必要字段，减少内存
    usecols = ["玩家id", "Dn", "分R", "iap_product_name", "相同UID第几次付费", "base_price"]
    df = pd.read_excel(
        io.BytesIO(excel_bytes),
        sheet_name=SHEET_NAME,
        usecols=usecols,
        engine="openpyxl",
    )

    # 清洗：去掉关键字段缺失的行
    df = df.dropna(subset=["玩家id", "iap_product_name", "相同UID第几次付费"])

    # 类型处理
    df["相同UID第几次付费"] = pd.to_numeric(df["相同UID第几次付费"], errors="coerce")
    df = df.dropna(subset=["相同UID第几次付费"])
    df["相同UID第几次付费"] = df["相同UID第几次付费"].astype(int)

    # base_price 作为金额，转为数值
    df["base_price"] = pd.to_numeric(df["base_price"], errors="coerce").fillna(0)

    # 分R / Dn 可能有浮点，统一成字符串方便筛选
    if "分R" in df.columns:
        df["分R"] = df["分R"].astype(str)
    if "Dn" in df.columns:
        df["Dn"] = df["Dn"].astype(str)

    return df


def build_sankey_data(
    df,
    max_step: int = 3,
    weight_mode: str = "players",  # "players"（人数）或 "revenue"（金额）
    min_edge_value: float = 1,
    top_k_edges: int | None = 80,
):
    """
    根据“相同UID第几次付费”构建前 max_step 次付费路径的 Sankey 结构。
    """
    df = df[df["相同UID第几次付费"] <= max_step].copy()
    df = df.sort_values(["玩家id", "相同UID第几次付费"])

    df["next_product"] = df.groupby("玩家id")["iap_product_name"].shift(-1)
    df["this_step"] = df["相同UID第几次付费"]
    df["next_step"] = df["相同UID第几次付费"] + 1

    edges = df.dropna(subset=["next_product"]).copy()
    edges = edges[edges["next_step"] <= max_step]

    # 构点+边
    edges["source_node"] = (
        "第" + edges["this_step"].astype(str) + "次：" + edges["iap_product_name"].astype(str)
    )
    edges["target_node"] = (
        "第" + edges["next_step"].astype(str) + "次：" + edges["next_product"].astype(str)
    )

    # 聚合边的权重：按人数或金额
    if weight_mode == "revenue":
        agg_series = edges.groupby(["source_node", "target_node"])["base_price"].sum()
        edge_counts = agg_series.reset_index(name="value")
    else:
        agg_series = edges.groupby(["source_node", "target_node"])["玩家id"].nunique()
        edge_counts = agg_series.reset_index(name="value")

    # 过滤掉权重太小的边，避免噪点
    edge_counts = edge_counts[edge_counts["value"] >= min_edge_value]

    # 仅保留前若干条最粗的路径，避免图太拥挤
    edge_counts = edge_counts.sort_values("value", ascending=False)
    if top_k_edges is not None:
        edge_counts = edge_counts.head(top_k_edges)

    if edge_counts.empty:
        return [], [], [], [], edge_counts

    # 构建节点列表（只包含筛选后的边）
    all_nodes = pd.unique(edge_counts[["source_node", "target_node"]].values.ravel())
    node_to_id = {name: i for i, name in enumerate(all_nodes)}

    # 转成 Sankey 需要的索引
    source_ids = edge_counts["source_node"].map(node_to_id)
    target_ids = edge_counts["target_node"].map(node_to_id)
    values = edge_counts["value"]

    return all_nodes, source_ids, target_ids, values, edge_counts


def _build_node_colors(labels):
    """根据“第n次”阶段给节点上不同色带，增强层级感。"""
    palette = {
        1: "rgba(59,130,246,0.80)",  # 第1次：蓝
        2: "rgba(16,185,129,0.80)",  # 第2次：绿
        3: "rgba(234,179,8,0.80)",  # 第3次：黄
        4: "rgba(249,115,22,0.80)",  # 第4次：橙
        5: "rgba(236,72,153,0.80)",  # 第5次：粉
    }
    colors = []
    for label in labels:
        step = 1
        try:
            if label.startswith("第") and "次：" in label:
                num_str = label.split("第")[1].split("次：")[0]
                step = int(num_str)
        except Exception:
            step = 1
        colors.append(palette.get(step, "rgba(148,163,184,0.8)"))
    return colors


def plot_sankey(all_nodes, source_ids, target_ids, values, weight_mode: str):
    node_colors = _build_node_colors(all_nodes)

    fig = go.Figure(
        data=[
            go.Sankey(
                node=dict(
                    pad=15,
                    thickness=20,
                    line=dict(color="rgba(148,163,184,0.7)", width=0.6),
                    label=all_nodes,
                    color=node_colors,
                ),
                link=dict(
                    source=source_ids,
                    target=target_ids,
                    value=values,
                    color="rgba(148, 163, 184, 0.35)",
                    hovertemplate=(
                        "从 %{source.label} → %{target.label}<br>"
                        + (
                            "人数: %{value} 位玩家"
                            if weight_mode == "players"
                            else "金额: %{value:.0f}"
                        )
                        + "<extra></extra>"
                    ),
                ),
            )
        ]
    )
    fig.update_layout(
        # 整体字体加深一些并略微放大，提升可读性
        font=dict(size=13, color="#000000"),
        height=650,
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


def main():
    st.title("Eden 商业化礼包 · 付费路径分析")
    st.caption("基于 `原始数据`：玩家id / Dn / 分R / iap_product_name / 相同UID第几次付费 / base_price")

    # ===== 数据源选择（必须上传，避免把内部数据提交到仓库）=====
    uploaded = st.sidebar.file_uploader(
        "拖拽/选择 Excel 文件（必选）",
        type=["xlsx"],
        accept_multiple_files=False,
    )

    if uploaded is None:
        st.info("请先在左侧上传 Excel。")
        st.stop()

    st.sidebar.caption("当前数据源：上传文件")
    try:
        df = load_data(uploaded.getvalue())
    except ValueError as e:
        # 常见：sheet_name 不存在、列名不匹配等
        st.error(f"读取 Excel 失败：{e}")
        st.stop()
    except Exception as e:
        st.error(f"读取 Excel 出错：{type(e).__name__}: {e}")
        st.stop()

    # ===== 筛选条件侧边栏 =====
    with st.sidebar:
        st.header("筛选条件")

        # 分R筛选
        r_values = sorted(df["分R"].dropna().unique().tolist())
        selected_r = st.multiselect("选择分R（可多选，留空=全部）", r_values, default=[])

        # Dn筛选
        dn_values = sorted(df["Dn"].dropna().unique().tolist())
        selected_dn = st.multiselect("选择 Dn（可多选，留空=全部）", dn_values, default=[])

        # 最大付费次数（路径长度）
        max_step = st.slider("分析前几次付费路径（前N次）", min_value=2, max_value=5, value=3, step=1)

        st.markdown("---")

        # Sankey 权重模式：人数 / 金额
        weight_mode = st.radio(
            "路径粗细按什么算？",
            options=["players", "revenue"],
            format_func=lambda x: "按人数" if x == "players" else "按金额（base_price）",
            index=0,
        )

        # 边过滤：最小人数/金额 & TopK
        min_edge_value = st.slider(
            "过滤小权重路径（至少多少人数 / 金额）",
            min_value=1,
            max_value=20,
            value=3,
            step=1,
        )

        top_k_edges = st.slider(
            "最多展示多少条路径边",
            min_value=20,
            max_value=150,
            value=80,
            step=10,
        )

    # 应用筛选
    filtered = df.copy()
    if selected_r:
        filtered = filtered[filtered["分R"].isin(selected_r)]
    if selected_dn:
        filtered = filtered[filtered["Dn"].isin(selected_dn)]

    # ===== 顶部关键指标区域 =====
    st.subheader("整体数据概览")
    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown(
            f"""
            <div class="metric-card">
              <div class="metric-label">样本玩家数</div>
              <div class="metric-value">{filtered["玩家id"].nunique():,}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"""
            <div class="metric-card">
              <div class="metric-label">样本付费记录数</div>
              <div class="metric-value">{len(filtered):,}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"""
            <div class="metric-card">
              <div class="metric-label">总 base_price（示意）</div>
              <div class="metric-value">{filtered["base_price"].sum():,.0f}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with st.expander("查看样本明细（前 50 行）", expanded=False):
        st.dataframe(filtered.head(50), use_container_width=True, height=350)

    if filtered["玩家id"].nunique() == 0:
        st.warning("当前筛选条件下没有数据，请调整筛选。")
        return

    # ===== 主体内容分区：路径 / 表格 / 分布 =====
    tab_path, tab_table, tab_dist = st.tabs(
        ["🚀 付费路径流向", "📊 路径明细表", "📈 商品 & 分R 分布"]
    )

    # ----- 付费路径 Sankey -----
    with tab_path:
        st.subheader("付费路径可视化（Sankey）")
        all_nodes, source_ids, target_ids, values, edge_counts = build_sankey_data(
            filtered,
            max_step=max_step,
            weight_mode=weight_mode,
            min_edge_value=min_edge_value,
            top_k_edges=top_k_edges,
        )

        if len(values) == 0:
            st.warning(
                "当前筛选条件下前几次付费记录不足以构成清晰路径，请尝试："
                "① 降低左侧“过滤小权重路径”阈值；② 减小前几次付费步数；③ 放宽分R / Dn 筛选。"
            )
        else:
            fig = plot_sankey(all_nodes, source_ids, target_ids, values, weight_mode)
            st.plotly_chart(fig, use_container_width=True, theme=None)

    # ----- 路径明细表 -----
    with tab_table:
        st.subheader("路径明细（从商品 A → 商品 B）")
        if not edge_counts.empty:
            pretty = edge_counts.rename(
                columns={
                    "source_node": "来源（第n次+商品）",
                    "target_node": "去向（第n次+商品）",
                    "value": "权重（人数或金额）",
                }
            )
            st.dataframe(
                pretty.sort_values("权重（人数或金额）", ascending=False),
                use_container_width=True,
                height=500,
            )
        else:
            st.info("没有可展示的路径明细，请先在“付费路径流向”中调整筛选条件。")

    # ----- 商品与分R 分布 -----
    with tab_dist:
        st.subheader("商品与分R / base_price 分布")
        col_a, col_b = st.columns(2)

        with col_a:
            st.markdown("**TOP 商品（按去重玩家数）**")
            top_products = (
                filtered.groupby("iap_product_name")["玩家id"]
                .nunique()
                .reset_index(name="玩家数")
                .sort_values("玩家数", ascending=False)
                .head(20)
            )
            if not top_products.empty:
                import plotly.express as px

                fig_prod = px.bar(
                    top_products,
                    x="玩家数",
                    y="iap_product_name",
                    orientation="h",
                    color="玩家数",
                    color_continuous_scale="Blues",
                    height=420,
                )
                fig_prod.update_layout(
                    yaxis_title="商品",
                    xaxis_title="玩家数",
                    margin=dict(l=10, r=10, t=30, b=10),
                    coloraxis_showscale=False,
                    paper_bgcolor="white",
                    plot_bgcolor="white",
                    font=dict(color="#111827"),
                )
                st.plotly_chart(fig_prod, use_container_width=True)
            else:
                st.info("当前筛选下没有商品数据。")

        with col_b:
            st.markdown("**分R 维度玩家量分布**")
            r_dist = (
                filtered.groupby("分R")["玩家id"]
                .nunique()
                .reset_index(name="玩家数")
                .sort_values("玩家数", ascending=False)
            )
            if not r_dist.empty:
                import plotly.express as px

                fig_r = px.bar(
                    r_dist,
                    x="分R",
                    y="玩家数",
                    color="玩家数",
                    color_continuous_scale="Viridis",
                    height=420,
                )
                fig_r.update_layout(
                    xaxis_title="分R 桶",
                    yaxis_title="玩家数",
                    margin=dict(l=10, r=10, t=30, b=10),
                    coloraxis_showscale=False,
                    paper_bgcolor="white",
                    plot_bgcolor="white",
                    font=dict(color="#111827"),
                )
                st.plotly_chart(fig_r, use_container_width=True)
            else:
                st.info("当前筛选下没有分R 数据。")


if __name__ == "__main__":
    main()
