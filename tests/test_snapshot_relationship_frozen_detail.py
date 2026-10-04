"""历史定版关系查询“证据明细按封存断言返回”的回归测试（无需真实数据库）。

缺陷（可复现混时态）：定版后撤回某条支撑证据，GET /snapshots/{v}/relationship 的
关系结论与 evidence_path 仍是封存值（取自 snapshot_conclusions），但 path_detail
却去**在线 assertions 表**读取 status，导致逐步状态从 active 变为 retracted——与
同一定版封存的专题成果包证据明细（取自 snapshots.assertions 冻结副本）不一致。

修复口径：
- 定版关系查询的 path_detail 只按**定版封存时断言**（snapshots.assertions）逐步
  展开，封存时断言均生效，逐步 status 恒为 active；
- 查询过程不读取在线 assertions 表：后续正式批次撤回、野外包勘误替换、推演并入
  撤回路径证据后，原版结论 / evidence_path / path_detail 均保持封存口径；
- 当前正式关系的现时状态仍由 GET /relationship 体现（本测试不在此覆盖）；
- 输入、响应字段、路径优选、未知定版（404 SNAPSHOT_NOT_FOUND）/ 未知地层
  （400 INVALID_STRATUM）错误约定不变；查询以 REPEATABLE READ READ ONLY
  快照事务开场，不写入断言或修订。

运行：python3 tests/test_snapshot_relationship_frozen_detail.py
"""
from __future__ import annotations

import os
import sys
import types
from contextlib import contextmanager

# app.db 顶部 import psycopg2 / psycopg2.pool；本测试不需要真实驱动
_psycopg2 = types.ModuleType("psycopg2")
_psycopg2_pool = types.ModuleType("psycopg2.pool")
_psycopg2_extras = types.ModuleType("psycopg2.extras")
_psycopg2_extras.Json = lambda obj: obj
_psycopg2.pool = _psycopg2_pool
_psycopg2.extras = _psycopg2_extras
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.pool", _psycopg2_pool)
sys.modules.setdefault("psycopg2.extras", _psycopg2_extras)

try:  # pragma: no cover - 有真实 pydantic 的环境直接使用
    import pydantic  # noqa: F401
except ImportError:
    _pydantic = types.ModuleType("pydantic")

    class BaseModel:  # noqa: D401
        pass

    def Field(default=None, **kwargs):  # noqa: D401
        return default

    _pydantic.BaseModel = BaseModel
    _pydantic.Field = Field
    sys.modules.setdefault("pydantic", _pydantic)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import service as service_mod  # noqa: E402
from app.errors import ApiError  # noqa: E402


# ------------------------------------------------------------------ 内存假连接
class FakeCursor:
    """只实现定版关系查询所需的 SELECT；任何在线 assertions 表读取都视为缺陷。"""

    def __init__(self, state: dict, log: list):
        self.state = state
        self.log = log
        self._rows = []

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.log.append(norm)

        if norm.upper().startswith("SET TRANSACTION"):
            assert "REPEATABLE READ" in norm.upper()
            assert "READ ONLY" in norm.upper()
            self._rows = []
            return
        # 兼容修复前（无只读快照事务开场）：不强制要求 SET TRANSACTION

        upper = norm.upper()

        if norm == "SELECT revision, assertions FROM snapshots WHERE version = %s":
            snap = self.state["snapshots"].get(params[0])
            self._rows = [] if snap is None else [(snap["revision"], list(snap["assertions"]))]
        elif norm == "SELECT revision FROM snapshots WHERE version = %s":
            # 兼容修复前的定版查询语句形态（只取 revision，封存副本未读）
            snap = self.state["snapshots"].get(params[0])
            self._rows = [] if snap is None else [(snap["revision"],)]
        elif norm == "SELECT 1 FROM strata WHERE stratum_id = %s":
            self._rows = [(1,)] if params[0] in self.state["strata"] else []
        elif norm == (
            "SELECT relation, path FROM snapshot_conclusions "
            "WHERE version = %s AND stratum_a = %s AND stratum_b = %s"
        ):
            row = self.state["conclusions"].get((params[0], params[1], params[2]))
            self._rows = [] if row is None else [row]
        elif "FROM ASSERTIONS WHERE EVIDENCE_ID = ANY(%S)" in upper:
            # 修复前 path_detail 读在线表：按 live_assertions_status 返回现时状态，
            # 从而把已撤回证据显示为 retracted（缺陷复现）。修复后不会执行到这里。
            detail = {
                "E1": ("E1", "L1", "L2", "same_period", self.state["live_assertions_status"]["E1"]),
                "E2": ("E2", "L2", "L3", "earlier_than", self.state["live_assertions_status"]["E2"]),
            }
            self._rows = [detail[e] for e in params[0] if e in detail]
        elif "FROM ASSERTIONS" in upper:
            raise AssertionError(f"定版关系查询不得读取在线 assertions 表: {sql}")
        else:
            raise AssertionError(f"unexpected SQL in snapshot relationship query: {sql}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _CursorCM:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, state):
        self.state = state
        self.sql_log = []
        self.committed = False

    def cursor(self):
        return _CursorCM(FakeCursor(self.state, self.sql_log))


@contextmanager
def _fake_conn(state):
    conn = FakeConn(state)
    yield conn
    conn.committed = True  # 查询事务正常结束（只读，不产生任何写入语句）


def run_snapshot(state, version, a, b):
    original = service_mod.db_conn
    service_mod.db_conn = lambda: _fake_conn(state)
    try:
        return service_mod.query_snapshot_relationship(version, a, b)
    finally:
        service_mod.db_conn = original


# ------------------------------------------------------------------ 场景构造
def frozen_state():
    """定版 1（修订 1）：E1(L1~L2) + E2(L2<L3) => L1 早于 L3，最优路径 [E1, E2]。

    在线断言表中 E2 已在修订 2 被正式批次撤回（也可代表野外包勘误替换 /
    推演并入撤回）：现时 L1、L3 已为未定，但封存明细不得受其影响。
    """
    return {
        "strata": {"L1", "L2", "L3"},
        # 在线表当前状态（修复后定版查询完全不读它；保留仅为证明不被触碰）
        "live_assertions_status": {"E1": "active", "E2": "retracted"},
        "snapshots": {
            1: {
                "revision": 1,
                "assertions": [
                    {"evidence_id": "E1", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
                    {"evidence_id": "E2", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
                ],
            }
        },
        "conclusions": {
            (1, "L1", "L3"): ("earlier_than", ["E1", "E2"]),
            (1, "L3", "L1"): ("later_than", ["E2", "E1"]),
            (1, "L1", "L2"): ("same_period", ["E1"]),
        },
    }


def detail_by_id(resp):
    return {step["evidence_id"]: step for step in resp["path_detail"]}


# ------------------------------------------------------------------ 用例
def test_path_detail_uses_sealed_assertions_after_retraction():
    """定版后撤回支撑证据：结论/路径/逐步明细全部保持封存值，逐步 status 恒 active。"""
    state = frozen_state()
    resp = run_snapshot(state, 1, "L1", "L3")
    assert resp["relation"] == "earlier_than"
    assert resp["relation_label"] == "早于"
    assert resp["evidence_path"] == ["E1", "E2"]
    assert resp["version"] == 1
    assert resp["snapshot_revision"] == 1
    assert resp["stratum_a"] == "L1" and resp["stratum_b"] == "L3"
    detail = detail_by_id(resp)
    assert set(detail) == {"E1", "E2"}
    assert detail["E1"] == {
        "evidence_id": "E1",
        "stratum_a": "L1",
        "stratum_b": "L2",
        "relation": "same_period",
        "status": "active",
    }
    assert detail["E2"] == {
        "evidence_id": "E2",
        "stratum_a": "L2",
        "stratum_b": "L3",
        "relation": "earlier_than",
        # 缺陷复现点：修复前此处为在线表的 "retracted"
        "status": "active",
    }


def test_reverse_direction_keeps_sealed_path_order():
    """反向查询（晚于）路径顺序与封存断言一致，明细按路径顺序逐步返回。"""
    state = frozen_state()
    resp = run_snapshot(state, 1, "L3", "L1")
    assert resp["relation"] == "later_than"
    assert resp["evidence_path"] == ["E2", "E1"]
    assert [step["evidence_id"] for step in resp["path_detail"]] == ["E2", "E1"]
    assert all(step["status"] == "active" for step in resp["path_detail"])


def test_single_step_same_period_detail_sealed():
    """单证据同一期路径同样按封存副本展开。"""
    state = frozen_state()
    resp = run_snapshot(state, 1, "L1", "L2")
    assert resp["relation"] == "same_period"
    assert resp["evidence_path"] == ["E1"]
    assert detail_by_id(resp)["E1"]["status"] == "active"


def test_undetermined_pair_has_no_path_or_detail():
    """封存时未定的地层对（无结论行）：关系未定、路径为 null、明细为空（约定不变）。"""
    state = frozen_state()
    # L1 与 L3 封存时确定；新增互不相干的地层 L9 使 (L1, L9) 在结论表中无行
    state["strata"].add("L9")
    resp = run_snapshot(state, 1, "L1", "L9")
    assert resp["relation"] == "undetermined"
    assert resp["relation_label"] == "未定"
    assert resp["evidence_path"] is None
    assert resp["path_detail"] == []


def test_same_stratum_is_same_period_empty_path():
    """同一地层查询：同一期、空路径、空明细（约定不变）。"""
    state = frozen_state()
    resp = run_snapshot(state, 1, "L1", "L1")
    assert resp["relation"] == "same_period"
    assert resp["evidence_path"] == []
    assert resp["path_detail"] == []


def test_query_never_reads_live_assertions_table():
    """整个查询过程只读取定版封存数据，不发任何针对在线 assertions 表的语句。"""
    state = frozen_state()
    conn_holder = {}

    @contextmanager
    def factory():
        conn = FakeConn(state)
        conn_holder["conn"] = conn
        yield conn

    original = service_mod.db_conn
    service_mod.db_conn = factory
    try:
        resp = service_mod.query_snapshot_relationship(1, "L1", "L3")
    finally:
        service_mod.db_conn = original
    assert resp["evidence_path"] == ["E1", "E2"]
    assert all("FROM ASSERTIONS" not in s.upper() for s in conn_holder["conn"].sql_log)
    assert any(s.upper().startswith("SET TRANSACTION") for s in conn_holder["conn"].sql_log)


def test_detail_matches_thematic_package_sealed_detail():
    """与同一定版封存的成果包证据明细口径一致：均来自 snapshots.assertions、status=active。"""
    state = frozen_state()
    sealed_by_id = {
        a["evidence_id"]: {
            "evidence_id": a["evidence_id"],
            "stratum_a": a["stratum_a"],
            "stratum_b": a["stratum_b"],
            "relation": a["relation"],
            "status": "active",
        }
        for a in state["snapshots"][1]["assertions"]
    }
    resp = run_snapshot(state, 1, "L1", "L3")
    assert [step["evidence_id"] for step in resp["path_detail"]] == resp["evidence_path"]
    for step in resp["path_detail"]:
        sealed = sealed_by_id[step["evidence_id"]]
        assert step["stratum_a"] == sealed["stratum_a"]
        assert step["stratum_b"] == sealed["stratum_b"]
        assert step["relation"] == sealed["relation"]
        assert step["status"] == sealed["status"] == "active"


def test_unknown_snapshot_404_convention_unchanged():
    """未知定版仍返回 404 SNAPSHOT_NOT_FOUND，且不检查地层以外的任何数据。"""
    state = frozen_state()
    try:
        run_snapshot(state, 999, "L1", "L3")
    except ApiError as exc:
        assert exc.status_code == 404
        assert exc.code == "SNAPSHOT_NOT_FOUND"
        assert exc.details == {"version": 999}
    else:
        raise AssertionError("expected SNAPSHOT_NOT_FOUND")


def test_unknown_stratum_400_convention_unchanged():
    """未知地层仍返回 400 INVALID_STRATUM（定版存在时才检查地层，顺序约定不变）。"""
    state = frozen_state()
    try:
        run_snapshot(state, 1, "L1", "LX")
    except ApiError as exc:
        assert exc.status_code == 400
        assert exc.code == "INVALID_STRATUM"
        assert exc.details == {"stratum_id": "LX"}
    else:
        raise AssertionError("expected INVALID_STRATUM")


def test_query_emits_no_write_sql():
    """查询事务内不出现任何写入语句（不写断言或修订）。"""
    state = frozen_state()
    conn_holder = {}

    @contextmanager
    def factory():
        conn = FakeConn(state)
        conn_holder["conn"] = conn
        yield conn

    original = service_mod.db_conn
    service_mod.db_conn = factory
    try:
        service_mod.query_snapshot_relationship(1, "L1", "L3")
    finally:
        service_mod.db_conn = original
    for stmt in conn_holder["conn"].sql_log:
        head = stmt.upper().split(" ", 1)[0]
        assert head in {"SET", "SELECT"}, stmt


ALL_TESTS = [
    test_path_detail_uses_sealed_assertions_after_retraction,
    test_reverse_direction_keeps_sealed_path_order,
    test_single_step_same_period_detail_sealed,
    test_undetermined_pair_has_no_path_or_detail,
    test_same_stratum_is_same_period_empty_path,
    test_query_never_reads_live_assertions_table,
    test_detail_matches_thematic_package_sealed_detail,
    test_unknown_snapshot_404_convention_unchanged,
    test_unknown_stratum_400_convention_unchanged,
    test_query_emits_no_write_sql,
]


def main():
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
    print(f"ALL {len(ALL_TESTS)} SNAPSHOT-FROZEN-DETAIL TESTS PASSED")


if __name__ == "__main__":
    main()
