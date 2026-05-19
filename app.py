"""주식 봉 리플레이 매매 연습기.

랜덤 종목·랜덤 시점의 과거 차트를 정체를 숨긴 채 보여주고, [다음 봉]으로
하루씩 전진하며 매수·매도 타이밍을 연습한다. 종목을 갈아타도 자본·수익률은
이어지며(누적 계좌), [처음부터]로 초기화할 수 있다.

실행:  ./run.sh   또는   .venv/bin/streamlit run app.py
"""
import datetime as dt
import random

import FinanceDataReader as fdr
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="봉 리플레이 매매 연습", page_icon="📊", layout="wide")

START_CASH = 10_000_000   # 시작 자본 (원)
CTX = 200                 # 시작 시 보여줄 과거 봉 수
HIST_YEARS = 12           # 종목당 가져올 과거 데이터 길이 (년)
PLOT_WINDOW = 600         # 캔들차트에 그릴 최근 봉 수

UP, DOWN = "#e84a5f", "#3d6bb3"
BUY_C, SELL_C = "#16c79a", "#ffa502"


@st.cache_data(ttl=86400, show_spinner="종목 목록 불러오는 중...")
def load_listing():
    df = fdr.StockListing("KRX")[["Code", "Name", "Market", "Marcap"]].dropna()
    df = df[df["Name"].astype(str).str.strip() != ""]
    return df.sort_values("Marcap", ascending=False).head(1200).reset_index(drop=True)


@st.cache_data(ttl=3600, show_spinner=False)
def load_history(code: str):
    start = (dt.date.today() - dt.timedelta(days=365 * HIST_YEARS)).isoformat()
    return fdr.DataReader(code, start)


def pick_stock(play_n: int):
    """랜덤 종목·시점으로 새 게임 dict를 만든다 (계좌 현금과 무관)."""
    listing = load_listing()
    need = CTX + play_n
    for _ in range(40):
        row = listing.sample(1).iloc[0]
        df = load_history(row["Code"])
        if df is None or df.empty:
            continue
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        # OHLC 어느 하나라도 0 이하인 봉은 데이터 오류 → 차트가 깨지므로 제외
        df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
        if len(df) < need + 5:
            continue
        s = random.randint(0, len(df) - need)
        w = df.iloc[s:s + need]
        # 표시 구간 안에 비정상 급변(전봉 대비 ±70% 초과)이 있으면 다른 종목으로
        cl = w["Close"].to_numpy()
        if len(cl) > 1:
            ratio = cl[1:] / cl[:-1]
            if ((ratio > 1.7) | (ratio < 0.3)).any():
                continue
        return {
            "code": row["Code"], "name": row["Name"], "market": row["Market"],
            "o": [float(x) for x in w["Open"]], "h": [float(x) for x in w["High"]],
            "l": [float(x) for x in w["Low"]], "c": [float(x) for x in w["Close"]],
            "dates": [d.date().isoformat() for d in w.index],
            "play_n": play_n, "cur": CTX - 1,
            "shares": 0, "cost": 0.0, "trades": [], "revealed": False,
            "uirev": f"{row['Code']}-{s}",
        }
    return None


def cur_price() -> float:
    g = st.session_state.game
    return g["c"][g["cur"]]


def total_asset() -> float:
    return st.session_state.cash + st.session_state.game["shares"] * cur_price()


def cur_return() -> float:
    return (total_asset() - START_CASH) / START_CASH * 100


def record_equity():
    st.session_state.equity_log.append(round(cur_return(), 3))


def new_account(play_n: int) -> bool:
    """계좌 초기화 — 자본·수익률 기록을 리셋하고 첫 종목으로."""
    g = pick_stock(play_n)
    if not g:
        st.error("게임용 종목을 못 찾았습니다. 봉 수를 줄여보세요.")
        return False
    st.session_state.cash = float(START_CASH)
    st.session_state.equity_log = []
    st.session_state.stock_no = 1
    st.session_state.game = g
    record_equity()
    return True


def next_stock(play_n: int) -> bool:
    """다음 종목 — 보유분을 현재가에 청산하고, 현금·수익률을 이어 새 종목으로."""
    g = st.session_state.game
    if g["shares"] > 0:
        st.session_state.cash += g["shares"] * cur_price()
    ng = pick_stock(play_n)
    if not ng:
        st.error("다음 종목을 못 찾았습니다. 다시 눌러주세요.")
        return False
    st.session_state.stock_no += 1
    st.session_state.game = ng
    record_equity()
    return True


def advance(k: int):
    """봉 전진. 수익률 그래프는 봉 전진으론 기록하지 않는다(매수·매도 때만)."""
    g = st.session_state.game
    for _ in range(k):
        if g["cur"] >= len(g["c"]) - 1:
            g["revealed"] = True
            break
        g["cur"] += 1


def do_buy():
    """몰빵 매수 — 보유 현금 전액으로 현재 봉 종가에 산다."""
    g = st.session_state.game
    p = cur_price()
    n = int(st.session_state.cash // p)
    if n < 1:
        st.toast("현금이 부족합니다.", icon="⚠️")
        return
    st.session_state.cash -= n * p
    g["shares"] += n
    g["cost"] += n * p
    g["trades"].append({"i": g["cur"], "type": "buy", "price": p, "n": n})
    record_equity()


def do_sell(pct: int):
    """분할 매도 — 보유 주식의 pct%를 현재 봉 종가에 판다."""
    g = st.session_state.game
    if g["shares"] < 1:
        st.toast("보유 주식이 없습니다.", icon="⚠️")
        return
    p = cur_price()
    n = g["shares"] if pct >= 100 else int(g["shares"] * pct / 100)
    if n < 1:
        st.toast("매도 수량이 0주입니다. 비율을 높이세요.", icon="⚠️")
        return
    avg = g["cost"] / g["shares"]
    g["cost"] -= avg * n
    g["shares"] -= n
    st.session_state.cash += n * p
    g["trades"].append({"i": g["cur"], "type": "sell", "price": p, "n": n})
    record_equity()


# ─────────────── 사이드바 ───────────────
with st.sidebar:
    st.header("⚙️ 설정")
    st.number_input("플레이 봉 수", min_value=150, max_value=500,
                    value=300, step=50, key="play_n",
                    help="한 종목당 플레이할 봉 수 (150~500).")
    st.caption(f"시작 자본 {START_CASH:,}원 · 종목을 갈아타도 자본·수익률은 누적됩니다.")

st.title("📊 봉 리플레이 매매 연습")

# ─────────────── 시작 전 ───────────────
if "game" not in st.session_state:
    st.markdown(
        "랜덤 종목의 과거 차트를 **정체를 숨긴 채** 봅니다. **[다음 봉]**으로 "
        "하루씩 넘기며 매수·매도 타이밍을 연습하세요.\n\n"
        "- **[몰빵 매수]** 현금 전액 매수 · **[매도]** 비율(10~100%) 분할 매도\n"
        "- **[다음 종목]** 종목을 갈아타며 자본·수익률을 **이어서 누적**\n"
        "- **[처음부터]** 자본·수익률 초기화\n"
        "- 차트 ✏️ 도구로 추세선을 그리고, 옮기고, 지울 수 있습니다")
    if st.button("🎲 시작하기", type="primary"):
        if new_account(int(st.session_state.play_n)):
            st.rerun()
    st.stop()

g = st.session_state.game
last = len(g["c"]) - 1
cur = g["cur"]
price = cur_price()
played = cur - (CTX - 1)
shares = g["shares"]
cash = st.session_state.cash
total = total_asset()
ret = cur_return()
avg = g["cost"] / shares if shares else 0.0

st.caption(f"종목 #{st.session_state.stock_no}  ·  정체는 [정답 공개] 또는 종목 종료 시 공개")

# ── 지표 ──
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("진행", f"{played} / {g['play_n']} 봉")
c2.metric("현재가", f"{price:,.0f} 원")
c3.metric("보유", f"{shares:,}주", f"평단 {avg:,.0f}" if shares else "현금 보유")
c4.metric("총자산", f"{total:,.0f} 원")
c5.metric("누적 수익률", f"{ret:+.2f} %", f"{total - START_CASH:+,.0f} 원")

# ── 컨트롤: 진행 ──
b1, b3 = st.columns(2)
if b1.button("▶ 다음 봉", width="stretch", disabled=g["revealed"]):
    advance(1)
    st.rerun()
if b3.button("🔎 정답 공개", width="stretch", disabled=g["revealed"]):
    g["revealed"] = True
    st.rerun()

# ── 컨트롤: 종목 ──
n1, n2 = st.columns(2)
if n1.button("➡️ 다음 종목 (자본 이어감)", width="stretch"):
    if next_stock(int(st.session_state.play_n)):
        st.rerun()
if n2.button("🔄 처음부터 (초기화)", width="stretch"):
    if new_account(int(st.session_state.play_n)):
        st.rerun()

# ── 컨트롤: 매매 ──
t1, t2 = st.columns(2)
with t1:
    if st.button("🟢 몰빵 매수", width="stretch",
                 disabled=g["revealed"] or shares > 0,
                 help="보유 현금 전액으로 현재 종가에 매수"):
        do_buy()
        st.rerun()
with t2:
    sell_pct = st.select_slider("매도 비율", options=list(range(10, 101, 10)),
                                value=100, key="sell_pct",
                                disabled=g["revealed"] or shares < 1)
    if st.button(f"🔴 {sell_pct}% 매도", width="stretch",
                 disabled=g["revealed"] or shares < 1,
                 help="보유 주식의 선택 비율만큼 현재 종가에 매도"):
        do_sell(int(sell_pct))
        st.rerun()

# ── 추세선 (자유 드로잉) ──
st.session_state.setdefault("trend_clear", 0)

# ── 캔들차트 ──
end = last if g["revealed"] else cur
lo = max(0, end - PLOT_WINDOW)
xs = list(range(lo, end + 1))
fig = go.Figure(go.Candlestick(
    x=xs, open=g["o"][lo:end + 1], high=g["h"][lo:end + 1],
    low=g["l"][lo:end + 1], close=g["c"][lo:end + 1],
    increasing_line_color=UP, decreasing_line_color=DOWN, name="가격"))
for tr in g["trades"]:
    if tr["i"] < lo:
        continue
    fig.add_trace(go.Scatter(
        x=[tr["i"]], y=[tr["price"]], mode="markers",
        marker=dict(size=13, color=BUY_C if tr["type"] == "buy" else SELL_C,
                    symbol="triangle-up" if tr["type"] == "buy" else "triangle-down",
                    line=dict(width=1, color="#222")),
        showlegend=False, hoverinfo="text",
        hovertext=f"{'매수' if tr['type']=='buy' else '매도'} {tr['n']:,}주 "
                  f"@ {tr['price']:,.0f}"))
# 현재봉 표시선·종가선은 layout.shapes 가 아닌 트레이스로 그린다.
# (shapes 로 그리면 봉을 전진할 때 사용자가 그린 추세선까지 함께 지워진다)
_vh = g["h"][lo:end + 1]
_vl = g["l"][lo:end + 1]
_ymin, _ymax = min(_vl), max(_vh)
fig.add_trace(go.Scatter(
    x=[cur, cur], y=[_ymin, _ymax], mode="lines",
    line=dict(color="#aaa", width=1, dash="dot"),
    showlegend=False, hoverinfo="skip"))
fig.add_trace(go.Scatter(
    x=[lo, end], y=[price, price], mode="lines",
    line=dict(color="#777", width=1, dash="dash"),
    showlegend=False, hoverinfo="skip"))
fig.add_annotation(x=lo, y=price, text=f"종가 {price:,.0f}원",
                   showarrow=False, xanchor="left", yanchor="bottom",
                   font=dict(size=13, color="#222"))
fig.update_layout(
    height=520, margin=dict(l=10, r=10, t=30, b=10),
    dragmode="pan", newshape=dict(line=dict(color="#ff9500", width=2)),
    uirevision=f"{g['uirev']}-{st.session_state.trend_clear}",
    xaxis=dict(rangeslider=dict(visible=False), showticklabels=False,
               title="(봉 번호 숨김)"),
    yaxis=dict(tickformat=",.0f", ticksuffix="원"),
    showlegend=False)
st.plotly_chart(fig, width="stretch", key="replaychart", config={
    "modeBarButtonsToAdd": ["drawline"],
    "displaylogo": False, "scrollZoom": True})
tc1, tc2 = st.columns([3, 1])
tc1.caption("추세선 — 차트 우상단 ✏️ 도구로 자유롭게 그립니다. 봉을 넘겨도 유지돼요. "
            "지울 땐 오른쪽 [추세선 모두 지우기].")
if tc2.button("🧹 추세선 모두 지우기", width="stretch"):
    st.session_state.trend_clear += 1
    st.rerun()

# ── 수익률 추이 (매수·매도 시점에만 기록) ──
st.subheader("📈 내 수익률 추이 (매매 시점 기준)")
elog = st.session_state.equity_log
ecolor = "#e8453c" if elog and elog[-1] < 0 else "#16a34a"
efig = go.Figure(go.Scatter(
    y=elog, mode="lines+markers", line=dict(color=ecolor, width=2),
    marker=dict(size=6, color=ecolor),
    fill="tozeroy", fillcolor="rgba(120,120,120,0.12)"))
efig.add_hline(y=0, line=dict(color="#999", width=1))
efig.update_layout(
    height=220, margin=dict(l=10, r=10, t=10, b=10),
    xaxis=dict(showticklabels=False, title="매매 시점 (매수·매도할 때마다 점 1개)"),
    yaxis=dict(ticksuffix="%"), showlegend=False)
st.plotly_chart(efig, width="stretch", key="equitychart")
st.caption("수익률 그래프는 **매수·매도할 때만** 갱신됩니다 (봉 전진으론 변하지 않음).")

# ── 정답 공개 ──
if g["revealed"]:
    d0, d1 = g["dates"][CTX - 1], g["dates"][cur]
    st.success(
        f"**정답** — {g['name']} ({g['code']}, {g['market']})  ·  "
        f"플레이 구간 {d0} ~ {d1}")
    st.markdown(
        f"현재 계좌 — 총자산 **{total:,.0f}원**, "
        f"누적 수익 **{total - START_CASH:+,.0f}원 ({ret:+.2f}%)**  ·  "
        "[다음 종목]으로 이어서 하거나 [처음부터]로 초기화하세요.")
else:
    st.caption("미래 봉은 가려져 있습니다. [다음 봉]으로 전진하세요.")
