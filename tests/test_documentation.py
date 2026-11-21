from pathlib import Path
import re
import unittest


class DocumentationTest(unittest.TestCase):
    def test_installer_preview_is_compact_and_links_to_the_full_screenshot(self):
        root = Path(__file__).resolve().parents[1]
        for lang, guide in (("en", "README.md"), ("ru", "README.ru.md")):
            with self.subTest(lang=lang):
                asset = f"docs/images/installer-{lang}.png"
                readme = (root / guide).read_text(encoding="utf-8")
                preview = re.search(rf'<a href="{asset}"><img src="{asset}" width="(\d+)"', readme)
                self.assertIsNotNone(preview)
                self.assertLessEqual(int(preview.group(1)), 440)
                self.assertTrue((root / asset).is_file())

    def test_bilingual_guides_share_the_same_commands_and_destinations(self):
        root = Path(__file__).resolve().parents[1]
        for folder in (root, root / "statusboard"):
            with self.subTest(folder=folder.name):
                en = (folder / "README.md").read_text(encoding="utf-8")
                ru = (folder / "README.ru.md").read_text(encoding="utf-8")
                bash = r"^\x60{3}bash\n(.*?)^\x60{3}"
                self.assertEqual(re.findall(bash, en, re.M | re.S), re.findall(bash, ru, re.M | re.S))
                self.assertEqual(self.destinations(en), self.destinations(ru))

    def destinations(self, value):
        return {target.replace("/README.ru.md", "/README.md").replace("installer-ru.png", "installer-en.png")
                for target in re.findall(r"\]\(([^)]+)\)", value)
                if target not in {"README.md", "README.ru.md"} and target.strip()}
