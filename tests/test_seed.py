"""演示数据「仅数据库首次初始化且 SEED_DEMO_DATA=1 时写入一次」测试。

覆盖工作站重启（同一 SQLite 文件重新进入 lifespan）场景：
- 首次初始化 SEED=1：4 片演示陶片 + v1 基线快照；再次重启不重复写入、
  不升版、快照原样；首次初始化 SEED=0：保持空白（仅 v0 空白基线），
  之后开启 SEED=1 重启也不补种；
- 用户经现有删除 API 清空全部陶片后重启：保持空装配，版本号与全部不可改
  快照不变（不重新出现四片演示陶片、不生成额外版本）；
- 用户把历史恢复到空装配（v0）后重启：保持恢复结果，不产生新版本；
- 迁移导入（含空装配包）后重启：原装配 / 版本 / 快照原样保留，不补种；
- 重启不改变迁移导入对空白目标与既有历史的判定（target_is_blank 语义）；
- 重启后录入 / 删除 / 历史等 HTTP API 仍可调用且语义不变。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import main, portable
from app.main import app


def _fresh_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # 交由 DB 自行建文件，确保语义上是「首次初始化」
    return path


@pytest.fixture()
def paths():
    created: list[str] = []

    def make() -> str:
        created.append(_fresh_db_path())
        return created[-1]

    yield make

    if main.db is not None:
        try:
            main.db.conn.close()
        except Exception:
            pass
    for p in created:
        if os.path.exists(p):
            os.unlink(p)


def _start(db_path: str, seed: bool) -> TestClient:
    """模拟工作站（重新）启动：以指定库文件与开关进入 lifespan。"""
    main.DB_PATH = db_path
    main.SEED_DEMO_DATA = seed
    client = TestClient(app)
    client.__enter__()
    return client


def _stop(client: TestClient):
    client.__exit__(None, None, None)


def _snapshot_versions(c: TestClient) -> list[int]:
    return [v["version"]
            for v in c.get("/api/assembly/versions").json()["versions"]]


# ---------------------------------------------------------------- 首次初始化

def test_demo_seeded_once_on_first_init(paths):
    p = paths()
    c = _start(p, True)
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 1
    assert [f["name"] for f in asm["fragments"]] == [
        "陶片A-左半", "陶片B-右半", "陶片C", "陶片D"]
    versions = c.get("/api/assembly/versions").json()
    assert _snapshot_versions(c) == [1]
    assert versions["versions"][0]["source"] == "baseline"
    # 持久化了首次初始化标记
    row = main.db.conn.execute(
        "SELECT value FROM meta WHERE key = 'initialized_at'").fetchone()
    assert row is not None and row["value"]
    _stop(c)

    # 重启（开关仍开）：不补种、不升版、快照不变
    c = _start(p, True)
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 1
    assert len(asm["fragments"]) == 4
    assert _snapshot_versions(c) == [1]
    _stop(c)

    # 再以开关关闭重启：数据同样不被改动（开关只影响首次初始化）
    c = _start(p, False)
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 1 and len(asm["fragments"]) == 4
    assert _snapshot_versions(c) == [1]


def test_no_seed_on_first_init_when_disabled(paths):
    p = paths()
    c = _start(p, False)
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 0
    assert asm["fragments"] == [] and asm["relations"] == []
    # 关闭开关的首次初始化仍只留空白基线
    assert _snapshot_versions(c) == [0]
    _stop(c)

    # 之后开启开关重启：绝不补种，版本 / 快照保持空白
    c = _start(p, True)
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 0 and asm["fragments"] == []
    assert _snapshot_versions(c) == [0]
    _stop(c)

    # 再次关闭重启亦然
    c = _start(p, False)
    assert c.get("/api/assembly").json()["version"] == 0


# ------------------------------------------------- 删除清空后重启不得回填

def test_restart_after_deleting_all_fragments_keeps_empty(paths):
    p = paths()
    c = _start(p, True)  # v1：4 片演示陶片
    # 经现有删除流程逐片清空（每删一片升一个版本：v2..v5）
    for expected in (1, 2, 3, 4):
        fid = c.get("/api/fragments").json()[0]["id"]
        r = c.delete(f"/api/fragments/{fid}?expected_version={expected}")
        assert r.status_code == 200, r.text
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 5 and asm["fragments"] == []
    assert _snapshot_versions(c) == [5, 4, 3, 2, 1]
    _stop(c)

    # 默认 SEED_DEMO_DATA=1 重启：演示陶片不得重新出现，不生成额外版本
    c = _start(p, True)
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 5
    assert asm["fragments"] == [] and asm["relations"] == []
    assert _snapshot_versions(c) == [5, 4, 3, 2, 1]

    # 重启后录入 API 语义不变：新碎片接续历史升版到 v6
    r = c.post("/api/fragments", json={
        "name": "用户新碎片",
        "contour": [[0, 0], [10, 0], [10, 10], [0, 10]],
    })
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 6
    assert r.json()["fragment"]["id"] == 5  # AUTOINCREMENT 只增不减（沿用既有删除语义）
    assert _snapshot_versions(c)[0] == 6


# ------------------------------------------------- 恢复到空装配后重启

def test_restart_after_restore_to_empty_keeps_restored_state(paths):
    p = paths()
    c = _start(p, False)  # v0 空白基线
    r = c.post("/api/fragments", json={
        "name": "临时碎片",
        "contour": [[0, 0], [10, 0], [10, 10], [0, 10]],
    })
    assert r.status_code == 201 and r.json()["version"] == 1
    # 把历史恢复到空装配 v0：成功后为 v2（空），旧快照 0/1 均保留
    r = c.post("/api/assembly/versions/0/restore/preview",
               json={"expected_version": 1})
    assert r.status_code == 200, r.text
    r = c.post("/api/assembly/versions/0/restore/confirm",
               json={"expected_version": 1})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    assert c.get("/api/assembly").json()["fragments"] == []
    assert _snapshot_versions(c) == [2, 1, 0]
    _stop(c)

    # 开关开启重启：保持恢复后的空装配，不补种、不升版
    c = _start(p, True)
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 2 and asm["fragments"] == []
    assert _snapshot_versions(c) == [2, 1, 0]


# ------------------------------------------------------------ 迁移导入后重启

def _export(c: TestClient) -> dict:
    r = c.get("/api/transfer/export")
    assert r.status_code == 200, r.text
    return r.json()


def test_restart_after_import_keeps_package(paths):
    src = paths()
    dst = paths()
    # 源站：演示数据（v1，4 片）
    cs = _start(src, True)
    pkg = _export(cs)
    _stop(cs)
    # 目标站：空白库导入整包 → v1
    c = _start(dst, False)
    r = c.post("/api/transfer/import/confirm",
               json={"expected_version": 0, "package": pkg})
    assert r.status_code == 201, r.text
    before = c.get("/api/assembly").json()
    assert before["version"] == 1 and len(before["fragments"]) == 4
    assert _snapshot_versions(c) == [1]
    _stop(c)

    # 以默认开关重启：库非首次初始化，不补种、不覆盖、不升版
    c = _start(dst, True)
    after = c.get("/api/assembly").json()
    assert after == before
    assert _snapshot_versions(c) == [1]
    # 导入保留的原 id / 时间戳仍在（与源站一致）
    assert after["fragments"] == before["fragments"]


def test_restart_after_importing_empty_package_keeps_blank(paths):
    src = paths()
    dst = paths()
    # 源站：仅 v0 空白基线的空装配包
    cs = _start(src, False)
    empty_pkg = _export(cs)
    assert empty_pkg["current_version"] == 0
    _stop(cs)
    # 导入到另一空白目标（空白基线被整包替换，仍为 v0 空白）
    c = _start(dst, False)
    r = c.post("/api/transfer/import/confirm",
               json={"expected_version": 0, "package": empty_pkg})
    assert r.status_code == 201, r.text
    assert c.get("/api/assembly").json()["version"] == 0
    _stop(c)

    # 开关开启重启：已初始化库即便全空也不补种
    c = _start(dst, True)
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 0 and asm["fragments"] == []
    assert _snapshot_versions(c) == [0]


# --------------------------------------- 迁移对空白目标 / 既有历史的判定不变

def test_blank_target_judgement_unchanged_across_restarts(paths):
    # 纯空白库（SEED=0）：重启前后均为可导入空白目标
    p = paths()
    c = _start(p, False)
    is_blank, target = portable.target_is_blank(main.db)
    assert is_blank is True
    assert target == {
        "current_version": 0, "fragment_count": 0, "relation_count": 0,
        "snapshot_count": 1, "nonempty_snapshot_versions": []}
    _stop(c)
    c = _start(p, False)
    assert portable.target_is_blank(main.db)[0] is True

    # 删除清空的库：当前无碎片，但存在非空历史 → 重启前后均非空白目标
    p2 = paths()
    c2 = _start(p2, True)
    for expected in (1, 2, 3, 4):
        fid = c2.get("/api/fragments").json()[0]["id"]
        assert c2.delete(
            f"/api/fragments/{fid}?expected_version={expected}").status_code == 200
    is_blank, target = portable.target_is_blank(main.db)
    assert is_blank is False
    assert target["fragment_count"] == 0
    # v1..v4 快照非空；v5 为删空后的空快照
    assert target["nonempty_snapshot_versions"] == [4, 3, 2, 1]
    _stop(c2)
    c2 = _start(p2, True)
    is_blank, target = portable.target_is_blank(main.db)
    assert is_blank is False
    assert target["nonempty_snapshot_versions"] == [4, 3, 2, 1]

    # 经迁移 HTTP 预览判定：空白库 ok=true；删空库 200 但 ok=false。
    # 所有 TestClient 共用全局 main.db，包用独立连接导出，避免串改全局库。
    from datetime import datetime, timezone
    from app.database import DB as DBCls
    src_db = DBCls(paths())
    main.seed_demo(src_db)
    pkg = portable.build_package(
        src_db, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    src_db.conn.close()

    main.db = DBCls(p)
    r = c.post("/api/transfer/import/preview", json={"package": pkg})
    assert r.status_code == 200 and r.json()["ok"] is True
    main.db = DBCls(p2)
    r = c2.post("/api/transfer/import/preview", json={"package": pkg})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert any("空白" in x for x in r.json()["reasons"])
    # 确认端点同样整次拒绝，库与历史原状
    r = c2.post("/api/transfer/import/confirm",
                json={"expected_version": 5, "package": pkg})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "target_not_blank"
    assert c2.get("/api/assembly").json()["version"] == 5


# ------------------------------------------------ 重启后 HTTP API 语义不变

def test_apis_still_work_after_restart(paths):
    p = paths()
    c = _start(p, True)
    assert len(c.get("/api/fragments").json()) == 4
    _stop(c)

    c = _start(p, True)
    # 录入 → 新版本
    r = c.post("/api/fragments", json={
        "name": "新片", "contour": [[0, 0], [10, 0], [10, 10], [0, 10]]})
    assert r.status_code == 201 and r.json()["version"] == 2
    # 删除（携带期望版本）
    fid = r.json()["fragment"]["id"]
    r = c.delete(f"/api/fragments/{fid}?expected_version=2")
    assert r.status_code == 200 and r.json()["version"] == 3
    # 历史只读 / 恢复预览仍可用
    assert c.get("/api/assembly/versions/1").status_code == 200
    r = c.post("/api/assembly/versions/1/restore/preview",
               json={"expected_version": 3})
    assert r.status_code == 200
    # 间隙查询只读可用
    r = c.post("/api/assembly/versions/1/gaps",
               json={"pairs": [{"fragment_a": 1, "fragment_b": 2}]})
    assert r.status_code == 200
