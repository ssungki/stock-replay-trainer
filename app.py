"""주식 봉 리플레이 매매 연습기.

랜덤 종목·랜덤 시점의 과거 차트를 정체를 숨긴 채 보여주고, [다음 봉]으로
하루씩 전진하며 매수·매도 타이밍을 연습한다. 종목을 갈아타도 자본·수익률은
이어지며(누적 계좌), [처음부터]로 초기화할 수 있다.

실행:  ./run.sh   또는   .venv/bin/streamlit run app.py
"""
import datetime as dt
import random
from pathlib import Path

import FinanceDataReader as fdr
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="봉 리플레이 매매 연습", page_icon="📊", layout="wide")

# 캔들차트 + 추세선 커스텀 컴포넌트 (자유 그리기 + 봉 전진 후에도 유지 + 개별 삭제)
_replay_chart = components.declare_component(
    "replay_chart", path=str(Path(__file__).parent / "chart_component"))

START_CASH = 10_000_000   # 시작 자본 (원)
CTX = 200                 # 시작 시 보여줄 과거 봉 수
HIST_YEARS = 15           # 종목당 가져올 과거 데이터 길이 (년)
PLOT_WINDOW = 200         # 캔들차트에 그릴 최근 봉 수 (슬라이딩 윈도우 — 봉 크기 고정)

UP, DOWN = "#e84a5f", "#3d6bb3"
BUY_C, SELL_C = "#16c79a", "#ffa502"
MAX_ENTRIES = 10          # 한 종목당 매수(진입) 최대 횟수


@st.cache_data(ttl=86400, show_spinner="종목 목록 불러오는 중...")
def load_listing():
    # Streamlit Cloud 에서 KRX 사이트 차단으로 fdr.StockListing 이 깨짐(2026-06-06).
    # 정적 CSV(repo 동봉)를 1순위로 읽고, 없으면 fdr 폴백 — 로컬 개발 환경 호환.
    from pathlib import Path
    csv_path = Path(__file__).parent / "krx_listing.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path, dtype={"Code": str})
        df = df[["Code", "Name", "Market", "Marcap"]].dropna()
    else:
        df = fdr.StockListing("KRX")[["Code", "Name", "Market", "Marcap"]].dropna()
    df = df[df["Name"].astype(str).str.strip() != ""]
    return df.sort_values("Marcap", ascending=False).head(200).reset_index(drop=True)


@st.cache_data(ttl=3600, show_spinner=False)
def load_history(code: str):
    start = (dt.date.today() - dt.timedelta(days=365 * HIST_YEARS)).isoformat()
    return fdr.DataReader(code, start)


@st.cache_resource
def leaderboard_store():
    """대회 순위표 — 앱 인스턴스의 모든 접속자가 공유하는 인메모리 저장소.
    형태: {대회코드: [{team, ret, asset, ts}, ...]}  (앱 재배포 시 초기화)"""
    return {}


def pick_stock(play_n: int, rng=None):
    """랜덤 종목·시점으로 새 게임 dict를 만든다 (계좌 현금과 무관).
    rng 를 주면(대회 모드) 그 시드로 종목 선택이 재현 가능해진다."""
    rnd = rng if rng is not None else random
    listing = load_listing()
    need = CTX + play_n
    for _ in range(40):
        row = listing.iloc[rnd.randrange(len(listing))]
        df = load_history(row["Code"])
        if df is None or df.empty:
            continue
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        # OHLC 어느 하나라도 0 이하인 봉은 데이터 오류 → 차트가 깨지므로 제외
        df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
        if len(df) < need + 5:
            continue
        s = rnd.randint(0, len(df) - need)
        w = df.iloc[s:s + need]
        # 표시 구간 안에 비정상 급변(전봉 대비 ±70% 초과)이 있으면 다른 종목으로
        cl = w["Close"].to_numpy()
        if len(cl) > 1:
            ratio = cl[1:] / cl[:-1]
            if ((ratio > 1.7) | (ratio < 0.3)).any():
                continue
        vol = (w["Volume"].fillna(0) if "Volume" in w
               else w["Close"] * 0)
        return {
            "code": row["Code"], "name": row["Name"], "market": row["Market"],
            "o": [float(x) for x in w["Open"]], "h": [float(x) for x in w["High"]],
            "l": [float(x) for x in w["Low"]], "c": [float(x) for x in w["Close"]],
            "v": [float(x) for x in vol],
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
    """새 세션 시작 — 자본·기록을 리셋하고 첫 종목으로.
    대회 코드가 있으면 그 시드로 세션 전체 종목을 고정 생성한다."""
    contest = st.session_state.get("pending_contest")
    session_len = int(st.session_state.session_len)
    if contest and contest.get("code"):
        rng = random.Random("stockreplay::" + contest["code"])
        games = []
        with st.spinner("대회 종목 준비 중... (처음 한 번만 시간이 걸립니다)"):
            for _ in range(session_len):
                sg = pick_stock(play_n, rng=rng)
                if not sg:
                    st.error("대회 종목을 만들지 못했습니다. 봉 수를 줄여보세요.")
                    return False
                games.append(sg)
        st.session_state.contest = {"team": contest.get("team") or "익명",
                                    "code": contest["code"]}
        st.session_state.contest_games = games
        g = games[0]
    else:
        g = pick_stock(play_n)
        if not g:
            st.error("게임용 종목을 못 찾았습니다. 봉 수를 줄여보세요.")
            return False
        st.session_state.contest = None
        st.session_state.contest_games = None
    st.session_state.contest_submitted = False
    st.session_state.cash = float(START_CASH)
    st.session_state.equity_log = []
    st.session_state.stock_no = 1
    st.session_state.history = []
    st.session_state.session_done = False
    st.session_state.game = g
    st.session_state.trendlines = []
    st.session_state.reset_token = st.session_state.get("reset_token", 0) + 1
    record_equity()
    return True


def archive_stock(g):
    """끝낸 종목의 매매 기록을 history 에 저장한다 (세션 분석용)."""
    trades = g["trades"]
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]
    buy_cost = sum(t["price"] * t["n"] for t in buys)
    sell_amt = sum(t["price"] * t["n"] for t in sells)
    traded = bool(buys)
    pnl = sell_amt - buy_cost if traded else 0.0
    pnl_pct = (pnl / buy_cost * 100) if buy_cost > 0 else 0.0
    first_buy_i = min((t["i"] for t in buys), default=None)
    last_sell_i = max((t["i"] for t in sells), default=None)
    hold = (last_sell_i - first_buy_i
            if first_buy_i is not None and last_sell_i is not None else None)
    quick = (any(s["i"] - first_buy_i <= 2 for s in sells)
             if first_buy_i is not None else False)
    e = g["cur"] + 1                    # 복기용 — 플레이한 구간까지의 차트
    st.session_state.history.append({
        "name": g["name"], "code": g["code"], "market": g["market"],
        "traded": traded, "n_trades": len(trades),
        "pnl": pnl, "pnl_pct": pnl_pct, "hold": hold, "quick_sell": quick,
        "chart": {
            "o": g["o"][:e], "h": g["h"][:e], "l": g["l"][:e], "c": g["c"][:e],
            "trades": [dict(t) for t in trades],
            "d0": g["dates"][CTX - 1], "d1": g["dates"][g["cur"]],
        },
    })


def _close_stock():
    """현재 종목 — 남은 보유분을 청산하고 매매 기록을 아카이브한다."""
    g = st.session_state.game
    if g["shares"] > 0:
        p = cur_price()
        st.session_state.cash += g["shares"] * p
        g["trades"].append({"i": g["cur"], "type": "sell",
                             "price": p, "n": g["shares"]})
        g["shares"] = 0
        g["cost"] = 0.0
    record_equity()
    archive_stock(g)


def session_len_eff() -> int:
    """이번 세션의 종목 수 — 대회 모드면 고정 생성된 종목 수, 아니면 설정값."""
    cg = st.session_state.get("contest_games")
    return len(cg) if cg else int(st.session_state.session_len)


def next_stock(play_n: int) -> bool:
    """다음 종목 — 현재 종목을 마치고 자본을 이어 새 종목으로.
    세션 마지막 종목이면 세션을 종료한다."""
    _close_stock()
    if st.session_state.stock_no >= session_len_eff():
        st.session_state.session_done = True   # 세션 종료 → 결과 리포트
        return True
    cg = st.session_state.get("contest_games")
    if cg:                                     # 대회 — 미리 고정한 종목 사용
        ng = cg[st.session_state.stock_no]
    else:
        ng = pick_stock(play_n)
        if not ng:
            st.error("다음 종목을 못 찾았습니다. 다시 눌러주세요.")
            return False
    st.session_state.stock_no += 1
    st.session_state.game = ng
    st.session_state.trendlines = []
    st.session_state.reset_token = st.session_state.get("reset_token", 0) + 1
    return True


def finish_session():
    """지금까지 한 것만으로 세션을 즉시 종료한다 (몇 종목이든)."""
    _close_stock()
    st.session_state.session_done = True


def analyze_session() -> dict:
    """세션 전체 매매를 분석해 통계와 코칭 피드백을 만든다."""
    hist = st.session_state.history
    n = len(hist)
    final = st.session_state.cash
    total_pnl = final - START_CASH
    total_ret = total_pnl / START_CASH * 100
    traded = [h for h in hist if h["traded"]]
    skipped = [h for h in hist if not h["traded"]]
    wins = [h for h in traded if h["pnl"] > 0]
    losses = [h for h in traded if h["pnl"] < 0]
    win_rate = (len(wins) / len(traded) * 100) if traded else 0.0
    avg_win = sum(h["pnl_pct"] for h in wins) / len(wins) if wins else 0.0
    avg_loss = sum(h["pnl_pct"] for h in losses) / len(losses) if losses else 0.0
    pl_ratio = (avg_win / abs(avg_loss)) if avg_loss < 0 else None
    holds = [h["hold"] for h in traded if h["hold"] is not None]
    avg_hold = sum(holds) / len(holds) if holds else None
    best = max(traded, key=lambda h: h["pnl_pct"], default=None)
    worst = min(traded, key=lambda h: h["pnl_pct"], default=None)
    quick = [h for h in traded if h["quick_sell"]]

    good, bad = [], []
    if skipped:
        bad.append(f"**기회 관망** — {n}개 중 {len(skipped)}개 종목에서 한 번도 "
                   "매매하지 않았습니다. 진입 기준이 너무 보수적이지 않은지 보세요.")
    if pl_ratio is not None and pl_ratio < 1:
        bad.append(f"**손실은 크게, 수익은 작게** — 평균 수익 +{avg_win:.1f}% 대 "
                   f"평균 손실 {avg_loss:.1f}%, 손익비 {pl_ratio:.2f}(1 미만). "
                   "손절은 빠르게·수익은 길게 가져가는 연습이 필요합니다.")
    if quick:
        bad.append(f"**조급한 매도** — {len(quick)}개 종목에서 매수 2봉 안에 "
                   "팔았습니다. 흔들림에 휘둘리는 뇌동매매 신호입니다.")
    if losses and avg_loss <= -15:
        bad.append(f"**손절 지연** — 손실 종목 평균이 {avg_loss:.1f}%까지 갔습니다. "
                   "-7~10%선에서 끊는 손절 원칙을 세워보세요.")
    if win_rate >= 60 and pl_ratio is not None and pl_ratio < 1:
        bad.append("**자주 이기지만 크게 잃는 패턴** — 승률은 높은데 한 번의 큰 "
                   "손실이 수익을 깎아먹습니다.")
    if worst and worst["pnl_pct"] < 0:
        bad.append(f"최대 손실 종목: **{worst['name']}** ({worst['pnl_pct']:+.1f}%).")
    if not bad:
        bad.append("뚜렷한 약점은 보이지 않습니다. 표본을 늘려 더 검증해보세요.")

    if wins:
        good.append(f"수익 종목 {len(wins)}개 · 평균 +{avg_win:.1f}%.")
    if best and best["pnl_pct"] > 0:
        good.append(f"베스트 거래: **{best['name']}** ({best['pnl_pct']:+.1f}%).")
    if pl_ratio is not None and pl_ratio >= 1.5:
        good.append(f"손익비 {pl_ratio:.2f} — 손절을 잘 지켜 이익을 키웠습니다.")
    if win_rate < 45 and pl_ratio is not None and pl_ratio >= 1.5:
        good.append("승률은 낮아도 손절이 단단해 손익비로 만회하는 스타일입니다.")
    if total_ret > 0:
        good.append(f"세션 누적 +{total_ret:.1f}% — 플러스로 마감했습니다.")
    if not good:
        good.append("아직 두드러진 강점은 없습니다 — 다음 세션에서 만들어봐요.")

    return {
        "n": n, "final": final, "total_pnl": total_pnl, "total_ret": total_ret,
        "traded": len(traded), "skipped": len(skipped),
        "wins": len(wins), "losses": len(losses), "win_rate": win_rate,
        "avg_win": avg_win, "avg_loss": avg_loss, "pl_ratio": pl_ratio,
        "avg_hold": avg_hold, "good": good, "bad": bad,
    }


def build_review_fig(h):
    """복기용 — 한 종목 캔들차트에 매수 ▲·매도 ▼ 지점을 찍는다."""
    ch = h["chart"]
    xs = list(range(len(ch["c"])))
    fig = go.Figure(go.Candlestick(
        x=xs, open=ch["o"], high=ch["h"], low=ch["l"], close=ch["c"],
        increasing_line_color=UP, decreasing_line_color=DOWN, name="가격"))
    rspan = (max(ch["h"]) - min(ch["l"])) or 1.0
    for t in ch["trades"]:
        is_buy = t["type"] == "buy"
        my = (ch["l"][t["i"]] - rspan * 0.045 if is_buy
              else ch["h"][t["i"]] + rspan * 0.045)
        fig.add_trace(go.Scatter(
            x=[t["i"]], y=[my], mode="markers",
            marker=dict(size=16, color=BUY_C if is_buy else SELL_C,
                        symbol="triangle-up" if is_buy else "triangle-down",
                        line=dict(width=2, color="#ffffff")),
            showlegend=False, hoverinfo="text",
            hovertext=f"{'매수' if is_buy else '매도'} {t['n']:,}주 "
                      f"@ {t['price']:,.0f}"))
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(rangeslider=dict(visible=False), showticklabels=False),
        yaxis=dict(tickformat=",.0f", ticksuffix="원"),
        showlegend=False, dragmode="pan")
    return fig


def advance(k: int):
    """봉 전진. 수익률 그래프는 봉 전진으론 기록하지 않는다(매수·매도 때만)."""
    g = st.session_state.game
    for _ in range(k):
        if g["cur"] >= len(g["c"]) - 1:
            g["revealed"] = True
            break
        g["cur"] += 1


def entry_count(g) -> int:
    """이 종목에서 지금까지 한 매수(진입) 횟수."""
    return sum(1 for t in g["trades"] if t["type"] == "buy")


def do_buy():
    """몰빵 매수 — 보유 현금 전액으로 현재 봉 종가에 산다."""
    g = st.session_state.game
    if entry_count(g) >= MAX_ENTRIES:
        st.toast(f"이 종목 매수(진입)는 {MAX_ENTRIES}회까지입니다.", icon="⚠️")
        return
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
    st.number_input("세션 종목 수", min_value=3, max_value=30,
                    value=15, step=1, key="session_len",
                    help="한 세션에 플레이할 종목 수. 다 끝내면 결과 리포트가 나옵니다.")
    st.number_input("플레이 봉 수", min_value=150, max_value=2000,
                    value=2000, step=50, key="play_n",
                    help="한 종목당 플레이할 봉 수 (150~2000).")
    st.caption(f"시작 자본 {START_CASH:,}원 · 종목을 갈아타도 자본·수익률은 누적됩니다.")

st.title("📊 봉 리플레이 매매 연습")

# ─────────────── 시작 전 ───────────────
if "game" not in st.session_state:
    st.markdown(
        f"랜덤 종목 **{st.session_state.session_len}개**를 차례로 플레이하는 "
        "한 세션입니다. 차트 정체를 숨긴 채 **[다음 봉]**으로 넘기며 매수·매도 "
        "타이밍을 연습하세요.\n\n"
        "- **[몰빵 매수]** 현금 전액 매수 · **[매도]** 비율(10~100%) 분할 매도\n"
        "- **[다음 종목]** 종목을 갈아타며 자본·수익률을 **이어서 누적**\n"
        "- 세션을 다 끝내면 **매매 분석 리포트**가 나옵니다\n"
        "- 차트를 드래그해 추세선을 그릴 수 있습니다")
    with st.expander("🏆 대회 모드로 참가 (여러 팀이 같은 종목으로 대결)"):
        st.text_input("팀 이름", key="in_team", placeholder="예: 1팀 / 홍길동")
        st.text_input("대회 코드", key="in_code",
                      placeholder="대회 코드 입력 (없으면 혼자 연습)",
                      help="같은 대회 코드를 넣은 팀끼리 똑같은 종목으로 대결하고, "
                           "세션을 마치면 순위표에 자동 등록됩니다.")
        st.caption("대회 참가자는 모두 같은 코드 + 같은 사이드바 설정을 쓰세요.")
    if st.button("🎲 세션 시작", type="primary"):
        _code = (st.session_state.get("in_code") or "").strip()
        _team = (st.session_state.get("in_team") or "").strip()
        st.session_state.pending_contest = ({"team": _team, "code": _code}
                                            if _code else None)
        if new_account(int(st.session_state.play_n)):
            st.rerun()
    st.stop()

# ─────────────── 세션 종료 → 결과 리포트 ───────────────
if st.session_state.get("session_done"):
    a = analyze_session()
    st.header("🏁 세션 결과")
    st.caption(f"종목 {a['n']}개 완료")
    m1, m2, m3 = st.columns(3)
    m1.metric("최종 자산", f"{a['final']:,.0f} 원")
    m2.metric("세션 수익률", f"{a['total_ret']:+.2f} %",
              f"{a['total_pnl']:+,.0f} 원")
    m3.metric("승률", f"{a['win_rate']:.0f} %", f"{a['wins']}승 {a['losses']}패")
    m4, m5, m6 = st.columns(3)
    m4.metric("매매한 종목", f"{a['traded']} / {a['n']}",
              f"관망 {a['skipped']}개" if a['skipped'] else "전부 매매")
    m5.metric("손익비", f"{a['pl_ratio']:.2f}" if a['pl_ratio'] is not None
              else "—", help="평균 수익률 ÷ 평균 손실률. 1보다 크면 좋음")
    m6.metric("평균 보유", f"{a['avg_hold']:.0f} 봉"
              if a['avg_hold'] is not None else "—")

    # ── 대회 순위표 ──
    _contest = st.session_state.get("contest")
    if _contest:
        _lb = leaderboard_store()
        _entries = _lb.setdefault(_contest["code"], [])
        if not st.session_state.get("contest_submitted"):
            _entries.append({
                "team": _contest["team"], "ret": a["total_ret"],
                "asset": a["final"], "curve": list(st.session_state.equity_log),
                "ts": dt.datetime.now().strftime("%m-%d %H:%M")})
            st.session_state.contest_submitted = True
        _ranked = sorted(_entries, key=lambda x: x["ret"], reverse=True)
        st.subheader(f"🏆 대회 순위표 — 코드 「{_contest['code']}」")
        st.dataframe([
            {"순위": i, "팀": e["team"], "수익률": f"{e['ret']:+.2f}%",
             "최종자산": f"{e['asset']:,.0f}원", "기록": e["ts"]}
            for i, e in enumerate(_ranked, 1)
        ], hide_index=True, width="stretch")
        # 팀별 수익률 곡선 한 그래프에 겹쳐 보기
        _cfig = go.Figure()
        for e in _ranked:
            _cv = e.get("curve") or []
            _cfig.add_trace(go.Scatter(
                y=_cv, x=list(range(len(_cv))), mode="lines+markers",
                name=e["team"], line=dict(width=2), marker=dict(size=5)))
        _cfig.add_hline(y=0, line=dict(color="#999", width=1))
        _cfig.update_layout(
            height=320, margin=dict(l=10, r=10, t=10, b=10),
            xaxis=dict(title="매매 진행 (매수·매도 시점 순서)"),
            yaxis=dict(ticksuffix="%"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02))
        st.plotly_chart(_cfig, width="stretch", key="contest_curves")
        st.caption(f"내 팀 **{_contest['team']}** · 팀별 수익률 곡선이 한 그래프에 "
                   "겹쳐 표시됩니다. 다른 팀이 세션을 마칠 때마다 갱신 "
                   "(앱 재배포 시 초기화).")
        if st.button("🔄 순위표 새로고침"):
            st.rerun()

    st.subheader("📋 매매 코칭")
    st.markdown("**미흡했던 점**")
    for _b in a["bad"]:
        st.markdown(f"- {_b}")
    st.markdown("**잘한 점**")
    for _g in a["good"]:
        st.markdown(f"- {_g}")

    st.subheader("📑 종목별 성적")
    st.dataframe([
        {"#": i, "종목": h["name"],
         "결과": "관망" if not h["traded"] else f"{h['pnl_pct']:+.1f}%",
         "거래수": h["n_trades"],
         "보유봉": h["hold"] if h["hold"] is not None else "-"}
        for i, h in enumerate(st.session_state.history, 1)
    ], hide_index=True, width="stretch")

    # ── 복기 — 종목별 매매 차트 ──
    st.subheader("🔍 복기 — 종목별 매매 차트")
    st.caption("매수 ▲ · 매도 ▼ 지점이 차트에 찍힙니다. 종목을 펼쳐서 확인하세요.")
    review_figs = []
    for i, h in enumerate(st.session_state.history, 1):
        res = "관망" if not h["traded"] else f"{h['pnl_pct']:+.1f}%"
        fig = build_review_fig(h)
        review_figs.append((i, h, res, fig))
        with st.expander(f"{i}. {h['name']}  ·  {res}  ·  거래 {h['n_trades']}회"):
            st.plotly_chart(fig, width="stretch", key=f"review_{i}")
            st.caption(f"{h['name']} ({h['code']}, {h['market']})  ·  "
                       f"{h['chart']['d0']} ~ {h['chart']['d1']}")

    # 복기 기록 HTML 다운로드 — 나중에 브라우저로 열어 다시 볼 수 있다
    _pl = f" · 손익비 {a['pl_ratio']:.2f}" if a['pl_ratio'] is not None else ""
    _html = ["<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>",
             "<title>매매 복기 기록</title>",
             "<script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script>",
             "<style>body{font-family:sans-serif;max-width:1000px;margin:24px "
             "auto;padding:0 12px;}h2{border-bottom:2px solid #eee;"
             "padding-top:14px;}</style></head><body>",
             f"<h1>매매 복기 기록 — {a['n']}종목</h1>",
             f"<p>세션 수익률 <b>{a['total_ret']:+.2f}%</b> · "
             f"승률 {a['win_rate']:.0f}%{_pl}</p>"]
    for i, h, res, fig in review_figs:
        _html.append(f"<h2>{i}. {h['name']} — {res}</h2>")
        _html.append(fig.to_html(include_plotlyjs=False, full_html=False))
    _html.append("</body></html>")
    st.download_button("📥 복기 기록 저장 (HTML — 나중에 열어볼 수 있음)",
                       "".join(_html), file_name="매매복기.html",
                       mime="text/html")

    if st.button("🔄 새 세션 시작", type="primary"):
        for _k in ("game", "session_done"):
            st.session_state.pop(_k, None)
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

_no = st.session_state.stock_no
_len = session_len_eff()
_contest_now = st.session_state.get("contest")
_tag = f"  ·  🏆 대회 [{_contest_now['code']}] · {_contest_now['team']}" \
    if _contest_now else ""
st.caption(f"종목 {_no} / {_len}"
           f"  ·  정체는 [정답 공개] 또는 종목 종료 시 공개{_tag}")

# ── 컨트롤: 종목 (제목 바로 밑) ──
_last = _no >= _len
n1, n2, n3 = st.columns(3)
if n1.button("🏁 세션 종료 · 결과 보기" if _last
             else f"➡️ 다음 종목 ({_no}/{_len})", width="stretch"):
    if next_stock(int(st.session_state.play_n)):
        st.rerun()
if n2.button("⏹ 지금 끝내기 (결과 보기)", width="stretch",
             help="지금까지 한 종목만으로 세션을 끝내고 리포트를 봅니다"):
    finish_session()
    st.rerun()
if n3.button("🔄 새 세션", width="stretch",
             help="시작 화면으로 — 혼자 연습/대회 모드를 다시 고를 수 있습니다"):
    for _k in ("game", "session_done"):
        st.session_state.pop(_k, None)
    st.rerun()

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

# ── 컨트롤: 매매 ──
_entries = entry_count(g)
_entries_left = MAX_ENTRIES - _entries
t1, t2 = st.columns(2)
with t1:
    _buy_label = (f"🟢 몰빵 매수 (진입 {_entries_left}/{MAX_ENTRIES})"
                  if _entries_left > 0 else "🟢 진입 횟수 소진")
    if st.button(_buy_label, width="stretch",
                 disabled=g["revealed"] or shares > 0 or _entries_left <= 0,
                 help=f"보유 현금 전액으로 현재 종가에 매수 · 한 종목당 {MAX_ENTRIES}회까지"):
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

# ── 정답 (공개되면 차트 바로 위에 표시) ──
if g["revealed"]:
    st.success(
        f"**정답** — {g['name']} ({g['code']}, {g['market']})  ·  "
        f"플레이 구간 {g['dates'][CTX - 1]} ~ {g['dates'][cur]}")

# ── 캔들차트 + 추세선 (커스텀 컴포넌트) ──
# 추세선은 컴포넌트에서 자유롭게 그리고, 봉을 넘겨도 session_state 로 유지된다.
st.session_state.setdefault("trendlines", [])
st.session_state.setdefault("view_all", False)
end = last if g["revealed"] else cur
# 차트 보기 범위 — 기본은 최근 PLOT_WINDOW봉(슬라이딩, 봉 크기 일정),
# [전체 보기] 누르면 지금까지 본 모든 봉을 한 번에 표시.
view_lo = 0 if st.session_state.view_all else max(0, end - PLOT_WINDOW + 1)
xs = list(range(view_lo, end + 1))
_vl = g["l"][view_lo:end + 1]
_vh = g["h"][view_lo:end + 1]
_ymin, _ymax = min(_vl), max(_vh)
_span = (_ymax - _ymin) or 1.0
# 보이는 구간의 최고·최저점 마킹용
_hi_off = _vh.index(_ymax)
_lo_off = _vl.index(_ymin)
peaks = [
    {"x": view_lo + _hi_off, "y": _ymax,
     "label": f"최고 {_ymax:,.0f}원", "kind": "high"},
    {"x": view_lo + _lo_off, "y": _ymin,
     "label": f"최저 {_ymin:,.0f}원", "kind": "low"},
]
markers = []
for tr in g["trades"]:
    if tr["i"] < view_lo:
        continue
    is_buy = tr["type"] == "buy"
    # 마커를 캔들 밖(매수 ▲ 아래 / 매도 ▼ 위)에 띄워 캔들을 가리지 않게
    my = (g["l"][tr["i"]] - _span * 0.045 if is_buy
          else g["h"][tr["i"]] + _span * 0.045)
    markers.append({
        "x": tr["i"], "y": my,
        "sym": "triangle-up" if is_buy else "triangle-down",
        "color": BUY_C if is_buy else SELL_C,
        "text": f"{'매수' if is_buy else '매도'} {tr['n']:,}주 "
                f"@ {tr['price']:,.0f}",
    })
_vol = (g.get("v") or [0.0] * len(g["c"]))[view_lo:end + 1]

# 보기 모드 토글 (차트 위)
_v1, _v2 = st.columns([4, 1])
_visible_n = end - view_lo + 1
_v1.caption(("📊 전체 보기" if st.session_state.view_all
             else f"📊 최근 {_visible_n}봉 (지난 봉은 [전체 보기])"))
_btn_label = ("🎯 최근 200봉으로" if st.session_state.view_all
              else "🔍 전체 보기")
if _v2.button(_btn_label, width="stretch", key="toggle_view"):
    st.session_state.view_all = not st.session_state.view_all
    st.rerun()

chart_val = _replay_chart(
    x=xs, o=g["o"][view_lo:end + 1], h=g["h"][view_lo:end + 1],
    l=g["l"][view_lo:end + 1], c=g["c"][view_lo:end + 1], v=_vol,
    markers=markers, peaks=peaks,
    cur=cur, lo=view_lo, end=end, price=price,
    ymin=_ymin, ymax=_ymax, up=UP, down=DOWN,
    trendlines=st.session_state.trendlines,
    resetToken=st.session_state.get("reset_token", 0), height=560,
    key="replaychart", default=None)
if isinstance(chart_val, dict) and "lines" in chart_val:
    st.session_state.trendlines = chart_val["lines"]

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

# ── 종목 진행 안내 ──
if g["revealed"]:
    st.markdown(
        f"현재 계좌 — 총자산 **{total:,.0f}원**, "
        f"누적 수익 **{total - START_CASH:+,.0f}원 ({ret:+.2f}%)**  ·  "
        "[다음 종목]으로 이어서 하거나 [새 세션]으로 초기화하세요.")
else:
    st.caption("미래 봉은 가려져 있습니다. [다음 봉]으로 전진하세요.")
