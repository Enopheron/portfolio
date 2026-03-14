#!/usr/bin/env python3
"""
show-links — todo.txt tree viewer

  ↑ ↓        navigation
  → / space  expand / collapse
  ←          collapse / go to parent
  v          switch view (Tree ↔ Day)
  f          search (text) / goto branch (digits)
  l n d c    toggles
  s @ +      filters
  enter      open in $EDITOR
  ctrl+enter open task note
  r          branch from line
  esc        reset
  q          quit
"""
from __future__ import annotations

import datetime
import os
import re
import select
import sys
import termios
import tty
from dataclasses import dataclass, field
from enum import Enum, auto
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from rich.console import Console
from rich.style import Style
from rich.text import Text

# ── Paths ──────────────────────────────────────────────────────────────
BASE_DIR  = Path.home() / "Documents" / "todo"
NOTES_DIR = BASE_DIR / "notes"

# ── Palette ────────────────────────────────────────────────────────────
C = dict(
    red="#d78787", blue="#87afd7", yellow="#d7af87", green="#87af87",
    mag="#af87af", cyan="#7aafaf", gray="#8a8a8a", dim="#585858",
    orange="#af875f", white="#c6c6c6", sep="#818181",
)
PRIO  = {"A": C["red"],    "B": C["blue"],   "C": C["sep"]}
STAT  = {"idea": C["yellow"], "todo": C["dim"], "run": C["blue"],
         "hold": C["orange"],  "lock": C["red"]}
NTYPE = {"OBS": C["sep"], "HYP": C["green"], "EVAL": C["yellow"],
         "DO": C["blue"], "RES": C["mag"], "HOLD": C["orange"], "LOCK": C["red"]}

# ── Right-panel column widths ───────────────────────────────────────────
_COL_PROG  = 3   # "2/5"
_COL_DUE   = 5   # "15.03"
_COL_ND    = 3   # "3d"
_COL_PRIO  = 1   # "A"
_COL_STAT  = 4   # "hold"
_COL_TYPE  = 4   # "dev "
_COL_CTX   = 6   # "@home "
_SEP_W     = 1   # "│"
_RIGHT_W   = (_COL_PROG + _COL_DUE + _COL_ND + _COL_PRIO +
              _COL_STAT + _COL_TYPE + _COL_CTX + _SEP_W * 6)

# ── Regexes ────────────────────────────────────────────────────────────
_RE_PRIORITY = re.compile(r"\(([A-Z])\)")
_RE_TITLE    = re.compile(r"^(?:x\s+)?(?:\([A-Z]\)\s+)?(.+?)(?:\s+(?:type:|st:|@|id:|link:|due:|rec:))")
_RE_TASK_ID  = re.compile(r"id:(\S+)")
_RE_LINK     = re.compile(r"link:(\S+)")
_RE_TYPE     = re.compile(r"type:(\S+)")
_RE_STATUS   = re.compile(r"st:(\S+)")
_RE_TAGS     = re.compile(r"\+(\S+)")
_RE_CTX      = re.compile(r"@(\S+)")
_RE_DUE      = re.compile(r"due:([\d-]+)")
_RE_HEADER   = re.compile(r"^(#+)\s+(.+)")
_RE_NDATE    = re.compile(r"date:([\d-]+)")
_RE_TAG_PAT  = re.compile(r"(\+\S+)")
_RE_MD       = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`")
_RE_ANSI     = re.compile(r"\x1b\[[0-9;]*m")

# ── Date helpers ───────────────────────────────────────────────────────
def _today() -> datetime.date:
    return datetime.date.today()

def _today_str() -> str:
    return _today().isoformat()

def _days_diff(from_d: str, to_d: str) -> Optional[int]:
    try:
        return (datetime.date.fromisoformat(to_d) - datetime.date.fromisoformat(from_d)).days
    except Exception:
        return None

def _fmt_date(d: Optional[str]) -> str:
    if not d: return ""
    p = d.split("-")
    return f"{p[2]}.{p[1]}" if len(p) == 3 else d

# ── Text helpers ───────────────────────────────────────────────────────
def _vlen(s: str) -> int:
    return len(_RE_ANSI.sub("", s))

def _ex(pat: re.Pattern, s: str) -> Optional[str]:
    m = pat.search(s)
    return m.group(1) if m else None

def _title_text(title: str, text_col: str, tag_col: str) -> Text:
    tx = Text(no_wrap=False)
    for part in _RE_TAG_PAT.split(title):
        tx.append(part, style=tag_col if _RE_TAG_PAT.match(part) else text_col)
    return tx

def _md_text(raw: str, base: str) -> Text:
    tx = Text(no_wrap=False); pos = 0
    for m in _RE_MD.finditer(raw):
        if m.start() > pos:
            tx.append(raw[pos:m.start()], style=base)
        if m.group(1) is not None:   tx.append(m.group(1), style=f"{base} bold")
        elif m.group(2) is not None: tx.append(m.group(2), style=f"{C['dim']} italic")
        else:                        tx.append(m.group(3), style=f"{C['dim']} on #303030")
        pos = m.end()
    if pos < len(raw):
        tx.append(raw[pos:], style=base)
    return tx

# ── Data model ─────────────────────────────────────────────────────────
@dataclass
class RNote:
    title: str
    ntype: Optional[str]  = None
    date:  Optional[str]  = None
    nid:   Optional[str]  = None
    link:  Optional[str]  = None
    level: int            = 2
    line_num: int         = 0
    filepath: Optional[Path] = None
    content:  Optional[str]  = None
    content_lines: List[Tuple[int, str]] = field(default_factory=list)
    children: List["RNote"] = field(default_factory=list)

@dataclass
class ContentLine:
    line_num: int
    filepath: Optional[Path] = None

@dataclass
class Task:
    line_num: int; raw: str; title: str
    done:     bool          = False
    priority: Optional[str] = None
    tid:      Optional[str] = None
    status:   Optional[str] = None
    link:     Optional[str] = None
    ttype:    Optional[str] = None
    tags:     List[str]     = field(default_factory=list)
    tags_lc:  Set[str]      = field(default_factory=set)   # lowercase cache
    ctx:      Optional[str] = None
    due:      Optional[str] = None
    filepath: Optional[Path] = None
    notes:    List[RNote]   = field(default_factory=list)

# ── Parsing ────────────────────────────────────────────────────────────
def parse_task(line: str, n: int) -> Optional[Task]:
    s = line.strip()
    if not s or s.startswith("#"): return None
    m = _RE_TITLE.match(line)
    tags = _RE_TAGS.findall(line)
    return Task(
        line_num=n, raw=line, title=m.group(1).strip() if m else s,
        done=s.startswith("x "), priority=_ex(_RE_PRIORITY, line),
        tid=_ex(_RE_TASK_ID, line), link=_ex(_RE_LINK, line),
        status=_ex(_RE_STATUS, line),
        ttype=_ex(_RE_TYPE, line),  tags=tags,
        tags_lc={t.lower() for t in tags},
        ctx=_ex(_RE_CTX, line),     due=_ex(_RE_DUE, line),
    )

def parse_notes(content: str, filepath: Optional[Path] = None) -> List[RNote]:
    notes: List[RNote] = []; stack: List[RNote] = []
    buf: List[Tuple[int,str]] = []; col = False
    for lineno, line in enumerate(content.split("\n"), 1):
        hm = _RE_HEADER.match(line)
        if hm:
            lv, raw = len(hm.group(1)), hm.group(2).strip()
            if col and stack and buf:
                stack[-1].content = "\n".join(l for _, l in buf).strip()
                stack[-1].content_lines = buf; buf = []
            while stack and stack[-1].level >= lv: stack.pop()
            if "type:" in raw:
                t = raw
                for pat in (_RE_TYPE, _RE_NDATE, _RE_TASK_ID, _RE_LINK):
                    t = pat.sub("", t)
                note = RNote(
                    title=t.strip(), ntype=_ex(_RE_TYPE, raw),
                    date=_ex(_RE_NDATE, raw), nid=_ex(_RE_TASK_ID, raw),
                    link=_ex(_RE_LINK, raw), level=lv,
                    line_num=lineno, filepath=filepath,
                )
                (stack[-1].children if stack else notes).append(note)
                stack.append(note); col = True
            else:
                col = False
        elif col and line.strip():
            buf.append((lineno, line))
    if col and stack and buf:
        stack[-1].content = "\n".join(l for _, l in buf).strip()
        stack[-1].content_lines = buf
    return notes

def load_tasks(base: Path, nd: Path) -> List[Task]:
    tasks: List[Task] = []
    for fn in ("todo.txt", "done.txt"):
        fp = base / fn
        if not fp.exists(): continue
        try:
            with open(fp, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    t = parse_task(line, n)
                    if not (t and t.tid): continue
                    t.filepath = fp
                    nf = nd / f"{t.tid}.md"
                    if nf.exists():
                        try: t.notes = parse_notes(nf.read_text(encoding="utf-8"), filepath=nf)
                        except: pass
                    tasks.append(t)
        except: pass

    # Deduplicate recurring tasks (rec:): same id may appear many times.
    # Keep only: active (non-done) if exists, else the one with latest due date.
    _RE_REC = re.compile(r"\brec:\S+")
    seen_rec: Dict[str, Task] = {}
    result: List[Task] = []
    for t in tasks:
        if not _RE_REC.search(t.raw):
            result.append(t); continue
        tid = t.tid
        if tid not in seen_rec:
            seen_rec[tid] = t
        else:
            prev = seen_rec[tid]
            # prefer active over done
            if prev.done and not t.done:
                seen_rec[tid] = t
            elif prev.done == t.done:
                # both same status — keep latest due
                if (t.due or "") > (prev.due or ""):
                    seen_rec[tid] = t
    for t in tasks:
        if _RE_REC.search(t.raw) and seen_rec.get(t.tid) is t:
            result.append(t)
    return result

def build_rels(tasks: List[Task]):
    id2t: Dict[str, Task] = {t.tid: t for t in tasks if t.tid}
    n2t:  Dict[str, Task] = {}
    for t in tasks:
        queue = list(t.notes)
        while queue:
            note = queue.pop()
            if note.nid: n2t[note.nid] = t
            queue.extend(note.children)
    rels: Dict[str, list] = {}
    c2p:  Dict[str, str]  = {}
    nrels: Dict[str, list] = {}
    for t in tasks:
        if not t.link: continue
        if t.link in id2t:
            rels.setdefault(t.link, []).append((t, "link"))
            c2p[t.tid] = t.link
        elif t.link in n2t:
            c2p[t.tid] = n2t[t.link].tid
            nrels.setdefault(t.link, []).append(t)
    return rels, id2t, c2p, n2t, nrels

# ── Labels ─────────────────────────────────────────────────────────────
def _task_title(t: Task, show_done: bool = False) -> Text:
    """Left part: [N] Title only."""
    dim = t.done and show_done
    d = C["dim"]; g = C["gray"]
    def s(col): return d if dim else col
    tx = Text(no_wrap=False)
    tx.append(f"[{t.line_num}]", style=s(C["cyan"]))
    tx.append(" ")
    tx.append_text(_title_text(t.title, s(C["white"]), s(g)))
    return tx

def _task_right(t: Task, show_done: bool = False,
                progress: Optional[Tuple[int,int]] = None,
                deadline: Optional[str] = None) -> Text:
    """Right part: fixed-width table columns.
    prog │ due   │ nd  │ P │ stat │ type  │ @ctx
    """
    dim = t.done and show_done
    d = C["dim"]; g = C["gray"]
    def s(col): return d if dim else col

    tx = Text(no_wrap=False)

    def cell(val: str, style: str, width: int) -> None:
        """Fixed-width column: value left-aligned, empty → dim spaces."""
        if val:
            tx.append(val[:width].ljust(width), style=style)
        else:
            tx.append(" " * width, style=C["dim"])

    def sep() -> None:
        tx.append("│", style=C["dim"])

    # 1. Progress  "2/5"
    prog_str = f"{progress[0]}/{progress[1]}" if progress is not None else ""
    all_done = progress is not None and progress[0] == progress[1]
    prog_style = (s(g) + " on #2e2e2e") if all_done else s(g)
    if prog_str:
        tx.append(prog_str[:_COL_PROG].ljust(_COL_PROG), style=prog_style)
    else:
        tx.append(" " * _COL_PROG, style=C["dim"])
    sep()

    # 2. Due date  "15.03"  — always dim/gray
    due_str = _fmt_date(t.due) if t.due else ""
    cell(due_str, s(C["dim"]), _COL_DUE)
    sep()

    # 3. Slack: days between task.due and parent deadline  "3d"  — red ≤1, blue 2-3, green >3
    nd_str = ""; nd_col = g
    if deadline and t.due and deadline != t.due:
        nd = _days_diff(t.due, deadline)
        if nd is not None and nd >= 0:
            nd_str = f"{nd}d"
            nd_col = C["red"] if nd <= 1 else (C["blue"] if nd <= 3 else C["green"])
    cell(nd_str, s(nd_col), _COL_ND)
    sep()

    # 4. Priority  "A"
    cell(t.priority or "", s(PRIO.get(t.priority or "", g)), _COL_PRIO)
    sep()

    # 5. Status  "run"
    cell(t.status or "", s(STAT.get(t.status or "", g)), _COL_STAT)
    sep()

    # 6. Type
    cell(t.ttype or "", s(C["mag"]), _COL_TYPE)
    sep()

    # 7. @ctx
    ctx_str = f"@{t.ctx}" if t.ctx else ""
    cell(ctx_str, s(C["gray"]), _COL_CTX)

    return tx

def _render_task_row(prefix: str, task: Task, is_cursor: bool, w: int,
                     show_done: bool, progress: Optional[Tuple[int,int]],
                     deadline: Optional[str]) -> str:
    """Unified task row renderer for both tree and day views."""
    left  = _task_title(task, show_done)
    right = _task_right(task, show_done, progress, deadline)

    # Build full left text with dim-styled prefix
    full_left = Text(no_wrap=False)
    if prefix:
        full_left.append(prefix, style=C["dim"])
    full_left.append_text(left)

    if is_cursor:
        full_left.stylize(Style(bgcolor="#2e2e2e"))
        right.stylize(Style(bgcolor="#2e2e2e"))

    right_s = _to_ansi(right, _RIGHT_W + 4).rstrip("\n")
    right_v = _vlen(right_s)
    avail   = max(1, w - right_v - 2)
    left_s  = _to_ansi(full_left, avail).rstrip("\n")
    left_v  = _vlen(left_s)
    gap     = max(1, w - left_v - right_v - 1)
    return left_s + " " * gap + right_s

def note_label(n: RNote) -> Text:
    """Left part only — date goes right via _render_note_row."""
    nt = (n.ntype or "NOTE").upper()
    tx = Text(no_wrap=False)
    tx.append(f"[{nt}]", style=NTYPE.get(nt, C["cyan"]))
    tx.append(f" {n.title}", style=C["white"])
    return tx

def _render_note_row(prefix: str, node_text: Text, date: Optional[str],
                     is_cursor: bool, w: int) -> str:
    left = Text(no_wrap=False)
    if prefix: left.append(prefix, style=C["dim"])
    left.append_text(node_text)

    # Build right panel same width as task rows so the date column aligns.
    # Layout: [prog_col][sep][due_col][sep][...rest as spaces...]
    right = Text(no_wrap=False)
    right.append(" " * _COL_PROG, style=C["dim"])          # empty progress
    right.append("│", style=C["dim"])
    if date:
        right.append(_fmt_date(date)[:_COL_DUE].ljust(_COL_DUE), style=C["dim"])
    else:
        right.append(" " * _COL_DUE, style=C["dim"])
    right.append("│", style=C["dim"])
    # remaining columns — blank filler to keep total width
    rest = _RIGHT_W - _COL_PROG - _SEP_W - _COL_DUE - _SEP_W
    right.append(" " * rest, style=C["dim"])

    if is_cursor:
        left.stylize(Style(bgcolor="#2e2e2e"))
        right.stylize(Style(bgcolor="#2e2e2e"))

    right_s = _to_ansi(right, _RIGHT_W + 4).rstrip("\n")
    right_v = _vlen(right_s)
    avail   = max(1, w - right_v - 2)
    left_s  = _to_ansi(left, avail).rstrip("\n")
    left_v  = _vlen(left_s)
    gap     = max(1, w - left_v - right_v - 1)
    return left_s + " " * gap + right_s

# ── View tree ──────────────────────────────────────────────────────────
@dataclass
class VNode:
    text:     Text
    data:     Any
    children: List["VNode"] = field(default_factory=list)
    expanded: bool          = True
    is_leaf:  bool          = False

@dataclass
class FlatItem:
    prefix:     str
    node:       VNode
    parent_idx: int = -1

def flatten(nodes: List[VNode], result: Optional[List[FlatItem]] = None,
            anc_last: Optional[List[bool]] = None, parent_idx: int = -1) -> List[FlatItem]:
    if result is None:   result = []
    if anc_last is None: anc_last = []
    for i, node in enumerate(nodes):
        is_last = (i == len(nodes) - 1)
        guide = ""
        for j, al in enumerate(anc_last):
            guide += " " if j == 0 else ("   " if al else "│  ")
        if anc_last:
            guide += "└─ " if is_last else "├─ "
        my_idx = len(result)
        result.append(FlatItem(guide, node, parent_idx))
        if not node.is_leaf and node.expanded and node.children:
            anc_last.append(is_last)
            flatten(node.children, result, anc_last, my_idx)
            anc_last.pop()
    return result

# ── App state ──────────────────────────────────────────────────────────
class Mode(Enum):
    NORMAL = auto(); SEARCH = auto(); GOTO = auto(); DROPDOWN = auto()

class ViewMode(Enum):
    TREE = auto(); DAY = auto()

@dataclass
class DayItem:
    kind:     str                        # "header" | "task" | "subitem"
    date:     Optional[str]              = None
    task:     Optional[Task]             = None
    deadline: Optional[str]              = None
    vnode:    Any                        = None
    progress: Optional[Tuple[int, int]]  = None

@dataclass
class St:
    base:      Path
    notes_dir: Path
    tasks:    List[Task]      = field(default_factory=list)
    rels:     Dict            = field(default_factory=dict)
    id2t:     Dict            = field(default_factory=dict)
    c2p:      Dict            = field(default_factory=dict)
    n2t:      Dict            = field(default_factory=dict)
    nrels:    Dict            = field(default_factory=dict)
    # line_num → Task index for fast lookup
    ln2t:     Dict[int, Task] = field(default_factory=dict)
    roots:    List[VNode]     = field(default_factory=list)
    flat:     List[FlatItem]  = field(default_factory=list)
    cursor:   int             = 0
    scroll:   int             = 0
    opt_done:    bool = False
    opt_nonotes: bool = True
    opt_linked:  bool = False
    opt_content: bool = False
    flt_status:  str  = ""
    flt_ctx:     str  = ""
    flt_item:    str  = ""
    flt_search:  str  = ""
    root_tid:        str  = ""
    root_via_goto:   bool = False  # True = set via 'r', False = set via 'f'+digits
    find_val:        str  = ""     # confirmed find input (text or digit), shown in status
    mode:      Mode    = Mode.NORMAL
    view_mode: ViewMode = ViewMode.TREE
    input_buf: str     = ""
    day_flat:   List[DayItem] = field(default_factory=list)
    day_cursor: int           = 0
    day_scroll: int           = 0
    # precomputed selectable indices for day view
    day_sel:    List[int]     = field(default_factory=list)
    dd_items:     List[str] = field(default_factory=list)
    dd_all_items: List[str] = field(default_factory=list)
    dd_field:     str  = ""
    dd_title:     str  = ""
    dd_cursor:    int  = 0
    dd_filter:    str  = ""
    dd_filtering: bool = False
    msg:          str  = ""

# ── State management ───────────────────────────────────────────────────
def do_load(st: St) -> None:
    st.tasks = load_tasks(st.base, st.notes_dir)
    st.rels, st.id2t, st.c2p, st.n2t, st.nrels = build_rels(st.tasks)
    ln2t: Dict[int, Task] = {}
    for t in st.tasks:
        if t.line_num not in ln2t:
            ln2t[t.line_num] = t
    st.ln2t = ln2t
    do_rebuild(st)

def do_rebuild(st: St) -> None:
    st.roots  = build_view(st)
    st.flat   = flatten(st.roots)
    st.cursor = max(0, min(st.cursor, len(st.flat) - 1))
    st.day_flat = build_day_flat(st)
    st.day_sel  = [i for i, d in enumerate(st.day_flat) if d.kind != "header"]
    st.day_cursor = max(0, min(st.day_cursor, max(0, len(st.day_flat) - 1)))

# ── Build view ─────────────────────────────────────────────────────────
def build_view(st: St) -> List[VNode]:
    rels = st.rels; id2t = st.id2t; n2t = st.n2t; nrels = st.nrels
    status = st.flt_status.lower()
    ctx    = st.flt_ctx.lower();    item   = st.flt_item.lower()
    search = st.flt_search.lower()
    show_done  = st.opt_done;   hide_notes = st.opt_nonotes
    only_link  = st.opt_linked; show_cont  = st.opt_content

    def vis(t: Task) -> bool:
        return show_done or not t.done

    def matches(t: Task) -> bool:
        if status and (t.status or "").lower() != status: return False
        if ctx    and (t.ctx    or "").lower() != ctx:    return False
        if item   and item not in t.tags_lc:              return False
        if search and search not in (t.title + " " + t.raw).lower(): return False
        return True

    def has_link(t: Task) -> bool:
        if t.tid in rels and any(vis(c) for c, _ in rels[t.tid]): return True
        return bool(t.link and (t.link in id2t or t.link in n2t))

    # memoised sub_matches
    _sub_cache: Dict[str, bool] = {}
    def sub_matches(t: Task) -> bool:
        if t.tid in _sub_cache: return _sub_cache[t.tid]
        result = matches(t) or any(sub_matches(c) for c, _ in rels.get(t.tid, []))
        _sub_cache[t.tid] = result
        return result

    printed: Set[str] = set()

    def make_note(note: RNote) -> VNode:
        kids: List[VNode] = []
        if show_cont and note.content:
            for lineno, ln in (note.content_lines or
                               [(note.line_num, l) for l in note.content.split("\n")]):
                if ln.strip():
                    cl = ContentLine(line_num=lineno, filepath=note.filepath)
                    kids.append(VNode(_md_text(" " + ln.strip(), C["gray"]), cl, is_leaf=True))
        for c in note.children:
            kids.append(make_note(c))
        if note.nid and note.nid in nrels:
            for ref in nrels[note.nid]:
                tn = make_task(ref)
                if tn: kids.append(tn)
        return VNode(note_label(note), note, kids, is_leaf=not kids)

    def make_task(t: Task) -> Optional[VNode]:
        if not vis(t) or t.tid in printed: return None
        if not sub_matches(t):             return None
        if only_link and not has_link(t):  return None
        printed.add(t.tid)
        kids: List[VNode] = []
        if not hide_notes:
            for note in t.notes: kids.append(make_note(note))
        for child, _ in rels.get(t.tid, []):
            cn = make_task(child)
            if cn: kids.append(cn)
        return VNode(_task_title(t, show_done), t, kids, is_leaf=not kids)

    if st.root_tid:
        t = id2t.get(st.root_tid)
        if t:
            n = make_task(t)
            return [n] if n else []
        return []

    all_cids: Set[str] = {c.tid for ch in rels.values() for c, _ in ch}
    root_ids: Set[str] = {tid for tid in rels if tid not in all_cids and tid in id2t}

    # memoised note_ref
    _nref_cache: Dict[int, bool] = {}
    def note_ref(note: RNote) -> bool:
        key = id(note)
        if key in _nref_cache: return _nref_cache[key]
        result = (bool(note.nid and note.nid in nrels and
                       any(vis(t) for t in nrels[note.nid])) or
                  any(note_ref(c) for c in note.children))
        _nref_cache[key] = result
        return result

    for t in st.tasks:
        if t.tid not in root_ids and any(note_ref(n) for n in t.notes):
            root_ids.add(t.tid)

    standalone = ([] if only_link else
                  [t for t in st.tasks
                   if t.tid not in root_ids and t.tid not in all_cids
                   and t.notes and vis(t)])

    result: List[VNode] = []
    for tid in root_ids:
        t = id2t.get(tid)
        if t:
            n = make_task(t)
            if n: result.append(n)
    for t in standalone:
        n = make_task(t)
        if n: result.append(n)
    return result

# ── Day view ───────────────────────────────────────────────────────────
def _get_deadline(t: Task, id2t: Dict, c2p: Dict) -> Optional[str]:
    pid = c2p.get(t.tid)
    return id2t[pid].due if pid and pid in id2t else None

def _branch_progress(t: Task, rels: Dict) -> Optional[Tuple[int, int]]:
    def collect(tid: str) -> Tuple[int, int]:
        done = total = 0
        for child, _ in rels.get(tid, []):
            total += 1
            if child.done: done += 1
            d, tt = collect(child.tid)
            done += d; total += tt
        return done, total
    done, total = collect(t.tid)
    if total == 0: return None
    return (done, total)

def build_day_flat(st: St) -> List[DayItem]:
    id2t = st.id2t; c2p = st.c2p
    entries: List[Tuple[Task, List[VNode]]] = []

    def collect(nodes: List[VNode]) -> None:
        for node in nodes:
            if isinstance(node.data, Task):
                entries.append((node.data, list(node.children)))
            collect(node.children)

    collect(st.roots)

    dated = [(t, ch) for t, ch in entries if t.due]
    dated.sort(key=lambda x: x[0].due)  # type: ignore[return-value]

    result: List[DayItem] = []
    seen:   Set[str] = set()
    current_date: Optional[str] = None

    for t, children in dated:
        if t.tid in seen: continue
        seen.add(t.tid)
        if t.due != current_date:
            current_date = t.due
            result.append(DayItem(kind="header", date=t.due))
        deadline = _get_deadline(t, id2t, c2p)
        progress = _branch_progress(t, st.rels)
        result.append(DayItem(kind="task", date=t.due, task=t,
                              deadline=deadline, progress=progress))

        def add_sub(child_nodes: List[VNode]) -> None:
            for child in child_nodes:
                if isinstance(child.data, (RNote, ContentLine)):
                    result.append(DayItem(kind="subitem", date=t.due, task=t, vnode=child))
                    add_sub(child.children)
        add_sub(children)

    return result

# ── Day rendering ──────────────────────────────────────────────────────
def _render_day_header(date: str, w: int) -> str:
    label = f" {_fmt_date(date)} "
    rest  = w - len(label) - 4
    DIM = "\x1b[38;5;240m"; DATE = "\x1b[38;5;179m"; RST = "\x1b[0m"
    return f"\n{DIM}──{DATE}{label}{DIM}{'─' * max(0, rest)}{RST}"

def _render_day_task(item: DayItem, is_cursor: bool, w: int, show_done: bool) -> str:
    return _render_task_row("  ", item.task, is_cursor, w,  # type: ignore
                            show_done, item.progress, item.deadline)

def _render_day_subitem(item: DayItem, is_cursor: bool, w: int) -> str:
    tx = Text(no_wrap=False)
    tx.append("      ")
    tx.append_text(item.vnode.text)
    if is_cursor:
        tx.stylize(Style(bgcolor="#2e2e2e"))
    return _to_ansi(tx, w)

# ── Rich → ANSI ────────────────────────────────────────────────────────
_RENDER_CON: Optional[Console] = None

def _to_ansi(text: Text, w: int, wrap: bool = False) -> str:
    global _RENDER_CON
    if _RENDER_CON is None or _RENDER_CON.width != w:
        _RENDER_CON = Console(file=StringIO(), width=w, highlight=False,
                              force_terminal=True, force_jupyter=False, no_color=False)
    _RENDER_CON._file = StringIO()
    if wrap:
        _RENDER_CON.print(text, end="")
    else:
        _RENDER_CON.print(text, end="", overflow="ellipsis", no_wrap=True, crop=True)
    return _RENDER_CON._file.getvalue()

# ── Terminal helpers ───────────────────────────────────────────────────
def term_size() -> Tuple[int, int]:
    try:    cols, rows = os.get_terminal_size(sys.stdout.fileno())
    except OSError:
        try: cols, rows = os.get_terminal_size(sys.stderr.fileno())
        except OSError: rows, cols = 24, 80
    return rows, cols

def screen_on()  -> None: sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[H");  sys.stdout.flush()
def screen_off() -> None: sys.stdout.write("\x1b[?25h\x1b[?1049l");         sys.stdout.flush()

# ── Drawing ────────────────────────────────────────────────────────────
def _search_bar(st: St, w: int) -> str:
    t = Text(no_wrap=True)
    t.append("  / ", style=C["cyan"])
    t.append(st.input_buf, style=C["white"])
    t.append("█", style=C["dim"])
    return _to_ansi(t, w)

def _write_screen(rows: List[str], h: int, w: int) -> None:
    out = ["\x1b[H"]
    for i, line in enumerate(rows[:h]):
        pad = max(0, w - 1 - _vlen(line))
        out.append(f"\x1b[{i+1};1H{line}{' ' * pad}\x1b[K")
    sys.stdout.write("".join(out))
    sys.stdout.flush()

def _overlay_modal(box: List[str], body_rows: List[str], body_h: int, w: int) -> None:
    bh = len(box)
    bw = max((_vlen(l) for l in box), default=0)
    row0 = max(0, (body_h - bh) // 2)
    col0 = max(0, (w - bw) // 2)
    pad  = " " * col0
    for i, line in enumerate(box):
        if row0 + i < len(body_rows):
            body_rows[row0 + i] = pad + line + " " * max(0, w - col0 - _vlen(line))

def draw(st: St) -> None:
    if st.view_mode == ViewMode.DAY: _draw_day(st)
    else:                            _draw_tree(st)

def _draw_day(st: St) -> None:
    h, w = term_size()
    body_h = max(1, h - 2 - (1 if st.mode == Mode.SEARCH else 0))
    rows: List[str] = []
    if st.mode == Mode.SEARCH:
        rows.append(_search_bar(st, w))

    all_lines: List[Tuple[int, str]] = []
    for i, item in enumerate(st.day_flat):
        if item.kind == "header":
            rendered = _render_day_header(item.date or "", w)
        elif item.kind == "task":
            rendered = _render_day_task(item, i == st.day_cursor, w, st.opt_done)
        else:
            rendered = _render_day_subitem(item, i == st.day_cursor, w)
        for line in rendered.split("\n"):
            if line: all_lines.append((i, line))

    total = len(all_lines)
    if total == 0:
        st.day_scroll = 0
    else:
        cur_lines = [idx for idx, (fi, _) in enumerate(all_lines) if fi == st.day_cursor]
        if cur_lines:
            top, bot = cur_lines[0], cur_lines[-1]
            if top < st.day_scroll:               st.day_scroll = top
            if bot >= st.day_scroll + body_h:     st.day_scroll = bot - body_h + 1
        st.day_scroll = max(0, min(st.day_scroll, max(0, total - body_h)))

    body_rows = [all_lines[i][1] if i < total else ""
                 for i in range(st.day_scroll, st.day_scroll + body_h)]
    if not st.day_flat and body_rows:
        body_rows[0] = "\x1b[38;5;240m  (нет задач с датой)\x1b[0m"

    if st.mode in (Mode.GOTO, Mode.DROPDOWN):
        box = _build_goto_box(st) if st.mode == Mode.GOTO else _build_dd_box(st, body_h)
        _overlay_modal(box, body_rows, body_h, w)

    rows += body_rows
    rows.append(_to_ansi(_build_status(st), w))
    rows.append(f"\x1b[38;5;236m{'─' * (w - 1)}\x1b[0m")
    _write_screen(rows, h, w)

def _draw_tree(st: St) -> None:
    h, w = term_size()
    tree_h = max(1, h - 2 - (1 if st.mode == Mode.SEARCH else 0))
    rows: List[str] = []
    if st.mode == Mode.SEARCH:
        rows.append(_search_bar(st, w))

    all_lines: List[Tuple[int, str]] = []
    for i, fi in enumerate(st.flat):
        is_cur = (i == st.cursor)
        if isinstance(fi.node.data, Task):
            task     = fi.node.data
            progress = _branch_progress(task, st.rels)
            deadline = _get_deadline(task, st.id2t, st.c2p)
            rendered = _render_task_row(" " + fi.prefix, task, is_cur, w,
                                        st.opt_done, progress, deadline)
        elif isinstance(fi.node.data, RNote):
            note     = fi.node.data
            rendered = _render_note_row(" " + fi.prefix, fi.node.text,
                                        note.date, is_cur, w)
        else:
            t = Text(no_wrap=False)
            t.append(" ")
            if fi.prefix: t.append(fi.prefix, style=C["dim"])
            t.append_text(fi.node.text)
            if is_cur: t.stylize(Style(bgcolor="#2e2e2e"))
            rendered = _to_ansi(t, w, wrap=True).rstrip("\n")
        for line in rendered.split("\n"):
            if line: all_lines.append((i, line))

    total = len(all_lines)
    if total == 0:
        st.scroll = 0
    else:
        cur_lines = [idx for idx, (fi, _) in enumerate(all_lines) if fi == st.cursor]
        if cur_lines:
            top, bot = cur_lines[0], cur_lines[-1]
            if top < st.scroll:               st.scroll = top
            if bot >= st.scroll + tree_h:     st.scroll = bot - tree_h + 1
        st.scroll = max(0, min(st.scroll, max(0, total - tree_h)))

    tree_rows = [all_lines[i][1] if i < total else ""
                 for i in range(st.scroll, st.scroll + tree_h)]

    if st.mode in (Mode.GOTO, Mode.DROPDOWN):
        box = _build_goto_box(st) if st.mode == Mode.GOTO else _build_dd_box(st, tree_h)
        _overlay_modal(box, tree_rows, tree_h, w)

    rows += tree_rows
    rows.append(_to_ansi(_build_status(st), w))
    rows.append(f"\x1b[38;5;236m{'─' * (w - 1)}\x1b[0m")
    _write_screen(rows, h, w)

# ── Box builders ───────────────────────────────────────────────────────
def _box(inner: List[str], title: str, min_width: int = 0) -> List[str]:
    iw  = max(max((_vlen(l) for l in inner), default=20) + 4,
              len(title) + 6, min_width)
    ti  = f" {title} " if title else ""
    DIM = "\x1b[38;5;240m"; RST = "\x1b[0m"
    out = [f"{DIM}╭{ti}{'─' * max(0, iw - 2 - len(ti))}╮{RST}"]
    for l in inner:
        out.append(f"{DIM}│{RST} {l}{' ' * max(0, iw - 2 - _vlen(l))} {DIM}│{RST}")
    out.append(f"{DIM}╰{'─' * (iw - 2)}╯{RST}")
    return out

def _build_goto_box(st: St) -> List[str]:
    D = "\x1b[38;5;240m"; W = "\x1b[38;5;251m"; CY = "\x1b[38;5;109m"; R = "\x1b[0m"
    return _box(["", f"{D}  строка:{R}  {W}{st.input_buf}{CY}█{R}",
                 "", f"{D}  ↵ перейти     esc отмена{R}", ""],
                "root: дерево от строки")

def _build_dd_box(st: St, tree_h: int) -> List[str]:
    MAX = max(1, min(12, tree_h - 4))
    items = st.dd_items; cur = st.dd_cursor; n = len(items)
    start = max(0, min(cur - MAX // 2, max(0, n - MAX)))
    end   = min(n, start + MAX); start = max(0, end - MAX)
    W = "\x1b[38;5;251m"; GR = "\x1b[38;5;245m"; SEL = "\x1b[48;5;237m"; R = "\x1b[0m"
    CY = "\x1b[38;5;109m"; DIM = "\x1b[38;5;240m"
    fixed_w = max(max((len(i) + 6 for i in st.dd_all_items), default=20),
                  len(st.dd_title) + 6)
    inner: List[str] = []
    if st.dd_filtering or st.dd_filter:
        inner += [f"{DIM}  /{R} {W}{st.dd_filter}{CY}█{R}",
                  f"{DIM}  {'─' * max(0, fixed_w - 4)}{R}"]
    for i, item in enumerate(items[start:end], start):
        inner.append(f"{SEL}{W}  {item}  {R}" if i == cur else f"{GR}  {item}  {R}")
    if not items:
        inner.append(f"{DIM}  (нет совпадений){R}")
    return _box(inner, st.dd_title, min_width=fixed_w)

def _build_status(st: St) -> Text:
    def _cell(tx: Text, prefix: str, label: str, active: bool) -> None:
        col_b = f"{C['cyan']} bold" if active else f"{C['dim']} bold"
        col_t = C["white"] if active else C["dim"]
        if prefix:
            tx.append(prefix, style=col_b)   # only @ or + is bold
            tx.append(label,  style=col_t)   # full label in normal weight
        else:
            tx.append(label[0],  style=col_b)
            tx.append(label[1:], style=col_t)

    t1 = Text(no_wrap=True)
    t1.append(" ")
    for lbl, active in [("Link", st.opt_linked), ("Notes", not st.opt_nonotes),
                         ("Done", st.opt_done),   ("Content",  st.opt_content)]:
        _cell(t1, "", lbl, active); t1.append("  ")
    t1.append("¦ ", style=C["sep"])
    for prefix, lbl, fval in [("", "Status", st.flt_status),
                                ("@", "Ctx", st.flt_ctx),  ("+", "Item",  st.flt_item)]:
        _cell(t1, prefix, lbl, bool(fval))
        if fval:
            t1.append(":", style=C["white"])
            t1.append(fval, style=C["yellow"])
        t1.append("  ")
    t1.append("¦ ", style=C["sep"])

    # find_val = confirmed search (text or digit branch)
    # during SEARCH mode, show live input_buf instead
    live_find = st.input_buf if st.mode == Mode.SEARCH else st.find_val
    find_active = bool(live_find)
    root_active = bool(st.root_tid) and st.root_via_goto

    _cell(t1, "", "Find", find_active)
    if live_find:
        t1.append(" → ", style=C["sep"])
        t1.append(live_find, style=C["yellow"])
    t1.append("  ")

    _cell(t1, "", "Root", root_active)
    if root_active:
        task = st.id2t.get(st.root_tid)
        t1.append(" → ", style=C["sep"])
        t1.append(str(task.line_num) if task else st.root_tid, style=C["yellow"])
    t1.append("  ")

    _cell(t1, "", "Quit", False)
    t1.append("  ")
    t1.append("¦ ", style=C["sep"])
    view_name = "Day" if st.view_mode == ViewMode.DAY else "Tree"
    t1.append("v", style=f"{C['dim']} bold")
    t1.append(f"iew:{view_name}", style=C["dim"])
    if st.msg:
        t1.append("  "); t1.append(st.msg, style=C["cyan"])
    return t1

# ── Key reading ────────────────────────────────────────────────────────
def read_key() -> str:
    fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        b = os.read(fd, 1)
        if b == b"\x1b":
            if not select.select([sys.stdin], [], [], 0.05)[0]: return "esc"
            b2 = os.read(fd, 1)
            if b2 == b"[":
                b3 = os.read(fd, 1)
                seq = b3.decode("latin1")
                # ctrl+enter: \x1b[13;5u  (kitty/xterm modifyOtherKeys)
                if seq == "1":
                    rest = b""
                    while True:
                        ch = os.read(fd, 1)
                        rest += ch
                        if ch in (b"u", b"~", b"R"): break
                    if rest == b"3;5u": return "ctrl_enter"
                    return "?"
                return {"A":"up","B":"down","C":"right","D":"left"}.get(seq, "?")
            elif b2 == b"O":
                os.read(fd, 1); return "?"
            else:
                ch2 = b2.decode("utf-8", errors="replace")
                return "ctrl_enter" if ch2 in ("\r", "\n") else "alt_" + ch2
        ch = b.decode("utf-8", errors="replace")
        if ch in ("\r", "\n"): return "enter"
        if ch == "\x7f":       return "backspace"
        if ch == "\x03":       return "ctrl_c"
        if ch == "\x04":       return "ctrl_d"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ── Event handling ─────────────────────────────────────────────────────
def open_dd(st: St, field_name: str, title: str, vals: List) -> None:
    cur = getattr(st, field_name)
    all_vals = sorted(set(v for v in vals if v))
    st.dd_field = field_name; st.dd_title = title
    st.dd_all_items = all_vals; st.dd_items = all_vals
    st.dd_filter = ""; st.dd_filtering = False
    st.dd_cursor = all_vals.index(cur) if cur in all_vals else 0
    st.mode = Mode.DROPDOWN

def _open_editor(target: str, st: St) -> None:
    import subprocess
    editor = os.environ.get("EDITOR", "hx")
    screen_off()
    try:    subprocess.call([editor, target])
    finally: screen_on(); do_load(st)

def open_in_editor(st: St) -> None:
    if not st.flat: return
    fi = st.flat[st.cursor]; data = fi.node.data
    if data is None:
        idx = fi.parent_idx
        while idx >= 0:
            pfi = st.flat[idx]
            if pfi.node.data is not None: data = pfi.node.data; break
            idx = pfi.parent_idx
    if isinstance(data, ContentLine) and data.filepath:
        _open_editor(f"{data.filepath}:{data.line_num}", st)
    elif isinstance(data, Task) and data.filepath:
        _open_editor(f"{data.filepath}:{data.line_num}", st)
    elif isinstance(data, RNote) and data.filepath:
        _open_editor(f"{data.filepath}:{data.line_num}", st)

def open_note_in_editor(st: St) -> None:
    """Open the note file (.md) for the task under cursor."""
    if not st.flat: return
    fi = st.flat[st.cursor]; data = fi.node.data
    # walk up to nearest Task if cursor is on a note/content line
    idx = st.cursor
    while not isinstance(data, Task) and idx >= 0:
        idx = st.flat[idx].parent_idx
        data = st.flat[idx].node.data if idx >= 0 else None
    if not isinstance(data, Task) or not data.tid: return
    nf = st.notes_dir / f"{data.tid}.md"
    _open_editor(str(nf), st)

def _day_open_note_editor(st: St) -> None:
    """Open the note file (.md) for the task under cursor in day view."""
    if not st.day_flat: return
    item = st.day_flat[st.day_cursor]
    task = item.task
    if not task or not task.tid: return
    nf = st.notes_dir / f"{task.tid}.md"
    _open_editor(str(nf), st)

def _apply_search(st: St) -> None:
    buf = st.input_buf
    if buf.isdigit() and buf:
        target = st.ln2t.get(int(buf))
        st.root_tid      = target.tid if target else ""
        st.root_via_goto = False
        st.flt_search    = ""
    else:
        st.root_tid      = ""
        st.root_via_goto = False
        st.flt_search    = buf
    do_rebuild(st)

def _reset_all(st: St) -> None:
    st.flt_status = st.flt_ctx = st.flt_item = st.flt_search = ""
    st.opt_linked = st.opt_done = st.opt_content = False
    st.opt_nonotes = True
    st.root_tid = ""; st.root_via_goto = False; st.find_val = ""
    do_rebuild(st)

def handle(st: St, key: str) -> bool:
    st.msg = ""
    m = st.mode; in_day = st.view_mode == ViewMode.DAY

    if m == Mode.NORMAL:
        if key == "v":
            st.view_mode = ViewMode.DAY if st.view_mode == ViewMode.TREE else ViewMode.TREE
            return True

        if in_day:
            sel = st.day_sel
            cur_pos = sel.index(st.day_cursor) if st.day_cursor in sel else 0
            if   key == "up"    and sel: st.day_cursor = sel[max(0, cur_pos - 1)]
            elif key == "down"  and sel: st.day_cursor = sel[min(len(sel)-1, cur_pos+1)]
            elif key == "enter" and st.day_flat: _day_open_editor(st)
            elif key == "ctrl_enter" and st.day_flat: _day_open_note_editor(st)
            elif key == "f": st.mode = Mode.SEARCH; st.input_buf = ""
            elif key == "n": st.opt_nonotes = not st.opt_nonotes; do_rebuild(st)
            elif key == "d": st.opt_done    = not st.opt_done;    do_rebuild(st)
            elif key == "c": st.opt_content = not st.opt_content; do_rebuild(st)
            elif key == "s": open_dd(st, "flt_status", "status", [t.status for t in st.tasks if t.status])
            elif key == "@": open_dd(st, "flt_ctx",    "@ctx",   [t.ctx    for t in st.tasks if t.ctx])
            elif key == "+":
                vis = [d.task for d in st.day_flat if d.task]
                open_dd(st, "flt_item", "+item", [tag for t in vis for tag in t.tags])
            elif key == "r": st.mode = Mode.GOTO; st.input_buf = ""
            elif key == "esc":
                if st.root_tid or st.find_val:
                    st.root_tid = ""; st.root_via_goto = False
                    st.find_val = ""; st.flt_search = ""
                    do_rebuild(st)
                else: _reset_all(st)
            elif key in ("q", "ctrl_c", "ctrl_d"): return False
            return True

        n = len(st.flat)
        if   key == "up"   and n: st.cursor = max(0, st.cursor - 1)
        elif key == "down" and n: st.cursor = min(n - 1, st.cursor + 1)
        elif key in ("right", " ") and n:
            fi = st.flat[st.cursor]
            if not fi.node.is_leaf and not fi.node.expanded:
                fi.node.expanded = True; st.flat = flatten(st.roots)
        elif key == "left" and n:
            fi = st.flat[st.cursor]
            if not fi.node.is_leaf and fi.node.expanded:
                fi.node.expanded = False
                st.flat = flatten(st.roots)
                st.cursor = min(st.cursor, len(st.flat) - 1)
            elif fi.parent_idx >= 0:
                st.cursor = fi.parent_idx
        elif key == "f": st.mode = Mode.SEARCH; st.input_buf = ""
        elif key == "l": st.opt_linked  = not st.opt_linked;  do_rebuild(st)
        elif key == "n": st.opt_nonotes = not st.opt_nonotes; do_rebuild(st)
        elif key == "d": st.opt_done    = not st.opt_done;    do_rebuild(st)
        elif key == "c": st.opt_content = not st.opt_content; do_rebuild(st)
        elif key == "r": st.mode = Mode.GOTO; st.input_buf = ""
        elif key == "s": open_dd(st, "flt_status", "status", [t.status for t in st.tasks if t.status])
        elif key == "@": open_dd(st, "flt_ctx",    "@ctx",   [t.ctx    for t in st.tasks if t.ctx])
        elif key == "+":
            vis_tasks = [fi.node.data for fi in st.flat if isinstance(fi.node.data, Task)]
            open_dd(st, "flt_item", "+item", [tag for t in vis_tasks for tag in t.tags])
        elif key == "enter" and st.flat: open_in_editor(st)
        elif key == "ctrl_enter" and st.flat: open_note_in_editor(st)
        elif key == "esc":
            if st.root_tid or st.find_val:
                st.root_tid = ""; st.root_via_goto = False
                st.find_val = ""; st.flt_search = ""
                do_rebuild(st)
            else: _reset_all(st)
        elif key in ("q", "ctrl_c", "ctrl_d"): return False

    elif m == Mode.SEARCH:
        if key == "esc":
            st.flt_search = ""; st.root_tid = ""; st.root_via_goto = False
            st.find_val = ""; st.input_buf = ""
            st.mode = Mode.NORMAL; do_rebuild(st)
        elif key == "enter":
            st.find_val = st.input_buf  # save whatever was typed (text or digits)
            st.mode = Mode.NORMAL
        elif key == "backspace":
            st.input_buf = st.input_buf[:-1]; _apply_search(st)
        elif len(key) == 1 and key.isprintable():
            st.input_buf += key; _apply_search(st)

    elif m == Mode.GOTO:
        if key == "esc":
            st.mode = Mode.NORMAL; st.input_buf = ""
        elif key == "backspace":
            st.input_buf = st.input_buf[:-1]
        elif key == "enter":
            st.mode = Mode.NORMAL
            if st.input_buf.isdigit():
                target = st.ln2t.get(int(st.input_buf))
                if target:
                    root = target; seen = {target.tid}
                    while root.tid in st.c2p:
                        pid = st.c2p[root.tid]
                        if pid in seen: break
                        seen.add(pid); root = st.id2t[pid]
                    st.root_tid      = root.tid
                    st.root_via_goto = True
                    do_rebuild(st)
                else:
                    st.msg = f"строка {st.input_buf} не найдена"
            st.input_buf = ""
        elif key.isdigit():
            st.input_buf += key

    elif m == Mode.DROPDOWN:
        if st.dd_filtering:
            if key == "esc":
                st.dd_filter = ""; st.dd_filtering = False
                st.dd_items = st.dd_all_items; st.dd_cursor = 0
            elif key == "enter":
                val = st.dd_items[st.dd_cursor] if st.dd_items else None
                if val is not None: setattr(st, st.dd_field, val)
                st.dd_filtering = False
                st.mode = Mode.NORMAL; do_rebuild(st)
            elif key == "backspace":
                st.dd_filter = st.dd_filter[:-1]
                q = st.dd_filter.lower()
                st.dd_items = [v for v in st.dd_all_items if q in v.lower()]
                st.dd_cursor = 0
            elif len(key) == 1 and key.isprintable():
                st.dd_filter += key
                q = st.dd_filter.lower()
                st.dd_items = [v for v in st.dd_all_items if q in v.lower()]
                st.dd_cursor = 0
        else:
            if   key == "up":   st.dd_cursor = max(0, st.dd_cursor - 1)
            elif key == "down": st.dd_cursor = min(len(st.dd_items) - 1, st.dd_cursor + 1)
            elif key == "enter":
                val = st.dd_items[st.dd_cursor] if st.dd_items else None
                if val is not None: setattr(st, st.dd_field, val)
                st.mode = Mode.NORMAL; do_rebuild(st)
            elif key == "backspace" and st.dd_filter:
                st.dd_filter = st.dd_filter[:-1]
                q = st.dd_filter.lower()
                st.dd_items = [v for v in st.dd_all_items if q in v.lower()]
                st.dd_filtering = True; st.dd_cursor = 0
            elif len(key) == 1 and key.isprintable():
                st.dd_filter += key
                q = st.dd_filter.lower()
                st.dd_items = [v for v in st.dd_all_items if q in v.lower()]
                st.dd_filtering = True; st.dd_cursor = 0
            elif key == "esc":
                if st.dd_filter:
                    st.dd_filter = ""; st.dd_items = st.dd_all_items; st.dd_cursor = 0
                else:
                    st.mode = Mode.NORMAL
    return True

# ── Main ───────────────────────────────────────────────────────────────
def main() -> int:
    if not BASE_DIR.exists():
        print(f"✗ not found: {BASE_DIR}"); return 1
    st = St(base=BASE_DIR, notes_dir=NOTES_DIR)
    do_load(st)
    screen_on()
    try:
        draw(st)
        while True:
            key = read_key()
            if not handle(st, key): break
            draw(st)
    finally:
        screen_off()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
