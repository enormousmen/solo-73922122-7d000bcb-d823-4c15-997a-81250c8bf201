"""测试共享工具：构造确定性清单、独立实现的“期望镜像”解析器。"""

from __future__ import annotations

import base64

import pytest

from app.store import BLOCK_SIZE, Store

DISK_SIZE = 65536  # 16 块，便于手算


def blk(fill: int) -> str:
    """生成整块填充某字节的 4KiB 块的 base64。"""
    assert 0 <= fill <= 255
    return base64.b64encode(bytes([fill]) * BLOCK_SIZE).decode("ascii")


def sample_layers() -> list[dict]:
    """base(0,1,2,7) <- mid(1 覆盖, 2 显式全零) <- left(0) / right(3)，left <- leaf(5)"""
    return [
        {"id": "base", "parent": None, "blocks": {
            "0": blk(0x01), "1": blk(0x02), "2": blk(0x09), "7": blk(0x07)}},
        {"id": "mid", "parent": "base", "blocks": {
            "1": blk(0x12), "2": blk(0x00)}},
        {"id": "left", "parent": "mid", "blocks": {"0": blk(0x1A)}},
        {"id": "right", "parent": "mid", "blocks": {"3": blk(0x1B)}},
        {"id": "leaf", "parent": "left", "blocks": {"5": blk(0x1C)}},
    ]


def make_manifest(layers=None, disk_size: int = DISK_SIZE) -> dict:
    return {"disk_size": disk_size, "layers": layers if layers is not None else sample_layers()}


def expected_image(layers: list[dict], layer_id: str, disk_size: int = DISK_SIZE) -> bytes:
    """独立于 Store 实现的解析器：沿父链覆盖合并。"""
    by_id = {l["id"]: l for l in layers}
    merged: dict[int, bytes] = {}
    cur = layer_id
    while cur is not None:
        layer = by_id[cur]
        for key, value in layer["blocks"].items():
            idx = int(key)
            if idx not in merged:  # 子层优先
                merged[idx] = base64.b64decode(value)
        cur = layer["parent"]
    buf = bytearray(disk_size)
    for idx, data in merged.items():
        buf[idx * BLOCK_SIZE:(idx + 1) * BLOCK_SIZE] = data
    return bytes(buf)


def manifest_files(store: Store) -> set[str]:
    return {info["file"] for info in store._manifest["layers"].values()}


def files_on_disk(store: Store) -> set[str]:
    return {p.name for p in store.layers_dir.iterdir() if p.is_file()}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def imported(store):
    store.import_manifest(make_manifest())
    return store
