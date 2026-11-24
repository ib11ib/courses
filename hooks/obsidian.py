"""
Обработчик сборки сайта.

Что он делает, чтобы курсы из Obsidian можно было класть «как есть»:
  * сам находит курсы: каждая папка в docs/ — отдельный курс;
  * сам строит меню курса (вводные заметки, уроки, проекты, справка);
  * делает красивые адреса страниц латиницей (python/urok-01-.../);
  * переводит разметку Obsidian: [[ссылки]], ![[вставки]], > [!tip] выноски,
    списки с отступом в 2–3 пробела;
  * добавляет кнопки «Скачать урок (.md)» и «Скачать курс (.zip)»;
  * собирает архивы курсов с исходными .md файлами.

Файлы в docs/ при этом не меняются.
"""

from __future__ import annotations

import html
import logging
import os
import posixpath
import re
import shutil
import zipfile

import yaml
from pymdownx.slugs import slugify as _pymdownx_slugify

log = logging.getLogger("mkdocs.hooks.obsidian")

SKIP_DIRS = {"assets", "downloads", "stylesheets", "javascripts", "images", "overrides"}
COURSE_FILE = "_course.yml"
HIDDEN_EXT = {".base", ".canvas"}  # служебные файлы Obsidian: в архив кладём, на сайт нет

heading_slug = _pymdownx_slugify(case="lower")

# ───────────────────────────── утилиты ─────────────────────────────

_TR = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
     "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
))


def slugify(text: str) -> str:
    text = "".join(_TR.get(ch, ch) for ch in text.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "page"


def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def read_front_matter(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def first_h1(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return None


# ───────────────────────────── курсы и меню ─────────────────────────────

# Порядок групп в меню курса, если в _course.yml не задан свой.
GROUP_ORDER = ["Введение", "Модули", "Уроки", "Недели", "Главы", "Темы", "Проекты", "Практика", "Справка"]
WORD_GROUPS = {
    "урок": "Уроки", "lesson": "Уроки",
    "неделя": "Недели", "week": "Недели",
    "глава": "Главы", "chapter": "Главы",
    "тема": "Темы",
    "проект": "Проекты", "project": "Проекты",
    "практика": "Практика", "задание": "Практика",
}
# для подписи на карточке: «18 уроков · 3 проекта»
COUNT_WORDS = {
    "урок": ("урок", "урока", "уроков"),
    "неделя": ("неделя", "недели", "недель"),
    "глава": ("глава", "главы", "глав"),
    "тема": ("тема", "темы", "тем"),
    "проект": ("проект", "проекта", "проектов"),
    "lesson": ("lesson", "lessons", "lessons"),
    "project": ("project", "projects", "projects"),
}
START_WORDS = ("старт", "start", "readme", "index", "о курсе", "оглавление")

COURSES: dict[str, dict] = {}   # slug папки -> данные курса
PAGES: dict[str, dict] = {}     # src_uri .md -> данные страницы
NAMES: dict[str, list[str]] = {}  # имя заметки (lower) -> [src_uri]


def _group_for(stem: str, meta: dict) -> str:
    for key in ("module", "модуль", "раздел", "section", "part", "часть"):
        if meta.get(key):
            return str(meta[key]).strip()
    if re.match(r"^\d", stem):
        return "Введение"
    word = re.match(r"^(\w+)\s+\d", stem)
    if word and word.group(1).lower() in WORD_GROUPS:
        return WORD_GROUPS[word.group(1).lower()]
    return "Справка"


def _group_rank(name: str, custom: list[str]):
    if name in custom:
        return (0, custom.index(name), "")
    if name in GROUP_ORDER:
        idx = GROUP_ORDER.index(name)
        # группы из frontmatter (модули) встают на место «Модули»
        return (1, idx, "")
    return (1, GROUP_ORDER.index("Модули"), natural_key(name))


def discover(docs_dir: str):
    COURSES.clear()
    PAGES.clear()
    NAMES.clear()
    for folder in sorted(os.listdir(docs_dir), key=natural_key):
        full = os.path.join(docs_dir, folder)
        if not os.path.isdir(full) or folder.startswith((".", "_")) or folder.lower() in SKIP_DIRS:
            continue
        md_files = sorted(
            (f for f in os.listdir(full) if f.endswith(".md")),
            key=natural_key,
        )
        if not md_files:
            continue

        cfg = {}
        cfg_path = os.path.join(full, COURSE_FILE)
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}

        stems = [f[:-3] for f in md_files]
        start = cfg.get("start")
        if start and start.removesuffix(".md") in stems:
            start = start.removesuffix(".md")
        else:
            start = next((s for s in stems if any(w in s.lower() for w in START_WORDS)), stems[0])

        cslug = slugify(folder)
        course = {
            "folder": folder,
            "slug": cslug,
            "title": cfg.get("title") or folder,
            "full_title": cfg.get("full_title") or first_h1(os.path.join(full, start + ".md")) or cfg.get("title") or folder,
            "description": cfg.get("description", ""),
            "icon": cfg.get("icon", "material/book-open-page-variant-outline"),
            "order": cfg.get("order", 100),
            "sections": cfg.get("sections", []),
            "start": start,
            "pages": [],
        }
        used_slugs = set()
        for stem in stems:
            src = f"{folder}/{stem}.md"
            meta = read_front_matter(os.path.join(full, stem + ".md"))
            is_start = stem == start
            pslug = slugify(stem)
            while pslug in used_slugs:
                pslug += "-2"
            used_slugs.add(pslug)
            page = {
                "src": src,
                "stem": stem,
                "course": cslug,
                "is_start": is_start,
                "slug": pslug,
                "url": f"{cslug}/" if is_start else f"{cslug}/{pslug}/",
                "group": None if is_start else _group_for(stem, meta),
                "download": f"downloads/{cslug}/{pslug}.md",
            }
            PAGES[src] = page
            NAMES.setdefault(stem.lower(), []).append(src)
            for alias in meta.get("aliases") or []:
                NAMES.setdefault(str(alias).lower(), []).append(src)
            course["pages"].append(page)
        course["zip_name"] = (re.sub(r'[\\/:*?"<>|]', "", str(course["full_title"])).strip() or folder) + ".zip"
        counts: dict[str, int] = {}
        for p in course["pages"]:
            w = re.match(r"^(\w+)\s+\d", p["stem"])
            if w and w.group(1).lower() in COUNT_WORDS:
                counts[w.group(1).lower()] = counts.get(w.group(1).lower(), 0) + 1
        course["counts"] = counts
        COURSES[cslug] = course


def build_nav() -> list:
    nav = [{"Главная": "index.md"}]
    for course in sorted(COURSES.values(), key=lambda c: (c["order"], natural_key(c["title"]))):
        groups: dict[str, list] = {}
        for p in course["pages"]:
            if p["is_start"]:
                continue
            groups.setdefault(p["group"], []).append({p["stem"]: p["src"]})
        children = [f"{course['folder']}/{course['start']}.md"]
        for name in sorted(groups, key=lambda g: _group_rank(g, course["sections"])):
            children.append({name: groups[name]})
        nav.append({course["title"]: children})
    return nav


# ───────────────────────────── хуки MkDocs ─────────────────────────────

def on_config(config, **kwargs):
    discover(config["docs_dir"])
    config["nav"] = build_nav()
    log.info("Курсов найдено: %d (%s)", len(COURSES), ", ".join(c["title"] for c in COURSES.values()))
    return config


def on_files(files, config, **kwargs):
    for f in list(files):
        ext = os.path.splitext(f.src_uri)[1].lower()
        name = posixpath.basename(f.src_uri)
        if name == COURSE_FILE or ext in HIDDEN_EXT:
            files.remove(f)
            continue
        page = PAGES.get(f.src_uri)
        if page:
            f.dest_uri = page["url"] + "index.html"
            f.url = page["url"]
            if page["is_start"]:
                f.name = "index"  # чтобы стартовая заметка стала страницей курса
    return files


def on_page_markdown(markdown, page, config, files, **kwargs):
    src = page.file.src_uri
    if src == "index.md":
        return markdown.replace("<!-- COURSES -->", _course_cards(page.url))
    info = PAGES.get(src)
    if not info:
        return markdown
    # поле status из Obsidian тема приняла бы за значок в меню — убираем
    page.meta.pop("status", None)
    course = COURSES[info["course"]]
    md = convert_obsidian(markdown, info, files, page.url)
    return _add_download_buttons(md, info, course, page.url)


def on_post_build(config, **kwargs):
    site_dir = config["site_dir"]
    docs_dir = config["docs_dir"]
    for course in COURSES.values():
        out_dir = os.path.join(site_dir, "downloads", course["slug"])
        os.makedirs(out_dir, exist_ok=True)
        for p in course["pages"]:
            shutil.copyfile(os.path.join(docs_dir, p["src"]), os.path.join(site_dir, p["download"]))
        # Архив курса: вся папка как есть (можно открыть как хранилище Obsidian)
        src_dir = os.path.join(docs_dir, course["folder"])
        zip_path = os.path.join(site_dir, "downloads", course["slug"] + ".zip")
        arc_root = course["zip_name"][:-4]
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, fnames in os.walk(src_dir):
                for fn in sorted(fnames):
                    if fn == COURSE_FILE or fn.startswith("."):
                        continue
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, src_dir)
                    zf.write(full, posixpath.join(arc_root, rel.replace(os.sep, "/")))
    log.info("Архивы курсов и файлы для скачивания готовы")


# ───────────────────────────── ссылки и кнопки ─────────────────────────────

def _rel(from_url: str, to_path: str) -> str:
    """Относительная ссылка со страницы from_url (например 'python/urok-01/') на путь сайта."""
    base = posixpath.dirname(from_url.rstrip("/") + "/x") if from_url else ""
    rel = posixpath.relpath(to_path or ".", base or ".")
    if to_path.endswith("/") and not rel.endswith("/"):
        rel += "/"
    return rel


def _icon(name: str) -> str:
    return ":" + name.replace("/", "-") + ":"


def _course_cards(from_url: str) -> str:
    items = []
    for c in sorted(COURSES.values(), key=lambda c: (c["order"], natural_key(c["title"]))):
        count_txt = " · ".join(f"{n} {_plural(n, *COUNT_WORDS[w])}" for w, n in sorted(c["counts"].items(), key=lambda kv: "проект" in kv[0] or "project" in kv[0]))
        if not count_txt:
            n = len(c["pages"])
            count_txt = f"{n} {_plural(n, 'страница', 'страницы', 'страниц')}"
        items.append(
            f"-   {_icon(c['icon'])}{{ .lg .middle }} __{c['full_title']}__\n\n"
            f"    ---\n\n"
            f"    {c['description']}\n\n"
            + f"    <small>{count_txt}</small>\n\n"
            + f"    [:octicons-arrow-right-24: Открыть курс](<{c['folder']}/{c['start']}.md>){{ .md-button .md-button--primary }}\n"
            f"    {_button(_rel(from_url, 'downloads/' + c['slug'] + '.zip'), 'Скачать .zip', 'material/download', c['zip_name']).replace('md-button dl-btn', 'md-button')}\n"
        )
    return '<div class="grid cards" markdown>\n\n' + "\n".join(items) + "\n</div>\n"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


_SVG_CACHE: dict[str, str] = {}


def _svg(name: str) -> str:
    """SVG-значок из набора темы Material (material/download и т.п.)."""
    if name not in _SVG_CACHE:
        import material
        path = os.path.join(os.path.dirname(material.__file__), "templates", ".icons", name + ".svg")
        try:
            with open(path, encoding="utf-8") as fh:
                _SVG_CACHE[name] = f'<span class="twemoji">{fh.read().strip()}</span>'
        except OSError:
            _SVG_CACHE[name] = ""
    return _SVG_CACHE[name]


def _button(href: str, text: str, icon: str, download: str, primary: bool = False) -> str:
    cls = "md-button dl-btn" + (" md-button--primary" if primary else "")
    dl = html.escape(download, quote=True)
    return f'<a class="{cls}" href="{html.escape(href, quote=True)}" download="{dl}">{_svg(icon)} {text}</a>'


def _add_download_buttons(md: str, info: dict, course: dict, page_url: str) -> str:
    buttons = '<div class="dl-bar">' + _button(
        _rel(page_url, info["download"]), "Скачать урок (.md)", "material/download", info["stem"] + ".md")
    if info["is_start"]:
        buttons += _button(_rel(page_url, "downloads/" + course["slug"] + ".zip"),
                           "Скачать весь курс (.zip)", "material/folder-download", course["zip_name"], primary=True)
    buttons += "</div>\n"
    # кнопки ставим сразу под заголовком страницы
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            lines.insert(i + 1, "\n" + buttons)
            return "\n".join(lines)
    return buttons + "\n" + md


def _resolve(name: str, info: dict):
    """Ищем заметку по имени: сначала в этом же курсе, потом во всех."""
    name = name.strip()
    key = posixpath.basename(name).removesuffix(".md").lower()
    cands = NAMES.get(key, [])
    for c in cands:
        if PAGES[c]["course"] == info["course"]:
            return PAGES[c]
    return PAGES[cands[0]] if cands else None


# ───────────────────────────── перевод разметки Obsidian ─────────────────────────────

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])(\s+|$)")
CALLOUT_RE = re.compile(r"^>\s*\[!([\w-]+)\]([+-]?)\s*(.*)$")

CALLOUT_TYPES = {
    "note": "note", "abstract": "abstract", "summary": "abstract", "tldr": "abstract",
    "info": "info", "todo": "info", "tip": "tip", "hint": "tip", "important": "tip",
    "success": "success", "check": "success", "done": "success",
    "question": "question", "help": "question", "faq": "question",
    "warning": "warning", "caution": "warning", "attention": "warning",
    "failure": "failure", "fail": "failure", "missing": "failure",
    "danger": "danger", "error": "danger", "bug": "bug", "example": "example",
    "quote": "quote", "cite": "quote",
}

OBSIDIAN_ONLY_NOTE = (
    '!!! info "Работает в Obsidian"\n'
    "    Здесь в Obsidian показывается таблица прогресса. "
    "Скачайте курс (.zip) и откройте папку в Obsidian, чтобы отмечать пройденные уроки.\n"
)


def convert_obsidian(md: str, info: dict, files, page_url: str) -> str:
    md = md.replace("\r\n", "\n")
    md = _strip_special_blocks(md)
    md = _convert_links(md, info, page_url)
    md = _fix_blocks(md)
    md = _convert_callouts(md)
    return md


def _iter_outside_fences(lines):
    """Отдаёт (индекс, строка, в_коде) для каждой строки."""
    fence = None
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if fence is None and m:
            fence = m.group(2)[0] * len(m.group(2))
            yield i, line, True
            continue
        if fence is not None:
            if m and m.group(2).startswith(fence) and not m.group(3).strip():
                fence = None
            yield i, line, True
            continue
        yield i, line, False


def _strip_special_blocks(md: str) -> str:
    # блоки dataview / dataviewjs / tasks работают только в Obsidian
    md = re.sub(r"^```(?:dataview|dataviewjs|tasks)\b.*?^```\s*$", OBSIDIAN_ONLY_NOTE, md, flags=re.S | re.M)
    # комментарии Obsidian %% ... %%
    md = re.sub(r"%%.*?%%", "", md, flags=re.S)
    return md


def _convert_callouts(md: str) -> str:
    lines = md.split("\n")
    out = []
    i = 0
    in_code = {idx for idx, _l, code in _iter_outside_fences(lines) if code}
    while i < len(lines):
        line = lines[i]
        m = CALLOUT_RE.match(line) if i not in in_code else None
        if not m:
            out.append(line)
            i += 1
            continue
        kind, fold, title = m.group(1).lower(), m.group(2), m.group(3).strip()
        body = []
        i += 1
        while i < len(lines) and lines[i].startswith(">"):
            body.append(re.sub(r"^> ?", "", lines[i]))
            i += 1
        body_md = _convert_callouts("\n".join(body))  # вложенные выноски
        marker = "???" if fold == "-" else "???+" if fold == "+" else "!!!"
        mk_type = CALLOUT_TYPES.get(kind, "note")
        head = f"{marker} {mk_type}"
        if title:
            head += ' "' + title.replace('"', "&quot;") + '"'
        elif kind not in CALLOUT_TYPES:
            head += f' "{kind.capitalize()}"'
        if out and out[-1].strip():
            out.append("")
        out.append(head)
        for b in _fix_blocks(body_md).split("\n"):
            out.append(("    " + b) if b.strip() else "")
        out.append("")
    return "\n".join(out)


_WIKI_RE = re.compile(r"(!?)\[\[([^\]\|#]*)(#[^\]\|]*)?(?:\|([^\]]*))?\]\]")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp")


def _convert_links(md: str, info: dict, page_url: str) -> str:
    lines = md.split("\n")
    code_lines = {idx for idx, _l, code in _iter_outside_fences(lines) if code}

    def repl(m):
        embed, target, anchor, alias = m.group(1), m.group(2).strip().rstrip("\\").strip(), m.group(3), m.group(4)
        if anchor:
            anchor = anchor.rstrip("\\")
        anchor = (anchor or "")[1:]
        low = target.lower()
        if embed and low.endswith(_IMG_EXT):
            path = posixpath.join(info["course"], target) if "/" not in target else target
            return f"![{alias or ''}]({_rel(page_url, path)})"
        if embed and (low.endswith(tuple(HIDDEN_EXT))):
            return "\n" + OBSIDIAN_ONLY_NOTE
        if not target and anchor:  # [[#Заголовок]] на этой же странице
            return f"[{alias or anchor}](#{heading_slug(anchor, '-')})"
        page = _resolve(target, info)
        text = alias or (target + (f" › {anchor}" if anchor else ""))
        if not page:
            return f"*{text}*"
        href = "<" + posixpath.relpath(page["src"], posixpath.dirname(info["src"]))
        if anchor and not anchor.startswith("^"):
            href += "#" + heading_slug(anchor, "-")
        href += ">"
        if embed:
            return f'!!! quote ""\n    [:material-file-document-outline: {text}]({href})'
        return f"[{text}]({href})"

    def repl_inline(line):
        # не трогаем `код` внутри строки
        parts = re.split(r"(`[^`]*`)", line)
        return "".join(p if p.startswith("`") else _WIKI_RE.sub(repl, p) for p in parts)

    return "\n".join(l if i in code_lines else repl_inline(l) for i, l in enumerate(lines))


def _fix_blocks(md: str) -> str:
    """
    Obsidian понимает списки с отступом 2–3 пробела и списки/таблицы/код
    сразу после абзаца. Сайту нужен отступ 4 пробела и пустая строка перед блоком.
    """
    lines = md.split("\n")
    out: list[str] = []
    stack: list[tuple[int, int, int]] = []  # (отступ маркера, колонка текста, уровень)
    fence = None
    fence_shift = 0
    prev_kind = "blank"  # blank | text | list | table | heading | fence

    for line in lines:
        # ── внутри блока кода: сдвигаем так же, как открывающую строку
        if fence is not None:
            m = FENCE_RE.match(line)
            stripped = line.expandtabs(4)
            if fence_shift > 0:
                stripped = " " * fence_shift + stripped
            elif fence_shift < 0:
                cut = min(-fence_shift, len(stripped) - len(stripped.lstrip(" ")))
                stripped = stripped[cut:]
            out.append(stripped)
            if m and m.group(2).startswith(fence) and not m.group(3).strip():
                fence = None
                prev_kind = "fence"
            continue

        raw = line.expandtabs(4)
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.lstrip(" ")

        if not body:
            out.append("")
            prev_kind = "blank"
            continue

        lm = LIST_RE.match(raw)
        fm = FENCE_RE.match(raw)

        if lm:
            marker = lm.group(2)
            # «1)» сайт не понимает — меняем на «1.»
            if marker.endswith(")"):
                marker = marker[:-1] + "."
            content_col = indent + len(lm.group(2)) + len(lm.group(3) or " ")
            while stack and not indent >= stack[-1][0] + 2:
                stack.pop()
            level = stack[-1][2] + 1 if stack else 0
            stack.append((indent, content_col, level))
            new = " " * (4 * level) + marker + " " + raw[lm.end():]
            if (prev_kind in ("text", "table", "heading") and level == 0) or prev_kind in ("fence", "cont"):
                out.append("")
            out.append(new.rstrip())
            prev_kind = "list"
            continue

        if stack and indent > stack[-1][0]:
            # продолжение пункта списка (текст или код внутри пункта)
            target = 4 * stack[-1][2] + 4
            shift = target - indent
            new = " " * target + body
            if prev_kind == "fence" and not fm:
                out.append("")
            if fm:
                fence = fm.group(2)[0] * len(fm.group(2))
                fence_shift = shift
                if prev_kind != "blank":
                    out.append("")
            out.append(new)
            prev_kind = "fence" if fm else "cont"
            continue

        if indent == 0 or not stack:
            if stack and prev_kind != "blank" and indent == 0 and not (fm or body.startswith(("|", "#", ">"))):
                # «ленивое» продолжение последнего пункта
                out.append(" " * (4 * stack[-1][2] + 4) + body)
                continue
            stack.clear()

        if fm:
            fence = fm.group(2)[0] * len(fm.group(2))
            fence_shift = 0
            if prev_kind not in ("blank",):
                out.append("")
            out.append(raw)
            prev_kind = "fence"
            continue

        is_table = body.startswith("|")
        is_heading = body.startswith("#") and re.match(r"#{1,6}\s", body)
        if is_table and prev_kind in ("text", "list", "heading"):
            out.append("")
        elif not is_table and prev_kind == "table":
            out.append("")
        elif prev_kind in ("list", "fence", "cont") and not is_heading:
            out.append("")
        out.append(raw)
        prev_kind = "table" if is_table else "heading" if is_heading else "text"

    return "\n".join(out)
