"""服务层业务规则测试：拆并单、挂起续办、拒收、幂等、并发。"""
from __future__ import annotations

import threading

import pytest

from app import services
from app.schemas import BatchIn, NewWaybillIn, PackageIn, WaybillIn


def wb(no: str, batches: dict[str, int]) -> WaybillIn:
    return WaybillIn(
        waybill_no=no,
        batches=[BatchIn(batch_no=k, weight_kg=v) for k, v in batches.items()],
    )


def pkg(no: str, old: list[str], new: dict[str, dict[str, int]]) -> PackageIn:
    return PackageIn(
        package_no=no,
        old_waybill_nos=old,
        new_waybills=[
            NewWaybillIn(
                waybill_no=n,
                batches=[BatchIn(batch_no=k, weight_kg=v) for k, v in b.items()],
            )
            for n, b in new.items()
        ],
    )


# --------------------------------------------------------------------------- #
def test_split_and_merge_roundtrip(temp_db):
    """一张原单拆成两张，再并回一张，逐货批重量始终相等。"""
    services.create_waybill(wb("W1", {"B1": 100, "B2": 200}))

    view, replayed = services.submit_package(
        pkg("P1", ["W1"], {"W1-A": {"B1": 100}, "W1-B": {"B2": 200}})
    )
    assert replayed is False
    assert view["status"] == "applied"

    actives = [w["waybill_no"] for w in services.list_waybills("active")]
    assert actives == ["W1-A", "W1-B"]
    old = services.get_waybill("W1")
    assert old["status"] == "replaced" and old["replaced_by_pkg"] == "P1"

    # 并单：两张有效单并回一张
    view2, _ = services.submit_package(
        pkg("P2", ["W1-A", "W1-B"], {"W1-M": {"B1": 100, "B2": 200}})
    )
    assert view2["status"] == "applied"
    actives = [w["waybill_no"] for w in services.list_waybills("active")]
    assert actives == ["W1-M"]
    assert services.stats()["active_total_weight_kg"] == 300


def test_suspended_then_auto_resume(temp_db):
    """旧单未到齐挂起；补齐原单后自动续办并生效。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    view, _ = services.submit_package(
        pkg("P1", ["W1", "W2"], {"N1": {"B1": 100}})
    )
    assert view["status"] == "suspended"
    assert "W2" in view["reason"]

    # 补齐缺失旧单：W2 与 W1 记同一货批，续办按合计 50+50=100 校验后生效
    services.create_waybill(wb("W2", {"B1": 50}))

    pkgs = {p["package_no"]: p for p in services.list_packages()}
    assert pkgs["P1"]["status"] == "applied"
    assert pkgs["P1"]["reason"] is None
    actives = [w["waybill_no"] for w in services.list_waybills("active")]
    assert "N1" in actives and "W1" not in actives and "W2" not in actives


def test_rejected_when_old_waybill_already_replaced(temp_db):
    services.create_waybill(wb("W1", {"B1": 10}))
    services.create_waybill(wb("W2", {"B2": 20}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 10}}))
    # W1 已被 P1 替换，P2 再引用 W1 -> 整包拒收
    view, _ = services.submit_package(
        pkg("P2", ["W1", "W2"], {"N2": {"B1": 10, "B2": 20}})
    )
    assert view["status"] == "rejected"
    assert "W1" in view["reason"]
    # W2 不受影响，仍然有效
    assert services.get_waybill("W2")["status"] == "active"


def test_suspended_package_rejected_on_resume_when_old_got_replaced(temp_db):
    """挂起中的包，续办时若发现旧单已被别的包替换 -> 转拒收。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    # P1 挂起等待 W2
    services.submit_package(
        pkg("P1", ["W1", "W2"], {"N1": {"B1": 10, "B2": 5}})
    )
    # 先把 W1 用另一个包替换掉
    services.submit_package(pkg("P2", ["W1"], {"N2": {"B1": 10}}))
    # 再录入 W2 触发续办
    services.create_waybill(wb("W2", {"B2": 5}))
    pkgs = {p["package_no"]: p for p in services.list_packages()}
    assert pkgs["P1"]["status"] == "rejected"
    assert "替换" in pkgs["P1"]["reason"]
    # P2 的新单 N2 仍有效
    assert services.get_waybill("N2")["status"] == "active"


def test_existing_new_no_rejected(temp_db):
    """新单号与现存运单（含有效单）冲突 -> 拒收。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    services.create_waybill(wb("W2", {"B1": 10}))
    view, _ = services.submit_package(pkg("P1", ["W1"], {"W2": {"B1": 10}}))
    assert view["status"] == "rejected"
    assert "W2" in view["reason"]


def test_reused_new_no_rejected_even_after_replacement(temp_db):
    """新单号“从未生效”：历史上生效过、即使已被替换也不可复用。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 10}}))
    services.submit_package(pkg("P2", ["N1"], {"N2": {"B1": 10}}))
    # N1 已被替换，尝试再次启用 N1
    view, _ = services.submit_package(pkg("P3", ["N2"], {"N1": {"B1": 10}}))
    assert view["status"] == "rejected"
    assert "N1" in view["reason"]


def test_weight_mismatch_rejected(temp_db):
    services.create_waybill(wb("W1", {"B1": 100, "B2": 200}))
    view, _ = services.submit_package(
        pkg("P1", ["W1"], {"N1": {"B1": 100, "B2": 201}})
    )
    assert view["status"] == "rejected"
    assert "重量" in view["reason"]
    # 拒收后原单依旧有效，数据未被改动
    assert services.get_waybill("W1")["status"] == "active"


def test_idempotent_same_content_replay(temp_db):
    services.create_waybill(wb("W1", {"B1": 10}))
    p = pkg("P1", ["W1"], {"N1": {"B1": 10}})
    first, replayed1 = services.submit_package(p)
    assert first["status"] == "applied" and replayed1 is False
    # 同包号同内容（即使 JSON 字段顺序不同）重传 -> 沿用原结果
    p2 = PackageIn.model_validate(
        {
            "new_waybills": [
                {"waybill_no": "N1", "batches": [
                    {"weight_kg": 10, "batch_no": "B1"}]}
            ],
            "old_waybill_nos": ["W1"],
            "package_no": "P1",
        }
    )
    second, replayed2 = services.submit_package(p2)
    assert replayed2 is True
    assert second["content_hash"] == first["content_hash"]
    assert second["status"] == "applied"


def test_same_package_no_different_content_rejected(temp_db):
    services.create_waybill(wb("W1", {"B1": 10}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 10}}))
    with pytest.raises(services.DomainError) as exc:
        services.submit_package(pkg("P1", ["W1"], {"N9": {"B1": 10}}))
    assert exc.value.status_code == 409
    assert "内容不同" in exc.value.message


def test_duplicate_waybill_entry_rejected(temp_db):
    services.create_waybill(wb("W1", {"B1": 10}))
    with pytest.raises(services.DomainError):
        services.create_waybill(wb("W1", {"B1": 10}))


def test_concurrent_packages_same_old_waybill(temp_db):
    """两个包并行替换同一旧单：恰好一个生效，另一个拒收。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    p1 = pkg("P1", ["W1"], {"N1": {"B1": 10}})
    p2 = pkg("P2", ["W1"], {"N2": {"B1": 10}})
    results: list[str] = []
    errors: list[str] = []

    def worker(p: PackageIn, out: list[str]) -> None:
        try:
            view, _ = services.submit_package(p)
            out.append(view["status"])
        except services.DomainError as e:
            errors.append(e.message)

    t1 = threading.Thread(target=worker, args=(p1, results))
    t2 = threading.Thread(target=worker, args=(p2, results))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == ["applied", "rejected"]
    actives = [w["waybill_no"] for w in services.list_waybills("active")]
    assert actives.count("W1") == 0
    assert len(actives) == 1 and actives[0] in {"N1", "N2"}


def test_chain_suspended_packages_resume_in_order(temp_db):
    """多个挂起包链式等待，一次补齐后按序全部续办。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    services.submit_package(pkg("P1", ["W1", "W2"], {"N1": {"B1": 10, "B2": 5}}))
    # P2 依赖 P1 的产物 N1，且还缺 W3
    services.submit_package(
        pkg("P2", ["N1", "W3"], {"N2": {"B1": 10, "B2": 5, "B3": 7}})
    )
    services.create_waybill(wb("W2", {"B2": 5}))
    services.create_waybill(wb("W3", {"B3": 7}))
    pkgs = {p["package_no"]: p for p in services.list_packages()}
    assert pkgs["P1"]["status"] == "applied"
    assert pkgs["P2"]["status"] == "applied"
    assert [w["waybill_no"] for w in services.list_waybills("active")] == ["N2"]


def test_schema_rejects_bad_inputs():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WaybillIn(waybill_no="W", batches=[])  # 无货批
    with pytest.raises(ValidationError):
        WaybillIn(
            waybill_no="W",
            batches=[BatchIn(batch_no="B1", weight_kg=0)],  # 重量非正
        )
    with pytest.raises(ValidationError):
        PackageIn(
            package_no="P",
            old_waybill_nos=["W1", "W1"],  # 旧单重复
            new_waybills=[NewWaybillIn(
                waybill_no="N1", batches=[BatchIn(batch_no="B1", weight_kg=1)])],
        )


def seed_legacy_rejected(package: PackageIn) -> None:
    """模拟旧版（logic_version=1）因旧单少算重量而误拒、已入库的包。"""
    import json

    from app import database
    from app.services import canonical_payload, content_hash

    with database.write_transaction() as conn:
        conn.execute(
            "INSERT INTO packages (package_no, payload, content_hash, status, "
            "reason, logic_version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'rejected', ?, 1, ?, ?)",
            (
                package.package_no,
                json.dumps(
                    canonical_payload(package),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                content_hash(package),
                "逐货批重量不一致 -> B1: 旧=50kg/新=100kg",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )


# --------------------------------------------------------------------------- #
# 修复：旧单跨单同货批合计校验
# --------------------------------------------------------------------------- #
def test_cross_waybill_same_batch_total_accepted(temp_db):
    """两张旧单各记同一货批 50kg，新单合计 100kg 必须通过（旧版会误拒）。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    view, _ = services.submit_package(
        pkg("P1", ["W1", "W2"], {"N1": {"B1": 100}})
    )
    assert view["status"] == "applied", view["reason"]
    assert services.stats()["active_total_weight_kg"] == 100
    assert [w["waybill_no"] for w in services.list_waybills("active")] == ["N1"]


def test_cross_waybill_same_batch_total_mismatch_rejected(temp_db):
    """合计不足或超出仍应整包拒收，并给出旧/新合计。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    view, _ = services.submit_package(
        pkg("P1", ["W1", "W2"], {"N1": {"B1": 101}})
    )
    assert view["status"] == "rejected"
    assert "旧合计=100kg" in view["reason"] and "新合计=101kg" in view["reason"]
    assert services.get_waybill("W1")["status"] == "active"
    assert services.get_waybill("W2")["status"] == "active"


def test_same_batch_across_new_waybills_still_invalid():
    """新单集合内货批号仍需唯一归属（本次修复只改旧单侧合计，不放宽该结构约束）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        pkg("P1", ["W1"], {"N1": {"B1": 40}, "N2": {"B1": 60}})


def test_rejected_by_old_logic_accepted_on_same_content_replay(temp_db):
    """旧版因少算旧单重量而误拒的包，同号同内容重传按现行台账复核后生效。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    p = pkg("P1", ["W1", "W2"], {"N1": {"B1": 100}})
    seed_legacy_rejected(p)
    view, replayed = services.submit_package(p)
    assert replayed is True
    assert view["status"] == "applied", view["reason"]
    assert view["reason"] is None
    assert [w["waybill_no"] for w in services.list_waybills("active")] == ["N1"]


def test_replay_recheck_keeps_rejection_when_still_conflicting(temp_db):
    """复核时旧单已被替换 / 新单号已生效 / 重量仍不符 -> 整包拒收并给出当前原因。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    p = pkg("P1", ["W1", "W2"], {"N1": {"B1": 100}})
    seed_legacy_rejected(p)
    # 旧单 W1 已被别的包替换（W2 未动）
    services.submit_package(pkg("P0", ["W1"], {"NX": {"B1": 50}}))

    view, replayed = services.submit_package(p)
    assert replayed is True
    assert view["status"] == "rejected"
    assert "W1" in view["reason"] and "替换" in view["reason"]
    # 未受影响的旧单保持有效
    assert services.get_waybill("W2")["status"] == "active"


def test_replay_recheck_rejects_when_new_no_already_active(temp_db):
    """复核时新单号已生效 -> 整包拒收，不能重复插入新单。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    p = pkg("P1", ["W1", "W2"], {"N1": {"B1": 100}})
    seed_legacy_rejected(p)
    # 另一张有效单已占用 N1
    services.create_waybill(wb("N1", {"B9": 1}))

    view, replayed = services.submit_package(p)
    assert replayed is True
    assert view["status"] == "rejected"
    assert "N1" in view["reason"]


def test_suspended_then_resume_uses_cross_waybill_totals(temp_db):
    """挂起包补齐旧单后自动续办时，同样按合计量校验。"""
    services.create_waybill(wb("W1", {"B1": 40}))
    view, _ = services.submit_package(
        pkg("P1", ["W1", "W2"], {"N1": {"B1": 100}})
    )
    assert view["status"] == "suspended"
    services.create_waybill(wb("W2", {"B1": 60}))
    pkgs = {p["package_no"]: p for p in services.list_packages()}
    assert pkgs["P1"]["status"] == "applied", pkgs["P1"]["reason"]
    assert services.get_waybill("N1")["status"] == "active"


def test_concurrent_replay_replaces_old_waybill_at_most_once(temp_db):
    """重传与另一改单并发：同一旧单最多被替换一次，恰好一包生效。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    p = pkg("P1", ["W1", "W2"], {"N1": {"B1": 100}})
    seed_legacy_rejected(p)
    # P2 只替换 W1，和 P1 的复核并发
    p2 = pkg("P2", ["W1"], {"N2": {"B1": 50}})
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(package: PackageIn) -> None:
        view, replayed = services.submit_package(package)
        with lock:
            results.append((package.package_no, view["status"]))

    threads = [
        threading.Thread(target=worker, args=(p,)),
        threading.Thread(target=worker, args=(p,)),
        threading.Thread(target=worker, args=(p2,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    status = dict(results)
    # P1 只能整包生效或整包拒收；不能出现部分替换
    assert status["P1"] in {"applied", "rejected"}
    # 同一旧单最多被替换一次：W1 的 replaced_by_pkg 只有一个包号
    w1 = services.get_waybill("W1")
    assert w1["status"] == "replaced"
    assert w1["replaced_by_pkg"] in {"P1", "P2"}
    actives = {w["waybill_no"] for w in services.list_waybills("active")}
    if w1["replaced_by_pkg"] == "P1":
        assert {"N1"} <= actives and "N2" not in actives
        assert status["P2"] == "rejected"
    else:
        assert {"N2"} <= actives and "N1" not in actives
        assert status["P1"] == "rejected"
    # 另一张未参与竞争的旧单要么有效、要么随 P1 生效被替换，绝不能重复替换
    pkg_rows = {p["package_no"]: p for p in services.list_packages()}
    assert pkg_rows["P1"]["status"] in {"applied", "rejected"}
    assert pkg_rows["P2"]["status"] in {"applied", "rejected"}


def test_new_logic_rejection_not_rechecked_on_replay(temp_db):
    """修复后逻辑判出的拒收不被当作旧版误拒而反复复核。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    p = pkg("P1", ["W1", "W2"], {"N1": {"B1": 90}})
    first, _ = services.submit_package(p)
    assert first["status"] == "rejected"
    assert first["logic_version"] == services.LOGIC_VERSION
    # 同内容重传：沿用现有拒收结果，不改动状态
    second, replayed = services.submit_package(p)
    assert replayed is True
    assert second["status"] == "rejected"
    assert second["updated_at"] == first["updated_at"]


def test_legacy_replay_missing_old_waybill_stays_rejected(temp_db):
    """复核时旧单已不存在（被删除/从未入账）也不得切回挂起，整包拒收。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    p = pkg("P1", ["W1", "W2", "W9"], {"N1": {"B1": 100}})
    seed_legacy_rejected(p)
    view, replayed = services.submit_package(p)
    assert replayed is True
    assert view["status"] == "rejected"
    assert "W9" in view["reason"]
    assert services.get_waybill("W1")["status"] == "active"


def test_init_db_migrates_legacy_database(tmp_path):
    """旧库（packages 无 logic_version 列）启动时自动迁移，历史包记为旧版。"""
    import sqlite3

    from app.database import init_db

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_no TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO packages (package_no, payload, content_hash, status, reason,
                              created_at, updated_at)
        VALUES ('P0', '{}', 'x', 'rejected',
                '逐货批重量不一致 -> B1: 旧=50kg/新=100kg', 't', 't');
        """
    )
    conn.commit()
    conn.close()

    init_db(str(db))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT logic_version FROM packages WHERE package_no = 'P0'"
    ).fetchone()
    conn.close()
    assert row["logic_version"] == 1


def test_underweight_equal_to_single_old_waybill_rejected(temp_db):
    """旧版覆盖写法下 50kg 会被误判通过；现按合计 100kg 必须拒收，CSV 不含新单。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    services.create_waybill(wb("W2", {"B1": 50}))
    view, _ = services.submit_package(
        pkg("P1", ["W1", "W2"], {"N1": {"B1": 50}})
    )
    assert view["status"] == "rejected", view["reason"]
    assert "旧合计=100kg" in view["reason"] and "新合计=50kg" in view["reason"]
    rows = services.active_waybill_rows()
    assert {r["waybill_no"] for r in rows} == {"W1", "W2"}
    assert sum(r["weight_kg"] for r in rows) == 100
