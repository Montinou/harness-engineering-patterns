import unittest

from harness_workbench.core.harness import (
    ALL_COMPONENTS,
    Harness,
    Plugin,
    Script,
    Skill,
    SubagentSpec,
    ToolSpec,
)


def make_harness() -> Harness:
    return Harness(
        id="h",
        version="1",
        system_prompt="You are careful.",
        guardrails=["cite sources"],
        memory_config={"window": 4, "strategy": "recent"},
        tools=[ToolSpec(name="search", description="web search", schema={"q": "str"}, guidance="g")],
        subagents=[SubagentSpec(name="rev", role="reviewer", system_prompt="p", tools=["search"])],
        skills=[Skill(name="plan", trigger="t", action="a", kind="invariant")],
        scripts=[Script(name="s", language="sh", body="echo hi")],
        plugins=[Plugin(name="pl", hook="pre", config={"k": 1})],
        notes="original notes",
        domain="research",
    )


class HarnessHashTests(unittest.TestCase):
    def test_all_components_is_the_eight_executable_components(self):
        self.assertEqual(
            ALL_COMPONENTS,
            ("system_prompt", "guardrails", "memory_config", "tools", "subagents", "skills", "scripts", "plugins"),
        )

    def test_hash_is_deterministic_sha256_hex(self):
        first = make_harness().execution_config_hash()
        self.assertEqual(first, make_harness().execution_config_hash())
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_hash_ignores_lineage_notes_and_version(self):
        base = make_harness()
        child = base.fork(source="test")
        child.notes = "different notes"
        child.name = "renamed"
        child.id = "other-id"
        child.domain = "coding"
        child.lineage.lessons.append("a lesson")
        child.lineage.metrics["score"] = 0.9
        self.assertNotEqual(child.version, base.version)
        self.assertEqual(base.execution_config_hash(), child.execution_config_hash())

    def test_hash_changes_with_any_executable_component(self):
        base_hash = make_harness().execution_config_hash()
        mutations = {
            "system_prompt": lambda h: setattr(h, "system_prompt", h.system_prompt + "!"),
            "guardrails": lambda h: h.guardrails.append("extra"),
            "memory_config": lambda h: h.memory_config.update(window=9),
            "tools": lambda h: setattr(h.tools[0], "guidance", "changed"),
            "subagents": lambda h: h.subagents[0].tools.append("other"),
            "skills": lambda h: setattr(h.skills[0], "action", "changed"),
            "scripts": lambda h: setattr(h.scripts[0], "body", "echo bye"),
            "plugins": lambda h: h.plugins[0].config.update(k=2),
        }
        self.assertEqual(set(mutations), set(ALL_COMPONENTS))
        for component, mutate in mutations.items():
            with self.subTest(component=component):
                harness = make_harness()
                mutate(harness)
                self.assertNotEqual(base_hash, harness.execution_config_hash())

    def test_public_components_has_no_administrative_keys(self):
        public = make_harness().public_components()
        self.assertEqual(set(public), set(ALL_COMPONENTS))
        for key in ("id", "version", "name", "notes", "lineage", "domain"):
            self.assertNotIn(key, public)
        self.assertIsInstance(public["tools"][0], dict)
        self.assertEqual(public["tools"][0]["name"], "search")

    def test_descriptor_holds_the_administrative_fields(self):
        harness = make_harness()
        descriptor = harness.descriptor()
        self.assertEqual(
            set(descriptor), {"id", "version", "name", "domain", "notes", "lineage"}
        )
        self.assertEqual(descriptor["id"], "h")
        self.assertEqual(descriptor["domain"], "research")
        self.assertIsInstance(descriptor["lineage"], dict)

    def test_hash_rejects_non_finite_numbers(self):
        from harness_workbench.experiments.validation import ValidationError

        harness = make_harness()
        harness.memory_config["bad"] = float("nan")
        with self.assertRaises(ValidationError):
            harness.execution_config_hash()


if __name__ == "__main__":
    unittest.main()
