#!/usr/bin/env bash
# 维护台演示脚本：导入 -> 下载 -> 撤除 -> 再下载 -> 再导入 -> 重启一致性。
# 仅依赖 curl；默认对接 http://localhost:8000（docker compose up 后的地址）。
#
# 用法：
#   docker compose up --build -d
#   ./scripts/demo.sh                 # 全流程演示
#   BASE_URL=http://127.0.0.1:8000 ./scripts/demo.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

wait_ready() {
    for _ in $(seq 1 30); do
        if curl -fsS "$BASE_URL/api/state" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    fail "服务在 30s 内未就绪：$BASE_URL"
}

say "0. 等待服务就绪"
wait_ready
curl -fsS "$BASE_URL/api/state" | python3 -m json.tool

say "1. 载入示例清单并导入"
curl -fsS "$BASE_URL/api/sample" -o "$WORK/sample.json"
curl -fsS -X POST "$BASE_URL/api/import" \
    -H 'Content-Type: application/json' --data @"$WORK/sample.json" \
    | python3 -c 'import sys,json; st=json.load(sys.stdin); print(st.get("message", st))'

say "2. 撤除前下载全部快照镜像（留底）"
for id in base snap-1 snap-2 snap-3 snap-4; do
    curl -fsS "$BASE_URL/api/download/$id" -o "$WORK/$id.before.raw"
done
ls -l "$WORK"/*.before.raw

say "3. 撤除 snap-1（非根、有直接子层 snap-2/snap-3）"
curl -fsS -X POST "$BASE_URL/api/remove/snap-1" \
    | python3 -c 'import sys,json; st=json.load(sys.stdin); print(st.get("message", st))'

say "4. 撤除后重新下载，逐字节比对（保留恢复点字节必须不变）"
for id in base snap-2 snap-3 snap-4; do
    curl -fsS "$BASE_URL/api/download/$id" -o "$WORK/$id.after.raw"
    if cmp -s "$WORK/$id.before.raw" "$WORK/$id.after.raw"; then
        echo "  $id: 逐字节一致 ✅"
    else
        fail "  $id: 撤除前后镜像不一致"
    fi
done

say "5. 导入第二份备份（整体替换）并下载新快照镜像"
python3 - "$WORK/vol2.json" <<'PY'
import base64, json, sys
m = {"disk_size": 8192, "layers": [
    {"id": "vol2-base", "parent": None,
     "blocks": {"0": base64.b64encode(b"X" * 4096).decode()}},
    {"id": "vol2-snap", "parent": "vol2-base",
     "blocks": {"1": base64.b64encode(b"Y" * 4096).decode()}},
]}
json.dump(m, open(sys.argv[1], "w"))
PY
curl -fsS -X POST "$BASE_URL/api/import" \
    -H 'Content-Type: application/json' --data @"$WORK/vol2.json" \
    | python3 -c 'import sys,json; st=json.load(sys.stdin); print(st.get("message", st))'
curl -fsS "$BASE_URL/api/download/vol2-snap" -o "$WORK/vol2-snap.pre.raw"
sha256sum "$WORK/vol2-snap.pre.raw"

say "6. 重启服务（docker compose restart web）"
if docker compose restart web >/dev/null 2>&1; then
    :
else
    echo "  （当前环境无 docker compose；若在本机用 uvicorn 演示，请手动重启进程）"
fi
wait_ready
curl -fsS "$BASE_URL/api/state" | python3 -m json.tool

say "7. 重启后再次下载，必须与重启前逐字节一致，且显示新导入的快照"
curl -fsS "$BASE_URL/api/download/vol2-snap" -o "$WORK/vol2-snap.post.raw"
cmp "$WORK/vol2-snap.pre.raw" "$WORK/vol2-snap.post.raw" \
    && echo "  vol2-snap: 重启前后逐字节一致 ✅"
curl -fsS "$BASE_URL/api/state" \
    | python3 -c 'import sys,json; st=json.load(sys.stdin); assert [l["id"] for l in st["layers"]] == ["vol2-base","vol2-snap"], st; print("  重启后显示新导入快照 ✅")'

say "演示完成：打开 $BASE_URL/ 可在维护页重复以上导入/撤除/下载操作。"
