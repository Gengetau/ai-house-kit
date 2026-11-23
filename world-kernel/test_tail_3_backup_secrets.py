"""Tail-3 regression tests: backup redaction / restore merge / archive hygiene / secret-file writes (A8, E8, D2, D3, N2, N9).

Every test builds a real runtime dir in a temp directory, backs it up, mutates it and restores it.
Secrets used here are obviously fake (`sk-test-fake-*`).
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ai_house.backup as B  # noqa: E402
from ai_house import __version__  # noqa: E402
from ai_house.backup import create_backup, restore_backup, verify_backup  # noqa: E402
from ai_house.cli import main as cli_main  # noqa: E402
from ai_house.onboarding import atomic_write_private_text, mask_secret  # noqa: E402

FAKE_A = "sk-test-fake-aaaaaaaaaaaa"
FAKE_B = "sk-test-fake-bbbbbbbbbbbb"
FAKE_FB = "sk-test-fake-fallbackkkk"
FAKE_NEW = "sk-test-fake-newvalue-9999"


def _mkdb(path: Path, val: str = "x") -> None:
    c = sqlite3.connect(str(path))
    c.execute("create table t(x)")
    c.execute("insert into t values(?)", (val,))
    c.commit()
    c.close()


def _ent(path: str, data: bytes, kind: str = "json") -> dict:
    return {"path": path, "kind": kind, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _read_member(arc: str, name: str) -> str:
    with tarfile.open(arc, "r:gz") as tf:
        return tf.extractfile(name).read().decode("utf-8")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.rt = self.tmp / "rt"
        self.rt.mkdir()
        _mkdb(self.rt / "life1.db")

    def build_archive(self, name: str, files, extra_members=(), manifest_extra=None, manifest_names=None) -> Path:
        arc = self.tmp / name
        man = {"version": __version__, "files": [_ent(p, b) for p, b in files]}
        if manifest_extra:
            man.update(manifest_extra)
        for k in [k for k, v in list(man.items()) if v is None]:
            man.pop(k)
        with tarfile.open(arc, "w:gz") as tf:
            mb = json.dumps(man).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(mb)
            tf.addfile(ti, io.BytesIO(mb))
            for p, b in files:
                ti = tarfile.TarInfo(p)
                ti.size = len(b)
                tf.addfile(ti, io.BytesIO(b))
            for m in extra_members:
                tf.addfile(m)
        return arc


class RestoreKeepsSecretsTests(_Base):
    def _seed(self) -> str:
        (self.rt / "llm_config.json").write_text(json.dumps({
            "endpoints": [
                {"name": "a", "api_key": FAKE_A, "base_url": "http://a.invalid"},
                {"name": "b", "api_key": FAKE_B, "base_url": "http://b.invalid"},
            ],
            "fallback": {"api_key": FAKE_FB, "model": "m-old"},
        }), encoding="utf-8")
        (self.rt / "plugin_settings.json").write_text(json.dumps(
            {"plugins": {"web_search": {"enabled": True, "values": {"api_key": FAKE_A, "region": "jp"}}}}), encoding="utf-8")
        return create_backup(self.rt)["archive"]

    def test_n2_restore_keeps_existing_keys_merges_nested_and_lists_missing(self) -> None:
        arc = self._seed()
        # Mutate: key for endpoint a rotated, endpoint b key removed, fallback section removed entirely.
        (self.rt / "llm_config.json").write_text(json.dumps({
            "endpoints": [
                {"name": "a", "api_key": FAKE_NEW, "base_url": "http://changed.invalid"},
                {"name": "b", "api_key": "", "base_url": "http://b.invalid"},
            ],
        }), encoding="utf-8")
        (self.rt / "plugin_settings.json").write_text(json.dumps(
            {"plugins": {"web_search": {"enabled": False, "values": {"api_key": FAKE_NEW, "region": "us"}}}}), encoding="utf-8")

        res = restore_backup(arc, self.rt, overwrite=True)

        llm = json.loads((self.rt / "llm_config.json").read_text(encoding="utf-8"))
        self.assertEqual(llm["endpoints"][0]["api_key"], FAKE_NEW, "existing key must survive restore")
        self.assertEqual(llm["endpoints"][0]["base_url"], "http://a.invalid", "non-secret fields come from the archive")
        self.assertEqual(llm["endpoints"][1]["api_key"], "")
        ps = json.loads((self.rt / "plugin_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(ps["plugins"]["web_search"]["values"]["api_key"], FAKE_NEW)
        self.assertEqual(ps["plugins"]["web_search"]["values"]["region"], "jp")
        self.assertTrue(ps["plugins"]["web_search"]["enabled"])

        missing = res.get("secrets_missing")
        self.assertIsInstance(missing, list)
        joined = "\n".join(missing)
        self.assertIn("llm_config.json", joined)
        self.assertIn("endpoints[1].api_key", joined)
        self.assertIn("fallback.api_key", joined)
        self.assertNotIn("endpoints[0].api_key", joined)
        for fake in (FAKE_A, FAKE_B, FAKE_FB, FAKE_NEW):
            self.assertNotIn(fake, json.dumps(res))

    def test_n2_restore_into_dir_without_config_lists_all_redacted_keys(self) -> None:
        arc = self._seed()
        fresh = self.tmp / "fresh"
        res = restore_backup(arc, fresh)
        joined = "\n".join(res.get("secrets_missing") or [])
        self.assertIn("endpoints[0].api_key", joined)
        self.assertIn("web_search", joined)
        self.assertEqual(json.loads((fresh / "llm_config.json").read_text())["endpoints"][0]["api_key"], "")

    def test_n2_cli_restore_prints_refill_list_without_secret_values(self) -> None:
        arc = self._seed()
        (self.rt / "llm_config.json").write_text(json.dumps({"endpoints": [{"name": "a", "api_key": FAKE_NEW}]}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main(["restore", arc, "--data-dir", str(self.rt), "--overwrite"])
        self.assertEqual(rc, 0)
        text = out.getvalue() + err.getvalue()
        self.assertIn("需要重新填写", text)
        self.assertIn("endpoints[1].api_key", text)
        for fake in (FAKE_A, FAKE_B, FAKE_FB, FAKE_NEW):
            self.assertNotIn(fake, text)
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            cli_main(["restore", arc, "--data-dir", str(self.rt), "--overwrite", "--json"])
        self.assertTrue(json.loads(out2.getvalue())["secrets_missing"])


class ArchivePathTests(_Base):
    def _restore_fails(self, arc: Path) -> None:
        target = self.tmp / "tgt"
        with self.assertRaises(ValueError):
            restore_backup(arc, target)
        self.assertFalse((self.tmp / "evil.json").exists())
        self.assertFalse(target.exists() and any(target.iterdir()), "nothing may be written on rejection")

    def test_d2_hidden_json_keeps_its_name_roundtrip(self) -> None:
        (self.rt / ".hidden.json").write_text('{"h": 1}', encoding="utf-8")
        arc = create_backup(self.rt)["archive"]
        fresh = self.tmp / "fresh"
        restore_backup(arc, fresh)
        self.assertTrue((fresh / ".hidden.json").is_file())
        self.assertFalse((fresh / "hidden.json").exists())

    def test_d2_leading_dot_slash_prefix_is_accepted_and_stripped(self) -> None:
        arc = self.build_archive("dot.tgz", [("./ok.json", b'{"a": 1}')])
        fresh = self.tmp / "fresh"
        # Manifest says ./ok.json too; only a leading "./" may be stripped.
        restore_backup(arc, fresh)
        self.assertEqual(json.loads((fresh / "ok.json").read_text()), {"a": 1})

    def test_d2_rejects_dotdot_member(self) -> None:
        self._restore_fails(self.build_archive("a.tgz", [("../evil.json", b"{}")]))
        self._restore_fails(self.build_archive("a2.tgz", [("a/../../evil.json", b"{}")]))
        self._restore_fails(self.build_archive("a3.tgz", [("./../evil.json", b"{}")]))

    def test_d2_rejects_env_member_instead_of_renaming(self) -> None:
        self._restore_fails(self.build_archive("e1.tgz", [(".env", b"K=1")]))
        self._restore_fails(self.build_archive("e2.tgz", [("./.env", b"K=1")]))
        self._restore_fails(self.build_archive("e3.tgz", [("media/.env", b"K=1")]))
        self._restore_fails(self.build_archive("e4.tgz", [(".env.local", b"K=1")]))

    def test_d2_rejects_absolute_member(self) -> None:
        self._restore_fails(self.build_archive("abs.tgz", [("/tmp/evil.json", b"{}")]))

    def test_d2_still_rejects_links_and_devices(self) -> None:
        for label, typ, link in (("sym", tarfile.SYMTYPE, "/etc/passwd"), ("hard", tarfile.LNKTYPE, "ok.json"), ("dev", tarfile.CHRTYPE, "")):
            ti = tarfile.TarInfo(f"x_{label}.json")
            ti.type = typ
            ti.linkname = link
            self._restore_fails(self.build_archive(f"{label}.tgz", [("ok.json", b"{}")], extra_members=[ti]))


class RedactionTests(_Base):
    def test_a8_redacts_by_key_pattern_everywhere_and_spares_lookalikes(self) -> None:
        doc = {
            "apikey": FAKE_A, "key": FAKE_A, "Authorization": "Bearer " + FAKE_A, "webhook_url": "https://h.invalid/hook/" + FAKE_A,
            "Cookie": "sid=" + FAKE_A, "passwd": FAKE_A, "clientSecret": FAKE_A, "access_token": FAKE_A, "auth": FAKE_A,
            "nested": {"list": [{"bot_token": FAKE_A, "name": "keep-me"}], "X-Api-Key": FAKE_A},
            "auth_cfg": {"auth": {"user": "u", "value": FAKE_A}},
            "proxy": "http://user:" + FAKE_A + "@proxy.invalid:8080/x",
            "callback": "https://cb.invalid/path?token=" + FAKE_A + "&ok=1",
            "cb2": "https://cb.invalid/path?foo=1&Key=" + FAKE_A,
            # Look-alikes that must NOT be touched:
            "keywords": ["alpha", "beta"], "max_tokens": 512, "token_count": 77, "author": "alice",
            "keyword": "visible", "tokens_used": 3, "authors": ["bob"], "monkey": "banana", "model": "m1",
            "plain_url": "https://example.invalid/page?x=1",
        }
        (self.rt / "other_state.json").write_text(json.dumps(doc), encoding="utf-8")
        (self.rt / "llm_config.json").write_text(json.dumps({"fallback": {"api_key": FAKE_A}}), encoding="utf-8")
        res = create_backup(self.rt)
        for name in ("other_state.json", "llm_config.json"):
            self.assertNotIn(FAKE_A, _read_member(res["archive"], name), name)
        out = json.loads(_read_member(res["archive"], "other_state.json"))
        for k in ("keywords", "max_tokens", "token_count", "author", "keyword", "tokens_used", "authors", "monkey", "model", "plain_url"):
            self.assertEqual(out[k], doc[k], f"{k} must not be redacted")
        self.assertEqual(out["nested"]["list"][0]["name"], "keep-me")
        self.assertIn("proxy.invalid", out["proxy"])
        self.assertNotIn(FAKE_A, out["proxy"])
        self.assertIn("cb.invalid/path", out["callback"])
        self.assertIn("ok=1", out["callback"])
        self.assertNotIn(FAKE_A, out["cb2"])
        self.assertIn("foo=1", out["cb2"])
        self.assertNotIn(FAKE_A, json.dumps(out))
        man = json.loads(_read_member(res["archive"], "manifest.json"))
        self.assertTrue(man["secrets_redacted"])
        by = {f["path"]: f for f in man["files"]}
        self.assertTrue(by["other_state.json"]["secrets_redacted"])

    def test_a8_pack_and_media_json_are_redacted_too(self) -> None:
        (self.rt / "custom-pack").mkdir()
        (self.rt / "custom-pack" / "p.json").write_text(json.dumps({"api_key": FAKE_A, "name": "n"}))
        res = create_backup(self.rt)
        self.assertNotIn(FAKE_A, _read_member(res["archive"], "custom-pack/p.json"))
        self.assertTrue(verify_backup(res["archive"])["ok"])

    def test_a8_bare_key_field_in_pack_content_survives_backup_and_restore(self) -> None:
        # 自定义世界包里名字恰好叫 key / auth 的普通标识字段不是密钥，脱敏不能把它清空
        pack = self.rt / "custom-pack"
        pack.mkdir()
        doc = {"rooms": [{"key": "bedroom_0", "auth": "resident"}], "api_key": FAKE_A, "auth_token": FAKE_A}
        (pack / "rooms.json").write_text(json.dumps(doc))
        arc = create_backup(self.rt)["archive"]
        staged = json.loads(_read_member(arc, "custom-pack/rooms.json"))
        self.assertEqual(staged["rooms"][0], {"key": "bedroom_0", "auth": "resident"})
        self.assertEqual(staged["api_key"], "")
        self.assertEqual(staged["auth_token"], "")
        # 顶层配置文件里的裸 key 仍按密钥处理
        (self.rt / "llm_config.json").write_text(json.dumps({"key": FAKE_A}))
        arc2 = create_backup(self.rt)["archive"]
        self.assertNotIn(FAKE_A, _read_member(arc2, "llm_config.json"))
        target = self.tmp / "restored"
        restore_backup(arc, target)
        restored = json.loads((target / "custom-pack" / "rooms.json").read_text())
        self.assertEqual(restored["rooms"][0]["key"], "bedroom_0")

    def test_a8_unredacted_json_files_are_flagged_false(self) -> None:
        (self.rt / "plain.json").write_text('{"a": 1}')
        res = create_backup(self.rt)
        man = json.loads(_read_member(res["archive"], "manifest.json"))
        self.assertFalse(man["secrets_redacted"])


class VersionTests(_Base):
    def test_d2_newer_major_minor_is_rejected_unless_forced(self) -> None:
        (self.rt / "k.json").write_text('{"a": 1}')
        arc = create_backup(self.rt)["archive"]
        for ver in ("99.0.0", "0.99.0", "1.0.0"):
            newer = self._with_version(arc, ver)
            fresh = self.tmp / ("f_" + ver)
            with self.assertRaises(ValueError) as cm:
                restore_backup(newer, fresh)
            self.assertIn("升级", str(cm.exception))
            self.assertFalse(fresh.exists() and any(fresh.iterdir()))
        res = restore_backup(self._with_version(arc, "99.0.0"), self.tmp / "forced", force=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["warnings"])

    def test_d2_newer_patch_and_same_version_are_accepted(self) -> None:
        (self.rt / "k.json").write_text('{"a": 1}')
        arc = create_backup(self.rt)["archive"]
        major, minor = __version__.split(".")[:2]
        self.assertTrue(restore_backup(self._with_version(arc, f"{major}.{minor}.99"), self.tmp / "p")["ok"])
        self.assertTrue(restore_backup(arc, self.tmp / "same")["ok"])

    def test_d2_missing_version_restores_with_notice(self) -> None:
        (self.rt / "k.json").write_text('{"a": 1}')
        arc = create_backup(self.rt)["archive"]
        res = restore_backup(self._with_version(arc, None), self.tmp / "old")
        self.assertTrue(res["ok"])
        self.assertTrue(any("version" in w or "版本" in w for w in res["warnings"]))

    def test_d2_cli_force_flag(self) -> None:
        (self.rt / "k.json").write_text('{"a": 1}')
        arc = create_backup(self.rt)["archive"]
        newer = self._with_version(arc, "99.0.0")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(["restore", str(newer), "--data-dir", str(self.tmp / "c1")]), 2)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(["restore", str(newer), "--data-dir", str(self.tmp / "c2"), "--force"]), 0)

    def _with_version(self, arc: str, version) -> Path:
        out = self.tmp / f"v_{version}.tar.gz"
        with tarfile.open(arc) as src, tarfile.open(out, "w:gz") as dst:
            for m in src.getmembers():
                data = src.extractfile(m).read()
                if m.name == "manifest.json":
                    mm = json.loads(data)
                    if version is None:
                        mm.pop("version", None)
                    else:
                        mm["version"] = version
                    data = json.dumps(mm).encode()
                    m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
        return out


class AtomicWriteAndMaskTests(_Base):
    def test_n9_symlinked_env_keeps_link_and_updates_real_file(self) -> None:
        real_dir = self.tmp / "real"
        real_dir.mkdir()
        real = real_dir / "real.env"
        real.write_text("OLD=1\n")
        link = self.tmp / ".env"
        os.symlink(real, link)
        atomic_write_private_text(link, "NEW=2\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "NEW=2\n")
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)

    def test_n9_replace_failure_falls_back_to_in_place_write(self) -> None:
        target = self.tmp / ".env"
        target.write_text("OLD=1\n")
        os.chmod(target, 0o644)
        ino = os.stat(target).st_ino
        for code in (errno.EBUSY, errno.EXDEV):
            with mock.patch("ai_house.onboarding.os.replace", side_effect=OSError(code, "boom")):
                atomic_write_private_text(target, f"NEW={code}\n")
            self.assertEqual(target.read_text(), f"NEW={code}\n")
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(target).st_ino, ino, "in-place fallback keeps the same inode (bind mount)")
            self.assertEqual([p.name for p in self.tmp.iterdir() if ".tmp" in p.name], [], "temp file cleaned up")

    def test_n9_in_place_fallback_tightens_mode_before_any_content_is_written(self) -> None:
        target = self.tmp / ".env"
        target.write_text("OLD=1\n")
        os.chmod(target, 0o644)
        ino = os.stat(target).st_ino
        seen: list = []
        real_fdopen = os.fdopen

        def spy(fd: int, *a: object, **kw: object):
            st = os.fstat(fd)
            if st.st_ino == ino:
                seen.append(st.st_mode & 0o777)  # mode at the moment the secret starts being written
            return real_fdopen(fd, *a, **kw)

        with mock.patch("ai_house.onboarding.os.replace", side_effect=OSError(errno.EBUSY, "boom")), \
                mock.patch("ai_house.onboarding.os.fdopen", side_effect=spy):
            atomic_write_private_text(target, "NEW=3\n")
        self.assertEqual(target.read_text(), "NEW=3\n")
        self.assertEqual(seen, [0o600], "a previously 0644 file must be 0600 before the new content goes in")

    def test_e8_mask_does_not_leak_substance(self) -> None:
        self.assertEqual(mask_secret(""), "")
        for n in (1, 8, 12, 13, 16, 19):
            val = "".join(chr(ord("a") + i % 26) for i in range(n))
            masked = mask_secret(val)
            self.assertEqual(set(masked), {"•"}, f"len {n} must be fully masked, got {masked!r}")
        key = "sk-test-fake-" + "Qx7Zp2Lm9Rt4"  # 25 chars
        masked = mask_secret(key)
        self.assertTrue(masked.endswith(key[-4:]))
        self.assertNotIn("Qx7Z", masked)
        self.assertNotIn("p2Lm", masked)
        visible = re.sub(r"[•…]", "", masked)
        self.assertLessEqual(len(visible), 3 + 4, masked)
        key13 = "abcdefghijklm"
        self.assertNotIn("abcd", mask_secret(key13))
        self.assertNotIn("jklm", mask_secret(key13))


class MiscBackupTests(_Base):
    def test_d2_user_supplied_out_dir_permissions_untouched_new_dir_is_0700(self) -> None:
        shared = self.tmp / "shared"
        shared.mkdir()
        os.chmod(shared, 0o755)
        create_backup(self.rt, out_dir=shared)
        self.assertEqual(os.stat(shared).st_mode & 0o777, 0o755)
        fresh_out = self.tmp / "newout"
        create_backup(self.rt, out_dir=fresh_out)
        self.assertEqual(os.stat(fresh_out).st_mode & 0o777, 0o700)
        create_backup(self.rt)
        self.assertEqual(os.stat(self.rt / "backups").st_mode & 0o777, 0o700)

    def test_d2_wal_shm_cleanup_is_rolled_back_when_restore_fails(self) -> None:
        (self.rt / "k.json").write_text('{"k": 1}')
        arc = create_backup(self.rt)["archive"]
        (self.rt / "life1.db-wal").write_bytes(b"unmerged-wal")
        (self.rt / "life1.db-shm").write_bytes(b"shm")
        (self.rt / "k.json").write_text('{"k": 2}')
        real_move = B.shutil.move
        count = {"n": 0}

        def flaky(src, dst, *a, **kw):
            if "restore_stage" in str(src):
                count["n"] += 1
                if count["n"] == 2:
                    raise OSError("disk full")
            return real_move(src, dst, *a, **kw)

        with mock.patch.object(B.shutil, "move", flaky):
            with self.assertRaises(OSError):
                restore_backup(arc, self.rt, overwrite=True)
        self.assertEqual((self.rt / "life1.db-wal").read_bytes(), b"unmerged-wal")
        self.assertEqual((self.rt / "life1.db-shm").read_bytes(), b"shm")
        self.assertEqual(json.loads((self.rt / "k.json").read_text()), {"k": 2})
        self.assertTrue((self.rt / "life1.db").is_file())

    def test_d3_size_hint_when_over_threshold_but_still_succeeds(self) -> None:
        (self.rt / "media").mkdir()
        (self.rt / "media" / "big.bin").write_bytes(os.urandom(200_000))
        res = create_backup(self.rt, size_warn_bytes=100_000)
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("warnings"))
        small = create_backup(self.rt, size_warn_bytes=10**12)
        self.assertFalse(small.get("warnings"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli_main(["backup", "--data-dir", str(self.rt), "--warn-size-mb", "0.05"])
        self.assertEqual(rc, 0)
        self.assertIn("体积", out.getvalue())

    def test_d2_cli_restore_refuses_with_clear_message_when_daemon_running(self) -> None:
        arc = create_backup(self.rt)["archive"]
        (self.rt / ".daemon.lock").write_text(json.dumps({"pid": os.getpid()}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli_main(["restore", arc, "--data-dir", str(self.rt), "--overwrite"])
        self.assertEqual(rc, 2)
        self.assertIn(str(os.getpid()), err.getvalue())
        self.assertIn("停止", err.getvalue())
        self.assertIn("未做任何改动", err.getvalue())

    def test_cli_help_documents_redaction_and_post_restore_behaviour(self) -> None:
        for cmd, needles in (("backup", ("脱敏",)), ("restore", ("脱敏", "重新填写", "保留"))):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
                cli_main([cmd, "--help"])
            text = re.sub(r"\s+", "", buf.getvalue())
            for n in needles:
                self.assertIn(n, text, f"{cmd} --help should mention {n}")


if __name__ == "__main__":
    unittest.main()
