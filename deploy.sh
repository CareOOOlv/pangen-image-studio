#!/usr/bin/env bash
# Pangen 生图工作台 · 一键部署
#
# 为什么是这个写法（踩过的坑，别改回去）：
#   1. 必须用 --source 让云托管自己构建。构建产物落在 CCR
#      （ccr.ccs.tencentyun.com），云托管才拉得动。
#      用 ghcr.io 的镜像会卡在「等待pod启动就绪」→ 超时 →
#      DescribeVersion_internal_error_，那个报错是次生的，别被它骗。
#   2. `tcb cloudrun deploy` 没有 --envParams。环境变量走服务级配置
#      （tcb api tcbr UpdateCloudRunServerConfig），deploy 时会 merge 进来，
#      所以平时不用传，也不会被覆盖。
#   3. CLI 有两个交互提问（灰度发布 / 有任务在跑要不要继续），
#      用管道喂回车，别改成 --force 就以为能跳过，跳不过。
#
# 用法：
#   ./deploy.sh              # 部署当前目录
#   ./deploy.sh --wait       # 部署完等流量切过去并验证 health
#
set -uo pipefail

ENV_ID="${TCB_ENV_ID:-careooolv-d8gnyhzsnfe9e7356}"
SERVICE="${TCB_SERVICE:-pangen-image-studio}"
PORT="${TCB_PORT:-9000}"
WAIT=0

for a in "$@"; do
  case "$a" in
    --wait) WAIT=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done

command -v tcb >/dev/null || { echo "找不到 tcb CLI" >&2; exit 1; }
[ -f Dockerfile ] || { echo "当前目录没有 Dockerfile，别在这儿跑" >&2; exit 1; }

online_version() {
  tcb cloudrun detail -e "$ENV_ID" -s "$SERVICE" --json 2>/dev/null \
    | sed -n 's/.*"VersionName": "\([^"]*\)".*/\1/p' | head -1
}

echo "==> 目标环境 $ENV_ID · 服务 $SERVICE · 端口 $PORT"

PREV_VER="$(online_version)"
[ -n "$PREV_VER" ] && echo "==> 当前线上版本 $PREV_VER"

# 环境变量体检：这四项缺了不会报错，但会静默出问题
#   PANGEN_API_KEY  缺了 → 所有出图 401
#   PANGEN_CODES    缺了 → 朋友口令会被当成 owner，无限量且不可见
#   STORAGE_DIR     缺了 → 落到镜像内路径，容器重建即丢
#   PANGEN_CHAT_MODEL 缺了 → 用默认模型，行为可能变
# deploy 只会 merge 服务级 EnvParams，所以这里查的是服务级配置。
echo "==> 检查服务级环境变量"
detail_env="$(tcb cloudrun detail -e "$ENV_ID" -s "$SERVICE" --json 2>/dev/null)"
missing=""
for k in PANGEN_API_KEY PANGEN_CODES STORAGE_DIR PANGEN_CHAT_MODEL; do
  echo "$detail_env" | grep -q "$k" || missing="$missing $k"
done
if [ -n "$missing" ]; then
  echo "!! 服务级配置缺:$missing" >&2
  echo "   补法：tcb api tcbr UpdateCloudRunServerConfig --api-version 2022-02-17 --body '<JSON>'" >&2
  echo "   改完必须再 deploy 一次才生效。" >&2
  exit 1
fi
if echo "$detail_env" | grep -q 'PANGEN_PROXY'; then
  echo "!! 服务级配置里有 PANGEN_PROXY —— 云端机房直连 Cloudflare 路径短，设了反而会挂" >&2
  exit 1
fi
echo "    ✔ 齐全，且没有 PANGEN_PROXY"

echo "==> 源码构建部署（产物走 CCR，不走 ghcr）"

# 交互应答：第 1 个回车 = 灰度发布选 No；y = 有任务在跑时确认继续
(
  printf '\n'; sleep 3
  printf 'y\n'; sleep 2
  printf '\n\n\n'
) | tcb cloudrun deploy -e "$ENV_ID" -s "$SERVICE" --source . --port "$PORT" --force
rc=$?

if [ $rc -ne 0 ]; then
  echo "!! 提交失败（exit $rc）" >&2
  echo "   如果是 create_failed，先看：tcb cloudrun record list -e $ENV_ID -s $SERVICE" >&2
  echo "   再看运行日志：tcb cloudrun logs process -e $ENV_ID --run-id <RunId>" >&2
  exit $rc
fi

[ "$WAIT" -eq 1 ] || { echo "==> 已提交。加 --wait 可等流量切换并验证 health"; exit 0; }

echo "==> 等待新版本接管流量…"
DOMAIN=""
for i in $(seq 1 40); do
  sleep 15
  cur_ver="$(online_version)"
  echo "    第 ${i} 次检查… 线上版本=${cur_ver:-未知}"

  if [ -n "$cur_ver" ] && [ "$cur_ver" != "$PREV_VER" ]; then
    # 版本换了，再确认它拿到 100% 流量
    if tcb cloudrun detail -e "$ENV_ID" -s "$SERVICE" --json 2>/dev/null \
         | grep -q '"FlowRatio": "100"'; then
      DOMAIN=$(tcb cloudrun detail -e "$ENV_ID" -s "$SERVICE" --json 2>/dev/null \
               | sed -n 's/.*"DefaultDomainName": "\([^"]*\)".*/\1/p' | head -1)
      echo "==> 流量已切到 $cur_ver"
      break
    fi
  fi

  # 早点失败：最新一条部署记录已 create_failed
  if tcb cloudrun record list -e "$ENV_ID" -s "$SERVICE" --json 2>/dev/null \
       | grep -q '"Status": "create_failed"'; then
    top_status=$(tcb cloudrun record list -e "$ENV_ID" -s "$SERVICE" --json 2>/dev/null \
                 | sed -n 's/.*"Status": "\([^"]*\)".*/\1/p' | head -1)
    if [ "$top_status" = "create_failed" ]; then
      echo "!! 部署 create_failed，别再等了" >&2
      echo "   看日志：tcb cloudrun logs process -e $ENV_ID --run-id <RunId>" >&2
      echo "   常见原因：镜像来自 ghcr.io（拉不动）。请确认本脚本走的是 --source。" >&2
      exit 1
    fi
  fi
done

if [ -z "$DOMAIN" ]; then
  echo "!! 等了 10 分钟还没切流量，去看控制台或 record list" >&2
  exit 1
fi

echo "==> 验证 health：$DOMAIN/api/health"
if curl -fsS --max-time 60 "$DOMAIN/api/health"; then
  echo
  echo "==> 部署完成：$DOMAIN/chat"
else
  echo "!! health 不通，检查容器日志" >&2
  exit 1
fi
