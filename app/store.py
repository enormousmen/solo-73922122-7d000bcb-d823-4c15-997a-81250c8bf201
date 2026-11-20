"""快照链存储引擎。

磁盘布局（DATA_DIR）：
    manifest.json    当前清单（原子替换更新），记录 disk_size 与每层 {parent, file, blocks}
    journal.json     撤除操作的两阶段日志，仅在操作进行中存在
    layers/*.layer   每层一个文件，JSON 存稀疏块 {"blocks": {"块索引": "base64(4KiB)"}}

撤除（remove）顺序 —— 提交点之前旧链始终完整可用：
    1. 为每个直接子层写出合并后的新层文件（uuid 命名，tmp+rename+fsync）
    2. 写 journal.json（记录旧/新 epoch、新文件、待删旧文件、完整新清单）
    3. 原子替换 manifest.json                      —— 提交点
    4. 删除旧层文件与 journal（收尾，失败可留给启动恢复清理）

启动恢复（保证重启后只呈现一套完整可恢复的链）：
    journal 有效且 manifest 已是新 epoch -> 前滚：补删旧文件
    journal 有效且 manifest 仍是旧 epoch -> 回滚：删除新文件，旧链原样保留
    journal 有效但 epoch 与清单都不匹配 -> 清单权威（典型场景：撤除后又导入了
        新备份），按清单对账后丢弃日志，被新清单取代的文件由孤儿清理解掉
    journal 缺失或损坏                   -> 切换未开始，清理孤儿/临时文件即可

导入与撤除在动手前都会先对残留 journal 做一次同样的对账，因此“撤除日志未清理 +
再次导入”不会留下任何在途状态；导入本身失败时旧清单与旧层文件原封不动。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import uuid
from pathlib import Path

BLOCK_SIZE = 4096                      # 4 KiB 盘块
MAX_DISK_SIZE = 16 * 1024 * 1024       # 单盘至多 16 MiB

MANIFEST_NAME = "manifest.json"
JOURNAL_NAME = "journal.json"
LAYERS_DIR = "layers"

_LAYER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class StoreError(Exception):
    """可预期的用户侧错误（校验失败、状态冲突），对应 HTTP 400。"""


class NotFoundError(StoreError):
    """引用了不存在的快照，对应 HTTP 404。"""


class CorruptStoreError(Exception):
    """存储内部不一致，需要人工介入，对应 HTTP 500。"""


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    """写临时文件 -> fsync -> rename -> fsync 目录。失败时目标文件保持原样。"""
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.layers_dir = self.root / LAYERS_DIR
        self.layers_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.recovery_events: list[str] = []
        self._recover()
        self._manifest = self._load_manifest()

    # ------------------------------------------------------------------ #
    # 启动恢复
    # ------------------------------------------------------------------ #
    def _recover(self) -> None:
        journal_path = self.root / JOURNAL_NAME
        if journal_path.exists():
            try:
                journal = json.loads(journal_path.read_text("utf-8"))
                if not self._is_valid_journal(journal):
                    raise ValueError("journal 结构不完整")
            except Exception:
                # 日志写在清单切换之前；日志损坏说明崩溃发生在切换开始之前，
                # 旧清单必然完好，按“无操作在途”处理并清掉残迹。
                journal_path.unlink(missing_ok=True)
                self.recovery_events.append(
                    "发现损坏的 journal（崩溃发生在切换前），已按无在途操作清理"
                )
            else:
                self._reconcile_journal(journal)

        # 清理根目录残留临时文件与 layers/ 下未被引用的文件（含导入中断的残骸）
        for tmp in self.root.glob("*.tmp-*"):
            tmp.unlink(missing_ok=True)
        self._gc_orphans()

    @staticmethod
    def _is_valid_journal(journal: object) -> bool:
        if not isinstance(journal, dict) or journal.get("op") != "remove":
            return False
        for key in ("old_epoch", "new_epoch", "new_files", "obsolete_files",
                    "new_manifest", "target"):
            if key not in journal:
                return False
        return (isinstance(journal["new_files"], list)
                and isinstance(journal["obsolete_files"], list)
                and all(isinstance(n, str) for n in journal["new_files"])
                and all(isinstance(n, str) for n in journal["obsolete_files"])
                and isinstance(journal["new_manifest"], dict))

    def _reconcile_journal(self, journal: dict) -> None:
        """按 manifest 与 journal 的实际提交状态对账，并清除 journal。

        启动恢复与导入/撤除前的预处理共用本方法，保证任何时刻都不会带着
        上一轮操作的在途日志进入新的清单切换。
        """
        journal_path = self.root / JOURNAL_NAME
        manifest = self._load_manifest()
        epoch = manifest.get("epoch") if manifest else None
        if epoch == journal.get("new_epoch"):
            # 清单已切换但收尾未完成 -> 前滚：补删旧文件
            for name in journal.get("obsolete_files", []):
                self._delete_layer_file(name)
            journal_path.unlink(missing_ok=True)
            self.recovery_events.append(
                f"检测到已切换未收尾的撤除（目标 {journal.get('target')}），"
                "已前滚并清理旧层文件"
            )
        elif epoch == journal.get("old_epoch"):
            # 清单尚未切换 -> 回滚，旧链原样保留
            for name in journal.get("new_files", []):
                self._delete_layer_file(name)
            journal_path.unlink(missing_ok=True)
            self.recovery_events.append(
                f"检测到未完成的撤除（目标 {journal.get('target')}），"
                "已回滚，保留旧链"
            )
        else:
            # epoch 与日志的新旧值都不匹配：清单已被后续操作（如再次导入）替换。
            # 清单是唯一的提交权威，journal 描述的在途状态已无意义，直接丢弃；
            # 日志里的新/旧文件若不被当前清单引用，由随后的孤儿清理解掉。
            journal_path.unlink(missing_ok=True)
            self.recovery_events.append(
                f"发现与当前清单 epoch 不匹配的陈旧 journal（撤除目标 "
                f"{journal.get('target')}），清单已被后续操作替换，已丢弃日志并对账"
            )

    def _gc_orphans(self) -> None:
        referenced = set()
        manifest = self._load_manifest()
        if manifest:
            referenced = {info["file"] for info in manifest["layers"].values()}
        removed = 0
        for f in self.layers_dir.iterdir():
            if f.is_file() and f.name not in referenced:
                f.unlink(missing_ok=True)
                removed += 1
        if removed:
            self.recovery_events.append(f"清理了 {removed} 个未被引用的层文件")

    def _reconcile_pending_journal(self) -> None:
        """操作入口处的在途日志对账：损坏即清除，有效则按提交状态收尾/回滚。

        调用方必须持有 self._lock。对账后 journal 必然不存在，且磁盘文件与
        当前 manifest 重新一致（孤儿由 _gc_orphans 兜底）。
        """
        journal_path = self.root / JOURNAL_NAME
        if not journal_path.exists():
            return
        try:
            journal = json.loads(journal_path.read_text("utf-8"))
            if not self._is_valid_journal(journal):
                raise ValueError("journal 结构不完整")
        except Exception:
            journal_path.unlink(missing_ok=True)
            self.recovery_events.append("发现损坏的 journal，已按无在途操作清理")
            self._gc_orphans()
            return
        self._reconcile_journal(journal)
        self._gc_orphans()

    def _load_manifest(self) -> dict | None:
        path = self.root / MANIFEST_NAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception as exc:
            raise CorruptStoreError(f"manifest.json 损坏：{exc}") from exc
        if not isinstance(data, dict) or "layers" not in data or "epoch" not in data:
            raise CorruptStoreError("manifest.json 结构不完整")
        return data

    # ------------------------------------------------------------------ #
    # 层文件读写
    # ------------------------------------------------------------------ #
    def _write_layer_file(self, blocks: dict[int, bytes]) -> str:
        """把稀疏块写成一个新层文件（uuid 命名，原子落盘），返回文件名。"""
        name = f"{uuid.uuid4().hex}.layer"
        payload = json.dumps(
            {"blocks": {str(i): base64.b64encode(b).decode("ascii")
                        for i, b in sorted(blocks.items())}}
        ).encode("utf-8")
        _atomic_write(self.layers_dir / name, payload)
        return name

    def _read_layer_file(self, name: str) -> dict[int, bytes]:
        path = self.layers_dir / name
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError as exc:
            raise CorruptStoreError(f"层文件丢失：{name}") from exc
        except Exception as exc:
            raise CorruptStoreError(f"层文件损坏：{name}: {exc}") from exc
        return {int(k): base64.b64decode(v) for k, v in raw["blocks"].items()}

    def _delete_layer_file(self, name: str) -> None:
        try:
            (self.layers_dir / name).unlink(missing_ok=True)
        except OSError:
            pass  # 留待下次启动时的孤儿清理

    # ------------------------------------------------------------------ #
    # 导入
    # ------------------------------------------------------------------ #
    def import_manifest(self, payload: dict) -> dict:
        """校验并整体替换当前存储。新清单原子切换，切换前旧链不受影响。"""
        disk_size, layers = self._validate_import(payload)
        with self._lock:
            # 上一轮撤除若留下未清理的 journal，先按实际提交状态对账，
            # 绝不能带着在途日志切换清单，否则重启恢复会无法判定状态。
            self._reconcile_pending_journal()
            files: dict[str, str] = {}
            try:
                for lid, layer in layers.items():
                    files[lid] = self._write_layer_file(layer["blocks"])
            except OSError as exc:
                for name in files.values():
                    self._delete_layer_file(name)
                raise StoreError(f"写入层文件失败，旧数据未受影响：{exc}") from exc

            new_manifest = {
                "epoch": uuid.uuid4().hex,
                "disk_size": disk_size,
                "layers": {
                    lid: {
                        "parent": layers[lid]["parent"],
                        "file": files[lid],
                        "blocks": len(layers[lid]["blocks"]),
                    }
                    for lid in layers
                },
            }
            try:
                _atomic_write(
                    self.root / MANIFEST_NAME,
                    json.dumps(new_manifest, ensure_ascii=False, indent=1).encode("utf-8"),
                )
            except OSError as exc:
                for name in files.values():
                    self._delete_layer_file(name)
                raise StoreError(f"切换清单失败，旧数据未受影响：{exc}") from exc
            old = self._manifest
            self._manifest = new_manifest
            if old:
                for info in old["layers"].values():
                    self._delete_layer_file(info["file"])
            state = self.state()
            state["message"] = f"导入成功：{len(layers)} 层，盘大小 {disk_size} 字节"
            return state

    @staticmethod
    def _validate_import(payload) -> tuple[int, dict]:
        if not isinstance(payload, dict):
            raise StoreError("清单必须是 JSON 对象")
        disk_size = payload.get("disk_size")
        if not isinstance(disk_size, int) or isinstance(disk_size, bool):
            raise StoreError("disk_size 必须是整数（字节）")
        if disk_size <= 0 or disk_size > MAX_DISK_SIZE:
            raise StoreError(f"disk_size 必须在 1..{MAX_DISK_SIZE}（16 MiB）字节之间")
        if disk_size % BLOCK_SIZE != 0:
            raise StoreError(f"disk_size 必须是 {BLOCK_SIZE} 的整数倍")
        max_blocks = disk_size // BLOCK_SIZE

        raw_layers = payload.get("layers")
        if not isinstance(raw_layers, list) or not raw_layers:
            raise StoreError("layers 必须是非空数组")

        layers: dict[str, dict] = {}
        for item in raw_layers:
            if not isinstance(item, dict):
                raise StoreError("layers 数组元素必须是对象")
            lid = item.get("id")
            if not isinstance(lid, str) or not _LAYER_ID_RE.fullmatch(lid):
                raise StoreError(f"非法层 id：{lid!r}（允许字母数字及 _.-，1-64 字符）")
            if lid in layers:
                raise StoreError(f"层 id 重复：{lid}")
            parent = item.get("parent")
            if parent is not None and not isinstance(parent, str):
                raise StoreError(f"层 {lid} 的 parent 必须是字符串或 null")
            raw_blocks = item.get("blocks")
            if not isinstance(raw_blocks, dict):
                raise StoreError(f"层 {lid} 的 blocks 必须是对象（索引 -> base64）")
            blocks: dict[int, bytes] = {}
            for key, value in raw_blocks.items():
                if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key:
                    raise StoreError(f"层 {lid} 的块索引非法：{key!r}")
                idx = int(key)
                if idx >= max_blocks:
                    raise StoreError(
                        f"层 {lid} 的块索引 {idx} 超出范围（盘共 {max_blocks} 块）"
                    )
                if not isinstance(value, str):
                    raise StoreError(f"层 {lid} 块 {idx} 的数据必须是 base64 字符串")
                try:
                    data = base64.b64decode(value, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise StoreError(f"层 {lid} 块 {idx} 的 base64 无法解码") from exc
                if len(data) != BLOCK_SIZE:
                    raise StoreError(
                        f"层 {lid} 块 {idx} 解码后为 {len(data)} 字节，"
                        f"必须为 {BLOCK_SIZE} 字节"
                    )
                blocks[idx] = data
            layers[lid] = {"parent": parent, "blocks": blocks}

        for lid, layer in layers.items():
            parent = layer["parent"]
            if parent is not None and parent not in layers:
                raise StoreError(f"层 {lid} 的父层 {parent} 不存在")
        for lid in layers:  # 环检测
            seen = {lid}
            cur = lid
            while layers[cur]["parent"] is not None:
                cur = layers[cur]["parent"]
                if cur in seen:
                    raise StoreError(f"检测到父子关系循环（经过层 {cur}）")
                seen.add(cur)
        return disk_size, layers

    # ------------------------------------------------------------------ #
    # 撤除（删除中间快照，只重写直接子层）
    # ------------------------------------------------------------------ #
    def remove(self, layer_id: str) -> dict:
        with self._lock:
            # 拦截并处理上一轮遗留的在途撤除日志，保证同一时刻只有一个在途操作
            self._reconcile_pending_journal()
            plan = self._prepare_remove(layer_id)
            try:
                self._commit_remove(plan)
            except OSError as exc:
                self._rollback_remove(plan)
                raise StoreError(f"切换清单失败，已回滚，旧链仍可用：{exc}") from exc
            self._finalize_remove(plan)
            state = self.state()
            state["message"] = (
                f"已撤除 {layer_id}，重写直接子层：{', '.join(plan['children'])}"
            )
            return state

    def _prepare_remove(self, layer_id: str) -> dict:
        """第 1-2 步：合并写出新子层文件 + 写 journal。旧链完全不动。"""
        manifest = self._require_manifest()
        layers = manifest["layers"]
        if layer_id not in layers:
            raise NotFoundError(f"快照 {layer_id} 不存在")
        info = layers[layer_id]
        if info["parent"] is None:
            raise StoreError("基盘（根层）不能撤除")
        children = sorted(
            lid for lid, x in layers.items() if x["parent"] == layer_id
        )
        if not children:
            raise StoreError(f"快照 {layer_id} 没有子层，不属于可撤除对象")

        target_blocks = self._read_layer_file(info["file"])
        new_files: dict[str, str] = {}
        merged_counts: dict[str, int] = {}
        try:
            for cid in children:
                child_blocks = self._read_layer_file(layers[cid]["file"])
                # 子层已有的块（含全零块）保持子层版本；缺块才从被撤除层继承
                merged = {**target_blocks, **child_blocks}
                new_files[cid] = self._write_layer_file(merged)
                merged_counts[cid] = len(merged)
        except OSError as exc:
            for name in new_files.values():
                self._delete_layer_file(name)
            raise StoreError(f"写入新子层文件失败，旧链未受影响：{exc}") from exc

        new_layers = {
            lid: dict(meta) for lid, meta in layers.items() if lid != layer_id
        }
        for cid in children:
            new_layers[cid]["parent"] = info["parent"]
            new_layers[cid]["file"] = new_files[cid]
            new_layers[cid]["blocks"] = merged_counts[cid]
        new_manifest = {
            "epoch": uuid.uuid4().hex,
            "disk_size": manifest["disk_size"],
            "layers": new_layers,
        }
        plan = {
            "op": "remove",
            "target": layer_id,
            "children": children,
            "old_epoch": manifest["epoch"],
            "new_epoch": new_manifest["epoch"],
            "new_files": list(new_files.values()),
            "obsolete_files": [info["file"]] + [layers[c]["file"] for c in children],
            "new_manifest": new_manifest,
        }
        try:
            _atomic_write(
                self.root / JOURNAL_NAME,
                json.dumps(plan, ensure_ascii=False).encode("utf-8"),
            )
        except OSError as exc:
            for name in new_files.values():
                self._delete_layer_file(name)
            raise StoreError(f"写入 journal 失败，旧链未受影响：{exc}") from exc
        return plan

    def _commit_remove(self, plan: dict) -> None:
        """第 3 步：原子替换 manifest —— 唯一的提交点。"""
        _atomic_write(
            self.root / MANIFEST_NAME,
            json.dumps(plan["new_manifest"], ensure_ascii=False, indent=1).encode("utf-8"),
        )
        self._manifest = plan["new_manifest"]

    def _finalize_remove(self, plan: dict) -> None:
        """第 4 步：收尾清理。失败不影响一致性，留给启动恢复。"""
        for name in plan["obsolete_files"]:
            self._delete_layer_file(name)
        try:
            (self.root / JOURNAL_NAME).unlink(missing_ok=True)
        except OSError:
            pass

    def _rollback_remove(self, plan: dict) -> None:
        for name in plan["new_files"]:
            self._delete_layer_file(name)
        try:
            (self.root / JOURNAL_NAME).unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 查询 / 下载
    # ------------------------------------------------------------------ #
    def _require_manifest(self) -> dict:
        if self._manifest is None:
            raise StoreError("尚未导入备份清单")
        return self._manifest

    def resolve_image(self, layer_id: str) -> bytes:
        """沿父链合并稀疏块，还原整盘原始字节（缺块为全零）。"""
        with self._lock:
            manifest = self._require_manifest()
            layers = manifest["layers"]
            if layer_id not in layers:
                raise NotFoundError(f"快照 {layer_id} 不存在")
            chain = []
            cur = layer_id
            while cur is not None:
                chain.append(layers[cur]["file"])
                cur = layers[cur]["parent"]
            merged: dict[int, bytes] = {}
            for name in reversed(chain):  # 根 -> 目标，逐层覆盖
                merged.update(self._read_layer_file(name))
            buf = bytearray(manifest["disk_size"])
            for idx, data in merged.items():
                buf[idx * BLOCK_SIZE:(idx + 1) * BLOCK_SIZE] = data
            return bytes(buf)

    def state(self) -> dict:
        with self._lock:
            base = {
                "block_size": BLOCK_SIZE,
                "max_disk_size": MAX_DISK_SIZE,
                "recovery_events": list(self.recovery_events),
            }
            if self._manifest is None:
                return {**base, "initialized": False, "disk_size": None,
                        "epoch": None, "layers": []}
            layers = self._manifest["layers"]
            children = {lid: [] for lid in layers}
            for lid, meta in layers.items():
                if meta["parent"] is not None:
                    children[meta["parent"]].append(lid)
            listing = [
                {
                    "id": lid,
                    "parent": meta["parent"],
                    "children": sorted(children[lid]),
                    "block_count": meta["blocks"],
                    "can_remove": meta["parent"] is not None and bool(children[lid]),
                }
                for lid, meta in sorted(layers.items())
            ]
            return {
                **base,
                "initialized": True,
                "disk_size": self._manifest["disk_size"],
                "epoch": self._manifest["epoch"],
                "layers": listing,
            }
