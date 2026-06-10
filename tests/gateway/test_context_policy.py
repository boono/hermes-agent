import tempfile
import unittest
from pathlib import Path

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.context_policy import (
    build_domain_context_prompt,
    classify_source_domain,
    render_domain_context_prompt,
    resolve_canonical_identity,
    resolve_context_policy,
    should_skip_builtin_memory,
)


POLICY_YAML = """
version: 1
top_domains: [work, life, personal-research]
subtopics:
  milk-tea:
    domain: personal-research
  project-internal:
    domain: work
  partner-safe:
    domain: life
platform_layers:
  wiki:
    kind: platform-wide-knowledge-layer
  ops:
    kind: platform-wide-operations-layer
  public:
    kind: limited-external-sharing-layer
platform_defaults:
  feishu:
    default_external_domain: work
    proactive_external_domains: [work]
  weixin:
    default_external_domain: null
    proactive_external_domains: [work, life, personal-research]
identities:
  owner:
    role: owner
    aliases: [weixin:user-1, user-1, feishu:feishu-user]
    allowed_domains: [work, life, personal-research]
    can_read_cross_platform_context: true
  partner:
    role: partner
    aliases: []
    allowed_domains: [life]
    can_read_cross_platform_context: false
  unknown:
    role: external
    aliases: []
    allowed_domains: []
    allowed_layers: [public]
    can_read_cross_platform_context: false
    current_session_only: true
"""


class ContextPolicyTest(unittest.TestCase):
    def _home(self):
        td = tempfile.TemporaryDirectory()
        home = Path(td.name)
        (home / "identity_policy.yaml").write_text(POLICY_YAML, encoding="utf-8")
        ctx = home / "active_context" / "owner"
        ctx.mkdir(parents=True)
        (ctx / "work.md").write_text("---\ndomain: work\n---\n# Work\nWORK_ONLY", encoding="utf-8")
        (ctx / "life.md").write_text("---\ndomain: life\n---\n# Life\nLIFE_ONLY", encoding="utf-8")
        (ctx / "personal-research.md").write_text("---\ndomain: personal-research\n---\n# PR\nPR_ONLY\n奶茶研究", encoding="utf-8")
        xh = home / "active_context" / "partner"
        xh.mkdir(parents=True)
        (xh / "life.md").write_text("---\ndomain: life\n---\n# Life\nPARTNER_SAFE", encoding="utf-8")
        self.addCleanup(td.cleanup)
        return home

    def test_feishu_defaults_to_work_even_for_milk_tea_without_explicit_request(self):
        home = self._home()
        source = SessionSource(platform=Platform.FEISHU, chat_id="c", chat_type="dm", user_id="u")

        decision = resolve_context_policy(
            source=source,
            message="奶茶品牌库继续更新",
            hermes_home=home,
            canonical_identity="owner",
        )

        self.assertEqual(decision.identity, "owner")
        self.assertEqual(decision.topic_domain, "personal-research")
        self.assertEqual(decision.inject_domains, ["work"])
        self.assertEqual(decision.proactive_external_domains, ["work"])
        self.assertTrue(should_skip_builtin_memory(decision))

    def test_weixin_classifies_milk_tea_as_personal_research(self):
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="user-1")

        self.assertEqual(classify_source_domain("喜茶和霸王茶姬菜单抓取", source), "personal-research")

    def test_partner_identity_only_injects_life(self):
        home = self._home()
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="partner-1", user_name="Partner")

        decision = resolve_context_policy(
            source=source,
            message="今天家里怎么安排",
            hermes_home=home,
            canonical_identity="partner",
        )
        prompt = render_domain_context_prompt(decision, hermes_home=home)

        self.assertEqual(decision.inject_domains, ["life"])
        self.assertFalse(decision.can_read_cross_platform_context)
        self.assertIn("PARTNER_SAFE", prompt)
        self.assertNotIn("LIFE_ONLY", prompt)

    def test_unknown_identity_gets_public_layer_not_top_domain(self):
        home = self._home()
        source = SessionSource(platform=Platform.API_SERVER, chat_id="external", chat_type="dm", user_id="external")

        decision = resolve_context_policy(
            source=source,
            message="介绍一下你自己",
            hermes_home=home,
            canonical_identity="unknown",
        )

        self.assertEqual(decision.inject_domains, [])
        self.assertEqual(decision.allowed_layers, ["public"])
        self.assertFalse(decision.can_read_cross_platform_context)
        self.assertTrue(should_skip_builtin_memory(decision))

    def test_render_prompt_only_reads_selected_active_context_file(self):
        home = self._home()
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="user-1")
        decision = resolve_context_policy(
            source=source,
            message="奶茶品牌库继续更新",
            hermes_home=home,
            canonical_identity="owner",
        )

        prompt = render_domain_context_prompt(decision, hermes_home=home)

        self.assertIn("PR_ONLY", prompt)
        self.assertIn("domain: personal-research", prompt)
        self.assertNotIn("WORK_ONLY", prompt)
        self.assertNotIn("LIFE_ONLY", prompt)

    def test_expired_active_context_is_not_injected(self):
        home = self._home()
        path = home / "active_context" / "owner" / "personal-research.md"
        path.write_text(
            "---\ndomain: personal-research\nexpires_at: 2000-01-01T00:00:00+00:00\n---\nEXPIRED_PR",
            encoding="utf-8",
        )
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="user-1")
        decision = resolve_context_policy(
            source=source,
            message="奶茶品牌库继续更新",
            hermes_home=home,
            canonical_identity="owner",
        )

        prompt = render_domain_context_prompt(decision, hermes_home=home)

        self.assertEqual(prompt, "")

    def test_active_context_allowed_identities_are_enforced(self):
        home = self._home()
        path = home / "active_context" / "owner" / "personal-research.md"
        path.write_text(
            "---\ndomain: personal-research\nallowed_identities: [partner]\n---\nWRONG_IDENTITY_PR",
            encoding="utf-8",
        )
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="user-1")
        decision = resolve_context_policy(
            source=source,
            message="奶茶品牌库继续更新",
            hermes_home=home,
            canonical_identity="owner",
        )

        prompt = render_domain_context_prompt(decision, hermes_home=home)

        self.assertEqual(prompt, "")

    def test_active_context_domain_frontmatter_must_match_file_domain(self):
        home = self._home()
        path = home / "active_context" / "owner" / "personal-research.md"
        path.write_text(
            "---\ndomain: life\n---\nWRONG_DOMAIN_PR",
            encoding="utf-8",
        )
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="user-1")
        decision = resolve_context_policy(
            source=source,
            message="奶茶品牌库继续更新",
            hermes_home=home,
            canonical_identity="owner",
        )

        prompt = render_domain_context_prompt(decision, hermes_home=home)

        self.assertEqual(prompt, "")

    def test_active_context_visibility_is_rendered_as_boundary_metadata(self):
        home = self._home()
        path = home / "active_context" / "owner" / "personal-research.md"
        path.write_text(
            "---\ndomain: personal-research\nvisibility: owner\n---\nVISIBLE_PR",
            encoding="utf-8",
        )
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="user-1")
        decision = resolve_context_policy(
            source=source,
            message="奶茶品牌库继续更新",
            hermes_home=home,
            canonical_identity="owner",
        )

        prompt = render_domain_context_prompt(decision, hermes_home=home)

        self.assertIn("visibility: owner", prompt)
        self.assertIn("VISIBLE_PR", prompt)

    def test_resolve_canonical_identity_uses_private_aliases(self):
        home = self._home()
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="user-1")

        self.assertEqual(resolve_canonical_identity(source, hermes_home=home), "owner")

    def test_unmapped_identity_defaults_unknown(self):
        home = self._home()
        source = SessionSource(platform=Platform.API_SERVER, chat_id="external", chat_type="dm", user_id="new-user")

        self.assertEqual(resolve_canonical_identity(source, hermes_home=home), "unknown")

    def test_build_domain_context_prompt_combines_policy_and_active_context(self):
        home = self._home()
        source = SessionSource(platform=Platform.WEIXIN, chat_id="c", chat_type="dm", user_id="user-1")

        prompt = build_domain_context_prompt(source=source, message="奶茶品牌库继续更新", hermes_home=home)

        self.assertIn("DOMAIN MEMORY BOUNDARY", prompt)
        self.assertIn("DOMAIN-FILTERED ACTIVE CONTEXT", prompt)
        self.assertIn("topic_domain: personal-research", prompt)
        self.assertIn("PR_ONLY", prompt)
        self.assertNotIn("WORK_ONLY", prompt)


if __name__ == "__main__":
    unittest.main()
