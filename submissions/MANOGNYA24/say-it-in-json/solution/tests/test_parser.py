import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pfcfg.parser import parse_text  # noqa: E402


class TestCommentStripping(unittest.TestCase):
    """_strip_comment must distinguish a real trailing comment from a '#'
    that's literal interpolation text — notifications.pfcfg's
    `channel = ${SLACK_CHANNEL:-#builds}` is real corpus evidence of the
    latter. Getting this wrong either way is a silent-corruption risk:
    swallow the '#' inside interpolation and you truncate a real default
    value; refuse to strip trailing comments at all and a genuine
    `key = value # note` from a real customer config folds the comment
    text into the value instead of erroring loudly.
    """

    def _keys(self, text: str):
        parsed = parse_text("test.pfcfg", text)
        return {n.key: n.raw_value for n in parsed.nodes if hasattr(n, "key")}

    def test_hash_inside_interpolation_default_is_not_a_comment(self):
        keys = self._keys("[notify.slack]\nchannel = ${SLACK_CHANNEL:-#builds}\n")
        self.assertEqual(keys["channel"], "${SLACK_CHANNEL:-#builds}")

    def test_hash_inside_nested_key_ref_default_is_not_a_comment(self):
        keys = self._keys("[release]\nversion = ${RELEASE_VERSION:-0.0.0-$(build.node_version)}\n")
        self.assertEqual(keys["version"], "${RELEASE_VERSION:-0.0.0-$(build.node_version)}")

    def test_genuine_trailing_comment_is_stripped(self):
        keys = self._keys("[build]\ntimeout_minutes = 45  # default from ops\n")
        self.assertEqual(keys["timeout_minutes"], "45")

    def test_full_line_comment_is_ignored(self):
        keys = self._keys("[build]\n# a full-line comment\ntimeout_minutes = 45\n; another style\n")
        self.assertEqual(keys, {"timeout_minutes": "45"})

    def test_hash_inside_quotes_is_not_a_comment(self):
        keys = self._keys('[build]\nlabel = "release #1"\n')
        self.assertEqual(keys["label"], "release #1")


if __name__ == "__main__":
    unittest.main()
