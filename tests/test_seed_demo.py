"""演示数据播种时机回归测试。

规则：4 片演示陶片仅在数据库**首次初始化**（文件本不存在 / 空文件建表）
且 SEED_DEMO_DATA 开启时写入一次。既有数据库 —— 哪怕碎片已被全部删除、
历史已恢复到空装配、或为迁移导入后的库 —— 重启时一律保持原装配、版本
与不可改快照，不回填演示陶片、不新增版本；首次初始化时关闭开关，后续
开启也不补种。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import main
from app.portable import target_is_blank

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


@pytest.fixture(autouse=True)
def _cleanup_db():
    """每个用例使用独立临时库；结束后关闭连接并复位全局状态。"""
    main.db and main.db.conn.close()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # DB( 自己创建文件，确保「首次初始化」路径真实
    main.DB_PATH = path
    yield path
    if main.db is not None:
        main.db.conn.close()
        main.db = None
    if os.path.exists(path):
        os.unlink(path)


def _restart(seed: bool):
    """模拟工作站重启：关闭当前连接，按当前 main.DB_PATH 重新走 lifespan。"""
    if main.db is not None:
        main.db.conn.close()
    main.db = None
    main.SEED_DEMO_DATA = seed
    return TestClient(main.app)


def _delete_all(c, start_version):
    v = start_version
    ids = [f["id"] for f in c.get("/api/fragments").json()]
    for fid in ids:
        r = c.delete(f"/api/fragments/{fid}?expected_version={v}")
        assert r.status_code == 200, r.text
        v = r.json()["version"]
    return v


# ---------------------------------------------------------------- 首次初始化

def test_fresh_db_with_seed_writes_demo_once(_cleanup_db):
    with _restart(seed=True) as c:
        asm = c.get("/api/assembly").json()
        assert len(asm["fragments"]) == 4
        assert asm["version"] == 1
        vs = c.get("/api/assembly/versions").json()["versions"]
        assert [(v["version"], v["source"]) for v in vs] == [(1, "baseline")]


def test_fresh_db_without_seed_stays_blank(_cleanup_db):
    with _restart(seed=False) as c:
        asm = c.get("/api/assembly").json()
        assert asm["fragments"] == []
        assert asm["version"] == 0
        vs = c.get("/api/assembly/versions").json()["versions"]
        assert [(v["version"], v["source"]) for v in vs] == [(0, "baseline")]
        # 空白基线是合法迁移目标
        assert target_is_blank(main.db)[0] is True


# ---------------------------------------------------------------- 删空后重启

def test_no_reseed_after_all_fragments_deleted(_cleanup_db):
    with _restart(seed=True) as c:
        v = _delete_all(c, 1)
        assert v == 5 and c.get("/api/fragments").json() == []

    # 重启（开关仍开启）：不回填、不新增版本与快照
    with _restart(seed=True) as c:
        asm = c.get("/api/assembly").json()
        assert asm["fragments"] == []
        assert asm["version"] == 5
        vs = c.get("/api/assembly/versions").json()["versions"]
        # 新版本在前：1 条 baseline + 4 条 delete，没有第二条 baseline
        assert [x["source"] for x in vs] == \
            ["delete", "delete", "delete", "delete", "baseline"]
        # 曾有非空快照 → 迁移导入空白目标的判定不变（非空白）
        is_blank, target = target_is_blank(main.db)
        assert is_blank is False
        assert target["nonempty_snapshot_versions"] == [4, 3, 2, 1]
        # 删除流程与历史 API 重启后仍可调用
        r = c.post("/api/assembly/versions/1/restore/preview",
                   json={"expected_version": 5})
        assert r.status_code == 200
        r = c.post("/api/assembly/versions/1/restore/confirm",
                   json={"expected_version": 5})
        assert r.status_code == 201
        assert r.json()["version"] == 6
        assert len(r.json()["assembly"]["fragments"]) == 4


def test_empty_state_idempotent_across_multiple_restarts(_cleanup_db):
    with _restart(seed=True) as c:
        _delete_all(c, 1)
    for _ in range(2):
        with _restart(seed=True) as c:
            asm = c.get("/api/assembly").json()
            assert asm["fragments"] == [] and asm["version"] == 5
            assert len(c.get("/api/assembly/versions").json()["versions"]) == 5


# ---------------------------------------------------------------- 恢复到空装配

def test_no_reseed_after_restoring_history_to_empty_assembly(_cleanup_db):
    with _restart(seed=True) as c:
        # v1 为非空基线；先把当前删空（v5），再录入一片（v6），
        # 然后恢复到空的 v5 → v7 为「恢复」产生的空装配快照
        _delete_all(c, 1)
        r = c.post("/api/fragments", json={"name": "陶片E", "contour": SQUARE})
        assert r.status_code == 201 and r.json()["version"] == 6
        r = c.post("/api/assembly/versions/5/restore/confirm",
                   json={"expected_version": 6})
        assert r.status_code == 201, r.text
        assert r.json()["version"] == 7
        assert r.json()["assembly"]["fragments"] == []

    # 重启：保持空装配 v7，不回填、不加版本
    with _restart(seed=True) as c:
        asm = c.get("/api/assembly").json()
        assert asm["fragments"] == [] and asm["version"] == 7
        vs = c.get("/api/assembly/versions").json()["versions"]
        assert [(v["version"], v["source"]) for v in vs] == \
            [(7, "restore"), (6, "create"),
             (5, "delete"), (4, "delete"), (3, "delete"),
             (2, "delete"), (1, "baseline")]
        assert target_is_blank(main.db)[0] is False


# ---------------------------------------------------------------- 开关时序

def test_seed_disabled_at_init_then_enabled_never_reseeds(_cleanup_db):
    with _restart(seed=False) as c:
        assert c.get("/api/fragments").json() == []
        assert c.get("/api/assembly").json()["version"] == 0

    # 后续重启把开关打开：仍保持空白，不补种
    with _restart(seed=True) as c:
        asm = c.get("/api/assembly").json()
        assert asm["fragments"] == [] and asm["version"] == 0
        vs = c.get("/api/assembly/versions").json()["versions"]
        assert [(v["version"], v["source"]) for v in vs] == [(0, "baseline")]
        assert target_is_blank(main.db)[0] is True
        # 之后正常录入，id 从 1 开始、版本正常递增
        r = c.post("/api/fragments", json={"name": "陶片E", "contour": SQUARE})
        assert r.status_code == 201
        assert r.json()["fragment"]["id"] == 1
        assert r.json()["version"] == 1


def test_user_fragment_survives_restart_without_demo_mix(_cleanup_db):
    with _restart(seed=False) as c:
        r = c.post("/api/fragments", json={"name": "陶片E", "contour": SQUARE})
        assert r.status_code == 201 and r.json()["version"] == 1

    with _restart(seed=True) as c:
        frags = c.get("/api/fragments").json()
        assert [f["name"] for f in frags] == ["陶片E"]
        assert c.get("/api/assembly").json()["version"] == 1


# ---------------------------------------------------------------- 迁移交互

def test_imported_database_not_reseeded_and_blank_judgment_kept(_cleanup_db):
    dst_path = _cleanup_db
    # 从全新 SEED=1 库导出演示包
    fd, src_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(src_path)
    main.db and main.db.conn.close()
    main.db = None
    main.DB_PATH = src_path
    main.SEED_DEMO_DATA = True
    with TestClient(main.app) as src:
        pkg = src.get("/api/transfer/export").json()
    os.unlink(src_path)

    # 目标为全新 SEED=0 空白库：可导入
    main.DB_PATH = dst_path
    with _restart(seed=False) as c:
        assert target_is_blank(main.db)[0] is True
        r = c.post("/api/transfer/import/confirm",
                   json={"package": pkg, "expected_version": 0})
        assert r.status_code == 201, r.text
        assert r.json()["imported_version"] == 1

    # 导入后重启（开关改为 1）：原装配 / 版本 / 快照原样保留，
    # 不补种、不新增版本，且不再是空白迁移目标
    with _restart(seed=True) as c:
        asm = c.get("/api/assembly").json()
        assert len(asm["fragments"]) == 4 and asm["version"] == 1
        vs = c.get("/api/assembly/versions").json()["versions"]
        assert [(v["version"], v["source"]) for v in vs] == [(1, "baseline")]
        assert target_is_blank(main.db)[0] is False
        # 既有 API 语义不变
        r = c.post("/api/transfer/import/confirm",
                   json={"package": pkg, "expected_version": 1})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "target_not_blank"
        r = c.post("/api/fragments", json={"name": "陶片F", "contour": SQUARE})
        assert r.status_code == 201 and r.json()["version"] == 2
