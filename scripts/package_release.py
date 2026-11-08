#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打一个「可以发给别人」的发布包。

背景：2026-11 把 16MB 的 stock-tracker.zip 发出去后才发现里面带着
config_local.py（明文 DeepSeek key）、.venv/、.git/、__pycache__/
和运行缓存。Git 忽略 ≠ 压缩包忽略，所以打包这件事不能再靠手点右键。

本脚本做四件事：
  1. 按黑名单排除（.git/.venv/__pycache__/config_local*/.env*/data/*.db …）
  2. 安全校验：符号链接默认拒绝；所有文件 realpath 必须落在项目根目录内
  3. 分块扫描每个待入包文件（不再因为"文件太大"跳过），发现疑似密钥直接中止
  4. 打完再流式复检 zip；复检不通过就删除产物，绝不留下一个没验过的包

用法：
    python3 scripts/package_release.py                # 输出到 dist/
    python3 scripts/package_release.py --dry-run      # 只报告，不写文件
    python3 scripts/package_release.py --out /tmp/x.zip
    python3 scripts/package_release.py --allow-symlinks   # 显式放行（仍要求目标在根目录内）

退出码：0 成功；1 发现密钥/私密条目；2 参数或 IO 错误；3 不安全条目（符号链接/越界路径）
"""

import argparse
import json
import os
import re
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 目录：整棵剪掉（任何层级同名都剪：这些是构建产物/依赖缓存）
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "stock-venv", "__pycache__", "dist", "build",
    ".idea", ".vscode", ".pytest_cache", "node_modules", ".mypy_cache",
}
# 目录：只剪项目根目录下那一个（运行时数据）
# 以前 "data" 写在全层级排除里，结果 services/data/ 这种代码包也一起被剪掉 ——
# 发布包缺模块，本地测试却全绿，是最难查的一类事故。
EXCLUDE_DIRS_TOP = {
    "data",                     # 本地 SQLite（政策事件库），属于运行时数据
    "logs",                     # 每日 feed 日志
}
# 文件：按名剪掉
EXCLUDE_FILES = {
    "config_local.py",          # 个人自选池 + 明文 key
    "_data_cache.json",         # 运行时行情快照
    "ai_cache.json",            # AI 分析结果缓存
    ".DS_Store", "Thumbs.db",
}
# 文件名前缀：剪掉（config_local.py.bak / config_local_backup.txt / .env.local 一律不带走）
EXCLUDE_PREFIXES = ("config_local", ".env", ".netrc", "id_rsa")
# 文件名后缀：剪掉
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".bak", ".log", ".tmp", ".swp",
                    ".db", ".sqlite", ".sqlite3", ".zip", ".pem", ".key", ".crt", ".p12")

# 疑似密钥：sk- 开头 / ark- 开头（火山方舟）/ 常见 key= 赋值
SECRET_PATTERNS = [
    ("deepseek/openai", re.compile(rb"sk-[A-Za-z0-9]{20,300}")),
    ("volcengine-ark", re.compile(rb"ark-[A-Za-z0-9-]{20,300}")),
    ("key-assignment", re.compile(
        rb"(?i)(api[_-]?key|access[_-]?token|secret[_-]?key|auth[_-]?token)"
        rb"\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,300}")),
]

# 分块扫描：1MB 一块，块间保留 128 字节重叠，避免密钥正好跨块边界被漏掉
CHUNK_SIZE = 1024 * 1024
CHUNK_OVERLAP = 128


class UnsafeEntry(Exception):
    """符号链接 / realpath 越界等不该入包的东西。"""


def _inside(root_real, path_real):
    """path 是否在 root 之内（用 commonpath，避免 /root2 被 /root 前缀骗过）。"""
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:      # 不同盘符/不同驱动器
        return False


def iter_files(allow_symlinks=False):
    """遍历项目文件，返回 (绝对路径, 相对路径)。

    符号链接默认直接报错（不静默跳过，也不静默跟随）：
    跟随可以把 /etc/passwd、~/.ssh/id_rsa 打进包里；静默跳过又会让人以为
    "链接的文件已经打进去了"。两种都比报错更糟。
    """
    root_real = os.path.realpath(ROOT)
    for dirpath, dirnames, filenames in os.walk(ROOT, followlinks=False):
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            rel_dir = os.path.relpath(full, ROOT).replace(os.sep, "/")
            if d in EXCLUDE_DIRS:
                continue
            if d in EXCLUDE_DIRS_TOP and "/" not in rel_dir:     # 只剪根目录下的 data/ logs/
                continue
            if os.path.islink(full):
                if not allow_symlinks:
                    raise UnsafeEntry(f"目录是符号链接：{os.path.relpath(full, ROOT)}")
                target = os.path.realpath(full)
                if not _inside(root_real, target):
                    raise UnsafeEntry(f"符号链接指向项目外：{os.path.relpath(full, ROOT)} -> {target}")
                continue        # 显式放行也不递归，避免重复打包/环路
            keep.append(d)
        dirnames[:] = keep

        for fn in sorted(filenames):
            if fn in EXCLUDE_FILES or fn.startswith(EXCLUDE_PREFIXES) \
                    or fn.lower().endswith(EXCLUDE_SUFFIXES):
                continue
            abs_p = os.path.join(dirpath, fn)
            rel_p = os.path.relpath(abs_p, ROOT).replace(os.sep, "/")
            if os.path.islink(abs_p):
                target = os.path.realpath(abs_p)
                if not allow_symlinks:
                    raise UnsafeEntry(f"文件是符号链接：{rel_p} -> {target}")
                if not _inside(root_real, target):
                    raise UnsafeEntry(f"符号链接指向项目外：{rel_p} -> {target}")
            real_p = os.path.realpath(abs_p)
            if not _inside(root_real, real_p):
                raise UnsafeEntry(f"路径越出项目根目录：{rel_p} -> {real_p}")
            if not os.path.isfile(real_p):
                raise UnsafeEntry(f"不是普通文件：{rel_p}")
            yield abs_p, rel_p


def scan_secrets(data, rel_path):
    """返回命中的 (标签, 掩码) 列表，不返回完整密钥。"""
    hits = []
    for label, pat in SECRET_PATTERNS:
        for m in pat.finditer(data):
            token = m.group(0).decode("latin-1")
            hits.append((label, f"{rel_path}: {token[:6]}...{token[-4:]}"))
    return hits


def scan_stream(read_fn, rel_path, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """分块扫描任意大小的流；按掩码去重，避免重叠区重复计数。

    旧实现遇到 >8MB 的文件直接 continue —— 等于给"把 key 塞进大文件"留了后门。
    """
    # 重叠必须 ≥ 单个匹配的最大长度，否则一个 key 被切在两块之间就永远拼不起来。
    overlap = max(int(overlap or 0), 512)
    hits, seen, tail = [], set(), b""
    while True:
        buf = read_fn(chunk_size)
        if not buf:
            break
        data = tail + buf
        for label, where in scan_secrets(data, rel_path):
            key = (label, where)
            if key not in seen:
                seen.add(key)
                hits.append((label, where))
        tail = data[-overlap:] if overlap else b""
    return hits


def scan_file(path, rel_path):
    """扫描单个文件：小文件整读，大文件一律流式（不把整个文件读进内存）。"""
    with open(path, "rb") as f:
        head = f.read(CHUNK_SIZE + 1)
    if len(head) <= CHUNK_SIZE:
        return scan_secrets(head, rel_path) + scan_json_credentials(head, rel_path)
    hits = []
    with open(path, "rb") as f:
        hits.extend(scan_stream(f.read, rel_path))
    # 大 JSON 也要做结构级检查（原来 >2MB 直接 return []，等于给大文件开后门）
    if head.lstrip()[:1] in (b"{", b"["):
        hits.extend(scan_json_credentials_stream(path, rel_path))
    return hits


SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(?:api[_-]?key|apikey|secret([_-]?key)?|token|access[_-]?token|auth[_-]?token|"
    r"password|passwd|pwd|authorization|credential|private[_-]?key|"
    r"deepseek[_-]?api[_-]?key|openai[_-]?api[_-]?key)s?$")
# 说明：结尾统一允许一个 s —— 现实里的配置常用复数（api_keys / tokens / passwords），
# 只认单数会让 {"api_keys": [...]} 整类漏掉。

# 同一份敏感字段名，用于"不解析 JSON 也能结构化扫描"的字节级正则
_SENSITIVE_KEY_SRC = (rb"(?:api[_-]?key|apikey|secret(?:[_-]?key)?|token|access[_-]?token|"
                      rb"auth[_-]?token|password|passwd|pwd|authorization|credential|"
                      rb"private[_-]?key|deepseek[_-]?api[_-]?key|openai[_-]?api[_-]?key)s?")

# 结构化扫描：字节级状态机，不再用"一条正则同时抓键和值"。
#
# 旧实现（2026-11 审查确认漏检）：
#   _JSON_PAIR_RE = "... \"(敏感键)\" \s*:\s* (?:\"([^\"]{8,4000})\" | \[(...){1,500}\])"
#   - 值长度写死 {8,4000} → >4000 字符的值漏检
#   - 数组元素写死 {1,500} → >500 项的数组漏检
#   - >2MB 的 JSON 只能走这条正则，于是上面两类在大文件里 100% 漏
#   - 大数组上还容易回溯
# 现在改成增量状态机：不设长度/条数上限，分块喂进去也不会把跨块的值切断。
_WS = b" \t\r\n"
_STR_STOP_RE = re.compile(rb'[\\"]')
_JSON_ESC_RE = re.compile(rb'\\u([0-9a-fA-F]{4})|\\(["\\/bfnrt])')
_ESC_MAP = {b'"': b'"', b'\\': b'\\', b'/': b'/',
            b'b': b'\b', b'f': b'\f', b'n': b'\n', b'r': b'\r', b't': b'\t'}


def _unescape_json(raw):
    """把 JSON 字符串里的转义还原成真实字符。

    漏洞背景：`"api\\u005fkey": "..."` 在文件里是 6 个可见字符 `\\u005f`，
    直接拿字节去匹配 `api_key` 匹配不到 —— 转义字段名因此绕过结构化扫描。
    """
    def sub(m):
        if m.group(1):
            try:
                return chr(int(m.group(1), 16)).encode("latin-1", "replace")
            except Exception:
                return m.group(0)
        return _ESC_MAP.get(m.group(2), m.group(2))
    try:
        return _JSON_ESC_RE.sub(sub, bytes(raw)).decode("latin-1", "replace")
    except Exception:
        return bytes(raw).decode("latin-1", "replace")


_KEY_HEAD_MAX = 256       # 只为判断"是不是敏感键"留窗口，键名不可能这么长
_KEY_HEAD = 128           # 报告里只留前 4 字节，够用就行
_VALUE_TAIL = 8


class _CredentialJsonScanner:
    """在字节流里找 `"敏感键": "值"` 与 `"敏感键": ["值1", ...]`。

    值长度、数组元素个数都不设上限；字符串只保留"前 128 字节 + 后 8 字节 + 总长度"，
    所以一个 5MB 的值不会把内存撑爆，但依然能被判定为凭据。
    """

    def __init__(self, rel_path):
        self.rel_path = rel_path
        self.hits = []
        self._seen = set()
        self._in_str = False
        self._esc = False          # 上一块末尾是转义符，下一块第一个字节被转义
        self._s_len = 0
        self._s_head = bytearray()
        self._s_tail = bytearray()
        self._just_closed = False  # 刚读完一个字符串，还不知道它是键还是值
        self._field = None         # 当前正等待值的敏感键
        self._value_pos = False
        self._arr_depth = 0        # >0 表示正在敏感键的数组里

    # ---- 字符串累积 ----
    def _str_reset(self):
        self._s_len = 0
        self._s_head = bytearray()
        self._s_tail = bytearray()

    def _str_add(self, chunk):
        if not chunk:
            return
        self._s_len += len(chunk)
        room = _KEY_HEAD - len(self._s_head)
        if room > 0:
            self._s_head += chunk[:room]
        self._s_tail += chunk[-_VALUE_TAIL:]
        if len(self._s_tail) > _VALUE_TAIL:
            self._s_tail = self._s_tail[-_VALUE_TAIL:]

    # ---- 判定 ----
    def _key_closed(self):
        """刚结束的字符串后面是 ':' → 它是键（键名先还原 \\uXXXX 之类的转义）。"""
        key = None
        if self._s_len <= _KEY_HEAD:
            key = _unescape_json(bytes(self._s_head))
        elif self._s_len <= _KEY_HEAD_MAX:
            # 超长键只按原始字节判断（保守：匹配上就报）
            key = bytes(self._s_head).decode("latin-1", "replace")
        if key and SENSITIVE_KEY_RE.match(key):
            self._field = key
            self._value_pos = True
        else:
            self._field = None
            self._value_pos = False

    def _value_closed(self):
        """刚结束的字符串处在"敏感键的值"位置 → 记一条。"""
        if not (self._value_pos or self._arr_depth > 0):
            return
        if self._s_len < 8:
            return
        head = bytes(self._s_head)
        tail = bytes(self._s_tail)
        if self._s_len <= _KEY_HEAD:
            v = head.decode("latin-1", "replace").strip()
            if v.lower() in _PLACEHOLDERS:
                return
        field = self._field or "?"
        where = (f"{self.rel_path}: {field}="
                 f"{head[:4].decode('latin-1', 'replace')}..."
                 f"{tail[-2:].decode('latin-1', 'replace')}")
        # 不去重：状态机每个字节只消费一次，不会重复计数；
        # 反过来，600 项数组就该报 600 条 —— 旧实现靠 where 去重会把它们压成 1 条。
        self.hits.append(("json-credential", where))

    # ---- 非字符串字节（结构符 / 数字 / 字面量）----
    def _gap(self, seg):
        for b in seg:
            if b in _WS:
                continue
            if self._just_closed:
                self._just_closed = False
                if b == 0x3A:                 # ':'
                    self._key_closed()
                    continue
                self._value_closed()          # 刚关掉的其实是值
                self._value_pos = False
                if self._arr_depth == 0:
                    self._field = None
                # 数组里的逗号/右括号交给下面的结构判定处理，
                # 不能在这里把 _arr_depth 清零 —— 那会让数组只报第一项。
                # 继续用同一个字节做结构判定
            if b == 0x7B:                     # '{'
                self._field = None
                self._value_pos = False
            elif b == 0x7D:                   # '}'
                self._field = None
                self._value_pos = False
                self._arr_depth = 0
            elif b == 0x5B:                   # '['
                if self._value_pos:
                    self._arr_depth = 1
                    self._value_pos = False
                elif self._arr_depth:
                    self._arr_depth += 1
            elif b == 0x5D:                   # ']'
                if self._arr_depth:
                    self._arr_depth -= 1
                    if self._arr_depth == 0:
                        self._field = None
            elif b == 0x2C or b == 0x3A:      # ',' ':'
                continue
            else:                             # 数字 / true / false / null
                self._value_pos = False

    # ---- 主入口 ----
    def feed(self, data, final=False):
        i, n = 0, len(data)
        if self._esc and n:                   # 上一块以 '\' 结尾
            self._str_add(data[:1])
            i = 1
            self._esc = False
        while i < n:
            if self._in_str:
                m = _STR_STOP_RE.search(data, i)
                if not m:
                    self._str_add(data[i:])
                    i = n
                    break
                self._str_add(data[i:m.start()])
                i = m.start()
                if data[i:i + 1] == b"\\":
                    # 转义符本身也要收进缓冲：否则 "api\u005fkey" 会被存成
                    # "apiu005fkey"，_unescape_json 再见不到 \u，转义字段名照样漏检。
                    self._str_add(b"\\")
                    i += 1
                    if i < n:
                        self._str_add(data[i:i + 1])
                        i += 1
                    else:
                        self._esc = True
                    continue
                i += 1                         # 结束引号
                self._in_str = False
                self._just_closed = True
                continue
            j = data.find(b'"', i)
            self._gap(data[i:] if j < 0 else data[i:j])
            if j < 0:
                i = n
                break
            i = j + 1
            self._in_str = True
            self._str_reset()
        if final:
            if self._esc:                      # 末尾孤立的 '\'：按普通字符收尾
                self._esc = False
            if self._just_closed:
                self._value_closed()
                self._just_closed = False
        return self.hits


_PLACEHOLDERS = ("", "none", "null", "undefined", "your_key_here", "changeme", "xxx", "***")


def _pairs_in(data, rel_path):
    """结构化扫描（不做 json.loads）：超大 / 被截断 / 非法 JSON 同样能查。

    覆盖 `"敏感键": "值"` 与 `"敏感键": ["值1", ...]` 两种形态，长度与条数都不设上限。
    """
    return _CredentialJsonScanner(rel_path).feed(data, final=True)


def scan_json_credentials_stream(path, rel_path, chunk_size=CHUNK_SIZE, overlap=None):
    """>2MB 的 JSON：逐块喂状态机，不整文件读进内存，跨块的值也不会被切断。

    `overlap` 参数只为兼容旧调用保留 —— 状态机会自己记住未读完的字符串，
    不再靠"重叠窗口"拼跨块匹配。
    """
    scanner = _CredentialJsonScanner(rel_path)
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            scanner.feed(buf)
    return scanner.feed(b"", final=True)



def _walk_json(obj, path="", key_name=""):
    """递归产出 (字段路径, 键, 值)。

    2026-11 修复：数组里的纯字符串元素原来用下标当键名（"0"/"1"），
    于是 {"api_keys": ["abcdefgh12345678"]} 永远命中不了敏感字段正则。
    现在数组元素继承外层字段名；数组里的字典仍用自己真实的键名。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}" if path else str(k)
            if isinstance(v, dict):
                yield from _walk_json(v, child, str(k))
            elif isinstance(v, list):
                yield from _walk_json(v, child, str(k))
            else:
                yield child, str(k), v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            child = f"{path}[{i}]"
            if isinstance(v, dict):
                yield from _walk_json(v, child, "")      # 字典用真实键名
            elif isinstance(v, list):
                yield from _walk_json(v, child, key_name)
            else:
                yield child, key_name or str(i), v


def scan_json_credentials(data, rel_path):
    """把整段内容当 JSON 解析，命中敏感字段且值像真凭据就报。

    为什么要单独做：`{"api_key": "abcd1234..."}` 不含 sk- 前缀，正则扫不出来；
    但打包发出去就是明文凭据。这里是结构化判断，不是靠扩展名。
    """
    if not data:
        return []
    head = data.lstrip()[:1]
    if head not in (b"{", b"["):
        return []
    if len(data) > 2 * 1024 * 1024:
        # 超大 JSON 不解析（避免峰值内存），退回字节级结构扫描：
        # 检查照做，只是不用 json.loads 建对象。
        return _pairs_in(data, rel_path)
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # 解析失败不等于没凭据：截断的 JSON 也照样扫
        return _pairs_in(data, rel_path)
    hits = []
    for field, key, value in _walk_json(obj):
        if not SENSITIVE_KEY_RE.match(key):
            continue
        if not isinstance(value, str):
            continue
        v = value.strip()
        if len(v) < 8 or v.lower() in _PLACEHOLDERS:
            continue
        hits.append(("json-credential", f"{rel_path}: {field}={v[:4]}...{v[-2:]}"))
    return hits


# Windows 保留设备名：解压到 Windows 会失败或被系统设备接管
_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                 | {f"COM{i}" for i in range(1, 10)}
                 | {f"LPT{i}" for i in range(1, 10)})


def _is_win_reserved(segment):
    """段名（去扩展名、去尾部空格点）是否是 Windows 保留设备名。"""
    base = (segment or "").split(".")[0].strip().rstrip(" .").upper()
    return base in _WIN_RESERVED


def unsafe_zip_name(name):
    """ZIP 条目路径是否危险（跨平台穿越）。返回原因或 None。

    Windows 解压器把反斜杠当分隔符、把 C: 当盘符，Linux 只认正斜杠；
    只检查一种分隔符就等于给另一种留后门。
    2026-11 补充：Windows 保留设备名（CON/NUL/COM1…）和 NTFS 备用数据流
    （a.txt:secret）也要拦，否则包在 Windows 上解压会失败或写出隐藏流。
    """
    n = name or ""
    if "\\" in n:
        return "含反斜杠（跨平台路径穿越）"
    if n.startswith("/") or n.startswith("\\"):
        return "绝对路径"
    if re.match(r"^[A-Za-z]:", n) or re.match(r"^[A-Za-z]:[\\/]", n):
        return "Windows 盘符绝对路径"
    if ":" in n:
        return "NTFS 备用数据流（路径含冒号）"
    parts = re.split(r"[\\/]", n)
    if any(p == ".." for p in parts):
        return "含 .. 路径穿越"
    if any(p == "" for p in parts[:-1]):
        return "空路径段"
    for p in parts:
        if _is_win_reserved(p):
            return f"Windows 保留设备名：{p}"
    return None


def audit_zip_names(names):
    """纯函数：检查包内条目（私密文件 / 跨平台穿越 / 重复路径）。

    返回 (bad_entries, unsafe_paths, dupes)，main 里任何一项非空都不发布。
    """
    bad = [n for n in names if
           n.startswith("stock-tracker/.git/") or
           n.startswith("stock-tracker/.venv/") or
           "__pycache__" in n or
           n.endswith("/config_local.py") or
           n.startswith("stock-tracker/data/") or
           n.endswith(("/_data_cache.json", "/ai_cache.json"))]
    unsafe = [(n, unsafe_zip_name(n)) for n in names if unsafe_zip_name(n)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    return bad, unsafe, dupes


def main():
    ap = argparse.ArgumentParser(description="打干净发布包（排除私密配置 + 分块扫描密钥）")
    ap.add_argument("--out", help="输出 zip 路径（默认 dist/stock-tracker-<时间>.zip）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--allow-symlinks", action="store_true",
                    help="显式放行符号链接（目标仍必须在项目根目录内）")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(ROOT, "dist", f"stock-tracker-{stamp}.zip")

    # ---- 0. 安全遍历（符号链接/越界路径在这里就出局） ----
    try:
        files = list(iter_files(allow_symlinks=args.allow_symlinks))
    except UnsafeEntry as e:
        print(f"[x] 不安全条目，拒绝打包：{e}")
        print("    处理：删掉该符号链接，或确认目标在项目内后用 --allow-symlinks。")
        return 3
    except OSError as e:
        print(f"[x] 遍历文件失败：{e}")
        return 2

    total_bytes = sum(os.path.getsize(p) for p, _ in files)

    # ---- 1. 入包前分块扫描（不按大小跳过） ----
    print(f"== 入包文件 {len(files)} 个，未压缩合计 {total_bytes / 1024:.1f} KB ==")
    leaks = []
    for abs_p, rel_p in files:
        try:
            leaks.extend(scan_file(abs_p, rel_p))
        except OSError as e:
            print(f"[x] 读取失败：{rel_p}: {e}")
            return 2
        except Exception as e:      # 扫描器自身崩了 = 安全检查没做完 → 一律不许发布
            print(f"[x] 安全扫描失败（{rel_p}）：{type(e).__name__}: {e}")
            print("    检查未完成即视为不通过，拒绝打包。")
            return 2

    if leaks:
        print("\n[x] 发现疑似密钥，已中止打包（不写任何文件）：")
        for label, where in leaks[:20]:
            print(f"    [{label}] {where}")
        print("\n    处理：把该值挪进环境变量或 config_local.py（后者已被排除），再重跑。")
        return 1
    print("  [v] 密钥扫描：0 命中（分块全量扫描，无大小豁免）")

    # ---- 2. 写出 zip ----
    if args.dry_run:
        print(f"\n[dry-run] 本应写出 {out}，未写文件。")
        return 0

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".part"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for abs_p, rel_p in files:
                zf.write(abs_p, arcname=f"stock-tracker/{rel_p}")
        os.replace(tmp, out)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"[x] 写 zip 失败：{e}")
        return 2

    # ---- 3. 打完流式复检；不通过就删产物 ----
    bad_entries, zip_leaks, names = [], [], []
    try:
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            bad_entries, unsafe_paths, dupes = audit_zip_names(names)
            for n in names:
                if n.endswith("/"):
                    continue
                with zf.open(n) as fh:
                    zip_leaks.extend(scan_stream(fh.read, n))
    except (zipfile.BadZipFile, OSError) as e:
        os.remove(out)
        print(f"[x] 复检失败（{e}），已删除产物 {out}")
        return 2

    if unsafe_paths or dupes:
        for n, why in unsafe_paths[:10]:
            print(f"[x] 危险条目 {n!r}: {why}")
        for n in dupes[:10]:
            print(f"[x] 重复条目: {n!r}")
        os.remove(out)
        print(f"[x] 复检不通过，已删除产物 {out}")
        return 3

    if zip_leaks:
        os.remove(out)
        print(f"[x] 包内复检发现 {len(zip_leaks)} 处疑似凭据，已删除产物 {out}")
        for label, where in zip_leaks[:10]:
            print(f"    [{label}] {where}")
        return 1

    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"\n[v] 已写出 {out}")
    print(f"    条目 {len(names)} 个，{size_mb:.2f} MB（源目录 {total_bytes/1024/1024:.2f} MB 未压缩）")
    print(f"    [{'v' if not bad_entries else 'x'}] 私密/垃圾条目：{len(bad_entries)} 个")
    print(f"    [{'v' if not zip_leaks else 'x'}] 包内密钥复检：{len(zip_leaks)} 命中")

    if bad_entries or zip_leaks:
        for n in (bad_entries + [w for _, w in zip_leaks])[:20]:
            print(f"      - {n}")
        # 关键：复检不通过的包必须删掉，否则它就躺在 dist/ 里等被人发出去
        try:
            os.remove(out)
            print(f"    [x] 已删除未通过复检的产物：{out}")
        except OSError as e:
            print(f"    [x] 删除失败，请手动删除 {out}：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
