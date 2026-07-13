# obsidipedia

Give it a Wikipedia article title. Get back a clean note for Obsidian, with the images already downloaded.

## What it does

Wikipedia pages are messy under the hood. Edit links, footnote numbers, big info boxes, hidden tracking spans, math formulas as tiny images. This script cleans all of that up. Here is what happens, step by step:

1. It fetches the article's HTML from Wikipedia. This is the same HTML your browser shows you, not the raw wiki markup.
2. It removes the clutter: edit links, navboxes, footnotes, info boxes, hidden date codes, and other junk.
3. It runs the clean HTML through `pandoc` to turn it into Markdown. Tables, lists, and code blocks stay intact.
4. It downloads every image. This includes math formulas, since Wikipedia turns each formula into its own small SVG image.
5. It changes each image link to Obsidian's own embed format, like `![[image.svg]]`. So images just work when you open the note.
6. It saves a new `.md` file with a timestamp and frontmatter, ready for your vault.

The script tries hard to show you everything from the article. But not all HTML turns into Markdown cleanly. Some pages have weird tables, or tables inside tables, or other odd markup. So once in a while, something will not look perfect. This is normal, and just how wiki HTML is. When it happens, use your hands and fix it. Or think for a second about whether you really need that part anyway :)

## Setup

Put the script in its own `scripts/` folder inside your vault. This keeps things tidy. But it also works fine in the vault root. The script does not care where the file lives. It only cares about the folder you run it from.

Example folder layout:

```
MyVault/
├── scripts/
│   └── obsidipedia.py
├── inbox/
│   └── 20260101120000-Kelly-criterion.md
└── attachments/
    └── Kelly_criterion_graph.svg
```

## Requirements

- Python 3.12 or newer, plus [uv](https://docs.astral.sh/uv/). The script lists its own dependencies at the top of the file. So uv installs them for you. No setup needed.
- `pandoc`, installed on your own. For example `brew install pandoc` on Mac, or `apt install pandoc` on Linux. uv cannot install this one for you.

## Usage

Run it from your vault's root folder:

```bash
uv run scripts/obsidipedia.py "Kelly criterion"
uv run scripts/obsidipedia.py "Two's complement" --lang en --out inbox
uv run scripts/obsidipedia.py "Computer" --out notes --attachments media
```

For all options, run:

```bash
uv run scripts/obsidipedia.py -h
```

## A warning about math

Wikipedia turns every math formula into its own small image. Each one needs its own download. An article like *Quantum logic gate* can have 30 or more formulas. That means 30 extra downloads, one by one. So on math-heavy pages, the script can take a minute or two. It is not stuck. It is just downloading a lot of small pictures.

## Changing the frontmatter

There is no settings file. The note template lives right in the code, in a function called `build_frontmatter()`. Want your own name instead of the placeholder? Open `obsidipedia.py` and find this line near the top:

```python
AUTHOR = "404 Author Not Found"
```

Change it to your name. Do the same for `USER_AGENT` just below it. Wikipedia asks bots to identify themselves in this field (see their [User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy)). Put your own repo link or contact info there before you run the script a lot.
