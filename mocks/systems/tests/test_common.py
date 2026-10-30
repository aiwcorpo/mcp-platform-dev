import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402


class JournalTest(unittest.TestCase):
    def test_a_known_pat_is_logged_as_its_account_not_its_value(self):
        for header, value in (("Authorization", "Bearer alice-pat"), ("Private-Token", "bob-pat")):
            described = common.describe_auth(common.Request("GET", "/x", {header: value}, b""))
            self.assertNotIn("-pat", described)
            self.assertEqual(described, "pat:" + value.split()[-1].removesuffix("-pat"))

    def test_unknown_credentials_are_not_echoed(self):
        self.assertEqual(common.describe_auth(common.Request("GET", "/x", {"Private-Token": "secret-value"}, b"")), "private-token:unknown")
        self.assertEqual(common.describe_auth(common.Request("GET", "/x", {"Authorization": "Bearer secret-value"}, b"")), "bearer:unknown")


if __name__ == "__main__":
    unittest.main()
