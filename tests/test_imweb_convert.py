import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
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

    def test_interactive_menu_defaults_to_first_article_and_copies_public_png_html(self):
        with tempfile.TemporaryDirectory() as directory:
            article_path = Path(directory) / "article.md"
            article_path.write_text(
                "# Article\n\n![Cover](assets/images/cover.svg)\n\nArticle text.\n",
                encoding="utf-8",
            )
            entries = [
                (
                    {
                        "file": f"article-{number}.md",
                        "date": "2026.11.20",
                        "category": "오늘의 패션",
                        "title": f"Article {number}",
                    },
                    article_path,
                )
                for number in range(1, 4)
            ]
            png_path = Path(directory) / "cover.png"
            public_url = (
                "https://raw.githubusercontent.com/example/repo/main/"
                "assets/images/cover.png"
            )
            clipboard = []
            output = StringIO()
            with (
                patch("sys.argv", ["imweb_convert.py"]),
                patch.object(imweb_convert, "_latest_articles", return_value=entries),
                patch.object(
                    imweb_convert,
                    "_prepare_article",
                    return_value=("Cover", "assets/images/cover.svg", png_path, public_url),
                ) as prepare_article,
                patch.object(imweb_convert, "_publish_pngs"),
                patch.object(
                    imweb_convert, "_copy_html_to_clipboard", side_effect=clipboard.append
                ),
                patch("builtins.input", side_effect=["", "q"]),
                redirect_stdout(output),
            ):
                imweb_convert.main()

            prepare_article.assert_called_once_with(article_path)
            self.assertIn("1. [2026.11.20] [오늘의 패션] Article 1", output.getvalue())
            self.assertIn("2. [2026.11.20] [오늘의 패션] Article 2", output.getvalue())
            self.assertIn("3. [2026.11.20] [오늘의 패션] Article 3", output.getvalue())
            self.assertIn(public_url, clipboard[0])
            self.assertIn('<img src="' + public_url + '" alt="Cover">', clipboard[0])
            self.assertNotIn("<!--", clipboard[0])
            self.assertIn("클립보드에 복사했습니다.", output.getvalue())

    def test_interactive_menu_repeats_until_quit_after_copying_articles(self):
        with tempfile.TemporaryDirectory() as directory:
            entries = []
            prepared = {}
            for number in range(1, 4):
                article_path = Path(directory) / f"article-{number}.md"
                article_path.write_text(
                    "# Article\n\n![Cover](assets/images/cover.svg)\n\n"
                    f"Article {number}.\n",
                    encoding="utf-8",
                )
                entries.append(
                    (
                        {
                            "file": article_path.name,
                            "date": "2026.11.20",
                            "category": "오늘의 패션",
                            "title": f"Article {number}",
                        },
                        article_path,
                    )
                )
                prepared[article_path] = (
                    "Cover",
                    "assets/images/cover.svg",
                    Path(directory) / f"cover-{number}.png",
                    f"https://raw.githubusercontent.com/example/repo/main/"
                    f"assets/images/cover-{number}.png",
                )

            clipboard = []
            output = StringIO()
            with (
                patch("sys.argv", ["imweb_convert.py"]),
                patch.object(imweb_convert, "_latest_articles", return_value=entries),
                patch.object(
                    imweb_convert, "_prepare_article", side_effect=prepared.__getitem__
                ) as prepare_article,
                patch.object(imweb_convert, "_publish_pngs"),
                patch.object(
                    imweb_convert, "_copy_html_to_clipboard", side_effect=clipboard.append
                ),
                patch("builtins.input", side_effect=["1", "2", "3", "q"]),
                redirect_stdout(output),
            ):
                imweb_convert.main()

            self.assertEqual(
                prepare_article.call_args_list,
                [call(entry[1]) for entry in entries],
            )
            self.assertEqual(len(clipboard), 3)
            for number, copied_html in enumerate(clipboard, start=1):
                self.assertIn(f"cover-{number}.png", copied_html)
                self.assertNotIn("<!--", copied_html)
            self.assertEqual(output.getvalue().count("최신 글 목록"), 4)

    def test_interactive_menu_quits_without_converting(self):
        entries = [
            (
                {"file": f"article-{number}.md", "title": f"Article {number}"},
                Path(f"article-{number}.md"),
            )
            for number in range(1, 4)
        ]
        output = StringIO()
        with (
            patch("sys.argv", ["imweb_convert.py"]),
            patch.object(imweb_convert, "_latest_articles", return_value=entries),
            patch.object(imweb_convert, "_prepare_article") as prepare_article,
            patch("builtins.input", return_value="q"),
            redirect_stdout(output),
        ):
            imweb_convert.main()

        prepare_article.assert_not_called()
        self.assertIn("변환을 종료합니다.", output.getvalue())

    def test_interactive_menu_accepts_article_number_after_invalid_input(self):
        entries = [
            (
                {"file": f"article-{number}.md", "title": f"Article {number}"},
                Path(f"article-{number}.md"),
            )
            for number in range(1, 4)
        ]
        output = StringIO()
        with (
            patch.object(imweb_convert, "_latest_articles", return_value=entries),
            patch("builtins.input", side_effect=["4", "2"]),
            redirect_stdout(output),
        ):
            selected = imweb_convert._select_article()

        self.assertEqual(selected, entries[1][1])
        self.assertIn("1부터 3 사이의 번호", output.getvalue())

    def test_html_uses_public_png_and_keeps_table_rows_together(self):
        with tempfile.TemporaryDirectory() as directory:
            article_path = Path(directory) / "article.md"
            article_path.write_text(
                "# Article\n\n![Cover](assets/images/cover.svg)\n\n"
                "## Section\n\n### Subsection\n\n"
                "| Item | Detail |\n|---|---|\n| One | Two |\n",
                encoding="utf-8",
            )

            rendered = imweb_convert._render_markdown(
                article_path,
                "https://raw.githubusercontent.com/example/repo/main/assets/images/cover.png",
                "Cover",
                "assets/images/cover.svg",
            )

            heading_style = (
                'style="font-size: 20px; font-weight: 700; margin: 32px 0 12px; '
                'line-height: 1.4;"'
            )
            self.assertIn(
                '<img src="https://raw.githubusercontent.com/example/repo/main/'
                'assets/images/cover.png" alt="Cover">',
                rendered,
            )
            self.assertIn(f"<h2 {heading_style}>Section</h2>", rendered)
            self.assertIn(f"<h3 {heading_style}>Subsection</h3>", rendered)
            self.assertIn(
                '<table style="width: 100%; border-collapse: collapse; margin: 20px 0;">',
                rendered,
            )
            self.assertIn(
                '<th style="padding: 10px 12px; border-bottom: 2px solid #ccc; '
                'text-align: left;">Item</th>',
                rendered,
            )
            self.assertIn(
                '<td style="padding: 10px 12px; border-bottom: 1px solid #e5e5e5; '
                'text-align: left;">One</td>',
                rendered,
            )
            self.assertNotIn("data:image", rendered)
            self.assertEqual(rendered.count("<table "), 1)
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
