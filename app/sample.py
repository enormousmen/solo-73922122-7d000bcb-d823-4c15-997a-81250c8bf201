"""生成一份可直接导入的示例备份清单，便于网页一键演示。

结构（盘 1 MiB = 256 块）：
    base                 块0=A 块1=B 块2=9 块200=Z
    └─ snap-1            块1=b（覆盖） 块2=全零（显式改写，覆盖 base 的 9）
       ├─ snap-2         块0=C
       │  └─ snap-4      块5=E
       └─ snap-3         块3=D

snap-1 是非根且有子层，适合演示“撤除”；snap-2 亦有一个子层。
"""

from __future__ import annotations

import base64

BLOCK_SIZE = 4096


def _fill(byte: bytes) -> str:
    """用单字节填满 4 KiB 并 base64 编码。"""
    return base64.b64encode(byte * BLOCK_SIZE).decode("ascii")


def build_sample_manifest() -> dict:
    return {
        "disk_size": 1024 * 1024,
        "layers": [
            {"id": "base", "parent": None, "blocks": {
                "0": _fill(b"A"), "1": _fill(b"B"), "2": _fill(b"\x39"),
                "200": _fill(b"Z")}},
            {"id": "snap-1", "parent": "base", "blocks": {
                "1": _fill(b"b"), "2": _fill(b"\x00")}},
            {"id": "snap-2", "parent": "snap-1", "blocks": {
                "0": _fill(b"C")}},
            {"id": "snap-4", "parent": "snap-2", "blocks": {
                "5": _fill(b"E")}},
            {"id": "snap-3", "parent": "snap-1", "blocks": {
                "3": _fill(b"D")}},
        ],
    }
