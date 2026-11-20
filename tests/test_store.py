"""存储引擎测试：导入/解析、撤除语义、崩溃恢复、写盘失败回滚、校验。"""

from __future__ import annotations

import json

import pytest

import app.store as store_mod
from app.store import (
    BLOCK_SIZE,
    JOURNAL_NAME,
    MAX_DISK_SIZE,
    Store,
    StoreError,
    NotFoundError,
)
from tests.conftest import (
    blk,
    expected_image,
    files_on_disk,
    make_manifest,
    manifest_files,
    sample_layers,
)


# --------------------------------------------------------------------- #
# 导入与镜像还原
# --------------------------------------------------------------------- #
class TestImportAndResolve:
    def test_resolve_matches_independent_parser(self, imported):
        layers = sample_layers()
        for lid in ["base", "mid", "left", "right", "leaf"]:
            assert imported.resolve_image(lid) == expected_image(layers, lid)

    def test_missing_blocks_are_zero(self, imported):
        img = imported.resolve_image("base")
        assert img[3 * BLOCK_SIZE:(4) * BLOCK_SIZE] == bytes(BLOCK_SIZE)
        assert img[0:BLOCK_SIZE] == bytes([0x01]) * BLOCK_SIZE

    def test_zero_block_overrides_parent(self, imported):
        # mid 的块2 是显式全零，覆盖 base 的 0x09
        img = imported.resolve_image("mid")
        assert img[2 * BLOCK_SIZE:3 * BLOCK_SIZE] == bytes(BLOCK_SIZE)
        # left 继承 mid 的全零块
        assert imported.resolve_image("left")[2 * BLOCK_SIZE:3 * BLOCK_SIZE] == bytes(BLOCK_SIZE)

    def test_one_file_per_layer(self, imported):
        assert len(files_on_disk(imported)) == 5
        assert files_on_disk(imported) == manifest_files(imported)

    def test_import_replaces_previous_store(self, imported, tmp_path):
        imported.import_manifest(make_manifest(
            layers=[{"id": "only", "parent": None, "blocks": {"0": blk(0x55)}}]))
        state = imported.state()
        assert [l["id"] for l in state["layers"]] == ["only"]
        assert files_on_disk(imported) == manifest_files(imported)
        assert imported.resolve_image("only")[:BLOCK_SIZE] == bytes([0x55]) * BLOCK_SIZE

    def test_state_flags(self, imported):
        state = imported.state()
        by_id = {l["id"]: l for l in state["layers"]}
        assert by_id["mid"]["can_remove"] is True      # 非根且有子层
        assert by_id["left"]["can_remove"] is True
        assert by_id["base"]["can_remove"] is False    # 根
        assert by_id["leaf"]["can_remove"] is False    # 无子层
        assert by_id["mid"]["children"] == ["left", "right"]


# --------------------------------------------------------------------- #
# 撤除语义
# --------------------------------------------------------------------- #
class TestRemove:
    def test_remove_middle_preserves_all_bytes(self, imported):
        layers = sample_layers()
        before = {lid: imported.resolve_image(lid)
                  for lid in ["base", "left", "right", "leaf"]}
        imported.remove("mid")
        for lid, img in before.items():
            assert imported.resolve_image(lid) == img, f"{lid} 字节发生变化"

    def test_remove_relinks_children_and_rewrites_only_them(self, imported):
        state = imported.remove("mid")
        by_id = {l["id"]: l for l in state["layers"]}
        assert set(by_id) == {"base", "left", "right", "leaf"}
        assert by_id["left"]["parent"] == "base"
        assert by_id["right"]["parent"] == "base"
        assert by_id["leaf"]["parent"] == "left"  # 孙层不动
        # 磁盘上只剩新清单引用的文件（旧子层文件与 mid 文件已删除）
        assert files_on_disk(imported) == manifest_files(imported)
        assert len(files_on_disk(imported)) == 4

    def test_merged_child_inherits_only_missing_blocks(self, imported):
        imported.remove("mid")
        left_file = imported._manifest["layers"]["left"]["file"]
        blocks = json.loads((imported.layers_dir / left_file).read_text())["blocks"]
        # left 自己的块0 保留自己的值；缺块1、2 从 mid 继承（2 是显式全零）
        assert blocks["0"] == blk(0x1A)
        assert blocks["1"] == blk(0x12)
        assert blocks["2"] == blk(0x00)
        # 块7 只在 base 里，不应物化进 left（缺块才向父层继承，保持稀疏）
        assert "7" not in blocks and "5" not in blocks and "3" not in blocks

    def test_remove_with_grandchild_untouched(self, imported):
        leaf_before = imported.resolve_image("leaf")
        left_file_before = imported._manifest["layers"]["leaf"]["file"]
        imported.remove("mid")
        assert imported._manifest["layers"]["leaf"]["file"] == left_file_before
        assert imported.resolve_image("leaf") == leaf_before

    def test_remove_root_rejected(self, imported):
        with pytest.raises(StoreError, match="根层|基盘"):
            imported.remove("base")

    def test_remove_childless_rejected(self, imported):
        with pytest.raises(StoreError, match="没有子层"):
            imported.remove("right")

    def test_remove_unknown_rejected(self, imported):
        with pytest.raises(NotFoundError):
            imported.remove("nope")

    def test_remove_without_import_rejected(self, store):
        with pytest.raises(StoreError, match="尚未导入"):
            store.remove("mid")

    def test_sequential_removes(self, imported):
        layers = sample_layers()
        expected_leaf = expected_image(layers, "leaf")
        expected_right = expected_image(layers, "right")
        imported.remove("mid")
        imported.remove("left")   # left 此时有子层 leaf
        assert imported.resolve_image("leaf") == expected_leaf
        assert imported.resolve_image("right") == expected_right
        assert {l["id"] for l in imported.state()["layers"]} == {"base", "right", "leaf"}
        assert files_on_disk(imported) == manifest_files(imported)


# --------------------------------------------------------------------- #
# 崩溃恢复（模拟进程中断后重启 = 新建 Store 实例）
# --------------------------------------------------------------------- #
class TestCrashRecovery:
    def test_crash_before_commit_rolls_back(self, imported, tmp_path):
        before = {lid: imported.resolve_image(lid)
                  for lid in ["base", "mid", "left", "right", "leaf"]}
        imported._prepare_remove("mid")          # 写完新子层文件 + journal 后“崩溃”
        assert (tmp_path / JOURNAL_NAME).exists()

        rebooted = Store(tmp_path)               # 重启
        assert any("回滚" in e for e in rebooted.recovery_events)
        assert not (tmp_path / JOURNAL_NAME).exists()
        # 旧链完整可用：五层都在，字节一致
        assert {l["id"] for l in rebooted.state()["layers"]} == set(before)
        for lid, img in before.items():
            assert rebooted.resolve_image(lid) == img
        # 新写的合并文件已清理，磁盘文件与清单一一对应
        assert files_on_disk(rebooted) == manifest_files(rebooted)

    def test_crash_after_commit_rolls_forward(self, imported, tmp_path):
        layers = sample_layers()
        expected = {lid: expected_image(layers, lid)
                    for lid in ["base", "left", "right", "leaf"]}
        plan = imported._prepare_remove("mid")
        imported._commit_remove(plan)            # 清单已切换、收尾前“崩溃”
        assert (tmp_path / JOURNAL_NAME).exists()

        rebooted = Store(tmp_path)
        assert any("前滚" in e for e in rebooted.recovery_events)
        assert not (tmp_path / JOURNAL_NAME).exists()
        assert {l["id"] for l in rebooted.state()["layers"]} == set(expected)
        for lid, img in expected.items():
            assert rebooted.resolve_image(lid) == img
        # 旧层文件（mid 与旧子层文件）已补删
        assert files_on_disk(rebooted) == manifest_files(rebooted)

    def test_corrupt_journal_treated_as_pre_switch(self, imported, tmp_path):
        before = imported.resolve_image("leaf")
        imported._prepare_remove("mid")
        (tmp_path / JOURNAL_NAME).write_text("{oops", encoding="utf-8")  # 写 journal 时崩溃
        rebooted = Store(tmp_path)
        assert rebooted.resolve_image("leaf") == before
        assert {l["id"] for l in rebooted.state()["layers"]} == {
            "base", "mid", "left", "right", "leaf"}
        assert files_on_disk(rebooted) == manifest_files(rebooted)

    def test_orphan_files_collected_on_boot(self, imported, tmp_path):
        (imported.layers_dir / "stray.layer").write_text("{}")
        (imported.layers_dir / "junk.tmp-123").write_text("x")
        rebooted = Store(tmp_path)
        assert "stray.layer" not in files_on_disk(rebooted)
        assert files_on_disk(rebooted) == manifest_files(rebooted)


# --------------------------------------------------------------------- #
# 写盘失败：切换前失败，旧链仍须可用
# --------------------------------------------------------------------- #
class TestWriteFailure:
    def test_commit_failure_keeps_old_chain(self, imported, monkeypatch):
        before = {lid: imported.resolve_image(lid)
                  for lid in ["base", "mid", "left", "right", "leaf"]}
        real_write = store_mod._atomic_write

        def fail_on_manifest(path, data):
            if path.name == store_mod.MANIFEST_NAME:
                raise OSError("模拟写盘失败")
            return real_write(path, data)

        monkeypatch.setattr(store_mod, "_atomic_write", fail_on_manifest)
        with pytest.raises(StoreError, match="旧链仍可用"):
            imported.remove("mid")
        monkeypatch.undo()

        # 旧链完整可用，journal 与新文件已回滚清理
        for lid, img in before.items():
            assert imported.resolve_image(lid) == img
        assert files_on_disk(imported) == manifest_files(imported)
        rebooted = Store(imported.root)  # 重启后依旧一致
        assert {l["id"] for l in rebooted.state()["layers"]} == set(before)

    def test_layer_write_failure_aborts_before_journal(self, imported, monkeypatch):
        real_write = store_mod._atomic_write

        def fail_on_layer(path, data):
            if path.parent.name == store_mod.LAYERS_DIR:
                raise OSError("模拟磁盘写满")
            return real_write(path, data)

        monkeypatch.setattr(store_mod, "_atomic_write", fail_on_layer)
        with pytest.raises(StoreError, match="旧链未受影响"):
            imported.remove("mid")
        monkeypatch.undo()

        assert not (imported.root / JOURNAL_NAME).exists()
        assert files_on_disk(imported) == manifest_files(imported)
        assert imported.resolve_image("leaf") == expected_image(sample_layers(), "leaf")


# --------------------------------------------------------------------- #
# 导入校验
# --------------------------------------------------------------------- #
class TestImportValidation:
    @pytest.mark.parametrize("disk_size", [0, -1, MAX_DISK_SIZE + BLOCK_SIZE, 1000])
    def test_bad_disk_size(self, store, disk_size):
        with pytest.raises(StoreError):
            store.import_manifest({"disk_size": disk_size, "layers": [
                {"id": "a", "parent": None, "blocks": {}}]})

    def test_max_disk_size_accepted(self, store):
        store.import_manifest({"disk_size": MAX_DISK_SIZE, "layers": [
            {"id": "a", "parent": None, "blocks": {"4095": blk(0x01)}}]})
        assert len(store.resolve_image("a")) == MAX_DISK_SIZE

    @pytest.mark.parametrize("bad_index", ["-1", "16", "01", " 1", "x", "1.0"])
    def test_bad_block_index(self, store, bad_index):
        with pytest.raises(StoreError):
            store.import_manifest({"disk_size": 65536, "layers": [
                {"id": "a", "parent": None, "blocks": {bad_index: blk(0x01)}}]})

    def test_bad_base64(self, store):
        with pytest.raises(StoreError, match="base64"):
            store.import_manifest({"disk_size": 65536, "layers": [
                {"id": "a", "parent": None, "blocks": {"0": "!!!not-base64!!!"}}]})

    def test_wrong_block_length(self, store):
        import base64 as b64
        with pytest.raises(StoreError, match="4096"):
            store.import_manifest({"disk_size": 65536, "layers": [
                {"id": "a", "parent": None,
                 "blocks": {"0": b64.b64encode(b"short").decode()}}]})

    def test_duplicate_id(self, store):
        with pytest.raises(StoreError, match="重复"):
            store.import_manifest({"disk_size": 65536, "layers": [
                {"id": "a", "parent": None, "blocks": {}},
                {"id": "a", "parent": None, "blocks": {}}]})

    def test_unknown_parent(self, store):
        with pytest.raises(StoreError, match="父层"):
            store.import_manifest({"disk_size": 65536, "layers": [
                {"id": "a", "parent": "ghost", "blocks": {}}]})

    def test_cycle_rejected(self, store):
        with pytest.raises(StoreError, match="循环"):
            store.import_manifest({"disk_size": 65536, "layers": [
                {"id": "a", "parent": "b", "blocks": {}},
                {"id": "b", "parent": "a", "blocks": {}}]})

    def test_empty_layers_rejected(self, store):
        with pytest.raises(StoreError):
            store.import_manifest({"disk_size": 65536, "layers": []})

    def test_failed_import_leaves_old_store_intact(self, imported):
        before = imported.resolve_image("leaf")
        with pytest.raises(StoreError):
            imported.import_manifest({"disk_size": 65536, "layers": [
                {"id": "x", "parent": None, "blocks": {"0": "bad!!"}}]})
        assert imported.resolve_image("leaf") == before
        assert files_on_disk(imported) == manifest_files(imported)
