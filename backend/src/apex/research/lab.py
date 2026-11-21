"""研究環境與標準報表（docs/研究報告/策略研究計畫.md §6）。

`Lab` 載入一次資料（行情、特徵庫、市場狀態、加權報酬指數），之後可以反覆回測策略，
每次都產生同一份報表：全期指標、相對基準、逐年、分市場狀態、過關檢查，並寫入試驗紀錄。
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from apex.backtest import CostModel, load, run, summarize
from apex.backtest.engine import BacktestResult, Context, Strategy
from apex.backtest.metrics import yearly_returns
from apex.backtest.strategies import buy_and_hold, liquid_universe
from apex.db import Database
from apex.research import trials
from apex.research.features import Features
from apex.research.regime import by_regime, index_series, market_regime


@dataclass(frozen=True)
class Thresholds:
    """過關門檻（docs/研究報告/策略研究計畫.md §6；最大回撤 −30% 由使用者 2026-11-19 決定）。"""

    max_drawdown: float = -0.30
    beat_year_ratio: float = 0.6
    random_percentile: float = 0.95
    goal: str = "beat"  # 'beat'：勝過大盤與 0050；'swing'：波段、低回撤（不要求報酬高於 0050）
    target_drawdown: float | None = None  # 希望達到的回撤（未達不算失敗，只標示）


# 2026-11-19 使用者決定：要的是 1～2 個月會上漲的股票、跌得比較少；回撤目標 −25%，必要時可到 −30%
GOALS = {
    "beat": Thresholds(),
    "swing": Thresholds(goal="swing", max_drawdown=-0.30, target_drawdown=-0.25),
    # 2026-11-21 使用者決定：主要資金放 0050 保底；額外資金（約 10～20%）找 2～5 個月內大漲的股票，
    # 這部分最多可接受 −40%（docs/研究報告/大漲股研究.md）
    "big": Thresholds(goal="big", max_drawdown=-0.40, random_percentile=0.90),
}


def _pct(x: float | None) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.2%}"


def _num(x: float | None) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.2f}"


@dataclass
class Report:
    spec: str
    start: dt.date
    end: dt.date
    freq: str
    capital: float
    metrics: dict[str, float]
    bench: dict[str, dict[str, float]]  # 'TAIEX_TR'、'0050' → 指標
    yearly: pd.DataFrame
    regimes: pd.DataFrame
    checks: dict[str, tuple[bool | None, str]]
    replacement: float
    random: pd.DataFrame | None = None
    trial_id: int | None = None
    events: list[str] = field(default_factory=list)
    equity: pd.Series | None = None

    @property
    def passed(self) -> bool:
        return all(ok for ok, _ in self.checks.values() if ok is not None)

    def text(self) -> str:
        m, tr, e50 = self.metrics, self.bench["TAIEX_TR"], self.bench["0050"]
        rows = [("年化報酬", "cagr", _pct), ("波動度", "volatility", _pct), ("Sharpe", "sharpe", _num),
                ("Sortino", "sortino", _num), ("最大回撤", "max_drawdown", _pct), ("Calmar", "calmar", _num)]
        lines = [
            f"策略 {self.spec}｜{self.start}～{self.end}｜本金 {self.capital:,.0f}｜調整 {self.freq}"
            + (f"｜試驗 #{self.trial_id}" if self.trial_id else ""),
            "",
            "| 指標 | 策略 | 報酬指數 | 0050 |",
            "|---|---|---|---|",
        ]
        lines += [f"| {label} | {f(m.get(k))} | {f(tr.get(k))} | {f(e50.get(k))} |" for label, k, f in rows]
        lines += [
            "",
            f"持股比例 {m.get('exposure', float('nan')):.0%}｜年週轉 {_num(m.get('turnover'))}｜"
            f"成本拖累 {_pct(m.get('cost_drag'))}｜每次換股比例 {self.replacement:.0%}｜"
            f"交易 {int(m.get('n_trades', 0))} 筆｜勝率 {_pct(m.get('win_rate'))}｜"
            f"Profit Factor {_num(m.get('profit_factor'))}",
            "",
            "逐年報酬：",
            self.yearly.to_string(float_format=lambda x: f"{x:+.1%}"),
            "",
            "分市場狀態（年化報酬，依前一日狀態）：",
            self.regimes.to_string(formatters={"策略": _pct, "報酬指數": _pct, "天數": lambda x: f"{x:.0f}"}),
        ]
        if self.random is not None and len(self.random):
            r = self.random
            lines += ["", f"隨機選股 {len(r)} 次（同檔數、同調整頻率、同換股比例）：年化報酬 "
                      f"5%={r['cagr'].quantile(.05):+.2%}、中位數={r['cagr'].median():+.2%}、"
                      f"95%={r['cagr'].quantile(.95):+.2%}"]
        lines += ["", "過關檢查："]
        for name, (ok, detail) in self.checks.items():
            mark = "—" if ok is None else ("✓" if ok else "✗")
            lines.append(f"  {mark} {name}：{detail}")
        lines.append(f"結論：{'通過' if self.passed else '未通過'}（參數穩定性與 DSR 需另外檢查）")
        return "\n".join(lines)


class Recorder:
    """包住策略，記錄每個訊號日的目標權重。"""

    def __init__(self, strategy: Strategy):
        self.strategy = strategy
        self.targets: dict[pd.Timestamp, dict[str, float]] = {}

    def __call__(self, ctx: Context) -> dict[str, float]:
        w = self.strategy(ctx)
        self.targets[ctx.date] = w
        return w


def replacement_rate(targets: dict[pd.Timestamp, dict[str, float]]) -> float:
    """平均每次調整換掉的持股比例（新名單中不在舊名單者 ÷ 新名單檔數）。"""
    sets = [{c for c, w in t.items() if w > 0} for _, t in sorted(targets.items())]
    rates = [len(b - a) / len(b) for a, b in zip(sets, sets[1:], strict=False) if b]
    return float(np.mean(rates)) if rates else 0.0


def strip_core(targets: dict[pd.Timestamp, dict[str, float]], core: str | None) -> dict:
    """去掉核心標的，只留衛星持股（算檔數與換股比例用）。"""
    if core is None:
        return targets
    return {d: {c: w for c, w in t.items() if c != core} for d, t in targets.items()}


def random_strategy(n: int, replace: float, seed: int) -> Strategy:
    """隨機選股對照：從同一個可投資範圍隨機選 n 檔，等權重；每次調整換掉 replace 比例的持股。

    已不在可投資範圍的持股一定換掉，並計入換股比例。
    """
    rng = np.random.default_rng(seed)
    held: list[str] = []

    def strategy(ctx: Context) -> dict[str, float]:
        nonlocal held
        universe = sorted(liquid_universe(ctx))
        if not universe:
            return {}
        uset = set(universe)
        keep = [c for c in held if c in uset]
        n_drop = max(0, round(replace * len(held)) - (len(held) - len(keep))) if held else 0
        if n_drop and keep:
            keep = sorted(rng.choice(keep, size=max(0, len(keep) - n_drop), replace=False))
        pool = sorted(uset - set(keep))
        size = min(n - len(keep), len(pool))
        add = list(rng.choice(pool, size=size, replace=False)) if size > 0 else []
        held = sorted(str(c) for c in keep + add)
        return {c: 1 / len(held) for c in held}
    return strategy


RANDOM_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_random (
    n INTEGER, replace DOUBLE, freq VARCHAR, start_date DATE, end_date DATE, capital DOUBLE, costs VARCHAR,
    variant VARCHAR, seed INTEGER, cagr DOUBLE, sharpe DOUBLE, max_drawdown DOUBLE,
    PRIMARY KEY (n, replace, freq, start_date, end_date, capital, costs, variant, seed)
);
"""
RANDOM_KEY = """n = ? AND replace = ? AND freq = ? AND start_date = ? AND end_date = ? AND capital = ?
                AND costs = ? AND variant = ?"""


def ensure_random_table(db: Database) -> None:
    """建立 research_random；舊版沒有 variant 欄位時搬移資料（variant＝''，純隨機選股）。"""
    cols = [r[0] for r in db.con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'research_random'").fetchall()]
    if cols and "variant" not in cols:
        db.con.execute("ALTER TABLE research_random RENAME TO research_random_old")
        db.con.execute(RANDOM_SCHEMA)
        db.con.execute("""INSERT INTO research_random SELECT n, replace, freq, start_date, end_date, capital,
                          costs, '', seed, cagr, sharpe, max_drawdown FROM research_random_old""")
        db.con.execute("DROP TABLE research_random_old")
    else:
        db.con.execute(RANDOM_SCHEMA)


class Lab:
    def __init__(self, db: Database, start: dt.date, end: dt.date, capital: float = 300_000,
                 costs: CostModel | None = None, thresholds: Thresholds | None = None):
        self.db = db
        self.capital = capital
        self.costs = costs or CostModel()
        self.thresholds = thresholds or Thresholds()
        self.data = load(db, start, end)
        self.features = Features(self.data)
        self.regime = market_regime(self.data)
        self.tr = index_series(self.data, "TAIEX_TR")
        self._bench: dict[tuple, BacktestResult] = {}

    def backtest(self, strategy: Strategy, start: dt.date, end: dt.date, freq: str = "M",
                 capital: float | None = None) -> tuple[BacktestResult, Recorder]:
        rec = Recorder(strategy)
        result = run(self.data, rec, start, end, capital or self.capital, getattr(strategy, "freq", freq),
                     self.costs, getattr(strategy, "min_trade", 0.0))
        return result, rec

    def benchmark_0050(self, start: dt.date, end: dt.date, capital: float) -> BacktestResult:
        key = (start, end, capital)
        if key not in self._bench:
            self._bench[key] = run(self.data, buy_and_hold("0050"), start, end, capital, "once", self.costs)
        return self._bench[key]

    def random_distribution(self, n: int, replace: float, freq: str, start: dt.date, end: dt.date,
                            capital: float, runs: int = 100,
                            progress: Callable[[int], None] | None = None,
                            wrap: Callable[[Strategy], Strategy] | None = None,
                            variant: str = "") -> pd.DataFrame:
        """隨機選股的績效分布；結果存在 research_random，相同設定不重算。

        wrap 把隨機選股包成同樣的組合（例如核心衛星的衛星部位），variant 是它的快取名稱。
        """
        ensure_random_table(self.db)
        replace = round(replace * 20) / 20  # 以 5% 為單位，方便重複使用
        key = [n, replace, freq, start, end, capital, json.dumps(vars(self.costs), sort_keys=True), variant]
        done = {r[0] for r in self.db.con.execute(
            f"SELECT seed FROM research_random WHERE {RANDOM_KEY}", key).fetchall()}
        for seed in range(runs):
            if seed in done:
                continue
            rs = random_strategy(n, replace, seed)
            result = run(self.data, wrap(rs) if wrap else rs, start, end, capital, freq, self.costs)
            m = summarize(result.equity)
            self.db.con.execute("INSERT OR REPLACE INTO research_random VALUES (" + ", ".join("?" * 12) + ")",
                                [*key, seed, m["cagr"], m["sharpe"], m["max_drawdown"]])
            if progress:
                progress(seed)
        return self.db.con.execute(f"""
            SELECT seed, cagr, sharpe, max_drawdown FROM research_random
            WHERE {RANDOM_KEY} AND seed < ? ORDER BY seed
        """, [*key, runs]).df()

    def evaluate(self, spec: str, strategy: Strategy, start: dt.date, end: dt.date, freq: str = "M",
                 capital: float | None = None, family: str = "", period: str = "custom", random_runs: int = 0,
                 record: bool = True, note: str = "") -> Report:
        capital = capital or self.capital
        freq = getattr(strategy, "freq", freq)  # 出場規則每天檢查
        result, rec = self.backtest(strategy, start, end, freq, capital)
        eq = result.equity
        m = summarize(eq, result.trades, self.tr, result.cash)
        b50 = self.benchmark_0050(start, end, capital)
        tr = self.tr.reindex(eq.index).ffill()
        bench = {"TAIEX_TR": summarize(tr), "0050": summarize(b50.equity)}

        ys, y50 = yearly_returns(eq, tr), yearly_returns(b50.equity)
        yearly = pd.DataFrame({"策略": ys.iloc[:, 0], "報酬指數": ys.iloc[:, 1], "0050": y50.iloc[:, 0]})
        yearly["超額(指數)"] = yearly["策略"] - yearly["報酬指數"]
        yearly["超額(0050)"] = yearly["策略"] - yearly["0050"]
        n_days = eq.groupby(eq.index.year).size().reindex(yearly.index).fillna(0)
        full = yearly[n_days.to_numpy() >= 120]  # 不足半年的年份不計逐年勝率

        reg = self.regime["regime"].reindex(eq.index)
        rs, rt = by_regime(eq.pct_change(), reg), by_regime(tr.pct_change(), reg)
        regimes = pd.DataFrame({"策略": rs["ann_return"], "報酬指數": rt["ann_return"], "天數": rs["days"]})

        # 核心衛星：檔數與換股比例只算衛星部位，隨機對照＝同樣核心＋隨機衛星
        core = getattr(strategy, "core", None)
        targets = strip_core(rec.targets, core)
        month_ends = getattr(strategy, "month_ends", None)
        if month_ends is not None:  # 每天檢查的策略：換股比例只看月底選股
            targets = {d: t for d, t in targets.items() if d in month_ends}
        replace = replacement_rate(targets)
        rnd = None
        if random_runs:
            sizes = [len(t) for t in targets.values() if t]
            n = int(np.median(sizes)) if sizes else 0
            wrap, variant = None, ""
            if core is not None:
                wrap = strategy.with_satellite
                variant = f"CS:{strategy.core_weight:g}:{core}:{strategy.core_regime}"
            if n:
                rnd = self.random_distribution(n, replace, freq, start, end, capital, random_runs,
                                               wrap=wrap, variant=variant)

        checks = self._checks(m, bench, full, rnd)
        report = Report(spec, start, end, freq, capital, m, bench, yearly, regimes, checks, replace, rnd,
                        events=result.events, equity=eq)
        if record:
            report.trial_id = trials.record(self.db, family=family, strategy=spec, period=period, start=start,
                                            end=end, freq=freq, capital=capital, costs=vars(self.costs),
                                            metrics=m, equity=eq, note=note)
        return report

    def _checks(self, m: dict, bench: dict, full: pd.DataFrame,
                rnd: pd.DataFrame | None) -> dict[str, tuple[bool | None, str]]:
        th = self.thresholds
        if th.goal == "swing":
            return self._swing_checks(m, bench, full, rnd)
        if th.goal == "big":
            return self._big_checks(m, bench, full, rnd)
        tr, e50 = bench["TAIEX_TR"], bench["0050"]
        ex = full["超額(指數)"]
        beat = float((ex > 0).mean()) if len(ex) else float("nan")
        rest = (1 + ex).drop(ex.idxmax()).prod() - 1 if len(ex) > 2 else None
        checks: dict[str, tuple[bool | None, str]] = {
            "勝過報酬指數": (bool(m["cagr"] > tr["cagr"]), f"年化 {m['cagr']:+.2%} vs {tr['cagr']:+.2%}"),
            "勝過 0050": (bool(m["cagr"] > e50["cagr"]), f"年化 {m['cagr']:+.2%} vs {e50['cagr']:+.2%}"),
            "Sharpe 高於 0050": (bool(m["sharpe"] > e50["sharpe"]),
                                f"{m['sharpe']:.2f} vs {e50['sharpe']:.2f}"),
            "最大回撤": (bool(m["max_drawdown"] >= th.max_drawdown),
                     f"{m['max_drawdown']:+.1%}（上限 {th.max_drawdown:.0%}）"),
            "逐年勝率": (bool(beat >= th.beat_year_ratio),
                     f"{beat:.0%} 的完整年份勝過報酬指數（門檻 {th.beat_year_ratio:.0%}）"),
            "不靠單一年份": (None, "年份不足") if rest is None else
            (bool(rest > 0), f"扣除超額最好的一年後，其餘年份累積超額 {rest:+.1%}"),
        }
        if rnd is None or not len(rnd):
            checks["隨機選股"] = (None, "未計算")
        else:
            pct = float((rnd["cagr"] < m["cagr"]).mean())
            checks["隨機選股"] = (pct >= th.random_percentile,
                              f"高於 {pct:.0%} 的隨機選股（門檻 {th.random_percentile:.0%}）")
        return checks

    def _big_checks(self, m: dict, bench: dict, full: pd.DataFrame,
                    rnd: pd.DataFrame | None) -> dict[str, tuple[bool | None, str]]:
        """大漲股（額外資金）：賺錢、回撤在 −40% 內、明顯勝過隨機選股；和 0050 的比較只列出、不算過關條件。"""
        th = self.thresholds
        e50 = bench["0050"]
        dd = m["max_drawdown"]
        checks: dict[str, tuple[bool | None, str]] = {
            "年化為正": (bool(m["cagr"] > 0), f"年化 {m['cagr']:+.2%}"),
            "最大回撤": (bool(dd >= th.max_drawdown), f"{dd:+.1%}（上限 {th.max_drawdown:.0%}）"),
            "和 0050 比較": (None, f"年化 {m['cagr']:+.2%} vs 0050 {e50['cagr']:+.2%}（只列出）"),
        }
        if rnd is None or not len(rnd):
            checks["隨機選股"] = (None, "未計算")
        else:
            pct = float((rnd["cagr"] < m["cagr"]).mean())
            checks["隨機選股"] = (pct >= th.random_percentile,
                              f"高於 {pct:.0%} 的隨機選股（門檻 {th.random_percentile:.0%}）")
        return checks

    def _swing_checks(self, m: dict, bench: dict, full: pd.DataFrame,
                      rnd: pd.DataFrame | None) -> dict[str, tuple[bool | None, str]]:
        """波段、低回撤目標：賺錢、比 0050 穩、回撤受控、逐年多數賺錢、不是運氣。"""
        th = self.thresholds
        e50 = bench["0050"]
        yr = full["策略"]
        up = float((yr > 0).mean()) if len(yr) else float("nan")
        rest = (1 + yr).drop(yr.idxmax()).prod() - 1 if len(yr) > 2 else None
        dd = m["max_drawdown"]
        target = ""
        if th.target_drawdown is not None:
            target = f"，目標 {th.target_drawdown:.0%}{'已達成' if dd >= th.target_drawdown else '未達'}"
        checks: dict[str, tuple[bool | None, str]] = {
            "年化為正": (bool(m["cagr"] > 0), f"年化 {m['cagr']:+.2%}"),
            "Sharpe 高於 0050": (bool(m["sharpe"] > e50["sharpe"]),
                                f"{m['sharpe']:.2f} vs {e50['sharpe']:.2f}"),
            "最大回撤": (bool(dd >= th.max_drawdown),
                     f"{dd:+.1%}（上限 {th.max_drawdown:.0%}{target}；0050 {e50['max_drawdown']:+.1%}）"),
            "逐年賺錢": (bool(up >= th.beat_year_ratio),
                     f"{up:.0%} 的完整年份報酬為正（門檻 {th.beat_year_ratio:.0%}）"),
            "不靠單一年份": (None, "年份不足") if rest is None else
            (bool(rest > 0), f"扣除最好的一年後，其餘年份累積報酬 {rest:+.1%}"),
        }
        if rnd is None or not len(rnd):
            checks["隨機選股"] = (None, "未計算")
        else:
            pct = float((rnd["cagr"] < m["cagr"]).mean())
            checks["隨機選股"] = (pct >= th.random_percentile,
                              f"高於 {pct:.0%} 的隨機選股（門檻 {th.random_percentile:.0%}）")
        return checks
