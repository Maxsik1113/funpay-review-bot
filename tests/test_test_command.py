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
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


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


if __name__ == "__main__":
    unittest.main()
