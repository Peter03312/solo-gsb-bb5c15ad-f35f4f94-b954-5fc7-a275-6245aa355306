#!/usr/bin/env bash
# 考古地层关系核验服务 —— 历史定版关系查询“封存明细不混时态”端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 演示场景：
#   定版后撤回某条支撑证据（正式批次撤回 / 野外包勘误替换 / 推演并入撤回同理）：
#   - 历史定版关系查询 GET /snapshots/{version}/relationship 的关系结论、
#     evidence_path 与逐步证据明细 path_detail 全部按**封存时断言**返回，
#     逐步 status 恒为 active，与同一定版封存的成果包证据明细一致；
#   - 当前正式关系查询 GET /relationship 继续如实显示现时状态（证据 retracted、
#     关系可能变为未定）；
#   - 任何查询只读，不写断言或修订；未知定版 404、未知地层 400 约定不变。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
TAG="${SNAPSHOT_DEMO_TAG:-$(( $(date +%s) % 100000 ))}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

# 本轮唯一前缀，便于重复执行（证据编号永久唯一）
P="SD${TAG}"
S1="SNAP-${TAG}-1"; S2="SNAP-${TAG}-2"; S3="SNAP-${TAG}-3"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 =="
for s in "$S1" "$S2" "$S3"; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" | pp
done

echo; echo "== 2. 批次 1：${S1}~${S2} 同一期（${P}-E1），${S2}<${S3} 早于（${P}-E2） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": 0,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"${P}-E1\", \"stratum_a\": \"${S1}\", \"stratum_b\": \"${S2}\", \"relation\": \"same_period\"},
    {\"op\": \"assert\", \"evidence_id\": \"${P}-E2\", \"stratum_a\": \"${S2}\", \"stratum_b\": \"${S3}\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 3. 创建定版（记录返回的 version；本例取最新定版号） =="
curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
  -d "{\"label\": \"定版明细封存演示 ${TAG}\"}" | pp
VERSION="$(curl -s "$BASE/snapshots" | python3 -c 'import sys,json; print(json.load(sys.stdin)[-1]["version"])')"
echo "本轮定版 version=${VERSION}"

echo; echo "== 4. 按同一定版封存成果包（${S1}/${S3}），作为明细一致性对照 =="
curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d "{\"version\": ${VERSION}, \"strata\": [\"${S1}\", \"${S3}\"]}" | pp

echo; echo "== 5. 定版查询（撤回前）：${S1} 早于 ${S3}，路径 [E1, E2]，逐步 status 均为 active =="
curl -s "$BASE/snapshots/${VERSION}/relationship?a=${S1}&b=${S3}" | pp

echo; echo "== 6. 后续正式批次撤回支撑证据 ${P}-E2（修订前进） =="
REV="$(curl -s "$BASE/revision" | python3 -c 'import sys,json; print(json.load(sys.stdin)["revision"])')"
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": ${REV},
  \"operations\": [
    {\"op\": \"retract\", \"evidence_id\": \"${P}-E2\"}
  ]}" | pp

echo; echo "== 7. 再查历史定版：结论 / evidence_path / path_detail 与封存时完全一致（E2 仍 active） =="
curl -s "$BASE/snapshots/${VERSION}/relationship?a=${S1}&b=${S3}" | pp

echo; echo "== 8. 当前正式关系查询：如实显示现时状态（${S1} 与 ${S3} 已未定，路径为空） =="
curl -s "$BASE/relationship?a=${S1}&b=${S3}" | pp

echo; echo "== 9. 在线断言台账：${P}-E2 现时为 retracted（对比定版明细仍为 active） =="
curl -s "$BASE/assertions?status=retracted" | python3 -c "
import sys, json
want = '${P}-E2'
rows = [r for r in json.load(sys.stdin) if r['evidence_id'] == want]
for r in rows: print(json.dumps(r, ensure_ascii=False, indent=2))
"

echo; echo "== 10. 同一定版成果包内容保持冻结（证据明细 status 同样为 active） =="
PKG="$(curl -s "$BASE/packages" | python3 -c 'import sys,json; print(json.load(sys.stdin)[-1]["id"])')"
curl -s "$BASE/packages/${PKG}" | pp

echo; echo "== 11. 错误约定不变：未知定版 404 SNAPSHOT_NOT_FOUND / 未知地层 400 INVALID_STRATUM =="
echo "-- 未知定版 --"
curl -s -i "$BASE/snapshots/999999/relationship?a=${S1}&b=${S3}" | head -1
curl -s "$BASE/snapshots/999999/relationship?a=${S1}&b=${S3}" | pp
echo "-- 未知地层 --"
curl -s -i "$BASE/snapshots/${VERSION}/relationship?a=${S1}&b=NO-SUCH-STRATUM" | head -1
curl -s "$BASE/snapshots/${VERSION}/relationship?a=${S1}&b=NO-SUCH-STRATUM" | pp

echo; echo "全部示例执行完毕。"
