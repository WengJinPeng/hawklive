from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
HAN = re.compile(r"[\u3400-\u9fff]")


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip_depth = 0
        self.messages: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        for name, value in attrs:
            if name in {"aria-label", "placeholder", "title"} and value:
                self._add(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self._add(" ".join(data.split()))

    def _add(self, value: str) -> None:
        if value and HAN.search(value):
            self.messages.add(value)


class I18nCatalogTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the JavaScript runtime tests")
    def test_actual_javascript_translation_runtime(self) -> None:
        result = subprocess.run(
            [shutil.which("node"), "--test", str(ROOT / "test_i18n_runtime.js")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_catalog_does_not_silently_override_duplicate_keys(self) -> None:
        catalog = (PUBLIC / "i18n.js").read_text(encoding="utf-8")
        keys = re.findall(r'^    "([^"]+)":', catalog, flags=re.MULTILINE)
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        self.assertEqual([], duplicates, "Duplicate translation keys:\n" + "\n".join(duplicates))

    def test_every_static_chinese_message_has_an_english_catalog_entry(self) -> None:
        catalog = (PUBLIC / "i18n.js").read_text(encoding="utf-8")
        missing: list[str] = []
        for filename in ("index.html", "collector.html"):
            parser = VisibleTextParser()
            parser.feed((PUBLIC / filename).read_text(encoding="utf-8"))
            for message in sorted(parser.messages):
                if f"{json.dumps(message, ensure_ascii=False)}:" not in catalog:
                    missing.append(f"{filename}: {message}")
        self.assertEqual([], missing, "Missing English catalog entries:\n" + "\n".join(missing))

    def test_both_surfaces_load_the_shared_locale_runtime_and_switcher(self) -> None:
        for filename in ("index.html", "collector.html"):
            html = (PUBLIC / filename).read_text(encoding="utf-8")
            self.assertIn("./i18n.js", html)
            self.assertIn('data-locale="zh-CN"', html)
            self.assertIn('data-locale="en-US"', html)

    def test_visible_date_formatters_do_not_force_chinese(self) -> None:
        for filename in ("app.js", "collector.js"):
            source = (PUBLIC / filename).read_text(encoding="utf-8")
            self.assertNotIn('toLocaleString("zh-CN"', source)

    def test_internal_view_navigation_does_not_capture_wallboard_link(self) -> None:
        source = (PUBLIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('document.querySelectorAll(".nav-button[data-view]")', source)
        self.assertNotIn(
            'document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener',
            source,
        )

    def test_exports_follow_locale_and_business_names_are_translation_safe(self) -> None:
        app_source = (PUBLIC / "app.js").read_text(encoding="utf-8")
        collector_source = (PUBLIC / "collector.js").read_text(encoding="utf-8")
        runtime = (PUBLIC / "i18n.js").read_text(encoding="utf-8")
        self.assertIn('params.set("locale", uiLocale())', app_source)
        self.assertIn('[data-i18n-ignore]', runtime)
        self.assertGreaterEqual(app_source.count("data-i18n-ignore"), 8)
        self.assertGreaterEqual(collector_source.count("data-i18n-ignore"), 3)

    def test_simple_runtime_messages_are_registered(self) -> None:
        catalog = (PUBLIC / "i18n.js").read_text(encoding="utf-8")
        literal = re.compile(r'(?P<quote>["\'])(?P<text>(?:\\.|(?!\1).)*?)(?P=quote)')
        excluded_fragments = {"尘埃粒子计数器", "个/ft³"}
        missing: list[str] = []
        for filename in ("app.js", "collector.js"):
            source = (PUBLIC / filename).read_text(encoding="utf-8")
            for match in literal.finditer(source):
                message = match.group("text")
                if (
                    HAN.search(message)
                    and "${" not in message
                    and "<" not in message
                    and message == message.strip()
                    and message not in excluded_fragments
                    and f"{json.dumps(message, ensure_ascii=False)}:" not in catalog
                ):
                    missing.append(f"{filename}: {message}")
        self.assertEqual([], missing, "Missing runtime catalog entries:\n" + "\n".join(missing))

    def test_static_messages_in_dynamic_html_are_registered(self) -> None:
        catalog = (PUBLIC / "i18n.js").read_text(encoding="utf-8")
        missing: list[str] = []
        for filename in ("app.js", "collector.js"):
            source = (PUBLIC / filename).read_text(encoding="utf-8")
            # HTML inside template literals is not covered by the HTML files or
            # the simple quoted-string check above. Interpolated text is tested
            # separately against the actual JavaScript translation runtime.
            for fragment in re.findall(r"<[A-Za-z][^>]*>([^<>]+)<[/A-Za-z]", source):
                message = " ".join(fragment.split())
                if (
                    HAN.search(message) and "${" not in message
                    and f"{json.dumps(message, ensure_ascii=False)}:" not in catalog
                ):
                    missing.append(f"{filename}: {message}")
        self.assertEqual([], missing, "Missing dynamic HTML entries:\n" + "\n".join(missing))

    def test_backend_scope_notices_have_english_translations(self) -> None:
        catalog = (PUBLIC / "i18n.js").read_text(encoding="utf-8")
        notices: list[str] = []
        for filename in ("cloud_api.py", "dashboard_server.py"):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "scope_notice":
                        self.assertIsInstance(value, ast.Constant, filename)
                        notices.append(value.value)
                        self.assertTrue(
                            json.dumps(value.value, ensure_ascii=False) + ":" in catalog,
                            f"{filename}: missing English scope_notice: {value.value}",
                        )
        self.assertEqual(2, len(notices), "Both cloud and local collector notices must be checked")

    def test_wallboard_catalogs_match_and_share_the_system_locale(self) -> None:
        source = (PUBLIC / "wallboard.js").read_text(encoding="utf-8")
        zh_block, remainder = source.split('"zh-CN": {', 1)[1].split('\n  },\n  "en-US": {', 1)
        en_block = remainder.split("\n  }\n};", 1)[0]
        key_pattern = re.compile(r'(?:^|,)\s*([A-Za-z][A-Za-z0-9]*):\s*"', re.MULTILINE)
        zh_keys = key_pattern.findall(zh_block)
        en_keys = key_pattern.findall(en_block)
        self.assertEqual(len(zh_keys), len(set(zh_keys)), "Duplicate Chinese wallboard translation key")
        self.assertEqual(len(en_keys), len(set(en_keys)), "Duplicate English wallboard translation key")
        self.assertEqual(set(zh_keys), set(en_keys), "Wallboard locale catalogs do not match")
        html = (PUBLIC / "wallboard.html").read_text(encoding="utf-8")
        used_keys = set(re.findall(r'data-i18n(?:-aria-label)?="([^"]+)"', html))
        self.assertFalse(used_keys - set(zh_keys), "Wallboard markup uses missing translation keys")
        for tag in re.findall(r"<[^>]+>", html):
            if re.search(r'aria-label="[^\"]*[\u3400-\u9fff]', tag):
                self.assertIn("data-i18n-aria-label=", tag)
        self.assertIn('const localeStorageKey = "hawkhive-locale"', source)
        self.assertNotIn("hawkhive-wallboard-locale", source)
        self.assertIn('window.addEventListener("storage"', source)
        self.assertIn('button.setAttribute("aria-pressed", String(active))', source)

    def test_wallboard_ignores_disabled_particle_thresholds(self) -> None:
        source = (PUBLIC / "wallboard.js").read_text(encoding="utf-8")
        self.assertIn("policy.particle_0_5_enabled === undefined", source)
        self.assertIn("enabled && Number.isFinite(value) ? [value] : []", source)

    def test_collector_recovers_once_from_a_stale_local_request_token(self) -> None:
        source = (PUBLIC / "collector.js").read_text(encoding="utf-8")
        self.assertIn("async function refreshCollectorSessionToken()", source)
        self.assertIn("return api(url, options, false)", source)
        self.assertIn("/Collector request token is invalid/i", source)
        self.assertNotIn("[/token/i", source)


if __name__ == "__main__":
    unittest.main()
