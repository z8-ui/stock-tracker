"""
应用入口 - 相当于 main() / public static void main
启动 Flask 服务器，注册路由
"""

import ipaddress

from flask import Flask
from routes import bp
from config import SERVER_HOST, SERVER_PORT, DEBUG

app = Flask(__name__)

# 注册路由（相当于 Spring 扫描 @Controller）
app.register_blueprint(bp)


def resolve_bind_host(host, debug):
    """调试模式下强制只绑回环地址。

    Werkzeug 调试器带交互式控制台，等于「谁连上谁就能执行任意代码」。
    因此 DEBUG=True 时绝不允许绑 0.0.0.0 / 局域网 IP：
    想开自动重载就在本机用 127.0.0.1 访问，想给局域网用就 DEBUG=False。
    """
    if not debug:
        return host, None
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.strip().lower() == "localhost"
    if is_loopback:
        return host, None
    return "127.0.0.1", f"DEBUG=True 时不允许绑定 {host}（Werkzeug 调试器可执行任意代码），已改为 127.0.0.1"


if __name__ == "__main__":
    bind_host, warning = resolve_bind_host(SERVER_HOST, DEBUG)
    if warning:
        print(f"⚠️  {warning}")
        print("   需要局域网访问请把 config_local.py 的 DEBUG 改成 False")
    print(f"🚀 {bind_host}:{SERVER_PORT} 启动" + ("（调试模式）" if DEBUG else ""))
    print(f"   浏览器打开 http://localhost:{SERVER_PORT}")
    print(f"   按 Ctrl+C 停止服务器")
    app.run(host=bind_host, port=SERVER_PORT, debug=DEBUG)
