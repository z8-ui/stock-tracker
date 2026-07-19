"""
应用入口 - 相当于 main() / public static void main
启动 Flask 服务器，注册路由
"""

from flask import Flask
from routes import bp
from config import SERVER_HOST, SERVER_PORT, DEBUG

app = Flask(__name__)

# 注册路由（相当于 Spring 扫描 @Controller）
app.register_blueprint(bp)

if __name__ == "__main__":
    print(f"🚀 {SERVER_HOST}:{SERVER_PORT} 启动")
    print(f"   浏览器打开 http://localhost:{SERVER_PORT}")
    print(f"   按 Ctrl+C 停止服务器")
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=DEBUG)
