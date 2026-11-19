import csv
import datetime as dt
import json
import shutil

import pandas as pd
import pytest

from apex import paper, site, snapshot
from apex.daily import MarketSummary
from apex.db import Database


def _summary():
    idx = pd.bdate_range("2026-01-01", periods=30)
    hist = pd.DataFrame({"加權報酬指數": [1 + i / 100 for i in range(30)],
                         "0050": [1 + i / 90 for i in range(30)],
                         "regime": ["多頭"] * 15 + ["高波動"] * 15}, index=idx)
    ret = pd.DataFrame({"近1月": [0.01, 0.02], "近3月": [-0.1, -0.2], "今年": [0.3, 0.4],
                        "距一年高點": [-0.2, -0.1]}, index=["加權報酬指數", "0050"])
    return MarketSummary(dt.date(2026, 2, 11), dt.date(2026, 2, 12), "高波動", dt.date(2026, 1, 22),
                         -0.07, 0.03, 0.95, 0.09, ret, hist)


def test_site_data_has_market_info_but_no_stock_picks(tmp_path):
    one = {"start": dt.date(2026, 11, 2), "cum": -0.06, "bench_cum": -0.12, "exposure": 0.87}
    perf = {"MLP:lgbm:60:15:3:0.6": pd.DataFrame([one]),
            "A1:15:3+D3": pd.DataFrame()}
    rows = site.paper_rows(perf, paper.TRACKED)
    out = site.build(_summary(), rows, tmp_path, dt.datetime(2026, 2, 12, 21, 30))
    text = out.read_text(encoding="utf-8")
    d = json.loads(text)
    assert d["regime"] == "高波動" and d["stale"] and d["paper"][0]["cum"] == -0.06
    assert len(d["history"]["dates"]) == 30 and d["history"]["regime"][-1] == "高波動"
    assert "2834" not in text and "MLP:lgbm" not in text  # 不公開個股，也只顯示策略說明
    for f in ("index.html", "app.js", "style.css", ".nojekyll"):  # 畫面來自 frontend/
        assert (tmp_path / f).exists()


def test_commitments_hide_picks_and_are_verifiable(tmp_path):
    db = Database()
    paper.ensure_tables(db)
    d = dt.date(2026, 10, 30)
    db.con.execute("INSERT INTO paper_runs VALUES (?, 'S', ?, false, 1, 0.5, ?, 'x', NULL)",
                   [d, dt.datetime(2026, 10, 30, 21, 0), d])
    db.con.execute("INSERT INTO paper_signals VALUES (?, 'S', '2330', 0.5)", [d])
    path = tmp_path / "commitments.csv"
    assert paper.write_commitments(db, path) == 1
    assert paper.write_commitments(db, path) == 0  # 不重複
    row = next(csv.DictReader(path.open(encoding="utf-8")))
    assert "2330" not in path.read_text(encoding="utf-8")
    salt = db.con.execute("SELECT salt FROM paper_runs").fetchone()[0]
    assert row["sha256"] == paper.commitment(d, "S", {"2330": 0.5}, salt)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl")
def test_snapshot_roundtrip(tmp_path, monkeypatch):
    src = tmp_path / "a.duckdb"
    db = Database(src)
    db.con.execute("CREATE TABLE t AS SELECT range AS x FROM range(1000)")
    db.close()
    monkeypatch.setenv(snapshot.KEY_ENV, "test-key")
    snap = tmp_path / "s.tar.enc"
    assert snapshot.export(src, snap) > 0
    assert b"PAR1" not in snap.read_bytes()  # 已加密，看不到 Parquet 內容
    dst = tmp_path / "b.duckdb"
    snapshot.restore(snap, dst)
    assert Database(dst).con.execute("SELECT sum(x) FROM t").fetchone()[0] == sum(range(1000))
    monkeypatch.setenv(snapshot.KEY_ENV, "wrong")
    with pytest.raises(snapshot.SnapshotError):
        snapshot.restore(snap, tmp_path / "c.duckdb")
