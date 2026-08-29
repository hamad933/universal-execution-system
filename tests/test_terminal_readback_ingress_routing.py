from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TARGETED = ROOT / ".github/workflows/ues-targeted-project-terminal-readback.yml"
EXACT = ROOT / ".github/workflows/ues-exact-terminal-readback.yml"
FINDING = ROOT / ".github/workflows/ues-exact-terminal-finding-readback.yml"


class TerminalReadbackIngressRoutingTests(unittest.TestCase):
    def test_exact_alias_has_one_issue_comment_ingress(self):
        targeted = TARGETED.read_text(encoding="utf-8")
        exact = EXACT.read_text(encoding="utf-8")

        self.assertNotIn("ues-exact-terminal-readback", targeted)
        self.assertIn("/ues-terminal-readback", targeted)
        self.assertIn("/ues-exact-terminal-readback", exact)

    def test_non_owning_comment_ingresses_prefilter_before_authorize(self):
        targeted = TARGETED.read_text(encoding="utf-8")
        exact = EXACT.read_text(encoding="utf-8")
        finding = FINDING.read_text(encoding="utf-8")

        self.assertIn("startsWith(github.event.comment.body, '/ues-terminal-readback ')", targeted)
        self.assertIn(
            "startsWith(github.event.comment.body, '/ues-exact-terminal-readback ')",
            exact,
        )
        self.assertIn(
            "startsWith(github.event.comment.body, '/ues-exact-terminal-finding-readback ')",
            finding,
        )
        self.assertNotIn("startsWith(github.event.comment.body, '/ues-exact-terminal-readback ')", targeted)
        self.assertNotIn("startsWith(github.event.comment.body, '/ues-exact-terminal-readback ')", finding)

    def test_exact_owned_family_keeps_strict_fullmatch_parser(self):
        exact = EXACT.read_text(encoding="utf-8")

        self.assertIn("pattern.fullmatch(os.environ[\"COMMENT_BODY\"].strip())", exact)
        self.assertIn('raise SystemExit("exact owner command required")', exact)
        self.assertIn("github.event_name == 'workflow_dispatch'", exact)

    def test_both_routes_remain_get_only_terminal_readback(self):
        targeted = TARGETED.read_text(encoding="utf-8")
        exact = EXACT.read_text(encoding="utf-8")

        self.assertIn("python -m ues.terminal_backfill", targeted)
        self.assertIn("python -m ues.exact_terminal_readback", targeted)
        self.assertIn("python -m ues.exact_terminal_readback", exact)
        self.assertIn("safe_to_blind_retry", targeted)


if __name__ == "__main__":
    unittest.main()
