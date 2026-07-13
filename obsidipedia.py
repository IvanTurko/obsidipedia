# /// script
# requires-python = ">=3.12"
# dependencies = ["requests", "beautifulsoup4"]
# ///
"""
Import a Wikipedia article as a vault-ready Obsidian note.

Pipeline: fetch rendered HTML (action=parse) -> clean chrome -> pandoc HTML->GFM
so TABLES / lists / structure survive (the old plaintext API extract dropped them)
-> download content images into attachments/ and embed inline as ![[file]]
-> write YYYYMMDDHHMMSS-Title.md with frontmatter into the output dir.

Usage:
    uv run obsidipedia.py "Transaction cost"
    uv run obsidipedia.py "Kelly criterion" --lang en --out inbox
"""

import argparse
import datetime as dt
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from html import unescape

import requests
from bs4 import BeautifulSoup

AUTHOR = "404 Author Not Found"
USER_AGENT = "obsidipedia/1.0 (+https://github.com/IvanTurko/obsidipedia)"

JUNK_IMG = re.compile(
    r"(?i)(OOjs_UI|Commons-logo|Wikimedia|Wikipedia-?logo|Wiki_letter|Edit-?icon|"
    r"Ambox|Question_book|Padlock|Symbol[_-]|Increase|Decrease|Steady|Red_pog|"
    r"Portal|Nuvola|Crystal|Emblem|Text_document|Folder|Magnify|Disambig|"
    r"Wiktionary|Wikiquote|Wikisource|Wikibooks|Wikinews|Wikiversity|Wikivoyage|_logo)"
)
TAIL_SECTIONS = re.compile(
    r"(?im)^#{1,6}\s+(references|notes|citations|sources|bibliography|"
    r"further reading|external links|see also)\s*$"
)
LANG_CLASS = re.compile(r"^mw-highlight-lang-(.+)$")


def build_frontmatter(title: str, note_id: str, now: dt.datetime) -> str:
    return (
        "---\n"
        f"id: {note_id}\n"
        f"date: {now:%Y-%m-%d %H:%M}\n"
        f"title: {title}\n"
        f"author: {AUTHOR}\n"
        "aliases:\n"
        f"  - {title}\n"
        "tags: []\n"
        "---\n"
    )


def fetch(topic: str, lang: str) -> tuple[str, str]:
    r = requests.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        params={
            "action": "parse",
            "format": "json",
            "redirects": 1,
            "prop": "text|displaytitle|properties",
            "page": topic,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        sys.exit(f"Wikipedia error: {data['error'].get('info', data['error'])}")
    parse = data["parse"]
    props = {p["name"]: p.get("*") for p in parse.get("properties", [])}
    if "disambiguation" in props:
        sys.exit(
            f"'{parse['title']}' is a disambiguation page — pick a specific title."
        )
    return parse["title"], parse["text"]["*"]


def orig_filename(src: str) -> str:
    m = re.search(r"/thumb/[0-9a-fA-F]/[0-9a-fA-F]{2}/([^/]+)/", src)
    name = m.group(1) if m else os.path.basename(src.split("?")[0])
    return urllib.parse.unquote(name).replace(" ", "_")


def clean_html(
    html: str, attachments_dir: str, rel_attachments: str
) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")

    for sel in [
        ".mw-editsection",
        ".navbox",
        ".metadata",
        ".mbox",
        ".hatnote",
        ".noprint",
        ".reference",
        "sup.reference",
        "style",
        "link",  # stray TemplateStyles <link> inside table cells crashes pandoc's table
        # parser, which then flattens the whole table into run-on paragraphs
        ".mw-empty-elt",
        ".reflist",
        ".mw-references-wrap",
        ".shortdescription",
        ".ambox",
        "table.sidebar",
        ".thumbcaption .magnify",
        ".infobox",  # renders as raw HTML in Obsidian: ![[wikilinks]] don't resolve inside
        # it, and pandoc's blank lines mid-table break CommonMark's HTML-block parsing
    ]:
        for el in soup.select(sel):
            el.decompose()

    # hCard microformat scaffolding (hidden ISO dates, zero-width-space spouse-count hacks etc.)
    for el in soup.find_all(style=re.compile(r"display:\s*none")):
        el.decompose()

    # SyntaxHighlight extension marks the language on the wrapping div (e.g.
    # "mw-highlight-lang-asm"); move it onto a <code class="language-asm"> so pandoc
    # emits a fenced code block with the language tag instead of a bare indented one
    for div in soup.select("div.mw-highlight"):
        lang = next(
            (m.group(1) for c in div.get("class", []) if (m := LANG_CLASS.match(c))),
            None,
        )
        pre = div.find("pre")
        if lang and pre:
            code = soup.new_tag("code")
            code["class"] = f"language-{lang}"
            for child in list(pre.contents):
                code.append(child.extract())
            pre.append(code)

    # GFM has no table-caption syntax: pandoc just drops the text somewhere near the
    # table (often after it) instead of before, so pull it out as its own paragraph
    for table in soup.find_all("table"):
        caption = table.find("caption")
        if caption:
            p = soup.new_tag("p")
            strong = soup.new_tag("strong")
            strong.string = caption.get_text(strip=True)
            p.append(strong)
            table.insert_before(p)
            caption.decompose()

    # GFM requires a header row: give header-less tables a real <thead> from their first row
    for table in soup.find_all("table"):
        thead = table.find("thead")
        if thead and thead.find(["th", "td"]):
            continue  # already has a real header row
        if thead:
            thead.decompose()  # drop an empty <thead>
        first_tr = table.find("tr")
        if not first_tr:
            continue
        for c in first_tr.find_all(["td", "th"], recursive=False):
            c.name = "th"  # first row -> header cells
        new_thead = soup.new_tag("thead")
        new_thead.append(first_tr.extract())
        table.insert(0, new_thead)

    for a in soup.find_all("a"):
        href = a.get("href", "")
        if href.startswith("http"):
            a.attrs = {"href": href}  # keep external links
        else:
            a.unwrap()  # internal /wiki/ links -> plain text

    # decorative wrappers (IPA pronunciation spans, nowrap, id-lock, abbr tooltips,
    # gallery .thumb/.gallerytext divs...) -> keep the text, drop the tag; an unclosed
    # <div style="width:...px"> left dangling inside a list item breaks everything after it
    for wrapper in soup.find_all(["span", "div"]):
        wrapper.unwrap()

    os.makedirs(attachments_dir, exist_ok=True)
    saved = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        try:
            w = int(img.get("width", "999"))
        except ValueError:
            w = 999
        name = orig_filename(src) if src else ""
        if not src or w < 40 or "/static/" in src or JUNK_IMG.search(name):
            img.decompose()
            continue
        full = "https:" + src if src.startswith("//") else src
        if name.lower().endswith(".svg"):
            # thumb URLs for SVGs serve a rasterized PNG; fetch the vector original instead
            full = re.sub(
                r"/thumb/([0-9a-fA-F]/[0-9a-fA-F]{2}/[^/]+)/[^/]+$", r"/\1", full
            )
        try:
            resp = requests.get(full, headers={"User-Agent": USER_AGENT}, timeout=30)
        except Exception as e:
            print(f"  ! image failed {name}: {e}")
            img.decompose()
            continue
        if "." not in name:
            # e.g. the Math extension's render API has no extension in the URL at all
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
            name += mimetypes.guess_extension(ctype) or ""
        with open(os.path.join(attachments_dir, name), "wb") as f:
            f.write(resp.content)
        saved.append(name)
        if img.parent and img.parent.name == "a":  # unwrap thumbnail link
            img.parent.replace_with(img)
        if any(t.find("table") for t in img.find_parents("table")):
            # a table containing another table anywhere inside it forces pandoc into
            # raw-HTML passthrough for the whole thing, where neither ![[wikilinks]] nor
            # markdown image syntax render (Obsidian never renders markdown inside raw
            # HTML, by design) -> only a real <img src="..."> tag with a working relative
            # path renders here; breaks if the note is later moved to a different folder
            # depth than --out, since this path is baked in relative to it at import time
            img.replace_with(f"\x00IMG:{rel_attachments}/{name}\x00")
        else:
            img.attrs = {"src": name, "alt": img.get("alt", "")}

    return str(soup), saved


def to_markdown(html: str) -> str:
    if not shutil.which("pandoc"):
        sys.exit("pandoc not found — install it.")
    p = subprocess.run(
        ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none"],
        input=html,
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        sys.exit(f"pandoc failed: {p.stderr}")
    return p.stdout


def _pad_tables(md: str) -> str:
    """Ensure exactly one blank line immediately before and after each table block."""

    def is_row(s):
        return s.lstrip().startswith("|")

    out = []
    for line in md.split("\n"):
        if is_row(line):
            if out and out[-1].strip() and not is_row(out[-1]):
                out.append("")  # blank line before a table starts
        elif line.strip() and out and is_row(out[-1]):
            out.append("")  # blank line after a table ends
        out.append(line)
    return "\n".join(out)


def postprocess(md: str) -> str:
    md = re.sub(r"<sup\b.*?</sup>", "", md, flags=re.S)  # footnote refs
    md = re.sub(
        r"(?is)<figcaption[^>]*>(.*?)</figcaption>", r"*\1*", md
    )  # caption -> italic
    md = re.sub(r"(?i)</?figure[^>]*>", "", md)  # drop figure tags
    md = re.sub(
        r'(?is)<img\b[^>]*\bsrc="(?!https?:|//)([^"]+)"[^>]*>',
        lambda m: f"![[{unescape(m.group(1))}]]",
        md,
    )  # raw local <img> (src is still HTML-entity-encoded, e.g. "Foo_&amp;_Bar.jpg")
    md = re.sub(
        r"!\[(?:[^\]\\]|\\.)*\]\(<?(?!https?:|//)((?:[^()<>]|\([^()]*\))+)>?\)",
        lambda m: f"![[{unescape(m.group(1))}]]",
        md,
    )  # markdown local img (alt text may contain \]-escaped brackets, e.g. LaTeX-formula
    # alt text from \sqrt[4]{...}; destination may contain one balanced paren pair)
    md = re.sub(
        r"\x00IMG:([^\x00]+)\x00", r'<img src="\1" alt="">', md
    )  # nested-table images smuggled past pandoc as raw <img> (see clean_html); must run
    # after the two conversions above so they don't immediately re-wrap this <img> too
    md = re.sub(r"(?im)^[ \t]*</?div\b[^>]*>[ \t]*$", "", md)  # stray wrapper divs
    md = re.sub(r"\s*\{#[^}]+\}", "", md)  # heading anchors
    md = re.sub(r"\[\]\{[^}]*\}", "", md)  # empty anchor spans
    md = re.sub("[\u200b\u200c\u200d\u200e\u200f\ufeff]", "", md)  # invisible chars
    md = re.sub(r"(?m)^(```+) (\S+)$", r"\1\2", md)  # "``` lang" -> "```lang"
    md = re.sub(r"(?m)^<!-- -->\n?", "", md)  # pandoc's empty-comment list separator
    m = TAIL_SECTIONS.search(md)  # trim References/See also/...
    if m:
        md = md[: m.start()]
    md = re.sub(
        r"(!\[\[[^\]\n]+\]\])\n(?=\S)", r"\1\n\n", md
    )  # one blank line after an image embed
    md = _pad_tables(md)  # exactly one blank line around tables
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)  # never 2+ blank lines
    return md.strip() + "\n"


def slugify(title: str) -> str:
    # keep unicode word chars + hyphens; drop filesystem-hostile chars
    s = re.sub(r"[^\w-]", "", title.replace(" ", "-"))
    return re.sub(r"-{2,}", "-", s).strip("-") or "note"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Import a Wikipedia article as a vault-ready Obsidian note.",
        epilog=(
            "examples:\n"
            '  uv run obsidipedia.py "Transaction cost"\n'
            '  uv run obsidipedia.py "Kelly criterion" --lang en --out inbox\n'
            '  uv run obsidipedia.py "Транзакционные издержки" --lang ru\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "topic", help='Wikipedia article title to import, e.g. "Kelly criterion"'
    )
    ap.add_argument(
        "--lang", default="en", help="Wikipedia language subdomain (default: en)"
    )
    ap.add_argument(
        "--out", default="inbox", help="output dir for the .md note (default: inbox)"
    )
    ap.add_argument(
        "--attachments",
        default="attachments",
        help="dir for downloaded images (default: attachments)",
    )
    args = ap.parse_args()

    title, html = fetch(args.topic, args.lang)
    rel_attachments = os.path.relpath(args.attachments, args.out)
    cleaned, images = clean_html(html, args.attachments, rel_attachments)
    md = postprocess(to_markdown(cleaned))

    now = dt.datetime.now()
    note_id = f"{now:%Y%m%d%H%M%S}-{slugify(title)}"
    url = f"https://{args.lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"

    # frontmatter, then the H1 flush against it (no blank line), then the body
    note = (
        build_frontmatter(title, note_id, now)
        + f"# {title}\n\n"
        + f"> Source: [Wikipedia]({url})\n\n"
        + f"{md}"
    )

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{note_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(note)

    print(f"Note:   {path}")
    print(f"Images: {len(images)} -> {args.attachments}/")
    for n in images:
        print(f"  - {n}")


if __name__ == "__main__":
    main()
