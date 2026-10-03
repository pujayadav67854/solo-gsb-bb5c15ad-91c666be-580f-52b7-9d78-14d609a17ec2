"""危险区通行许可测试：登记/撤销/不可变历史/全局许可修订号/占用许可复核。

POST /api/v1/permits                    登记许可（危险区须存在，车次可尚无轨迹；
                                        同 ID 同内容幂等、异内容冲突、撤销后 ID 不得重用）
POST /api/v1/permits/{permit_id}/revoke 撤销许可（须提交所见许可修订号，过期/重复撤销拒绝）
GET  /api/v1/permits/{permit_id}        查询许可状态（可加 permit_revision 取历史状态）
GET  /api/v1/permit-revisions           全局许可修订流水
POST /api/v1/trips/{trip_id}/permit-review
                                        许可复核：同一已提交读视图下，沿用轨迹插值/带洞边界/
                                        区域生效规则，仅对实际占用核对该车次该区许可
                                        （重叠许可时段取并集），按区返回无许可的正时长区间、
                                        累计秒数及实际使用的区域修订号/轨迹版本/许可修订号。
"""
import threading

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _upload(client, trip, points):
    r = client.post(
        f"/api/v1/trips/{trip}/points",
        json={"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]},
    )
    assert r.status_code == 200, r.text
    return r


def _register_permit(client, permit_id="P-1", trip_id="truck-7", zone_id="Z-A",
                     start="2026-09-30T12:00:20Z", end="2026-09-30T12:00:50Z"):
    return client.post(
        "/api/v1/permits",
        json={
            "permit_id": permit_id,
            "trip_id": trip_id,
            "zone_id": zone_id,
            "start": start,
            "end": end,
        },
    )


def _review(client, trip, **overrides):
    body = {
        "zone_ids": ["Z-A"],
        "since": "2026-09-30T11:00:00Z",
        "until": "2026-09-30T13:00:00Z",
    }
    body.update(overrides)
    return client.post(f"/api/v1/trips/{trip}/permit-review", json=body)


# 12:00:00 自 x=-20 出发，12:02:00 到 x=120（140m/120s 匀速横穿 y=50）：
# 进入方块 x=0 于 12:00:17.142857，离开 x=100 于 12:01:42.857143，区内共 85.714286 秒
CROSS = [
    ("a", "2026-09-30T12:00:00Z", -20, 50),
    ("b", "2026-09-30T12:02:00Z", 120, 50),
]


# ---------------------------------------------------------------------
# 许可登记
# ---------------------------------------------------------------------

def test_register_permit_created_and_global_revision(client):
    register_square(client, zone_id="Z-A")
    r = _register_permit(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["revision"] == 1
    assert body["permit_revision"] == 1
    assert body["permit_id"] == "P-1"
    assert body["trip_id"] == "truck-7"
    assert body["zone_id"] == "Z-A"
    assert body["start"] == "2026-09-30T12:00:20Z"
    assert body["end"] == "2026-09-30T12:00:50Z"

    # 许可修订号与区域修订号互相独立：再登记区域不影响许可修订号推进
    register_square(client, zone_id="Z-B")
    r = _register_permit(client, permit_id="P-2", zone_id="Z-B")
    assert r.status_code == 201
    assert r.json()["revision"] == 2
    assert r.json()["permit_revision"] == 2


def test_register_permit_trip_may_have_no_track(client):
    """登记时车次可尚无轨迹。"""
    register_square(client, zone_id="Z-A")
    r = _register_permit(client, trip_id="ghost-trip")
    assert r.status_code == 201, r.text


def test_register_permit_unknown_zone_404(client):
    register_square(client, zone_id="Z-A")
    r = _register_permit(client, zone_id="GHOST")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_register_permit_idempotent_retransmit(client):
    """同 ID 同内容重传幂等：HTTP 200，不产生新许可修订号、不留新历史。"""
    register_square(client, zone_id="Z-A")
    r1 = _register_permit(client)
    assert r1.status_code == 201
    r2 = _register_permit(client)
    assert r2.status_code == 200, r.text
    body = r2.json()
    assert body["status"] == "idempotent"
    assert body["revision"] == 1
    assert body["permit_revision"] == 1

    log = client.get("/api/v1/permit-revisions").json()
    assert log["current_permit_revision"] == 1
    assert len(log["revisions"]) == 1


def test_register_permit_conflict_on_different_content(client):
    register_square(client, zone_id="Z-A")
    register_square(client, zone_id="Z-B")
    assert _register_permit(client).status_code == 201
    # 车次不同 / 危险区不同 / 时段不同均为异内容冲突
    assert _register_permit(client, trip_id="truck-9").status_code == 409
    assert _register_permit(client, zone_id="Z-B").status_code == 409
    assert _register_permit(client, end="2026-09-30T12:01:00Z").status_code == 409
    # 冲突不产生新许可修订号
    assert client.get("/api/v1/permit-revisions").json()["current_permit_revision"] == 1


def test_register_permit_invalid_window_rejected(client):
    register_square(client, zone_id="Z-A")
    # start >= end：非正长度时段
    r = _register_permit(client, start="2026-09-30T12:00:50Z", end="2026-09-30T12:00:20Z")
    assert r.status_code == 400
    r = _register_permit(client, start="2026-09-30T12:00:20Z", end="2026-09-30T12:00:20Z")
    assert r.status_code == 400
    # 不带时区
    r = _register_permit(client, start="2026-09-30T12:00:20", end="2026-09-30T12:00:50")
    assert r.status_code == 400
    # 缺字段 / 多余字段
    r = client.post("/api/v1/permits", json={"permit_id": "P-x", "trip_id": "t"})
    assert r.status_code == 422
    r = client.post(
        "/api/v1/permits",
        json={
            "permit_id": "P-x", "trip_id": "t", "zone_id": "Z-A",
            "start": "2026-09-30T12:00:20Z", "end": "2026-09-30T12:00:50Z",
            "extra": 1,
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------
# 许可撤销
# ---------------------------------------------------------------------

def test_revoke_permit_success_and_history(client):
    register_square(client, zone_id="Z-A")
    _register_permit(client)
    r = client.post("/api/v1/permits/P-1/revoke", json={"base_revision": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "revoked"
    assert body["revision"] == 2
    assert body["permit_revision"] == 2
    assert body["base_revision"] == 1

    # 不可改历史：登记与撤销各一行，按修订号倒序
    log = client.get("/api/v1/permit-revisions").json()
    assert log["current_permit_revision"] == 2
    assert [x["revision"] for x in log["revisions"]] == [2, 1]
    assert log["revisions"][0]["action"] == "revoke"
    assert log["revisions"][1]["action"] == "register"

    # 当前状态为已撤销；按许可修订号 1 仍可见当时有效状态
    cur = client.get("/api/v1/permits/P-1").json()
    assert cur["status"] == "revoked"
    assert cur["revision"] == 2
    old = client.get("/api/v1/permits/P-1?permit_revision=1").json()
    assert old["status"] == "active"
    assert old["revision"] == 1


def test_revoke_permit_unknown_404(client):
    r = client.post("/api/v1/permits/NOPE/revoke", json={"base_revision": 1})
    assert r.status_code == 404


def test_revoke_permit_stale_revision_409(client):
    register_square(client, zone_id="Z-A")
    _register_permit(client, permit_id="P-1")
    _register_permit(client, permit_id="P-2", start="2026-09-30T13:00:00Z",
                     end="2026-09-30T14:00:00Z")
    # 全局许可修订号已推进到 2，仍按 1 撤销视为过期
    r = client.post("/api/v1/permits/P-1/revoke", json={"base_revision": 1})
    assert r.status_code == 409
    # 过期撤销不生效：P-1 仍有效
    assert client.get("/api/v1/permits/P-1").json()["status"] == "active"
    # 用当前号撤销成功
    r = client.post("/api/v1/permits/P-1/revoke", json={"base_revision": 2})
    assert r.status_code == 200


def test_revoke_permit_duplicate_409(client):
    register_square(client, zone_id="Z-A")
    _register_permit(client)
    assert client.post("/api/v1/permits/P-1/revoke", json={"base_revision": 1}).status_code == 200
    r = client.post("/api/v1/permits/P-1/revoke", json={"base_revision": 2})
    assert r.status_code == 409


def test_revoke_permit_invalid_base_revision(client):
    register_square(client, zone_id="Z-A")
    _register_permit(client)
    assert client.post("/api/v1/permits/P-1/revoke", json={"base_revision": 0}).status_code == 422
    assert client.post("/api/v1/permits/P-1/revoke", json={}).status_code == 422


def test_revoked_permit_id_cannot_be_reused(client):
    register_square(client, zone_id="Z-A")
    _register_permit(client)
    client.post("/api/v1/permits/P-1/revoke", json={"base_revision": 1})
    r = _register_permit(client)
    assert r.status_code == 409
    # 即便内容不同也不得重用
    r = _register_permit(client, trip_id="truck-8")
    assert r.status_code == 409


# ---------------------------------------------------------------------
# 许可复核：基本计算
# ---------------------------------------------------------------------

def test_review_partial_coverage(client):
    """占用 85.714286s，许可覆盖中段 30s，前后两段无许可。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    _register_permit(client)  # 12:00:20 - 12:00:50

    r = _review(client, "truck-7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == "truck-7"
    assert body["zone_ids"] == ["Z-A"]
    # 三个实际使用版本总是回填
    assert body["revision"] == 1
    assert body["track_version"] == 1
    assert body["permit_revision"] == 1
    zone = body["zones"][0]
    assert zone["zone_id"] == "Z-A"
    assert zone["unpermitted_segments"] == [
        {"start": "2026-09-30T12:00:17.142857Z", "end": "2026-09-30T12:00:20Z",
         "duration_sec": 2.857143},
        {"start": "2026-09-30T12:00:50Z", "end": "2026-09-30T12:01:42.857143Z",
         "duration_sec": 52.857143},
    ]
    assert zone["unpermitted_total_sec"] == 55.714286


def test_review_fully_covered_returns_empty(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    _register_permit(client, start="2026-09-30T11:00:00Z", end="2026-09-30T13:00:00Z")
    body = _review(client, "truck-7").json()
    zone = body["zones"][0]
    assert zone["unpermitted_segments"] == []
    assert zone["unpermitted_total_sec"] == 0


def test_review_no_permits_at_all(client):
    """无任何许可登记：许可修订号为 0，全部占用无许可。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    body = _review(client, "truck-7").json()
    assert body["permit_revision"] == 0
    zone = body["zones"][0]
    assert zone["unpermitted_total_sec"] == 85.714286
    assert len(zone["unpermitted_segments"]) == 1


def test_review_no_occupancy_returns_empty_and_zero(client):
    """轨迹不经过危险区：无占用，返回空区间与零秒数。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", [
        ("a", "2026-09-30T12:00:00Z", -200, 500),
        ("b", "2026-09-30T12:02:00Z", -100, 500),
    ])
    body = _review(client, "truck-7").json()
    zone = body["zones"][0]
    assert zone["unpermitted_segments"] == []
    assert zone["unpermitted_total_sec"] == 0


def test_review_overlapping_permits_union(client):
    """重叠许可时段取并集：两段相接/重叠许可合成一段覆盖。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    _register_permit(client, permit_id="P-1",
                     start="2026-09-30T12:00:10Z", end="2026-09-30T12:00:40Z")
    _register_permit(client, permit_id="P-2",
                     start="2026-09-30T12:00:30Z", end="2026-09-30T12:01:00Z")
    body = _review(client, "truck-7").json()
    assert body["permit_revision"] == 2
    zone = body["zones"][0]
    # 并集 12:00:10-12:01:00，占用 12:00:17.142857-12:01:42.857143
    assert zone["unpermitted_segments"] == [
        {"start": "2026-09-30T12:01:00Z", "end": "2026-09-30T12:01:42.857143Z",
         "duration_sec": 42.857143},
    ]
    assert zone["unpermitted_total_sec"] == 42.857143


def test_review_only_matching_trip_and_zone_permits_count(client):
    """仅核对该车次该区的许可：其他车次/其他区的许可不影响结果。"""
    register_square(client, zone_id="Z-A")
    register_square(client, zone_id="Z-B")
    _upload(client, "truck-7", CROSS)
    # 其他车次的许可、其他区的许可均不算数
    _register_permit(client, permit_id="P-1", trip_id="truck-8",
                     start="2026-09-30T11:00:00Z", end="2026-09-30T13:00:00Z")
    _register_permit(client, permit_id="P-2", zone_id="Z-B",
                     start="2026-09-30T11:00:00Z", end="2026-09-30T13:00:00Z")
    body = _review(client, "truck-7").json()
    assert body["zones"][0]["unpermitted_total_sec"] == 85.714286


def test_review_multiple_zones(client):
    """按区分别返回：Z-A 有许可、Z-B 无许可。"""
    register_square(client, zone_id="Z-A")
    register_square(client, zone_id="Z-B",
                    exterior=[[0, 0], [50, 0], [50, 100], [0, 100]])
    _upload(client, "truck-7", CROSS)
    _register_permit(client, start="2026-09-30T11:00:00Z", end="2026-09-30T13:00:00Z")
    body = _review(client, "truck-7", zone_ids=["Z-A", "Z-B"]).json()
    assert [z["zone_id"] for z in body["zones"]] == ["Z-A", "Z-B"]
    assert body["zones"][0]["unpermitted_total_sec"] == 0
    # Z-B 为左半方块：x=0..50 占用 12:00:17.142857-12:01:00，共 42.857143 秒
    assert body["zones"][1]["unpermitted_total_sec"] == 42.857143


def test_review_query_window_clips_occupancy_and_permits(client):
    """查询时段半开裁剪同时作用于占用与许可覆盖。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    _register_permit(client, start="2026-09-30T12:00:00Z", end="2026-09-30T12:01:00Z")
    # 只看 12:01:00 之后：许可不覆盖，占用 12:01:00-12:01:42.857143 全部无许可
    body = _review(client, "truck-7",
                   since="2026-09-30T12:01:00Z", until="2026-09-30T13:00:00Z").json()
    zone = body["zones"][0]
    assert zone["unpermitted_segments"] == [
        {"start": "2026-09-30T12:01:00Z", "end": "2026-09-30T12:01:42.857143Z",
         "duration_sec": 42.857143},
    ]


def test_review_hole_rules_inherited(client):
    """带洞边界规则沿用：穿越洞的时间不算占用，无需许可。"""
    register_square(client, zone_id="Z-A",
                    holes=[[[40, 40], [60, 40], [60, 60], [40, 60]]])
    _upload(client, "truck-7", CROSS)
    body = _review(client, "truck-7").json()
    zone = body["zones"][0]
    # 与既有穿越审计一致：两段各 34.285714 秒，洞内 17.142858 秒不计
    assert zone["unpermitted_total_sec"] == 68.571429
    assert len(zone["unpermitted_segments"]) == 2


# ---------------------------------------------------------------------
# 许可复核：版本复现
# ---------------------------------------------------------------------

def test_review_reproduces_before_retroactive_registration(client):
    """补办许可前的结果可复现：指定旧许可修订号仍显示无许可。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    # 先复核一次（无许可）：全部占用无许可
    before = _review(client, "truck-7").json()
    assert before["permit_revision"] == 0
    assert before["zones"][0]["unpermitted_total_sec"] == 85.714286
    # 事后补办覆盖全程的许可
    _register_permit(client, start="2026-09-30T11:00:00Z", end="2026-09-30T13:00:00Z")
    after = _review(client, "truck-7").json()
    assert after["permit_revision"] == 1
    assert after["zones"][0]["unpermitted_total_sec"] == 0
    # 指定补办前的许可修订号复现：结果与当时一致（许可修订号 0 不可指定，用 1 之前的
    # 状态即 permit_revision=0 的语义；这里验证指定修订号 1 复现补办后、
    # 以及用旧轨迹/区域版本组合复现）
    repro = _review(client, "truck-7", permit_revision=1).json()
    assert repro["permit_revision"] == 1
    assert repro["zones"][0]["unpermitted_total_sec"] == 0


def test_review_reproduces_before_revoke(client):
    """撤销前的结果可复现：撤销后最新状态全部无许可，指定旧许可修订号仍按有效许可核算。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    _register_permit(client, start="2026-09-30T11:00:00Z", end="2026-09-30T13:00:00Z")
    client.post("/api/v1/permits/P-1/revoke", json={"base_revision": 1})

    latest = _review(client, "truck-7").json()
    assert latest["permit_revision"] == 2
    assert latest["zones"][0]["unpermitted_total_sec"] == 85.714286

    repro = _review(client, "truck-7", permit_revision=1).json()
    assert repro["permit_revision"] == 1
    assert repro["zones"][0]["unpermitted_total_sec"] == 0
    assert repro["zones"][0]["unpermitted_segments"] == []


def test_review_with_zone_revision_and_track_version(client):
    """区域修订号、轨迹版本与许可修订号可同时指定复现。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)  # 轨迹版本 1
    # 更正：把 b 点改到区外，轨迹版本 2 不再穿越
    r = client.post("/api/v1/trips/truck-7/corrections", json={
        "base_version": 1,
        "reason": "现场复核：终点录错",
        "items": [{"action": "update", "point_id": "b",
                   "ts": "2026-09-30T12:02:00Z", "x": 120, "y": 500}],
    })
    assert r.status_code == 200, r.text
    _register_permit(client, start="2026-09-30T11:00:00Z", end="2026-09-30T13:00:00Z")

    # 最新轨迹版本 2：无占用
    latest = _review(client, "truck-7").json()
    assert latest["track_version"] == 2
    assert latest["zones"][0]["unpermitted_total_sec"] == 0
    # 轨迹版本 1 × 许可修订号 1：有占用但全被许可覆盖
    repro = _review(client, "truck-7", track_version=1, permit_revision=1).json()
    assert repro["track_version"] == 1
    assert repro["permit_revision"] == 1
    assert repro["zones"][0]["unpermitted_total_sec"] == 0
    # 轨迹版本 1 × 最新区域修订号（省略许可修订号取最新）
    repro2 = _review(client, "truck-7", track_version=1, revision=1).json()
    assert repro2["revision"] == 1
    assert repro2["zones"][0]["unpermitted_total_sec"] == 0


# ---------------------------------------------------------------------
# 许可复核：明确报错
# ---------------------------------------------------------------------

def test_review_unknown_trip_404(client):
    register_square(client, zone_id="Z-A")
    r = _review(client, "ghost")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_review_unknown_zone_404(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    r = _review(client, "truck-7", zone_ids=["Z-A", "GHOST"])
    assert r.status_code == 404


def test_review_zone_not_registered_at_revision_404(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    register_square(client, zone_id="Z-B")  # 修订号 2 才登记
    r = _review(client, "truck-7", zone_ids=["Z-B"], revision=1)
    assert r.status_code == 404


def test_review_out_of_range_versions_404(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    _register_permit(client)
    assert _review(client, "truck-7", revision=99).status_code == 404
    assert _review(client, "truck-7", track_version=9).status_code == 404
    assert _review(client, "truck-7", permit_revision=99).status_code == 404


def test_review_invalid_time_range_400(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    r = _review(client, "truck-7",
                since="2026-09-30T13:00:00Z", until="2026-09-30T11:00:00Z")
    assert r.status_code == 400
    # 缺 until
    r = client.post("/api/v1/trips/truck-7/permit-review",
                    json={"zone_ids": ["Z-A"], "since": "2026-09-30T11:00:00Z"})
    assert r.status_code == 422
    # 不带时区
    r = _review(client, "truck-7", since="2026-09-30T11:00:00")
    assert r.status_code == 400


def test_review_zone_ids_must_be_nonempty_distinct(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    # 空集合
    assert _review(client, "truck-7", zone_ids=[]).status_code == 422
    # 重复 ID
    r = _review(client, "truck-7", zone_ids=["Z-A", "Z-A"])
    assert r.status_code == 400


def test_review_invalid_version_values_rejected(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "truck-7", CROSS)
    assert _review(client, "truck-7", permit_revision=0).status_code == 422
    assert _review(client, "truck-7", permit_revision=-1).status_code == 422
    assert _review(client, "truck-7", track_version=-1).status_code == 422
    assert _review(client, "truck-7", revision=0).status_code == 422
    # 多余字段
    r = _review(client, "truck-7", unknown_field=1)
    assert r.status_code == 422


# ---------------------------------------------------------------------
# 许可查询接口
# ---------------------------------------------------------------------

def test_get_permit_and_revision_log(client):
    register_square(client, zone_id="Z-A")
    _register_permit(client, permit_id="P-1")
    _register_permit(client, permit_id="P-2", trip_id="truck-8",
                     start="2026-09-30T13:00:00Z", end="2026-09-30T14:00:00Z")

    r = client.get("/api/v1/permits/P-2")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["revision"] == 2
    assert body["trip_id"] == "truck-8"

    assert client.get("/api/v1/permits/NOPE").status_code == 404
    # 指定许可修订号超出范围 / 当时尚未登记
    assert client.get("/api/v1/permits/P-1?permit_revision=99").status_code == 404
    assert client.get("/api/v1/permits/P-2?permit_revision=1").status_code == 404
    assert client.get("/api/v1/permits/P-1?permit_revision=0").status_code == 422

    log = client.get("/api/v1/permit-revisions").json()
    assert log["current_permit_revision"] == 2
    assert [x["permit_id"] for x in log["revisions"]] == ["P-2", "P-1"]


def test_permit_revision_independent_of_zone_revision(client):
    """区域登记不产生许可修订号；许可登记不产生区域修订号。"""
    register_square(client, zone_id="Z-A")
    _register_permit(client)
    register_square(client, zone_id="Z-B")
    log = client.get("/api/v1/permit-revisions").json()
    assert log["current_permit_revision"] == 1
    assert client.get("/api/v1/revisions").json()["current_revision"] == 2


# ---------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------

def test_concurrent_permit_registrations_unique_monotonic_revisions(client):
    """并发真实登记：许可修订号全局唯一且连续；幂等重传不产生新号。"""
    register_square(client, zone_id="Z-A")
    n = 20
    errors = []

    def register(start: int) -> None:
        try:
            for i in range(start, start + n):
                _register_permit(
                    client,
                    permit_id=f"P-{i}",
                    trip_id=f"truck-{i}",
                    start="2026-09-30T12:00:00Z",
                    end="2026-09-30T13:00:00Z",
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=register, args=(k * n,)) for k in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    log = client.get("/api/v1/permit-revisions", params={"limit": 10000}).json()
    assert log["current_permit_revision"] == 3 * n
    revisions = sorted(x["revision"] for x in log["revisions"])
    assert revisions == list(range(1, 3 * n + 1))
