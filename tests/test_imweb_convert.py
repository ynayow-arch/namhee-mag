import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from PIL import Image

import article_images
import imweb_convert


class ImwebConvertTests(unittest.TestCase):
    def test_svg_is_converted_to_verified_png(self):
        with tempfile.TemporaryDirectory() as directory:
            svg_path = Path(directory) / "cover.svg"
            svg_path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8">'
                '<rect width="12" height="8" fill="#d77d6b"/></svg>',
                encoding="utf-8",
            )

            png_path = imweb_convert._convert_to_png(svg_path)

            self.assertEqual(png_path.suffix, ".png")
            with Image.open(png_path) as image:
                self.assertEqual(image.size, (1200, 800))
                self.assertEqual(image.format, "PNG")

    def test_raster_image_is_converted_to_png(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "cover.jpg"
            Image.new("RGB", (10, 6), "#d77d6b").save(source_path, format="JPEG")

            png_path = imweb_convert._convert_to_png(source_path)

            self.assertEqual(png_path.suffix, ".png")
            with Image.open(png_path) as image:
                self.assertEqual(image.size, (10, 6))
                self.assertEqual(image.format, "PNG")

    def test_public_png_uses_stable_ascii_filename(self):
        article = imweb_convert._latest_article()

        png_path = imweb_convert._png_destination(article)

        self.assertRegex(png_path.name, r"^imweb-[a-z0-9-]+\.png$")

    def test_html_uses_public_png_and_keeps_table_rows_together(self):
        with tempfile.TemporaryDirectory() as directory:
            article_path = Path(directory) / "article.md"
            article_path.write_text(
                "# Article\n\n![Cover](assets/images/cover.svg)\n\n"
                "| Item | Detail |\n|---|---|\n| One | Two |\n",
                encoding="utf-8",
            )

            rendered = imweb_convert._render_markdown(
                article_path,
                "https://raw.githubusercontent.com/example/repo/main/assets/images/cover.png",
                "Cover",
                "assets/images/cover.svg",
            )

            self.assertIn(
                '<img src="https://raw.githubusercontent.com/example/repo/main/'
                'assets/images/cover.png" alt="Cover">',
                rendered,
            )
            self.assertNotIn("data:image", rendered)
            self.assertEqual(rendered.count("<table>"), 1)
            self.assertEqual(rendered.count("<tr>"), 2)

    def test_cf_html_offsets_point_to_utf8_fragment(self):
        fragment = "<p>안녕하세요</p>"
        payload = imweb_convert._cf_html_payload(fragment)
        self.assertIn(fragment.encode("utf-8"), payload)
        offsets = {
            key: int(value)
            for key, value in (
                line.split(b":", 1)
                for line in payload.split(b"\r\n")[:5]
                if b":" in line and not line.startswith(b"Version")
            )
        }
        start = offsets[b"StartFragment"]
        end = offsets[b"EndFragment"]
        self.assertEqual(payload[start:end], fragment.encode("utf-8"))

    def test_local_illustration_fallback_is_saved_as_png(self):
        with tempfile.TemporaryDirectory() as directory:
            article_path = Path(directory) / "article.md"
            article_path.write_text("# Winter style\n\n> **한 줄 요약:** Cozy\n", encoding="utf-8")

            with patch.object(
                article_images,
                "_find_commons_image",
                side_effect=RuntimeError("no image"),
            ):
                info = article_images.ensure_article_image(article_path)

            image_path = article_path.parent / "assets" / "images" / info["filename"]
            self.assertEqual(info["mime"], "image/png")
            self.assertEqual(image_path.suffix, ".png")
            self.assertEqual(image_path.read_bytes()[:8], imweb_convert.RAW_PNG_SIGNATURE)

    def test_publisher_commits_only_the_png_and_verifies_publication(self):
        image_path = imweb_convert.ROOT / "assets" / "images" / "test-image.png"
        git_calls = []

        def fake_git(args):
            git_calls.append(args)
            if args[0] == "status":
                return "?? assets/images/test-image.png"
            if args[0] == "rev-list":
                return "0\t0"
            return ""

        with patch.object(imweb_convert, "_run_git", side_effect=fake_git), patch.object(
            imweb_convert, "_remote_png_is_available", side_effect=[False, True]
        ):
            imweb_convert._publish_pngs(
                [
                    (
                        image_path,
                        "https://raw.githubusercontent.com/example/repo/main/assets/images/test-image.png",
                    )
                ]
            )

        commit_call = next(args for args in git_calls if args[0] == "commit")
        self.assertIn("--only", commit_call)
        self.assertEqual(commit_call[-1], "assets/images/test-image.png")
        self.assertIn(["push", "origin", "main"], git_calls)


if __name__ == "__main__":
    unittest.main()
