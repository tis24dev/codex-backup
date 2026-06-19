from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
BACKUP_AUTH = PROJECT_DIR / "backup_auth.py"
INSTALL = PROJECT_DIR / "install.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Impossibile caricare {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def jwt_with_exp(exp: int) -> str:
    header = b64url(json.dumps({"alg": "none"}).encode("utf-8"))
    body = b64url(
        json.dumps(
            {
                "exp": exp,
                "sub": "user-test",
                "email": "user@example.test",
                "iat": 1,
            }
        ).encode("utf-8")
    )
    return f"{header}.{body}.sig"


class BackupAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_module(BACKUP_AUTH, f"backup_auth_test_{id(self)}")
        self.previous_env = {
            "CODEX_AUTH_PATH": os.environ.get("CODEX_AUTH_PATH"),
            "CODEX_BACKUP_VERIFY_CODEX": os.environ.get("CODEX_BACKUP_VERIFY_CODEX"),
            "CODEX_BACKUP_VERIFY_TIMEOUT": os.environ.get("CODEX_BACKUP_VERIFY_TIMEOUT"),
            "CODEX_BACKUP_CODEX_BIN": os.environ.get("CODEX_BACKUP_CODEX_BIN"),
            "CODEX_BACKUP_RESTORE_CAPACITY": os.environ.get("CODEX_BACKUP_RESTORE_CAPACITY"),
            "CODEX_BACKUP_RESTORE_CAPACITY_TIMEOUT": os.environ.get("CODEX_BACKUP_RESTORE_CAPACITY_TIMEOUT"),
            "CODEX_HOME": os.environ.get("CODEX_HOME"),
        }
        os.environ["CODEX_BACKUP_VERIFY_CODEX"] = "off"
        os.environ["CODEX_BACKUP_RESTORE_CAPACITY"] = "off"
        self.mod._DEBUG_LEVEL = 0
        self.mod.BACKUP_BASE = self.root / "data_backup"
        self.mod.LEGACY_BACKUP_BASES = ()

    def tearDown(self) -> None:
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write_auth(self, content: dict) -> Path:
        auth_dir = self.root / "codex"
        auth_dir.mkdir()
        auth_path = auth_dir / "auth.json"
        auth_path.write_text(json.dumps(content), encoding="utf-8")
        os.environ["CODEX_AUTH_PATH"] = str(auth_path)
        return auth_path

    def test_make_backup_creates_metadata_and_detects_duplicate(self) -> None:
        self.write_auth(
            {
                "email": "user@example.test",
                "account_id": "acct-test",
                "tokens": {
                    "id_token": jwt_with_exp(1893456000),
                    "access_token": jwt_with_exp(1893542400),
                },
            }
        )

        target = self.mod.make_backup()

        self.assertTrue(target.is_dir())
        self.assertTrue((target / "auth.json").is_file())
        self.assertTrue((target / "meta.json").is_file())
        self.assertTrue(target.name.startswith("backup00-"))
        self.assertIn("_exp-2030-01-02", target.name)
        meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(
            meta["auth_snapshot"]["backup_expiry"],
            meta["extracted"]["backup_expiry"],
        )
        self.assertEqual(meta["auth_snapshot"]["backup_expiry"]["token_type"], "access_token")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((target / "auth.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((target / "meta.json").stat().st_mode), 0o600)

        with self.assertRaises(self.mod.DuplicateBackupError) as exc:
            self.mod.make_backup()
        self.assertEqual(exc.exception.backup_dir, target)
        self.assertIn(str(target), str(exc.exception))

    def test_folder_name_recomputes_saved_backup_expiry_from_jwt_tokens(self) -> None:
        meta = {
            "analyzed_at": "2020-01-01T00:00:00+00:00",
            "extracted": {
                "backup_expiry": {
                    "exp": "1577923200",
                    "expires_at_utc": "2020-01-02T00:00:00+00:00",
                    "path": "tokens.id_token",
                    "token_type": "id_token",
                },
                "jwt_tokens": [
                    {
                        "exp": "1893542400",
                        "expires_at_utc": "2030-01-02T00:00:00+00:00",
                        "path": "tokens.access_token",
                        "token_type": "access_token",
                    }
                ],
            },
        }

        name = self.mod._build_backup_folder_name(
            meta,
            datetime(2026, 6, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(name, "backup00-2026-06-15_exp-2030-01-02")

    def test_folder_name_uses_auth_snapshot_expiry_when_present(self) -> None:
        meta = {
            "analyzed_at": "2020-01-01T00:00:00+00:00",
            "auth_snapshot": {
                "backup_expiry": {
                    "exp": "1893628800",
                    "expires_at_utc": "2030-01-03T00:00:00+00:00",
                    "path": "tokens.access_token",
                    "source": "analyze_preferred",
                    "token_type": "access_token",
                },
            },
            "extracted": {
                "backup_expiry": {
                    "exp": "1893542400",
                    "expires_at_utc": "2030-01-02T00:00:00+00:00",
                    "path": "tokens.access_token",
                    "token_type": "access_token",
                },
            },
        }

        name = self.mod._build_backup_folder_name(
            meta,
            datetime(2026, 6, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(name, "backup00-2026-06-15_exp-2030-01-03")

    def test_folder_name_uses_stored_backup_expiry_without_jwt_tokens(self) -> None:
        meta = {
            "analyzed_at": "2020-01-01T00:00:00+00:00",
            "extracted": {
                "backup_expiry": {
                    "exp": "1577923200",
                    "expires_at_utc": "2020-01-02T00:00:00+00:00",
                    "path": "tokens.id_token",
                    "token_type": "id_token",
                },
            },
        }

        name = self.mod._build_backup_folder_name(
            meta,
            datetime(2026, 6, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(name, "backup00-2026-06-15_exp-2020-01-02")

    def test_folder_name_prefers_access_token_over_short_lived_id_token(self) -> None:
        meta = {
            "analyzed_at": "2020-01-01T00:00:00+00:00",
            "extracted": {
                "jwt_tokens": [
                    {
                        "exp": "1577923200",
                        "expires_at_utc": "2020-01-02T00:00:00+00:00",
                        "path": "tokens.id_token",
                        "token_type": "id_token",
                    },
                    {
                        "exp": "1893542400",
                        "expires_at_utc": "2030-01-02T00:00:00+00:00",
                        "path": "tokens.access_token",
                        "token_type": "access_token",
                    },
                ]
            },
        }

        name = self.mod._build_backup_folder_name(
            meta,
            datetime(2026, 6, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(name, "backup00-2026-06-15_exp-2030-01-02")

    def test_migrate_new_name_repairs_saved_backup_expiry_metadata(self) -> None:
        backup_root = self.mod.BACKUP_BASE
        backup_root.mkdir()
        wrong_dir = backup_root / "backup00-2026-06-15_exp-2026-06-15"
        wrong_dir.mkdir()
        (wrong_dir / "auth.json").write_text('{"ok": true}\n', encoding="utf-8")
        meta = {
            "analyzed_at": "2026-06-15T09:53:32+00:00",
            "extracted": {
                "backup_expiry": {
                    "exp": "1781518356",
                    "expires_at_utc": "2026-06-15T10:12:36+00:00",
                    "path": "tokens.id_token",
                    "source": "analyze_preferred",
                    "token_type": "id_token",
                },
                "jwt_tokens": [
                    {
                        "exp": "1781518356",
                        "expires_at_utc": "2026-06-15T10:12:36+00:00",
                        "path": "tokens.id_token",
                        "token_type": "id_token",
                    },
                    {
                        "exp": "1782378756",
                        "expires_at_utc": "2026-06-25T09:12:36+00:00",
                        "path": "tokens.access_token",
                        "token_type": "access_token",
                    },
                ],
            },
        }
        (wrong_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        scanned, migrated = self.mod._migrate_backup_names(backup_root)

        repaired_dir = backup_root / "backup00-2026-06-15_exp-2026-06-25"
        self.assertEqual((scanned, migrated), (1, 1))
        self.assertFalse(wrong_dir.exists())
        self.assertTrue(repaired_dir.is_dir())
        repaired_meta = json.loads((repaired_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(repaired_meta["extracted"]["backup_expiry"]["token_type"], "access_token")
        self.assertEqual(repaired_meta["auth_snapshot"]["backup_expiry"]["token_type"], "access_token")
        self.assertEqual(
            repaired_meta["auth_snapshot"]["backup_expiry"],
            repaired_meta["extracted"]["backup_expiry"],
        )
        self.assertEqual(
            repaired_meta["extracted"]["backup_expiry"]["expires_at_utc"],
            "2026-06-25T09:12:36+00:00",
        )
        self.assertFalse(self.mod._refresh_backup_expiry_metadata(repaired_meta))

    def test_codex_status_verification_uses_isolated_home_for_custom_auth(self) -> None:
        auth_path = self.write_auth({"email": "user@example.test"})
        os.environ["CODEX_BACKUP_VERIFY_CODEX"] = "status"
        os.environ["CODEX_BACKUP_VERIFY_TIMEOUT"] = "12"
        original_run = self.mod.subprocess.run
        calls = []

        def fake_run(command: list[str], **kwargs):
            calls.append((command, kwargs))
            codex_home = Path(kwargs["env"]["CODEX_HOME"])
            self.assertTrue((codex_home / "auth.json").is_file())
            self.assertNotEqual(codex_home, auth_path.parent.resolve())
            return self.mod.subprocess.CompletedProcess(
                command,
                0,
                "Logged in using ChatGPT\n",
                "",
            )

        self.mod.subprocess.run = fake_run
        try:
            result = self.mod._verify_codex_auth_for_file(auth_path, "backup")
        finally:
            self.mod.subprocess.run = original_run

        self.assertEqual(result["mode"], "status")
        self.assertEqual(result["context"], "backup")
        self.assertEqual(Path(calls[0][0][0]).name, "codex")
        self.assertEqual(calls[0][0][1:], ["login", "status"])
        self.assertEqual(calls[0][1]["timeout"], 12.0)
        self.assertFalse(Path(calls[0][1]["env"]["CODEX_HOME"]).exists())

    def test_codex_live_verification_parses_jsonl_success(self) -> None:
        auth_path = self.write_auth({"email": "user@example.test"})
        os.environ["CODEX_BACKUP_VERIFY_CODEX"] = "live"
        original_run = self.mod.subprocess.run
        calls = []
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "CODEX_AUTH_OK",
                },
            }
        )

        def fake_run(command: list[str], **kwargs):
            calls.append((command, kwargs))
            self.assertEqual(kwargs["stdin"], self.mod.subprocess.DEVNULL)
            return self.mod.subprocess.CompletedProcess(command, 0, stdout + "\n", "")

        self.mod.subprocess.run = fake_run
        try:
            result = self.mod._verify_codex_auth_for_file(auth_path, "backup")
        finally:
            self.mod.subprocess.run = original_run

        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["detail"], "codex exec completed an authenticated request")
        self.assertEqual(Path(calls[0][0][0]).name, "codex")
        self.assertEqual(calls[0][0][1:4], ["--ask-for-approval", "never", "exec"])
        self.assertIn("--ephemeral", calls[0][0])
        self.assertIn("--json", calls[0][0])

    def test_make_backup_records_codex_verification_metadata(self) -> None:
        self.write_auth(
            {
                "email": "user@example.test",
                "account_id": "acct-test",
                "tokens": {
                    "id_token": jwt_with_exp(1893456000),
                    "access_token": jwt_with_exp(1893542400),
                },
            }
        )
        os.environ["CODEX_BACKUP_VERIFY_CODEX"] = "status"
        original_run = self.mod.subprocess.run

        def fake_run(command: list[str], **kwargs):
            return self.mod.subprocess.CompletedProcess(
                command,
                0,
                "Logged in using ChatGPT\n",
                "",
            )

        self.mod.subprocess.run = fake_run
        try:
            target = self.mod.make_backup()
        finally:
            self.mod.subprocess.run = original_run

        meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
        snapshot = meta["auth_snapshot"]
        self.assertEqual(snapshot["codex_auth_verification"]["mode"], "status")
        self.assertEqual(snapshot["codex_auth_verification"]["context"], "backup")
        self.assertTrue(snapshot["codex_auth_verification"]["ok"])
        self.assertTrue(snapshot["codex_valid"])
        self.assertEqual(snapshot["backup_expiry"], meta["extracted"]["backup_expiry"])

    def test_restore_aborts_when_codex_verification_fails(self) -> None:
        backup_dir = self.root / "backup"
        backup_dir.mkdir()
        source = backup_dir / "auth.json"
        source.write_text('{"ok": true}\n', encoding="utf-8")
        destination = self.root / "auth.json"
        destination.write_text("old\n", encoding="utf-8")
        os.environ["CODEX_BACKUP_VERIFY_CODEX"] = "status"
        original_run = self.mod.subprocess.run

        def fake_run(command: list[str], **kwargs):
            return self.mod.subprocess.CompletedProcess(command, 1, "Not logged in\n", "")

        self.mod.subprocess.run = fake_run
        try:
            with self.assertRaises(self.mod.CodexAuthVerificationError):
                self.mod._restore_backup_entry(backup_dir, destination)
        finally:
            self.mod.subprocess.run = original_run

        self.assertEqual(destination.read_text(encoding="utf-8"), "old\n")

    def test_restore_is_atomic_and_cleans_temp_on_hash_mismatch(self) -> None:
        backup_dir = self.root / "backup"
        backup_dir.mkdir()
        source = backup_dir / "auth.json"
        source.write_text('{"ok": true}\n', encoding="utf-8")

        destination_dir = self.root / "dest"
        destination_dir.mkdir()
        destination = destination_dir / "auth.json"
        destination.write_text("old\n", encoding="utf-8")

        result = self.mod._restore_backup_entry(backup_dir, destination)
        self.assertEqual(result, destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), '{"ok": true}\n')
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(list(destination_dir.glob(".auth.json.restore-*.tmp")), [])

        destination.write_text("old-again\n", encoding="utf-8")
        original_copy2 = self.mod.shutil.copy2

        def corrupt_copy(_source: Path, target: Path) -> Path:
            Path(target).write_text("corrupt\n", encoding="utf-8")
            return Path(target)

        self.mod.shutil.copy2 = corrupt_copy
        try:
            with self.assertRaises(RuntimeError):
                self.mod._restore_backup_entry(backup_dir, destination)
        finally:
            self.mod.shutil.copy2 = original_copy2

        self.assertEqual(destination.read_text(encoding="utf-8"), "old-again\n")
        self.assertEqual(list(destination_dir.glob(".auth.json.restore-*.tmp")), [])

    def test_codex_capacity_parser_reads_remaining_and_used_percent(self) -> None:
        self.assertEqual(
            self.mod._parse_codex_capacity_percent("usage remaining: 73%"),
            73,
        )
        self.assertEqual(
            self.mod._parse_codex_capacity_percent("plan used 90%"),
            10,
        )
        self.assertEqual(
            self.mod._parse_codex_capacity_percent('{"available_percent":0.42}'),
            42,
        )

    def test_codex_capacity_parser_prefers_5h_window(self) -> None:
        self.assertEqual(
            self.mod._parse_codex_capacity_percent("weekly remaining: 91%\n5h remaining: 37%"),
            37,
        )
        self.assertEqual(
            self.mod._parse_codex_capacity_percent("weekly used: 10%\n5h used: 63%"),
            37,
        )
        self.assertEqual(
            self.mod._parse_codex_capacity_percent(
                json.dumps(
                    {
                        "limits": [
                            {"window": "weekly", "available_percent": 91},
                            {"window": "5h", "available_percent": 37},
                        ]
                    }
                )
            ),
            37,
        )

    def test_codex_capacity_parser_reads_codex_rate_limits_primary(self) -> None:
        reset_epoch = 1781450295
        event = {
            "type": "event_msg",
            "payload": {"type": "token_count"},
            "rate_limits": {
                "primary": {
                    "used_percent": 84.0,
                    "window_minutes": 300,
                    "resets_at": reset_epoch,
                },
                "secondary": {
                    "used_percent": 60.0,
                    "window_minutes": 10080,
                    "resets_at": 1782001083,
                },
            },
        }
        output = json.dumps(event)

        detail = self.mod._parse_codex_5h_capacity(output)

        self.assertIsNotNone(detail)
        self.assertEqual(detail["available_percent"], 16)
        self.assertEqual(detail["used_percent"], 84)
        self.assertEqual(detail["window_minutes"], 300)
        self.assertEqual(
            detail["reset_at"],
            datetime.fromtimestamp(reset_epoch, tz=timezone.utc).isoformat(),
        )
        self.assertEqual(self.mod._parse_codex_capacity_percent(output), 16)

    def test_codex_capacity_check_live_reads_5h_rate_limits(self) -> None:
        auth_path = self.write_auth({"email": "user@example.test", "account_id": "acct-test"})
        os.environ["CODEX_BACKUP_RESTORE_CAPACITY"] = "live"
        os.environ["CODEX_BACKUP_RESTORE_CAPACITY_TIMEOUT"] = "13"
        original_run = self.mod.subprocess.run
        calls = []
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "CODEX_AUTH_OK"},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "token_count"},
                        "rate_limits": {
                            "primary": {
                                "used_percent": 84.0,
                                "window_minutes": 300,
                                "resets_at": 1781450295,
                            }
                        },
                    }
                ),
            ]
        )

        def fake_run(command: list[str], **kwargs):
            calls.append((command, kwargs))
            return self.mod.subprocess.CompletedProcess(command, 0, stdout + "\n", "")

        self.mod.subprocess.run = fake_run
        try:
            result = self.mod._check_codex_capacity_for_auth(auth_path)
        finally:
            self.mod.subprocess.run = original_run

        self.assertTrue(result["ok"])
        self.assertEqual(result["available_percent"], 16)
        self.assertEqual(result["used_percent"], 84)
        self.assertEqual(result["window_minutes"], 300)
        self.assertEqual(result["source"], "rate_limits.primary")
        self.assertEqual(calls[0][0][1:4], ["--ask-for-approval", "never", "exec"])
        self.assertEqual(calls[0][1]["timeout"], 13.0)

    def test_codex_capacity_check_live_reads_rate_limits_from_temp_session(self) -> None:
        auth_path = self.write_auth({"email": "user@example.test", "account_id": "acct-test"})
        os.environ["CODEX_BACKUP_RESTORE_CAPACITY"] = "live"
        original_run = self.mod.subprocess.run
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "CODEX_AUTH_OK"},
            }
        )

        def fake_run(command: list[str], **kwargs):
            codex_home = Path(kwargs["env"]["CODEX_HOME"])
            session_dir = codex_home / "sessions" / "2026" / "06" / "15"
            session_dir.mkdir(parents=True)
            (session_dir / "rollout-test.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-15T12:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "token_count"},
                        "rate_limits": {
                            "primary": {
                                "used_percent": 72.0,
                                "window_minutes": 300,
                                "resets_at": 1781450295,
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return self.mod.subprocess.CompletedProcess(command, 0, stdout + "\n", "")

        self.mod.subprocess.run = fake_run
        try:
            result = self.mod._check_codex_capacity_for_auth(auth_path)
        finally:
            self.mod.subprocess.run = original_run

        self.assertTrue(result["ok"])
        self.assertEqual(result["available_percent"], 28)
        self.assertEqual(result["used_percent"], 72)
        self.assertEqual(result["window_minutes"], 300)
        self.assertEqual(result["source"], "session.rate_limits.primary")

    def test_codex_capacity_check_live_reports_network_failure(self) -> None:
        auth_path = self.write_auth({"email": "user@example.test", "account_id": "acct-test"})
        os.environ["CODEX_BACKUP_RESTORE_CAPACITY"] = "live"
        original_run = self.mod.subprocess.run
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started"}),
                json.dumps(
                    {
                        "type": "error",
                        "message": "error sending request for url (https://chatgpt.com/backend-api/codex/responses)",
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.failed",
                        "error": {"message": "Operation not permitted (os error 1)"},
                    }
                ),
            ]
        )

        def fake_run(command: list[str], **kwargs):
            return self.mod.subprocess.CompletedProcess(command, 1, stdout + "\n", "")

        self.mod.subprocess.run = fake_run
        try:
            result = self.mod._check_codex_capacity_for_auth(auth_path)
        finally:
            self.mod.subprocess.run = original_run

        self.assertFalse(result["ok"])
        self.assertIsNone(result["available_percent"])
        self.assertEqual(result["label"], "network")
        self.assertIn("Operation not permitted", result["error"])

    def test_codex_capacity_check_uses_recent_default_session_for_same_account(self) -> None:
        auth_path = self.write_auth({"email": "user@example.test", "account_id": "acct-test"})
        default_home = self.root / "default_codex_home"
        session_dir = default_home / "sessions" / "2026" / "06" / "15"
        session_dir.mkdir(parents=True)
        (default_home / "auth.json").write_text(
            json.dumps({"email": "user@example.test", "account_id": "acct-test"}),
            encoding="utf-8",
        )
        (session_dir / "rollout-current.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "type": "event_msg",
                    "payload": {"type": "token_count"},
                    "rate_limits": {
                        "primary": {
                            "used_percent": 59.0,
                            "window_minutes": 300,
                            "resets_at": 1781450295,
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["CODEX_HOME"] = str(default_home)
        os.environ["CODEX_BACKUP_RESTORE_CAPACITY"] = "live"
        original_run = self.mod.subprocess.run

        def forbidden_run(command: list[str], **kwargs):
            raise AssertionError("recent local rate-limit should avoid live subprocess")

        self.mod.subprocess.run = forbidden_run
        try:
            result = self.mod._check_codex_capacity_for_auth(auth_path)
        finally:
            self.mod.subprocess.run = original_run

        self.assertTrue(result["ok"])
        self.assertEqual(result["available_percent"], 41)
        self.assertEqual(result["used_percent"], 59)
        self.assertEqual(result["window_minutes"], 300)
        self.assertEqual(result["source"], "default_session.rate_limits.primary")

    def test_codex_capacity_label_falls_back_to_ok_and_fail_states(self) -> None:
        self.assertEqual(
            self.mod._format_codex_capacity_label({"available_percent": 31, "ok": True}),
            "31%",
        )
        self.assertEqual(
            self.mod._format_codex_capacity_label({"available_percent": None, "ok": True}),
            "unknown",
        )
        self.assertEqual(
            self.mod._format_codex_capacity_label({"available_percent": None, "ok": False, "label": "timeout"}),
            "timeout",
        )

    def test_codex_capacity_annotation_reuses_account_cache(self) -> None:
        os.environ["CODEX_BACKUP_RESTORE_CAPACITY"] = "live"
        backup_dirs = []
        for index in range(2):
            backup_dir = self.root / f"backup{index}"
            backup_dir.mkdir()
            (backup_dir / "auth.json").write_text(
                json.dumps(
                    {
                        "email": "user@example.test",
                        "account_id": "acct-test",
                        "tokens": {"account_id": "acct-test"},
                    }
                ),
                encoding="utf-8",
            )
            backup_dirs.append(backup_dir)

        calls = []
        original_check = self.mod._check_codex_capacity_for_auth

        def fake_check(auth_path: Path) -> dict:
            calls.append(auth_path)
            return {"available_percent": 31, "ok": True, "mode": "live"}

        backups = [{"dir": backup_dir} for backup_dir in backup_dirs]
        self.mod._check_codex_capacity_for_auth = fake_check
        try:
            self.mod._annotate_backups_with_codex_capacity(backups)
        finally:
            self.mod._check_codex_capacity_for_auth = original_check

        self.assertEqual(len(calls), 1)
        self.assertEqual([item["capacity"] for item in backups], ["31%", "31%"])


class InstallTests(unittest.TestCase):
    def load_install(self, name: str = "install_test"):
        return load_module(INSTALL, f"{name}_{id(self)}")

    def test_debug_is_off_by_default_and_enabled_by_flag(self) -> None:
        old_debug = os.environ.pop("CODEX_BACKUP_DEBUG", None)
        try:
            install = self.load_install("install_debug_default")
            self.assertFalse(install.DEBUG)
            install._configure_debug(["-v"])
            self.assertTrue(install.DEBUG)
        finally:
            if old_debug is not None:
                os.environ["CODEX_BACKUP_DEBUG"] = old_debug

    def test_filter_existing_entries_detects_canonical_without_rewrite(self) -> None:
        install = self.load_install("install_filter_canonical")
        tag = "# codex-backup hourly auth backup"
        command = "0 * * * * cd /opt/codex-backup && /opt/codex-backup/run-backup.sh >> /opt/codex-backup/codex-backup.log 2>&1"
        lines = ["00 02 * * * /usr/local/bin/proxsave", "", tag, command]

        cleaned, removed_other, had_canonical = install._filter_existing_entries(
            lines,
            tag,
            command,
            "/opt/codex-backup/run-backup.sh",
        )

        self.assertEqual(cleaned, ["00 02 * * * /usr/local/bin/proxsave"])
        self.assertFalse(removed_other)
        self.assertTrue(had_canonical)

    def test_filter_existing_entries_removes_duplicate_and_legacy_entries(self) -> None:
        install = self.load_install("install_filter_duplicates")
        tag = "# codex-backup hourly auth backup"
        command = "0 * * * * cd /opt/codex-backup && /opt/codex-backup/run-backup.sh >> /opt/codex-backup/codex-backup.log 2>&1"
        lines = [
            "00 02 * * * /usr/local/bin/proxsave",
            "",
            tag,
            command,
            "",
            tag,
            command,
            "15 * * * * /old/codex-backup/run-backup.sh",
        ]

        cleaned, removed_other, had_canonical = install._filter_existing_entries(
            lines,
            tag,
            command,
            "/opt/codex-backup/run-backup.sh",
        )

        self.assertEqual(cleaned, ["00 02 * * * /usr/local/bin/proxsave"])
        self.assertTrue(removed_other)
        self.assertTrue(had_canonical)


if __name__ == "__main__":
    unittest.main()
