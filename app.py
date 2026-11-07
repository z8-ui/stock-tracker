"""
应用入口 - 相当于 main() / public static void main

V2.0.1 收尾（2026-11）：
1. 认证在 `create_app()` 里安装，模块导入时立即生效 —— 不再依赖 `if __name__ == "__main__"`。
   因此 `python app.py` / `flask --app app run` / `gunicorn app:app` 走同一套保护。
2. 每个请求再做一次「远程地址 + 认证」检查：即使有人用 `flask run --host 0.0.0.0` 绕过
   启动参数，非回环请求没有有效 Basic 凭据也会被 403 拒绝（fail closed）。
3. 配置为非回环却没有任何认证 token → 直接拒绝启动（import 时抛 RuntimeError，
   命令行下退出码非零），绝不静默降级成“裸奔”。
4. 本机回环（127.0.0.1/::1）免认证：本机用户本来就能读配置、起进程，
   再让浏览器弹 Basic 只是添麻烦；局域网/公网必须认证。

⚠️ HTTP Basic 只做 base64 编码，不是加密：仅用于可信局域网，或放在 HTTPS 反代之后。
   要暴露到公网，请用 Nginx/Caddy 终止 TLS，不要把 5000 端口直接映射出去。
"""

import hmac
import ipaddress
import sys

from flask import Flask, Response, request

from routes import bp
from config import SERVER_HOST, SERVER_PORT, DEBUG, SERVER_AUTH_TOKEN

# Basic 认证用户名固定，密码用 SERVER_AUTH_TOKEN
AUTH_USER = "stock"
AUTH_REALM = "stock-tracker"


def is_loopback(host):
    """判断 host 是否只在本机（127.0.0.1 / ::1 / localhost）。"""
    host = (host or "").strip()
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def resolve_bind_host(host, debug):
    """调试模式下强制只绑回环地址（Werkzeug 调试器 = 任意代码执行）。"""
    if not debug:
        return host, None
    if is_loopback(host):
        return host, None
    return "127.0.0.1", f"DEBUG=True 时不允许绑定 {host}，已改为 127.0.0.1"


def lan_auth_error(host, token):
    """绑定非回环地址却没有认证 → 返回错误文案（None 表示可以启动）。"""
    if is_loopback(host):
        return None
    if token:
        return None
    return (f"拒绝启动：SERVER_HOST={host} 会暴露到局域网，但 SERVER_AUTH_TOKEN 为空。\n"
            f"         要么改回 127.0.0.1，要么设置 SERVER_AUTH_TOKEN（浏览器用 HTTP Basic 登录）。")


def _trusted_loopback(host):
    """严格的回环判定：只有能解析成 IP 且确为回环地址才算「本机」。

    和 is_loopback 的区别（安全相关，别合并）：
      - 空值 / 无法解析 → False（旧实现空值直接放行 = fail open）
      - 字符串 "localhost" → False（REMOTE_ADDR 是原始 socket 地址，
        正常情况下只会是 IP；出现字符串说明来源不可信，不能当本机）
      - IPv4-mapped IPv6（::ffff:127.0.0.1）→ 按内嵌的 IPv4 判定
    而且这里只读 remote_addr —— 绝不读 X-Forwarded-For 之类客户端可伪造的头。
    """
    try:
        ip = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _credentials_ok(auth, token):
    """定长比较；auth 缺失/解析异常都用 False 收尾，绝不放行。"""
    if not auth or not token:
        return False
    try:
        return (hmac.compare_digest(auth.username or "", AUTH_USER)
                and hmac.compare_digest(auth.password or "", token))
    except Exception:
        return False


def install_access_guard(app_obj, token):
    """给所有请求安装访问控制（任何启动方式都会生效）。

    规则：
      - 明确的回环地址 → 放行（本机可信）
      - 其余一律认证：remote_addr 为空 / 无法解析 = 拿不到可信来源 → 拒绝
      - 非回环 + 没配置 token → 403（fail closed，不静默放行）
      - 非回环 + 有 token → 校验 HTTP Basic，失败 401（带 WWW-Authenticate）
    只看请求实际来源，不信任启动参数，避免 CLI/WSGI/Gunicorn 入口差异导致绕过。
    """
    @app_obj.before_request
    def _access_guard():
        remote = (request.remote_addr or "").strip()
        # 2026-11 修复：原来 `not remote or is_loopback(remote)` 在 remote_addr 为空时直接
        # 放行（fail open）。空值/非法值现在一律走认证分支。
        if _trusted_loopback(remote):
            return None
        if not token:
            return Response("未配置访问认证，已拒绝非本机请求", 403)
        if _credentials_ok(request.authorization, token):
            return None
        return Response("需要认证", 401,
                        {"WWW-Authenticate": f'Basic realm="{AUTH_REALM}", charset="UTF-8"'})

    return True


def create_app():
    """创建并初始化应用。认证在这里安装，任何导入方式都绕不过去。"""
    app_obj = Flask(__name__)
    app_obj.register_blueprint(bp)

    bind_host, warning = resolve_bind_host(SERVER_HOST, DEBUG)
    if warning:
        print(f"⚠️  {warning}")

    err = lan_auth_error(bind_host, SERVER_AUTH_TOKEN)
    if err:
        # import 阶段就失败：WSGI / flask run 同样拒绝启动，避免无认证暴露
        raise RuntimeError(err)

    install_access_guard(app_obj, SERVER_AUTH_TOKEN)
    app_obj.config["BIND_HOST"] = bind_host
    return app_obj


app = create_app()


if __name__ == "__main__":
    bind_host = app.config["BIND_HOST"]
    if SERVER_AUTH_TOKEN and not is_loopback(bind_host):
        print(f"🔐 已启用局域网 Basic 认证（用户名 {AUTH_USER}，请配合 HTTPS 使用）")
    print(f"🚀 {bind_host}:{SERVER_PORT} 启动" + ("（调试模式）" if DEBUG else ""))
    print(f"   浏览器打开 http://localhost:{SERVER_PORT}")
    try:
        app.run(host=bind_host, port=SERVER_PORT, debug=DEBUG)
    except SystemExit as e:
        sys.exit(e.code if isinstance(e.code, int) else 1)
