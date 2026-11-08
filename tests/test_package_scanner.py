# -*- coding: utf-8 -*-
"""发布包扫描器：JSON 凭据 + 跨平台路径 + 大文件 + fail-closed（P2）

全部用虚构凭据；不读取任何真实 key。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import package_release as pr   # noqa: E402

FAKE = "abcdefgh12345678"      # 虚构；不含 sk- 前缀，专门测结构化扫描


class TestJsonScan(unittest.TestCase):
    def test_plain_json_key(self):
        hits = pr.scan_json_credentials(json.dumps({"api_key": FAKE}).encode(), "a.json")
        self.assertEqual(len(hits), 1)
        self.assertIn("a.json", hits[0][1])
        self.assertNotIn(FAKE, hits[0][1])        # 报告里只留掩码

    def test_nested_and_array(self):
        obj = {"cfg": {"deepseek_api_key": FAKE},
               "list": [{"token": FAKE}, {"name": "ok"}],
               "authorization": "Bearer " + FAKE,
               "password": FAKE}
        hits = pr.scan_json_credentials(json.dumps(obj).encode(), "nested.json")
        self.assertEqual(len(hits), 4, hits)

    def test_short_placeholder_ignored(self):
        for obj in ({"api_key": ""}, {"api_key": "null"}, {"token": "short"}):
            self.assertEqual(pr.scan_json_credentials(json.dumps(obj).encode(), "x.json"), [])

    def test_json_inside_txt_is_scanned(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.write(json.dumps({"secret": FAKE}))
        tmp.close()
        try:
            hits = pr.scan_file(tmp.name, "notes.txt")
            self.assertTrue(any(h[0] == "json-credential" for h in hits))
        finally:
            os.unlink(tmp.name)

    def test_large_file_full_scan(self):
        """>8MB 也不能跳过；key 放末尾照样要扫出来。"""
        tmp = tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False)
        tmp.write(b"A" * (9 * 1024 * 1024))
        tmp.write(b" sk-" + b"Z" * 24)
        tmp.close()
        try:
            hits = pr.scan_file(tmp.name, "big.txt")
            self.assertTrue(hits, "大文件末尾的 key 被漏检")
        finally:
            os.unlink(tmp.name)
        self.assertGreater(9 * 1024 * 1024, pr.CHUNK_SIZE)

    def test_cross_chunk_boundary(self):
        data = b"prefix" + b"sk-" + b"Q" * 24 + b"suffix"
        pos = {"i": 0}

        def reader(n):
            chunk = data[pos["i"]:pos["i"] + 8]
            pos["i"] += len(chunk)
            return chunk

        hits = pr.scan_stream(reader, "split.txt", chunk_size=8, overlap=8)
        self.assertTrue(hits, "跨分块边界的 key 被漏检")


class TestZipPaths(unittest.TestCase):
    def test_traversal_and_absolute(self):
        bs = chr(92)
        for bad in ("../evil", "a/../../b", "/etc/passwd", "C:/Windows/x",
                    "c:" + bs + "x", "a" + bs + ".." + bs + "b"):
            self.assertIsNotNone(pr.unsafe_zip_name(bad), bad)
        for good in ("stock-tracker/app.py", "stock-tracker/static/js/a.js"):
            self.assertIsNone(pr.unsafe_zip_name(good), good)

    def test_audit_detects_duplicates_and_private(self):
        names = ["stock-tracker/app.py", "stock-tracker/app.py",
                 "stock-tracker/config_local.py", "stock-tracker/data/x.db"]
        bad, unsafe, dupes = pr.audit_zip_names(names)
        self.assertIn("stock-tracker/config_local.py", bad)
        self.assertEqual(dupes, ["stock-tracker/app.py"])
        self.assertFalse(unsafe)


class TestCliFailClosed(unittest.TestCase):
    """把脚本复制进临时项目根，跑真实进程验证退出码与产物。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "scripts"))
        shutil.copy(os.path.join(ROOT, "scripts", "package_release.py"),
                    os.path.join(self.tmp, "scripts", "package_release.py"))
        with open(os.path.join(self.tmp, "app.py"), "w", encoding="utf-8") as f:
            f.write("print('ok')\n")
        self.out = os.path.join(self.tmp, "dist", "out.zip")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return subprocess.run([sys.executable, os.path.join(self.tmp, "scripts", "package_release.py"),
                               "--out", self.out], capture_output=True, text=True)

    def test_clean_package_succeeds(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.out))

    def test_json_key_fails_and_writes_nothing(self):
        with open(os.path.join(self.tmp, "notes.txt"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"api_key": FAKE}))
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertFalse(os.path.exists(self.out), "扫描失败仍然留下了发布包")

    def test_symlink_refused(self):
        os.symlink("/etc/passwd", os.path.join(self.tmp, "evil.txt"))
        r = self._run()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertFalse(os.path.exists(self.out))


class TestJsonArraysAndBigFiles(unittest.TestCase):
    """P1：字符串数组 / >2MB JSON / 跨块边界 —— 都是原实现漏检的形态。"""

    def test_string_array_is_detected(self):
        hits = pr.scan_json_credentials(json.dumps({"api_keys": [FAKE]}).encode(), "a.json")
        self.assertEqual(len(hits), 1, hits)

    def test_nested_string_array_is_detected(self):
        obj = {"cfg": {"tokens": [FAKE, "short"]}}
        hits = pr.scan_json_credentials(json.dumps(obj).encode(), "a.json")
        self.assertEqual(len(hits), 1, hits)

    def test_array_of_placeholders_ignored(self):
        hits = pr.scan_json_credentials(json.dumps({"api_keys": ["null", "none"]}).encode(), "a.json")
        self.assertEqual(hits, [])

    def test_big_json_direct_call_still_checked(self):
        """>2MB 的 JSON 不许跳过：原来 len>2MB 直接 return []。"""
        payload = json.dumps({"pad": "x" * (2 * 1024 * 1024 + 10), "api_key": FAKE}).encode()
        self.assertGreater(len(payload), 2 * 1024 * 1024)
        hits = pr.scan_json_credentials(payload, "big.json")
        self.assertTrue(hits, ">2MB 的 JSON 被整体跳过了")

    def test_big_json_file_scanned_via_stream(self):
        tmp = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
        tmp.write(b'{"items": [' + b'"pad",' * 300000 + b'"end"], "api_keys": ["' + FAKE.encode() + b'"]}')
        tmp.close()
        try:
            self.assertGreater(os.path.getsize(tmp.name), pr.CHUNK_SIZE)
            hits = pr.scan_file(tmp.name, "big.json")
            self.assertTrue(any(h[0] == "json-credential" for h in hits), hits)
        finally:
            os.unlink(tmp.name)

    def test_json_pair_across_chunk_boundary(self):
        body = b'{"api_key": "' + FAKE.encode() + b'"}'
        tmp = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
        tmp.write(b" " * 7 + body)      # 让字段名跨块
        tmp.close()
        try:
            hits = pr.scan_json_credentials_stream(tmp.name, "x.json", chunk_size=11, overlap=64)
            self.assertTrue(hits, "跨分块边界的 JSON 字段名被漏检")
        finally:
            os.unlink(tmp.name)

    def test_truncated_json_still_scanned(self):
        hits = pr.scan_json_credentials(b'{"api_key": "' + FAKE.encode() + b'"', "cut.json")
        self.assertTrue(hits, "截断的 JSON 解析失败后直接返回 []，凭据被放过")


class TestNoLengthOrCountCaps(unittest.TestCase):
    """V2.0.1 P1：旧实现在值长度 / 数组条数上写了硬上限，导致漏检。

    _JSON_PAIR_RE 原来是 `{8,4000}` 和 `{1,500}`：>4000 字符的值、>500 项的数组
    全部漏掉，而且 >2MB 的 JSON 只能走这条正则，等于在大文件里必然漏。
    """

    def test_long_value_over_4000(self):
        value = "A" * 5000 + "ZZ"
        hits = pr.scan_json_credentials(json.dumps({"api_key": value}).encode(), "long.json")
        self.assertEqual(len(hits), 1, hits)

    def test_large_string_array_over_500_items(self):
        obj = {"api_keys": [f"fakekey{i:04d}" for i in range(600)]}
        hits = pr.scan_json_credentials(json.dumps(obj).encode(), "many.json")
        self.assertEqual(len(hits), 600, f"数组只报出 {len(hits)} 条")

    def test_big_json_with_long_value(self):
        payload = json.dumps({"api_key": "B" * 5000 + "YY",
                              "pad": "x" * (2 * 1024 * 1024)}).encode()
        self.assertGreater(len(payload), 2 * 1024 * 1024)
        self.assertTrue(pr.scan_json_credentials(payload, "big_long.json"))

    def test_big_json_with_large_array(self):
        payload = json.dumps({"pad": "x" * (2 * 1024 * 1024),
                              "api_keys": [f"fakekey{i:04d}" for i in range(600)]}).encode()
        hits = pr.scan_json_credentials(payload, "big_arr.json")
        self.assertEqual(len(hits), 600, f"大 JSON 数组只报出 {len(hits)} 条")

    def test_streaming_long_value_across_chunks(self):
        """5000 字符的值在 4KB 分块下必然跨块，旧实现靠 64KB 重叠也接不上。"""
        body = b'{"api_key": "' + b"C" * 5000 + b'ZZ"}'
        tmp = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
        tmp.write(body)
        tmp.close()
        try:
            hits = pr.scan_json_credentials_stream(tmp.name, "s.json", chunk_size=4096)
            self.assertEqual(len(hits), 1, hits)
        finally:
            os.unlink(tmp.name)

    def test_streaming_matches_whole_buffer(self):
        """分块与整读必须给出一致结论 —— 否则"分块扫描"就是个假动作。"""
        payload = json.dumps({
            "pad": "x" * 9000,
            "api_keys": [f"fakekey{i:04d}" for i in range(30)],
            "nested": {"auth_token": "tok-" + "9" * 40},
        }).encode()
        whole = pr.scan_json_credentials(payload, "w.json")
        tmp = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
        tmp.write(payload)
        tmp.close()
        try:
            streamed = pr.scan_json_credentials_stream(tmp.name, "w.json", chunk_size=257)
        finally:
            os.unlink(tmp.name)
        self.assertEqual(len(whole), len(streamed), (len(whole), len(streamed)))
        # 字段路径写法两边不同（json.loads 走 api_keys[0]，状态机走 api_keys），
        # 但"哪些值被判定为凭据"必须一致 —— 比较掩码部分。
        self.assertEqual(sorted(h[1].split("=", 1)[1] for h in whole),
                         sorted(h[1].split("=", 1)[1] for h in streamed))

    def test_escaped_quotes_do_not_hide_later_key(self):
        body = (b'{"note": "he said \\"api_key\\": \\"zzzzzzzzzz\\"",'
                b' "token": "real-token-123456"}')
        hits = pr.scan_json_credentials(body, "esc.json")
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("token=", hits[0][1])

    def test_non_string_values_not_reported(self):
        for raw in (b'{"api_key": null}', b'{"api_key": 12345678}',
                    b'{"api_key": {"x": "yyyyyyyy"}}'):
            with self.subTest(raw=raw):
                self.assertEqual(pr.scan_json_credentials(raw, "n.json"), [])

    def test_scan_failure_blocks_release(self):
        """扫描器抛异常 = 安全检查没做完 → main() 必须非零退出且不产出 zip。"""
        from unittest import mock
        out = os.path.join(tempfile.mkdtemp(), "should-not-exist.zip")
        argv = ["package_release.py", "--out", out]
        with mock.patch.object(pr, "scan_file", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "argv", argv):
            rc = pr.main()
        self.assertEqual(rc, 2, "扫描失败却返回了成功")
        self.assertFalse(os.path.exists(out), "扫描失败却写出了 zip")
        shutil.rmtree(os.path.dirname(out), ignore_errors=True)


class TestWindowsUnsafeNames(unittest.TestCase):
    def test_reserved_device_names(self):
        for bad in ("CON", "con.txt", "stock-tracker/NUL", "a/COM1.log", "LPT9",
                    "aux.json", "stock-tracker/prn.py"):
            self.assertIsNotNone(pr.unsafe_zip_name(bad), bad)

    def test_ntfs_alt_data_stream(self):
        for bad in ("a.txt:secret", "stock-tracker/app.py:$DATA", "x/" + "y.txt:z"):
            self.assertIsNotNone(pr.unsafe_zip_name(bad), bad)

    def test_normal_names_still_pass(self):
        for good in ("stock-tracker/app.py", "stock-tracker/console.py",
                     "stock-tracker/com10.py", "stock-tracker/contract.md",
                     "stock-tracker/static/js/a.js"):
            self.assertIsNone(pr.unsafe_zip_name(good), good)


class TestCliReservedNameFailsClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "scripts"))
        shutil.copy(os.path.join(ROOT, "scripts", "package_release.py"),
                    os.path.join(self.tmp, "scripts", "package_release.py"))
        self.out = os.path.join(self.tmp, "dist", "out.zip")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reserved_name_aborts_and_leaves_no_zip(self):
        with open(os.path.join(self.tmp, "CON"), "w", encoding="utf-8") as f:
            f.write("x\n")
        r = subprocess.run([sys.executable,
                            os.path.join(self.tmp, "scripts", "package_release.py"),
                            "--out", self.out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.out), "被拦下的包仍然留在了磁盘上")


class TestDataDirExclusion(unittest.TestCase):
    """`data` 只该剪项目根目录的运行时库，不能连服务层代码包一起剪掉。

    曾经的坑：EXCLUDE_DIRS 里写着全层级 "data"，发布包把 services/data/ 整棵剪了，
    本地测试全绿、包却缺模块 —— 拿到包的人一跑就 ImportError。
    """

    def test_nested_data_package_is_included(self):
        rels = {rel for _, rel in pr.iter_files()}
        self.assertIn("services/data/__init__.py", rels,
                      "services/data/ 是代码包，必须入包")
        self.assertIn("services/data/providers/tencent.py", rels)

    def test_root_data_dir_is_excluded(self):
        rels = {rel for _, rel in pr.iter_files()}
        self.assertFalse([r for r in rels if r.startswith("data/")],
                         "根目录 data/ 是本地 SQLite 运行时数据，不能入包")


if __name__ == "__main__":
    unittest.main()
