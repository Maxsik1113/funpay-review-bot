import sqlite3
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

funpay_stub = ModuleType("FunPayAPI")
funpay_stub.Account = object
funpay_stub.Runner = object
funpay_stub.enums = SimpleNamespace()
sys.modules.setdefault("FunPayAPI", funpay_stub)

import main


class FakeAccount:
    def __init__(self, chats=None):
        self.sent = []
        self.chats = chats or []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))

    def request_chats(self):
        return self.chats

    def add_chats(self, chats):
        return None


def make_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE processed_test_messages (
            message_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            processed_at INTEGER NOT NULL,
            PRIMARY KEY (message_id, chat_id)
        )
        """
    )
    return connection


def make_message(author="TestBuyer", text="!secret-check", message_id=1):
    return SimpleNamespace(
        author=author,
        chat_name=author,
        text=text,
        id=message_id,
        chat_id=123,
    )


class TestCommandTests(unittest.TestCase):
    def test_allowed_user_receives_one_safe_reply(self):
        account = FakeAccount()
        connection = make_connection()

        with patch.object(main, "TEST_ALLOWED_USER", "TestBuyer"), patch.object(
            main, "TEST_COMMAND", "!secret-check"
        ), patch.object(main, "DOWNLOAD_URL", "https://secret.invalid/mod.jar"):
            message = make_message()
            main.handle_test_command(account, connection, message)
            main.handle_test_command(account, connection, message)

        self.assertEqual(len(account.sent), 1)
        self.assertIn("TestBuyer", account.sent[0][1])
        self.assertNotIn("https://secret.invalid/mod.jar", account.sent[0][1])

    def test_wrong_user_or_command_is_ignored(self):
        account = FakeAccount()
        connection = make_connection()

        with patch.object(main, "TEST_ALLOWED_USER", "TestBuyer"), patch.object(
            main, "TEST_COMMAND", "!secret-check"
        ):
            main.handle_test_command(account, connection, make_message(author="Other"))
            main.handle_test_command(account, connection, make_message(text="hello"))

        self.assertEqual(account.sent, [])

    def test_chat_update_fallback_handles_command(self):
        account = FakeAccount()
        connection = make_connection()
        chat = SimpleNamespace(
            id=123,
            name="TestBuyer",
            last_message_text="!secret-check",
            node_msg_id=7,
            last_by_bot=False,
            last_by_vertex=False,
        )

        with patch.object(main, "TEST_ALLOWED_USER", "TestBuyer"), patch.object(
            main, "TEST_COMMAND", "!secret-check"
        ):
            main.handle_test_chat_update(account, connection, chat)
            main.handle_test_chat_update(account, connection, chat)

        self.assertEqual(len(account.sent), 1)

    def test_chat_update_ignores_bot_message(self):
        account = FakeAccount()
        connection = make_connection()
        chat = SimpleNamespace(
            id=123,
            name="TestBuyer",
            last_message_text="!secret-check",
            node_msg_id=8,
            last_by_bot=True,
            last_by_vertex=False,
        )

        with patch.object(main, "TEST_ALLOWED_USER", "TestBuyer"), patch.object(
            main, "TEST_COMMAND", "!secret-check"
        ):
            main.handle_test_chat_update(account, connection, chat)

        self.assertEqual(account.sent, [])

    def test_startup_scan_handles_existing_command(self):
        chat = SimpleNamespace(
            id=123,
            name="TestBuyer",
            last_message_text="  !SECRET-CHECK  ",
            node_msg_id=9,
            last_by_bot=False,
            last_by_vertex=False,
        )
        account = FakeAccount([chat])
        connection = make_connection()

        with patch.object(main, "TEST_ALLOWED_USER", "testbuyer"), patch.object(
            main, "TEST_COMMAND", "!secret-check"
        ):
            main.scan_test_chat(account, connection)

        self.assertEqual(len(account.sent), 1)


if __name__ == "__main__":
    unittest.main()
