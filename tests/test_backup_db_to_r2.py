"""Retention rules for the nightly R2 dump. No network and no database."""
import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup_db_to_r2.py"


def load_module():
    spec = importlib.util.spec_from_file_location("backup_db_to_r2", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = load_module()


def _name(stamp):
    return f"db-backups/gojo_{stamp}.sql.gz"


class BackupKeyTests(unittest.TestCase):
    def test_accepts_only_the_nightly_name(self):
        self.assertTrue(MOD.is_backup_object_key(_name("20261104_0315")))

    def test_rejects_media_prefixes_and_lookalikes(self):
        rejected = [
            "chat-media/gojo_20261104_0315.sql.gz",
            "stickers/gojo_20261104_0315.sql.gz",
            "db-backups/chat-media/gojo_20261104_0315.sql.gz",
            "db-backups/stickers/pack.sql.gz",
            "db-backups/gojo_20261104_0315.sql",
            "db-backups/gojo_20261104_0315.sql.gz.bak",
            "db-backups/gojo_20261104_031.sql.gz",
            "db-backups/other_20261104_0315.sql.gz",
            "gojo_20261104_0315.sql.gz",
            "",
            None,
        ]
        for key in rejected:
            with self.subTest(key=key):
                self.assertFalse(MOD.is_backup_object_key(key))

    def test_protected_keys_include_nested_media_prefixes(self):
        self.assertTrue(MOD.is_protected_key("chat-media/a.png"))
        self.assertTrue(MOD.is_protected_key("stickers/a.png"))
        self.assertTrue(MOD.is_protected_key("db-backups/chat-media/a.png"))
        self.assertTrue(MOD.is_protected_key("db-backups/stickers/a.png"))
        self.assertFalse(MOD.is_protected_key(_name("20261104_0315")))


class RetentionPlanTests(unittest.TestCase):
    def test_keeps_the_newest_fourteen(self):
        uploaded = _name("20261116_0315")
        listed = [_name(f"202611{day:02d}_0315") for day in range(1, 17)]
        delete_keys, ignored = MOD.retention_plan(listed, 14, uploaded)
        self.assertEqual(ignored, [])
        self.assertEqual(delete_keys, [
            _name("20261101_0315"),
            _name("20261102_0315"),
        ])
        self.assertNotIn(uploaded, delete_keys)

    def test_counts_the_new_key_when_the_list_has_not_caught_up(self):
        uploaded = _name("20261115_0315")
        listed = [_name(f"202611{day:02d}_0315") for day in range(1, 15)]
        delete_keys, _ignored = MOD.retention_plan(listed, 14, uploaded)
        self.assertEqual(delete_keys, [_name("20261101_0315")])
        self.assertNotIn(uploaded, delete_keys)

    def test_does_not_delete_the_new_key_when_its_name_sorts_oldest(self):
        uploaded = _name("20261101_0315")
        listed = [uploaded, _name("20261102_0315")]
        delete_keys, _ignored = MOD.retention_plan(listed, 1, uploaded)
        self.assertEqual(delete_keys, [])

    def test_aborts_when_a_media_key_is_listed(self):
        uploaded = _name("20261104_0315")
        for extra in ("chat-media/a.png", "stickers/a.png", "db-backups/stickers/a.png"):
            with self.subTest(extra=extra):
                with self.assertRaises(RuntimeError):
                    MOD.retention_plan([uploaded, extra], 14, uploaded)

    def test_ignores_unrelated_names_under_the_backup_prefix(self):
        uploaded = _name("20261104_0315")
        delete_keys, ignored = MOD.retention_plan(
            [uploaded, "db-backups/README.txt"],
            14,
            uploaded,
        )
        self.assertEqual(delete_keys, [])
        self.assertEqual(ignored, ["db-backups/README.txt"])

    def test_rejects_a_keep_count_below_one(self):
        with self.assertRaises(ValueError):
            MOD.retention_plan([], 0, _name("20261104_0315"))
        with self.assertRaises(ValueError):
            MOD.retention_plan([], True, _name("20261104_0315"))


class CommandTests(unittest.TestCase):
    def test_compose_command_does_not_carry_a_url(self):
        command = MOD.build_dump_command(
            "compose",
            user="gojo",
            database="gojo",
            database_url="postgresql://gojo:secret@db:5432/gojo",
            compose_file=ROOT / "deploy" / "docker-compose.yml",
            docker_bin="docker",
            pg_dump_bin="pg_dump",
        )
        text = " ".join(command)
        self.assertNotIn("secret", text)
        self.assertNotIn("postgresql://", text)
        self.assertIn("exec", command)
        self.assertIn("pg_dump", command)
        self.assertEqual(command[command.index("-U") + 1], "gojo")
        self.assertEqual(command[command.index("-d") + 1], "gojo")

    def test_pg_dump_command_redacts_the_url(self):
        url = "postgresql://gojo:secret@db:5432/gojo"
        command = MOD.build_dump_command(
            "pg_dump",
            user="gojo",
            database="gojo",
            database_url=url,
            compose_file=ROOT / "deploy" / "docker-compose.yml",
            docker_bin="docker",
            pg_dump_bin="pg_dump",
        )
        self.assertEqual(command[command.index("--dbname") + 1], url)
        shown = " ".join(MOD.redact_command(command))
        self.assertNotIn("secret", shown)
        self.assertNotIn(url, shown)
        self.assertIn("<redacted>", shown)

    def test_pg_dump_mode_rejects_a_non_url(self):
        with self.assertRaises(RuntimeError):
            MOD.build_dump_command(
                "pg_dump",
                user="gojo",
                database="gojo",
                database_url="db:5432",
                compose_file=ROOT / "deploy" / "docker-compose.yml",
                docker_bin="docker",
                pg_dump_bin="pg_dump",
            )

    def test_compose_mode_rejects_an_unsafe_identifier(self):
        with self.assertRaises(RuntimeError):
            MOD.build_dump_command(
                "compose",
                user="gojo;rm",
                database="gojo",
                database_url="",
                compose_file=ROOT / "deploy" / "docker-compose.yml",
                docker_bin="docker",
                pg_dump_bin="pg_dump",
            )


class EnvFileTests(unittest.TestCase):
    def test_loads_missing_keys_without_overriding_or_leaking_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "\n".join([
                    "# comment",
                    "export KEEP_ME=from-file",
                    "ALREADY=from-file",
                    'QUOTED="two words"',
                    "NOT_A_PAIR super-secret-value",
                ]),
                encoding="utf-8",
            )
            env = {"ALREADY": "from-process"}
            with self.assertRaises(RuntimeError) as caught:
                MOD.load_env_file(path, env)
            self.assertNotIn("super-secret-value", str(caught.exception))
            self.assertEqual(env["ALREADY"], "from-process")
            self.assertEqual(env["KEEP_ME"], "from-file")
            self.assertEqual(env["QUOTED"], "two words")


if __name__ == "__main__":
    unittest.main()
