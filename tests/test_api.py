"""API 端到端测试：网页入口、导入 -> 下载 -> 撤除 -> 下载 全流程。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.sample import build_sample_manifest
from app.store import BLOCK_SIZE
from tests.conftest import make_manifest


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path)))


class TestPages:
    def test_index_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "虚拟机备份维护台" in resp.text

    def test_initial_state_empty(self, client):
        st = client.get("/api/state").json()
        assert st["initialized"] is False
        assert st["layers"] == []
        assert st["max_disk_size"] == 16 * 1024 * 1024

    def test_sample_endpoint(self, client):
        sample = client.get("/api/sample").json()
        assert sample["disk_size"] == 1024 * 1024
        assert len(sample["layers"]) == 5


class TestFullFlow:
    def test_import_download_remove_download(self, client):
        sample = build_sample_manifest()

        st = client.post("/api/import", json=sample).json()
        assert st["initialized"] is True
        assert len(st["layers"]) == 5

        # 撤除前各恢复点的原始镜像
        before = {}
        for lid in ["base", "snap-1", "snap-2", "snap-3", "snap-4"]:
            resp = client.get(f"/api/download/{lid}")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/octet-stream"
            assert "attachment" in resp.headers["content-disposition"]
            assert len(resp.content) == sample["disk_size"]
            before[lid] = resp.content

        # 抽查内容：snap-2 的块0 被自身覆盖为 C，块2 继承 snap-1 的显式全零
        assert before["snap-2"][:BLOCK_SIZE] == b"C" * BLOCK_SIZE
        assert before["snap-2"][2 * BLOCK_SIZE:3 * BLOCK_SIZE] == bytes(BLOCK_SIZE)
        assert before["snap-1"][2 * BLOCK_SIZE:3 * BLOCK_SIZE] == bytes(BLOCK_SIZE)
        assert before["base"][2 * BLOCK_SIZE:3 * BLOCK_SIZE] == b"\x39" * BLOCK_SIZE

        # 撤除 snap-1（非根、有子层 snap-2/snap-3）
        st = client.post("/api/remove/snap-1").json()
        assert "snap-2" in st["message"] and "snap-3" in st["message"]
        by_id = {l["id"]: l for l in st["layers"]}
        assert set(by_id) == {"base", "snap-2", "snap-3", "snap-4"}
        assert by_id["snap-2"]["parent"] == "base"
        assert by_id["snap-3"]["parent"] == "base"
        assert by_id["snap-4"]["parent"] == "snap-2"  # 孙层不动

        # 保留点恢复字节与撤除前完全一致
        for lid in ["base", "snap-2", "snap-3", "snap-4"]:
            resp = client.get(f"/api/download/{lid}")
            assert resp.content == before[lid], f"{lid} 字节不一致"

        # 被撤除的快照不可再下载
        assert client.get("/api/download/snap-1").status_code == 404

    def test_download_16mib_limit_exposed(self, client):
        st = client.get("/api/state").json()
        assert st["block_size"] == 4096


class TestApiErrors:
    def test_remove_root_400(self, client):
        client.post("/api/import", json=build_sample_manifest())
        resp = client.post("/api/remove/base")
        assert resp.status_code == 400
        assert "基盘" in resp.json()["detail"]

    def test_remove_leaf_400(self, client):
        client.post("/api/import", json=build_sample_manifest())
        resp = client.post("/api/remove/snap-3")
        assert resp.status_code == 400

    def test_remove_unknown_404(self, client):
        client.post("/api/import", json=build_sample_manifest())
        assert client.post("/api/remove/ghost").status_code == 404

    def test_download_unknown_404(self, client):
        client.post("/api/import", json=build_sample_manifest())
        assert client.get("/api/download/ghost").status_code == 404

    def test_import_invalid_json_body(self, client):
        resp = client.post("/api/import", content=b"{not json",
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 400

    def test_import_validation_error_400(self, client):
        bad = make_manifest(layers=[{"id": "a", "parent": None,
                                     "blocks": {"0": "not-base64!!"}}])
        resp = client.post("/api/import", json=bad)
        assert resp.status_code == 400
        assert "base64" in resp.json()["detail"]

    def test_remove_before_import_400(self, client):
        assert client.post("/api/remove/x").status_code == 400
