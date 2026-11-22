#!/usr/bin/env bash
# Gemini5.0 Proxy 一键部署（Ubuntu 22.04 / 24.04，需要 root）
#
# 在仓库内：  sudo bash deploy/install.sh [选项]
# 一行安装：  curl -fsSL <raw 地址>/deploy/install.sh | sudo bash -s -- [选项]
set -Eeuo pipefail
umask 022

APP_NAME="gemini-pro"
APP_USER="geminipro"
APP_DIR="/opt/gemini-pro"
ENV_DIR="/etc/gemini-pro"
ENV_FILE="${ENV_DIR}/proxy.env"
STATE_ROOT="/var/lib/gemini-pro"
UNIT_FILE="/etc/systemd/system/${APP_NAME}.service"
DROPIN_DIR="/etc/systemd/system/${APP_NAME}.service.d"
CADDYFILE="/etc/caddy/Caddyfile"
CADDY_BEGIN="# BEGIN gemini-pro"
CADDY_END="# END gemini-pro"
SAVED_CONF="${ENV_DIR}/install.conf"
DEFAULT_REPO="https://github.com/hammer233/gemini-pro.git"

REPO_URL="${REPO_URL:-}"
BRANCH="${BRANCH:-main}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
PORT="${PORT:-8888}"
DOMAIN="${DOMAIN:-}"
API_KEY="${API_KEY:-}"
YES_KEY="${YES_KEY:-}"
EGRESS_PROXY="${EGRESS_PROXY:-}"
BACKEND_MODE="browser"
PUBLIC=0
SWAP=1
SELFTEST=0
ACTION="install"
PURGE=0
LOCAL_SRC=""
WORK=""
GENERATED_KEY=0

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_BLD=""; C_OFF=""
fi

info() { printf '%s[+]%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

cleanup() {
  local rc=$?
  if [ -n "$WORK" ] && [ -d "$WORK" ]; then rm -rf "$WORK"; fi
  exit "$rc"
}
trap cleanup EXIT
trap 'warn "脚本在第 ${LINENO} 行失败"' ERR

usage() {
  cat <<'EOF'
用法: sudo bash install.sh [选项]

  --domain 域名        用 Caddy 反代并自动申请 HTTPS 证书（域名需已解析到本机，开放 80/443）
  --public             不用域名，直接对外监听 0.0.0.0:端口（明文 HTTP，仅在可信网络使用）
  --port N             内部服务端口，默认 8888
  --api-key KEY        管理员/调用密钥；不填则自动生成并保存
  --yes-key KEY        YesCaptcha 客户端密钥；不填会在交互终端中询问
  --egress-proxy URL   服务出网代理，如 http://127.0.0.1:7897
  --mock               使用 mock 后端，只做链路冒烟，返回的是模拟文本而不是真实回答
  --repo URL           源码仓库 https 地址（默认取脚本所在仓库，其次 DEFAULT_REPO）
  --branch NAME        分支，默认 main
  --no-swap            内存小于 3GB 时也不自动创建 2GB swap
  --selftest           装完后真实请求一次（会消耗打码额度）
  --update             只更新代码与依赖并重启，保留现有配置
  --uninstall          卸载服务与代码（保留配置和数据）
  --purge              与 --uninstall 连用：连配置、数据、服务用户一并删除
  -h, --help           显示帮助

环境变量等价于同名参数: DOMAIN PORT API_KEY YES_KEY EGRESS_PROXY REPO_URL BRANCH
私有仓库请用环境变量 GITHUB_TOKEN 传入只读令牌（不要写在命令行里）。
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --domain) DOMAIN="${2:?缺少域名}"; shift 2 ;;
      --public) PUBLIC=1; shift ;;
      --port) PORT="${2:?缺少端口}"; shift 2 ;;
      --api-key) API_KEY="${2:?缺少密钥}"; shift 2 ;;
      --yes-key) YES_KEY="${2:?缺少密钥}"; shift 2 ;;
      --egress-proxy) EGRESS_PROXY="${2:?缺少代理地址}"; shift 2 ;;
      --mock) BACKEND_MODE="mock"; shift ;;
      --repo) REPO_URL="${2:?缺少仓库地址}"; shift 2 ;;
      --branch) BRANCH="${2:?缺少分支名}"; shift 2 ;;
      --no-swap) SWAP=0; shift ;;
      --selftest) SELFTEST=1; shift ;;
      --update) ACTION="update"; shift ;;
      --uninstall) ACTION="uninstall"; shift ;;
      --purge) PURGE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "未知参数: $1" ;;
    esac
  done
}

validate_inputs() {
  local domain_re='^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$'
  local key_re='^[A-Za-z0-9._~+=-]{16,}$'
  local token_re='^[A-Za-z0-9._~+=-]+$'
  local proxy_re='^[A-Za-z0-9+.-]+://[A-Za-z0-9._~:@%/!*+=,-]+$'
  local branch_re='^[A-Za-z0-9._/-]+$'
  local port_re='^[0-9]+$'

  [[ "$PORT" =~ $port_re ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口不合法: $PORT"
  if [ -n "$DOMAIN" ]; then
    [[ "$DOMAIN" =~ $domain_re ]] || die "域名不合法: $DOMAIN"
    [ "$PUBLIC" -eq 0 ] || die "--domain 与 --public 不能同时使用"
  fi
  if [ -n "$API_KEY" ]; then
    [[ "$API_KEY" =~ $key_re ]] || die "API_KEY 只能含字母数字和 . _ ~ + = -，且不少于 16 位"
  fi
  if [ -n "$YES_KEY" ]; then
    [[ "$YES_KEY" =~ $token_re ]] || die "YES_KEY 含有不允许的字符"
  fi
  if [ -n "$EGRESS_PROXY" ]; then
    [[ "$EGRESS_PROXY" =~ $proxy_re ]] || die "EGRESS_PROXY 格式不合法（只允许常见 URL 字符）"
  fi
  [[ "$BRANCH" =~ $branch_re ]] || die "分支名不合法: $BRANCH"
}

need_root() {
  [ "$(id -u)" -eq 0 ] || die "请用 root 运行: sudo bash install.sh"
  [ -d /run/systemd/system ] || die "未检测到 systemd，本脚本不支持当前环境（如 WSL1/容器）"
}

detect_system() {
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_ID="${ID:-unknown}"
  OS_VER="${VERSION_ID:-0}"
  OS_NAME="${PRETTY_NAME:-$OS_ID}"
  if [ "$OS_ID" != "ubuntu" ]; then
    warn "仅在 Ubuntu 22.04/24.04 上验证过，当前为 ${OS_NAME}"
  elif [ "$OS_VER" != "22.04" ] && [ "$OS_VER" != "24.04" ]; then
    warn "Ubuntu ${OS_VER} 未经验证，推荐 22.04 / 24.04"
  fi
  ARCH="$(uname -m)"
  TOTAL_MB="$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)"
  SWAP_MB="$(awk '/^SwapTotal:/ {print int($2/1024)}' /proc/meminfo)"
  CPUS="$(nproc)"
  DISK_FREE_MB="$(df -Pm / | awk 'NR==2 {print $4}')"
  info "系统 ${OS_NAME} / ${ARCH} / ${CPUS} 核 / 内存 ${TOTAL_MB}MB / swap ${SWAP_MB}MB / 磁盘剩余 ${DISK_FREE_MB}MB"
  if [ "$TOTAL_MB" -lt 1500 ]; then
    warn "内存低于 1.5GB，Chrome 很容易被 OOM，建议至少 2GB（已自动限制并发）"
  fi
  [ "$DISK_FREE_MB" -ge 3000 ] || die "磁盘剩余不足 3GB，无法安装 Chrome 与依赖"
}

locate_source() {
  local self="${BASH_SOURCE[0]:-}" cand=""
  if [ -z "$REPO_URL" ] && [ -n "$self" ] && [ -f "$self" ]; then
    cand="$(cd "$(dirname "$self")/.." && pwd)"
    if [ -f "$cand/proxy_svc/app.py" ] && [ "$cand" != "$APP_DIR" ]; then
      LOCAL_SRC="$cand"
    fi
  fi
  if [ -z "$LOCAL_SRC" ] && [ -z "$REPO_URL" ] && [ -f "$SAVED_CONF" ]; then
    REPO_URL="$(sed -n 's/^REPO_URL=//p' "$SAVED_CONF" | tail -n 1)"
  fi
  if [ -z "$LOCAL_SRC" ] && [ -z "$REPO_URL" ]; then
    REPO_URL="$DEFAULT_REPO"
  fi
  if [ -z "$LOCAL_SRC" ]; then
    [[ "$REPO_URL" == https://* ]] || die "仓库地址必须以 https:// 开头"
  fi
}

apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends "$@"
}

apt_update() {
  export DEBIAN_FRONTEND=noninteractive
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if apt-get -o DPkg::Lock::Timeout=300 update -y; then return 0; fi
    warn "apt update 失败（可能系统正在做初始化），10 秒后重试 (${attempt}/6)"
    sleep 10
  done
  die "apt update 反复失败，请检查网络与软件源"
}

install_packages() {
  info "安装系统依赖"
  apt_update
  apt_install ca-certificates curl git gnupg openssl python3 python3-venv python3-pip libatomic1 xz-utils
}

ensure_swap() {
  [ "$SWAP" -eq 1 ] || return 0
  [ "$TOTAL_MB" -lt 3000 ] || return 0
  [ "$SWAP_MB" -eq 0 ] || return 0
  if [ -e /swapfile ]; then warn "/swapfile 已存在，跳过创建 swap"; return 0; fi
  if [ "$DISK_FREE_MB" -lt 6000 ]; then warn "磁盘剩余不足 6GB，跳过创建 swap"; return 0; fi
  info "内存不足 3GB 且没有 swap，创建 2GB /swapfile"
  dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
}

ensure_user() {
  if ! id -u "$APP_USER" >/dev/null 2>&1; then
    info "创建服务用户 ${APP_USER}"
    useradd --system --user-group --home-dir "$STATE_ROOT" --shell /usr/sbin/nologin "$APP_USER"
  fi
  install -d -o "$APP_USER" -g "$APP_USER" -m 0700 \
    "$STATE_ROOT" "$STATE_ROOT/data" "$STATE_ROOT/states" "$STATE_ROOT/home"
}

fetch_source() {
  local src="$LOCAL_SRC"
  if [ -z "$src" ]; then
    info "拉取源码 ${REPO_URL} (${BRANCH})"
    WORK="$(mktemp -d)"
    local -a gitcfg=()
    if [ -n "$GITHUB_TOKEN" ]; then
      gitcfg=(-c "http.extraHeader=Authorization: Basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 -w0)")
    fi
    GIT_TERMINAL_PROMPT=0 git "${gitcfg[@]}" clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$WORK/src" \
      || die "克隆失败。私有仓库请先 export GITHUB_TOKEN=<只读令牌>"
    src="$WORK/src"
  else
    info "使用本地源码 ${src}"
  fi
  [ -f "$src/proxy_svc/app.py" ] && [ -f "$src/requirements.txt" ] || die "源码目录不完整: $src"
  install -d -m 0755 "$APP_DIR"
  rm -rf "$APP_DIR/proxy_svc" "$APP_DIR/deploy" "$APP_DIR/requirements.txt"
  cp -a "$src/proxy_svc" "$src/deploy" "$src/requirements.txt" "$APP_DIR/"
  find "$APP_DIR/proxy_svc" -name '__pycache__' -type d -prune -exec rm -rf {} +
  chown -R root:root "$APP_DIR/proxy_svc" "$APP_DIR/deploy" "$APP_DIR/requirements.txt"
  chmod -R go-w,a+rX "$APP_DIR/proxy_svc" "$APP_DIR/deploy"
  if [ -z "$LOCAL_SRC" ]; then
    install -d -m 0750 -o root -g root "$ENV_DIR"
    printf 'REPO_URL=%s\n' "$REPO_URL" > "$SAVED_CONF"
    chmod 0600 "$SAVED_CONF"
  fi
}

setup_venv() {
  info "准备 Python 虚拟环境并安装依赖"
  [ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
  "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip wheel
  "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
  chmod -R a+rX "$APP_DIR/venv"
}

install_browser() {
  [ "$BACKEND_MODE" = "browser" ] || return 0
  if [ "$ARCH" = "x86_64" ]; then
    if [ -x /usr/bin/google-chrome-stable ]; then
      info "Chrome 已安装: $(/usr/bin/google-chrome-stable --version)"
      return 0
    fi
    info "安装 Google Chrome"
    local deb
    deb="$(mktemp --suffix=.deb)"
    curl -fsSL --retry 3 -o "$deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    chmod 0644 "$deb"
    apt_install "$deb"
    rm -f "$deb"
    info "Chrome 已安装: $(/usr/bin/google-chrome-stable --version)"
  else
    warn "架构 ${ARCH} 没有 Google Chrome 官方包，改装 Playwright Chromium（未经验证）"
    PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/browsers" "$APP_DIR/venv/bin/python" -m playwright install --with-deps chromium
    chmod -R a+rX "$APP_DIR/browsers"
  fi
}

env_get() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

env_set() {
  local tmp
  tmp="$(mktemp "${ENV_DIR}/.env.XXXXXX")"
  {
    if [ -f "$ENV_FILE" ]; then grep -v "^$1=" "$ENV_FILE" || true; fi
    printf '%s=%s\n' "$1" "$2"
  } > "$tmp"
  chmod 0600 "$tmp"
  chown root:root "$tmp"
  mv -f "$tmp" "$ENV_FILE"
}

env_default() {
  if [ -z "$(env_get "$1")" ]; then env_set "$1" "$2"; fi
}

prompt_yes_key() {
  [ "$BACKEND_MODE" = "browser" ] || return 0
  [ -z "$YES_KEY" ] || return 0
  [ -z "$(env_get YES_KEY)" ] || return 0
  if ( : < /dev/tty ) 2>/dev/null; then
    printf '请输入 YesCaptcha 客户端密钥（直接回车跳过，稍后编辑 %s）: ' "$ENV_FILE" > /dev/tty
    read -rs YES_KEY < /dev/tty || true
    printf '\n' > /dev/tty
    if [ -n "$YES_KEY" ] && ! [[ "$YES_KEY" =~ ^[A-Za-z0-9._~+=-]+$ ]]; then
      warn "输入含有不允许的字符，已忽略"
      YES_KEY=""
    fi
  fi
}

write_env() {
  info "写入配置 ${ENV_FILE}"
  install -d -m 0750 -o root -g root "$ENV_DIR"
  [ -f "$ENV_FILE" ] || { : > "$ENV_FILE"; chmod 0600 "$ENV_FILE"; }

  if [ -n "$API_KEY" ]; then
    env_set API_KEY "$API_KEY"
  elif [ -z "$(env_get API_KEY)" ]; then
    API_KEY="sk-$(openssl rand -hex 24)"
    GENERATED_KEY=1
    env_set API_KEY "$API_KEY"
  else
    API_KEY="$(env_get API_KEY)"
  fi

  prompt_yes_key
  [ -z "$YES_KEY" ] || env_set YES_KEY "$YES_KEY"
  [ -z "$EGRESS_PROXY" ] || env_set EGRESS_PROXY "$EGRESS_PROXY"

  local max_workers=$(( (TOTAL_MB - 600) / 450 ))
  [ "$max_workers" -ge 1 ] || max_workers=1
  [ "$max_workers" -le 6 ] || max_workers=6
  [ "$max_workers" -le $(( CPUS * 3 )) ] || max_workers=$(( CPUS * 3 ))

  env_set BACKEND "$BACKEND_MODE"
  env_set PORT "$PORT"
  if [ -n "$DOMAIN" ] || [ "$PUBLIC" -eq 0 ]; then
    env_set HOST "127.0.0.1"
  else
    env_set HOST "0.0.0.0"
  fi
  if [ -x /usr/bin/google-chrome-stable ]; then
    env_set CHROME_PATH "/usr/bin/google-chrome-stable"
  elif [ -d "$APP_DIR/browsers" ]; then
    env_set PLAYWRIGHT_BROWSERS_PATH "$APP_DIR/browsers"
  fi
  env_set DATA_DIR "$STATE_ROOT/data"
  env_set STATE_DIR "$STATE_ROOT/states"
  env_default MIN_WORKERS 1
  env_default MAX_WORKERS "$max_workers"
  env_default TENANT_ROTATE_IDENTITY false
  env_default HEADLESS true
  env_default BLOCK_ASSETS true
  env_default ANSWER_TIMEOUT 90
  env_default GOTO_TIMEOUT 40
  env_default METRICS_PREVIEW_CHARS 0
  env_default TRUSTED_PROXIES "127.0.0.1,::1"
  env_default LOG_LEVEL INFO
}

write_unit() {
  info "安装 systemd 服务"
  install -m 0644 "$APP_DIR/deploy/${APP_NAME}.service" "$UNIT_FILE"
  install -d -m 0755 "$DROPIN_DIR"
  local high=$(( TOTAL_MB * 75 / 100 )) max=$(( TOTAL_MB * 88 / 100 ))
  [ "$max" -ge 768 ] || max=768
  [ "$high" -lt "$max" ] || high=$(( max - 128 ))
  cat > "$DROPIN_DIR/resources.conf" <<EOF
[Service]
MemoryHigh=${high}M
MemoryMax=${max}M
EOF
  systemctl daemon-reload
}

port_in_use() {
  ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .
}

check_port_free() {
  if port_in_use "$PORT" && ! systemctl is-active --quiet "$APP_NAME"; then
    die "端口 ${PORT} 已被其它程序占用（查看: ss -ltnp | grep :${PORT}），请换一个: --port N"
  fi
}

setup_caddy() {
  [ -n "$DOMAIN" ] || return 0
  info "配置 Caddy 反向代理与 HTTPS (${DOMAIN})"
  if ! command -v caddy >/dev/null 2>&1; then
    if port_in_use 80 || port_in_use 443; then
      die "80/443 端口已被其它服务占用，请改用 --public，或自行把域名反代到 127.0.0.1:${PORT}"
    fi
    apt_install gnupg apt-transport-https
    curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
    apt_update
    apt_install caddy
  fi

  local backup="" tmp
  tmp="$(mktemp)"
  if [ -f "$CADDYFILE" ]; then
    backup="${CADDYFILE}.bak.$(date +%s)"
    cp -a "$CADDYFILE" "$backup"
    awk -v b="$CADDY_BEGIN" -v e="$CADDY_END" '$0==b {skip=1} !skip {print} $0==e {skip=0}' "$CADDYFILE" > "$tmp"
  fi
  cat >> "$tmp" <<EOF

${CADDY_BEGIN}
${DOMAIN} {
	request_body {
		max_size 8MB
	}
	reverse_proxy 127.0.0.1:${PORT} {
		flush_interval -1
	}
}
${CADDY_END}
EOF
  install -d -m 0755 /etc/caddy
  install -m 0644 "$tmp" "$CADDYFILE"
  rm -f "$tmp"
  if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    if [ -n "$backup" ]; then cp -a "$backup" "$CADDYFILE"; else rm -f "$CADDYFILE"; fi
    die "Caddyfile 校验失败，已回滚"
  fi
  systemctl enable caddy >/dev/null 2>&1 || true
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  info "域名解析: $(getent hosts "$DOMAIN" | awk '{print $1}' | head -n 1 || true)（应为本机公网 IP）"
}

setup_firewall() {
  command -v ufw >/dev/null 2>&1 || return 0
  ufw status 2>/dev/null | grep -q '^Status: active' || { info "ufw 未启用，跳过本机防火墙配置"; return 0; }
  if [ -n "$DOMAIN" ]; then
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    info "ufw 已放行 80/443"
  elif [ "$PUBLIC" -eq 1 ]; then
    ufw allow "${PORT}/tcp" >/dev/null
    info "ufw 已放行 ${PORT}/tcp"
  fi
}

start_service() {
  info "启动服务"
  systemctl enable "$APP_NAME" >/dev/null 2>&1
  systemctl restart "$APP_NAME"
}

verify_service() {
  info "等待服务就绪"
  local ready=0 code
  for _ in $(seq 1 60); do
    if systemctl is-failed --quiet "$APP_NAME"; then break; fi
    if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
  done
  if [ "$ready" -ne 1 ]; then
    journalctl -u "$APP_NAME" -n 40 --no-pager >&2 || true
    die "服务未能就绪，请查看上面的日志"
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/v1/models" || true)"
  [ "$code" = "401" ] || die "鉴权自检失败: 不带密钥应返回 401，实际 ${code}"
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${API_KEY}" "http://127.0.0.1:${PORT}/v1/models" || true)"
  [ "$code" = "200" ] || die "鉴权自检失败: 带密钥应返回 200，实际 ${code}"
  info "服务已就绪，鉴权工作正常"
  if [ "$BACKEND_MODE" = "browser" ] && [ -z "$(env_get YES_KEY)" ]; then
    warn "尚未配置 YES_KEY：服务能启动，但遇到验证码时会失败。编辑 ${ENV_FILE} 后执行 systemctl restart ${APP_NAME}"
  fi
}

run_selftest() {
  [ "$SELFTEST" -eq 1 ] || return 0
  info "真实请求自检（可能需要几十秒）"
  curl -sS --max-time 150 "http://127.0.0.1:${PORT}/v1/chat/completions" \
    -H "Authorization: Bearer ${API_KEY}" -H 'Content-Type: application/json' \
    -d '{"model":"Gemini5.0","messages":[{"role":"user","content":"Reply with the single word OK."}]}' \
    | head -c 600 || true
  printf '\n'
}

print_summary() {
  local base key_note="" fw_note=""
  if [ -n "$DOMAIN" ]; then base="https://${DOMAIN}"
  elif [ "$PUBLIC" -eq 1 ]; then base="http://<服务器公网IP>:${PORT}"
  else base="http://127.0.0.1:${PORT}"; fi
  if [ "$GENERATED_KEY" -eq 1 ]; then key_note="  (已自动生成，保存在 ${ENV_FILE})"; fi
  if [ -n "$DOMAIN" ]; then fw_note="TCP 80 与 443"; else fw_note="TCP ${PORT}"; fi
  cat <<EOF

${C_BLD}部署完成${C_OFF}
  API Base URL : ${base}/v1
  模型名       : Gemini5.0
  API Key      : ${API_KEY}${key_note}
  控制台       : ${base}/   （在页面里填入上面的 API Key）

测试:
  curl ${base}/v1/chat/completions -H "Authorization: Bearer ${API_KEY}" \\
    -H "Content-Type: application/json" \\
    -d '{"model":"Gemini5.0","messages":[{"role":"user","content":"你好"}]}'

管理:
  状态  systemctl status ${APP_NAME}
  日志  journalctl -u ${APP_NAME} -f
  配置  ${ENV_FILE}  （改完执行 systemctl restart ${APP_NAME}）
  更新  sudo bash ${APP_DIR}/deploy/install.sh --update
  卸载  sudo bash ${APP_DIR}/deploy/install.sh --uninstall
EOF
  if [ -z "$DOMAIN" ] && [ "$PUBLIC" -eq 0 ]; then
    cat <<EOF

当前只监听本机。从自己电脑访问可用 SSH 隧道:
  ssh -L ${PORT}:127.0.0.1:${PORT} <用户>@<服务器IP>
想直接对外服务，请重跑并加 --domain 你的域名（推荐）或 --public。
EOF
  else
    cat <<EOF

云厂商防火墙（如 AWS Lightsail / 安全组）还需手动放行: ${fw_note}
EOF
  fi
  if [ "$BACKEND_MODE" = "mock" ]; then
    printf '\n%s当前为 mock 后端：返回的是模拟文本，不是真实回答，仅用于链路测试。%s\n' "$C_YEL" "$C_OFF"
  fi
}

do_uninstall() {
  info "停止并移除服务"
  systemctl disable --now "$APP_NAME" 2>/dev/null || true
  rm -f "$UNIT_FILE"
  rm -rf "$DROPIN_DIR" "$APP_DIR"
  systemctl daemon-reload
  if [ -f "$CADDYFILE" ] && grep -qF "$CADDY_BEGIN" "$CADDYFILE"; then
    local tmp
    tmp="$(mktemp)"
    awk -v b="$CADDY_BEGIN" -v e="$CADDY_END" '$0==b {skip=1} !skip {print} $0==e {skip=0}' "$CADDYFILE" > "$tmp"
    install -m 0644 "$tmp" "$CADDYFILE"
    rm -f "$tmp"
    systemctl reload caddy 2>/dev/null || true
    info "已从 Caddyfile 移除本项目的站点块"
  fi
  if [ "$PURGE" -eq 1 ]; then
    rm -rf "$ENV_DIR" "$STATE_ROOT"
    if id -u "$APP_USER" >/dev/null 2>&1; then userdel "$APP_USER" 2>/dev/null || true; fi
    info "配置、数据与服务用户已删除"
  else
    info "已保留 ${ENV_DIR} 与 ${STATE_ROOT}（加 --purge 可一并删除）。Chrome、Caddy、swap 不会被卸载"
  fi
}

main() {
  parse_args "$@"
  need_root
  validate_inputs
  if [ "$ACTION" = "uninstall" ]; then do_uninstall; exit 0; fi
  detect_system
  locate_source

  if [ "$ACTION" = "update" ]; then
    [ -f "$ENV_FILE" ] || die "没有找到 ${ENV_FILE}，请先完整安装"
    API_KEY="$(env_get API_KEY)"
    PORT="$(env_get PORT)"; PORT="${PORT:-8888}"
    BACKEND_MODE="$(env_get BACKEND)"; BACKEND_MODE="${BACKEND_MODE:-browser}"
    ensure_user
    fetch_source
    setup_venv
    write_unit
    start_service
    verify_service
    info "更新完成"
    exit 0
  fi

  check_port_free
  install_packages
  ensure_swap
  ensure_user
  fetch_source
  setup_venv
  install_browser
  write_env
  write_unit
  setup_caddy
  setup_firewall
  start_service
  verify_service
  run_selftest
  print_summary
}

main "$@"
