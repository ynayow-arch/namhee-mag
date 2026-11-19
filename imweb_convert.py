import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pyperclip
import resvg_py
from PIL import Image, ImageOps

from article_images import ensure_article_image


ROOT = Path(__file__).resolve().parent
IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
RAW_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PUBLISH_RETRIES = 30


class ConversionError(RuntimeError):
    pass


def _run_git(args):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ConversionError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _article_path(value):
    candidate = (ROOT / value).resolve()
    if not candidate.is_file() or not candidate.is_relative_to(ROOT):
        raise ConversionError(f"Article file was not found inside the repository: {value}")
    return candidate


def _latest_article():
    posts = json.loads((ROOT / "posts.json").read_text(encoding="utf-8-sig"))
    if not posts:
        raise ConversionError("posts.json does not contain any articles.")
    return _article_path(posts[0]["file"])


def _read_markdown_image(article_path):
    text = article_path.read_text(encoding="utf-8")
    match = IMAGE_PATTERN.search(text)
    if not match:
        ensure_article_image(article_path)
        text = article_path.read_text(encoding="utf-8")
        match = IMAGE_PATTERN.search(text)
    if not match:
        raise ConversionError(f"No article image was added to {article_path.name}.")

    source = match.group(2)
    if source.startswith(("https://", "http://")):
        raise ConversionError(
            "The article image must be stored in this repository before it can be "
            "converted and published."
        )

    image_path = (article_path.parent / Path(source.replace("/", os.sep))).resolve()
    image_root = (ROOT / "assets" / "images").resolve()
    if not image_path.is_relative_to(image_root) or not image_path.is_file():
        raise ConversionError(f"Article image is missing or outside assets/images: {source}")
    return match.group(1), source, image_path


def _convert_to_png(source_path, png_path=None):
    png_path = png_path or source_path.with_suffix(".png")
    if source_path.resolve() != png_path.resolve():
        if source_path.suffix.lower() == ".svg":
            png_path.write_bytes(
                resvg_py.svg_to_bytes(svg_path=str(source_path), width=1200)
            )
        else:
            with Image.open(source_path) as source:
                image = ImageOps.exif_transpose(source)
                mode = "RGBA" if "A" in image.getbands() else "RGB"
                image.convert(mode).save(png_path, format="PNG", optimize=True)

    if not png_path.is_file() or png_path.stat().st_size > 20 * 1024 * 1024:
        raise ConversionError(f"PNG output is missing or exceeds 20 MB: {png_path.name}")
    with Image.open(png_path) as image:
        image.verify()
    if png_path.read_bytes()[:8] != RAW_PNG_SIGNATURE:
        raise ConversionError(f"Converted file is not a valid PNG: {png_path.name}")
    return png_path


def _png_destination(article_path):
    posts = json.loads((ROOT / "posts.json").read_text(encoding="utf-8-sig"))
    post = next((item for item in posts if item.get("file") == article_path.name), None)
    if not post:
        raise ConversionError(f"No posts.json entry found for {article_path.name}.")
    slug = post.get("slug", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ConversionError(f"Expected a lowercase ASCII slug for {article_path.name}.")
    return ROOT / "assets" / "images" / f"imweb-{slug}.png"


def _github_repository():
    remote = _run_git(["remote", "get-url", "origin"])
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if not match:
        raise ConversionError(f"Cannot determine the GitHub repository from origin: {remote}")
    return match.group(1), match.group(2)


def _public_url(png_path):
    branch = _run_git(["branch", "--show-current"])
    if branch != "main":
        raise ConversionError(f"Publish images from the main branch; current branch is {branch!r}.")
    owner, repository = _github_repository()
    relative_path = png_path.resolve().relative_to(ROOT).as_posix()
    return (
        f"https://raw.githubusercontent.com/{owner}/{repository}/main/"
        f"{urllib.parse.quote(relative_path, safe='/')}"
    )


def _remote_png_is_available(url):
    request = urllib.request.Request(url, headers={"Range": "bytes=0-7"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(8) == RAW_PNG_SIGNATURE
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise ConversionError(f"GitHub image check failed with HTTP {error.code}: {url}") from error
    except urllib.error.URLError as error:
        raise ConversionError(f"Could not verify the public GitHub image URL: {error}") from error


def _publish_pngs(images):
    images = list(dict.fromkeys((path.resolve(), url) for path, url in images))
    pending = [
        (path, url)
        for path, url in images
        if not (
            _remote_png_is_available(url)
            and not _run_git(
                ["status", "--porcelain", "--", path.relative_to(ROOT).as_posix()]
            )
        )
    ]
    if not pending:
        return

    _run_git(["fetch", "origin", "main"])
    divergence = _run_git(["rev-list", "--left-right", "--count", "HEAD...origin/main"])
    if divergence != "0\t0":
        raise ConversionError(
            f"Local main and origin/main are not synchronized ({divergence}). "
            "Sync the repository before publishing this image."
        )

    changed_paths = [
        path.relative_to(ROOT).as_posix()
        for path, _ in pending
        if _run_git(
            ["status", "--porcelain", "--", path.relative_to(ROOT).as_posix()]
        )
    ]
    if changed_paths:
        _run_git(["add", "--", *changed_paths])
        _run_git(
            [
                "commit",
                "--only",
                "-m", "Publish PNG images for Imweb",
                "--", *changed_paths,
            ]
        )

    _run_git(["push", "origin", "main"])
    for attempt in range(PUBLISH_RETRIES):
        if all(_remote_png_is_available(url) for _, url in images):
            return
        if attempt < PUBLISH_RETRIES - 1:
            time.sleep(2)
    raise ConversionError("One or more pushed PNG images are not publicly available yet.")


def _inline(markdown):
    value = html.escape(markdown, quote=False)
    value = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        value,
    )
    value = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    return value


def _cf_html_payload(fragment):
    prefix = "<html><body><!--StartFragment-->"
    suffix = "<!--EndFragment--></body></html>"
    fragment_bytes = fragment.encode("utf-8")
    prefix_bytes = prefix.encode("utf-8")
    suffix_bytes = suffix.encode("utf-8")
    header = (
        "Version:1.0\r\n"
        "StartHTML:{:010d}\r\n"
        "EndHTML:{:010d}\r\n"
        "StartFragment:{:010d}\r\n"
        "EndFragment:{:010d}\r\n"
    )
    header_size = len(header.format(0, 0, 0, 0).encode("ascii"))
    start_html = header_size
    start_fragment = start_html + len(prefix_bytes)
    end_fragment = start_fragment + len(fragment_bytes)
    end_html = end_fragment + len(suffix_bytes)
    return (
        header.format(start_html, end_html, start_fragment, end_fragment).encode("ascii")
        + prefix_bytes
        + fragment_bytes
        + suffix_bytes
    )


def _copy_html_to_clipboard(fragment):
    if os.name != "nt":
        try:
            pyperclip.copy(fragment)
        except pyperclip.PyperclipException as error:
            raise ConversionError(f"Could not copy HTML to the clipboard: {error}") from error
        return

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    formats = (
        (user32.RegisterClipboardFormatW("HTML Format"), _cf_html_payload(fragment)),
        (13, (fragment + "\0").encode("utf-16le")),
    )
    if not user32.OpenClipboard(None):
        raise ConversionError(f"Could not open the Windows clipboard: {ctypes.WinError()}")
    try:
        if not user32.EmptyClipboard():
            raise ConversionError(f"Could not clear the Windows clipboard: {ctypes.WinError()}")
        for clipboard_format, payload in formats:
            handle = kernel32.GlobalAlloc(0x0002, len(payload))
            if not handle:
                raise ConversionError(f"Could not allocate clipboard memory: {ctypes.WinError()}")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                kernel32.GlobalFree(handle)
                raise ConversionError(f"Could not lock clipboard memory: {ctypes.WinError()}")
            ctypes.memmove(pointer, payload, len(payload))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(clipboard_format, handle):
                kernel32.GlobalFree(handle)
                raise ConversionError(f"Could not set clipboard data: {ctypes.WinError()}")
    finally:
        user32.CloseClipboard()


def _render_markdown(article_path, image_url, image_alt, source_image):
    lines = article_path.read_text(encoding="utf-8").splitlines()
    output = []
    list_tag = None

    def close_list():
        nonlocal list_tag
        if list_tag:
            output.append(f"</{list_tag}>")
            list_tag = None

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            close_list()
            index += 1
            continue
        if line.startswith("# "):
            index += 1
            continue

        image_match = IMAGE_PATTERN.fullmatch(line)
        if image_match and image_match.group(2) == source_image:
            close_list()
            output.append(
                f'<p><img src="{html.escape(image_url, quote=True)}" '
                f'alt="{html.escape(image_alt, quote=True)}"></p>'
            )
            index += 1
            continue

        heading = re.match(r"^(#{2,3})\s+(.+)$", line)
        if heading:
            close_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if line.startswith("|"):
            close_list()
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"[-: ]+", cell) for cell in cells):
                    rows.append(cells)
                index += 1
            if rows:
                header, *body = rows
                output.append(
                    "<table><thead><tr>"
                    + "".join(f"<th>{_inline(cell)}</th>" for cell in header)
                    + "</tr></thead><tbody>"
                    + "".join(
                        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
                        for row in body
                    )
                    + "</tbody></table>"
                )
            continue

        unordered = re.match(r"^[-*]\s+(.+)$", line)
        ordered = re.match(r"^\d+[.)]\s+(.+)$", line)
        if unordered or ordered:
            tag = "ul" if unordered else "ol"
            if list_tag != tag:
                close_list()
                output.append(f"<{tag}>")
                list_tag = tag
            content = (unordered or ordered).group(1)
            output.append(f"<li>{_inline(content)}</li>")
            index += 1
            continue

        close_list()
        if line.startswith("> "):
            output.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        else:
            output.append(f"<p>{_inline(line)}</p>")
        index += 1

    close_list()
    return "\n".join(output)


def _prepare_article(article_path):
    image_alt, source_image, source_path = _read_markdown_image(article_path)
    png_path = _convert_to_png(source_path, _png_destination(article_path))
    url = _public_url(png_path)
    return image_alt, source_image, png_path, url


def convert_article(article_path):
    image_alt, source_image, png_path, url = _prepare_article(article_path)
    _publish_pngs([(png_path, url)])
    content = _render_markdown(article_path, url, image_alt, source_image)
    return content, url, png_path


def main():
    parser = argparse.ArgumentParser(
        description="Convert article images to public PNG URLs and render Imweb-ready HTML."
    )
    parser.add_argument(
        "articles",
        nargs="*",
        help="Article Markdown paths; defaults to the newest entry in posts.json.",
    )
    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="Print HTML without copying it to the clipboard.",
    )
    args = parser.parse_args()
    articles = [_article_path(value) for value in args.articles] or [_latest_article()]

    prepared = [_prepare_article(article_path) for article_path in articles]
    _publish_pngs([(png_path, url) for _, _, png_path, url in prepared])

    sections = []
    for article_path, (image_alt, source_image, png_path, url) in zip(articles, prepared):
        content = _render_markdown(article_path, url, image_alt, source_image)
        sections.append(f"<!-- Image: {png_path.name} | {url} -->\n{content}")
    result = "\n\n".join(sections)
    if not args.no_clipboard:
        _copy_html_to_clipboard(result)
        print("Imweb-ready HTML copied to the clipboard.", file=sys.stderr)
    print(result)


if __name__ == "__main__":
    try:
        main()
    except ConversionError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
