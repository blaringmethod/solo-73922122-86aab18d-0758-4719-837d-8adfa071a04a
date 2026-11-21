"""API 层测试：HTTP 状态码、CSV、并发请求与页面可访问。"""
from __future__ import annotations

import threading


def _post_waybill(client, no: str, batches: dict[str, int]):
    return client.post(
        "/api/waybills",
        json={
            "waybill_no": no,
            "batches": [
                {"batch_no": k, "weight_kg": v} for k, v in batches.items()
            ],
        },
    )


def _post_package(client, no: str, old: list[str], new: dict[str, dict]):
    return client.post(
        "/api/packages",
        json={
            "package_no": no,
            "old_waybill_nos": old,
            "new_waybills": [
                {
                    "waybill_no": n,
                    "batches": [
                        {"batch_no": k, "weight_kg": v} for k, v in b.items()
                    ],
                }
                for n, b in new.items()
            ],
        },
    )


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "联运改单".encode() in r.content


def test_full_flow_over_http(client):
    # 录入两张原单
    assert _post_waybill(client, "W1", {"B1": 100, "B2": 200}).status_code == 201
    assert _post_waybill(client, "W2", {"B3": 300}).status_code == 201
    # 重复录入 409
    assert _post_waybill(client, "W1", {"B1": 1}).status_code == 409
    # 结构错误 422（FastAPI 标准请求校验错误；重量为 0）
    bad = client.post(
        "/api/waybills",
        json={"waybill_no": "W9", "batches": [{"batch_no": "B", "weight_kg": 0}]},
    )
    assert bad.status_code == 422

    # 拆单：W1 -> N1/N2
    r = _post_package(client, "P1", ["W1"],
                      {"N1": {"B1": 100}, "N2": {"B2": 200}})
    assert r.status_code == 201
    assert r.json()["package"]["status"] == "applied"

    # 挂起：缺 W4
    r = _post_package(client, "P2", ["W2", "W4"],
                      {"N3": {"B3": 300, "B4": 40}})
    assert r.status_code == 201
    assert r.json()["package"]["status"] == "suspended"

    # 同包号同内容重传 -> 200 且 replayed
    r2 = _post_package(client, "P2", ["W2", "W4"],
                       {"N3": {"B3": 300, "B4": 40}})
    assert r2.status_code == 200 and r2.json()["replayed"] is True

    # 同包号异内容 -> 409 拒收
    r3 = _post_package(client, "P2", ["W2"], {"N3": {"B3": 300}})
    assert r3.status_code == 409

    # 补齐 W4 -> 自动续办
    assert _post_waybill(client, "W4", {"B4": 40}).status_code == 201
    pkgs = {p["package_no"]: p for p in client.get("/api/packages").json()}
    assert pkgs["P2"]["status"] == "applied"

    # 引用已替换旧单 -> 拒收
    r4 = _post_package(client, "P3", ["W1"], {"NX": {"B1": 100, "B2": 200}})
    assert r4.json()["package"]["status"] == "rejected"


def test_reconciliation_csv_only_active(client):
    _post_waybill(client, "W1", {"B1": 100, "B2": 200})
    _post_package(client, "P1", ["W1"], {"N1": {"B1": 100}, "N2": {"B2": 200}})
    r = client.get("/api/reconciliation.csv")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    lines = [ln for ln in text.strip().split("\r\n") if ln]
    assert lines[0] == "运单号,货批号,重量kg"
    data = lines[1:]
    # 仅含有效单 N1/N2，不含已替换的 W1
    assert any(ln.startswith("N1,") for ln in data)
    assert any(ln.startswith("N2,") for ln in data)
    assert not any(ln.startswith("W1,") for ln in data)
    assert "100" in text and "200" in text


def test_concurrent_package_requests(client):
    """并行两个替换同一旧单的请求：仅一个生效。"""
    _post_waybill(client, "W1", {"B1": 10})
    outcomes: list[dict] = []
    lock = threading.Lock()

    def worker(no: str, new_no: str) -> None:
        r = _post_package(client, no, ["W1"], {new_no: {"B1": 10}})
        with lock:
            outcomes.append(r.json())

    t1 = threading.Thread(target=worker, args=("P1", "N1"))
    t2 = threading.Thread(target=worker, args=("P2", "N2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(o["package"]["status"] for o in outcomes)
    assert statuses == ["applied", "rejected"]
    actives = [
        w["waybill_no"]
        for w in client.get("/api/waybills?status=active").json()
    ]
    assert actives in (["N1"], ["N2"])


def test_stats_endpoint(client):
    _post_waybill(client, "W1", {"B1": 10})
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["active_waybills"] == 1
    assert body["active_total_weight_kg"] == 10
    assert set(body["packages"]) == {"applied", "suspended", "rejected"}


# --------------------------------------------------------------------------- #
# 修复后的回归：跨旧单同货批合计
# --------------------------------------------------------------------------- #
def test_http_cross_waybill_batch_total_not_rejected(client):
    """旧单同货批合计 100kg，新单足量时必须生效并进入对账 CSV。"""
    _post_waybill(client, "W1", {"B1": 50})
    _post_waybill(client, "W2", {"B1": 50})
    r = _post_package(client, "P1", ["W1", "W2"], {"N1": {"B1": 100}})
    assert r.status_code == 201
    assert r.json()["package"]["status"] == "applied", r.json()

    text = client.get("/api/reconciliation.csv").content.decode("utf-8-sig")
    assert "N1,B1,100" in text
    assert "W1" not in text and "W2" not in text
    assert client.get("/api/stats").json()["active_total_weight_kg"] == 100


def test_http_underweight_new_package_rejected(client):
    """少报重量的新单仍必须拒收，不能进入 CSV。"""
    _post_waybill(client, "W1", {"B1": 50})
    _post_waybill(client, "W2", {"B1": 50})
    r = _post_package(client, "P1", ["W1", "W2"], {"N1": {"B1": 99}})
    assert r.status_code == 201
    body = r.json()["package"]
    assert body["status"] == "rejected"
    assert "旧合计=100kg" in body["reason"] and "新合计=99kg" in body["reason"]
    text = client.get("/api/reconciliation.csv").content.decode("utf-8-sig")
    assert "N1" not in text


def _seed_legacy_rejection(client, payload):
    """直接写入一条旧版逻辑造成的“重量不一致”拒收记录。"""
    import json

    from app import database
    from app.schemas import PackageIn
    from app.services import canonical_payload, content_hash

    pkg = PackageIn.model_validate(payload)
    digest = content_hash(pkg)
    with database.write_transaction() as conn:
        conn.execute(
            "INSERT INTO packages (package_no, payload, content_hash, status, "
            "reason, logic_version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'rejected', ?, 1, ?, ?)",
            (
                pkg.package_no,
                json.dumps(canonical_payload(pkg), ensure_ascii=False, sort_keys=True),
                digest,
                "逐货批重量不一致 -> B1: 旧=50kg/新=100kg",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )


def test_http_legacy_false_rejection_replayed_and_applied(client):
    """旧版误拒包收到原包号原内容重传：200 replayed，并按当前台账切换。"""
    _post_waybill(client, "W1", {"B1": 50})
    _post_waybill(client, "W2", {"B1": 50})
    payload = {
        "package_no": "P1",
        "old_waybill_nos": ["W1", "W2"],
        "new_waybills": [{"waybill_no": "N1", "batches": [
            {"batch_no": "B1", "weight_kg": 100}]}],
    }
    _seed_legacy_rejection(client, payload)
    r = client.post("/api/packages", json=payload)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    p = r.json()["package"]
    assert p["status"] == "applied" and p["reason"] is None
    assert "N1,B1,100" in client.get("/api/reconciliation.csv").content.decode(
        "utf-8-sig"
    )


def test_http_legacy_replay_still_conflicting_reports_current_reason(client):
    """复核时冲突仍在：整包拒收，返回当前原因，不进入 CSV。"""
    _post_waybill(client, "W1", {"B1": 50})
    _post_waybill(client, "W2", {"B1": 50})
    payload = {
        "package_no": "P1",
        "old_waybill_nos": ["W1", "W2"],
        "new_waybills": [{"waybill_no": "N1", "batches": [
            {"batch_no": "B1", "weight_kg": 90}]}],
    }
    _seed_legacy_rejection(client, payload)
    r = client.post("/api/packages", json=payload)
    assert r.status_code == 200
    p = r.json()["package"]
    assert p["status"] == "rejected"
    assert "旧合计=100kg" in p["reason"] and "新合计=90kg" in p["reason"]
    assert "N1" not in client.get("/api/reconciliation.csv").content.decode(
        "utf-8-sig"
    )


def test_http_replay_same_no_different_content_still_409(client):
    _post_waybill(client, "W1", {"B1": 50})
    _post_waybill(client, "W2", {"B1": 50})
    assert _post_package(client, "P1", ["W1", "W2"],
                         {"N1": {"B1": 100}}).status_code == 201
    # 同号异内容（即便同样足量）必须 409
    r = _post_package(client, "P1", ["W1", "W2"], {"N2": {"B1": 100}})
    assert r.status_code == 409
    assert "内容不同" in r.json()["detail"]
    # 原结果保留
    p = {x["package_no"]: x for x in client.get("/api/packages").json()}["P1"]
    assert p["status"] == "applied"


def test_openapi_schema_available(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/api/packages" in r.json()["paths"]


def test_package_view_exposes_logic_version(client):
    _post_waybill(client, "W1", {"B1": 10})
    r = _post_package(client, "P1", ["W1"], {"N1": {"B1": 10}})
    assert r.json()["package"]["logic_version"] == 2
    listed = client.get("/api/packages").json()
    assert listed[0]["logic_version"] == 2


def test_write_conflict_maps_to_409(client, monkeypatch):
    """写锁等待超时（多进程部署场景）返回 409 且提示可重试。"""
    from app import services
    from app.database import WriteConflict

    def boom(*args, **kwargs):
        raise WriteConflict("database is locked")

    monkeypatch.setattr(services, "write_transaction", boom)
    r = _post_waybill(client, "W1", {"B1": 10})
    assert r.status_code == 409
    assert "重试" in r.json()["detail"]


def test_http_underweight_matching_one_old_waybill_not_in_csv(client):
    """旧版会放行的“等于单张旧单重量”的少报新单，现在必须拒收且不进 CSV。"""
    _post_waybill(client, "W1", {"B1": 50})
    _post_waybill(client, "W2", {"B1": 50})
    r = _post_package(client, "P1", ["W1", "W2"], {"N1": {"B1": 50}})
    body = r.json()["package"]
    assert body["status"] == "rejected"
    assert "旧合计=100kg" in body["reason"]
    text = client.get("/api/reconciliation.csv").content.decode("utf-8-sig")
    assert "N1" not in text
    assert "W1,B1,50" in text and "W2,B1,50" in text
    assert client.get("/api/stats").json()["active_total_weight_kg"] == 100
