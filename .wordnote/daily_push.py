#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WordNote 每日放行脚本（方案 C）

分工：
  - Spaced Repetition 插件：负责每张卡的记忆间隔（写回 <!--SR:!日期,间隔,难度-->）
  - 本脚本：只负责"放行速度"——每天让固定数量的新词进入复习队列

判断逻辑：
  1) 扫描 Words/ 下所有词条
  2) 分类：
       pending  = 没有 #flashcards 标签（还没进队列，不参与算法）
       new      = 有 #flashcards，但没有有效复习记录（新卡）
       due      = 有 #flashcards，且 SR 到期日 <= 今天
  3) 今日已占用 = due 数量 + 未复习新卡数量
     今日可放行 = clamp(每日目标 - 今日已占用, 0, 单日新词上限)
  4) 从 pending 队列按顺序取出相应数量，加上 #flashcards 标签
  5) 生成 Review/今日复习.md，并追加日志

用法：
  python3 daily_push.py --status           # 只看统计，不修改任何文件
  python3 daily_push.py --dry-run          # 模拟执行（打印将要放行的词，不写文件）
  python3 daily_push.py                    # 正式执行
  python3 daily_push.py --init             # 初始化：只保留前 N 个词在队列里，其余收回标签
  python3 daily_push.py --date 2026-10-14  # 指定"今天"（测试用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT = SCRIPT_DIR.parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "daily_push.log"

DEFAULT_CONFIG = {
    "daily_target": 20,          # 每天希望复习的总量（到期 + 新词）
    "new_cap": 20,               # 每天最多放行多少新词
    "words_dir": "Words",        # 词条目录（相对词库根目录）
    "review_dir": "Review",      # 生成今日清单的目录
    "flashcard_tag": "flashcards",
    "order": "created_then_name",  # 放行顺序：created_then_name / name
    "today_note": "今日复习.md",
}

FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.S)
TAGS_INLINE_RE = re.compile(r"^tags:\s*\[(.*?)\]\s*$", re.M)
TAGS_BLOCK_RE = re.compile(r"^tags:\s*\n((?:[ \t]*-[ \t]*.*\n?)+)", re.M)
SR_RE = re.compile(r"<!--SR:([^>]*)-->")
SR_SEG_RE = re.compile(r"!(\d{4})-(\d{2})-(\d{2}),(\d+),(\d+)")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def read_field(text: str, key: str, default: str = "") -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", text, re.M)
    if not m:
        return default
    return m.group(1).strip().strip('"').strip("'")


def read_tags(text: str):
    """返回 (tags, form, span)；form = 'inline' | 'block' | None"""
    m = TAGS_INLINE_RE.search(text)
    if m:
        items = [t.strip().strip('"').strip("'") for t in m.group(1).split(",") if t.strip()]
        return items, "inline", m.span()
    m = TAGS_BLOCK_RE.search(text)
    if m:
        items = []
        for line in m.group(1).splitlines():
            v = line.strip()
            if v.startswith("-"):
                v = v[1:].strip()
            if v:
                items.append(v.strip('"').strip("'"))
        return items, "block", m.span()
    return [], None, None


def write_tags(text: str, tags, form, span) -> str:
    if form == "block":
        block = "tags:\n" + "".join(f"  - {t}\n" for t in tags)
        return text[: span[0]] + block + text[span[1] :]
    inline = "tags: [" + ", ".join(tags) + "]"
    if span is None:  # 没有 tags 行时，插到 frontmatter 末尾
        m = FM_RE.match(text)
        if not m:
            return text
        idx = m.end(1)
        return text[:idx] + "\n" + inline + text[idx:]
    return text[: span[0]] + inline + text[span[1] :]


def parse_sr(text: str):
    """解析插件的复习记录，返回 [(due_or_None, interval, ease), ...]
    due=None 表示该卡还没复习过（插件用 2000-01-01 作哨兵）。"""
    out = []
    for m in SR_RE.finditer(text):
        for seg in SR_SEG_RE.finditer(m.group(1)):
            y, mo, d, interval, ease = (int(seg.group(i)) for i in range(1, 6))
            if y <= 2000:
                out.append((None, interval, ease))
            else:
                out.append((dt.date(y, mo, d), interval, ease))
    return out


def scan_words(cfg: dict):
    words_dir = VAULT / cfg["words_dir"]
    records = []
    for path in sorted(words_dir.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not FM_RE.match(text):
            continue
        tags, form, span = read_tags(text)
        released = cfg["flashcard_tag"] in tags
        srs = parse_sr(text)
        real_due = [d for d, _, _ in srs if d is not None]
        records.append(
            {
                "path": path,
                "name": path.stem,
                "word": read_field(text, "word", path.stem),
                "created": read_field(text, "created", "9999-99-99"),
                "tags": tags,
                "form": form,
                "span": span,
                "text": text,
                "released": released,
                "has_history": bool(real_due),
                "due": min(real_due) if real_due else None,
            }
        )
    return records


def sort_key(rec, order: str):
    if order == "name":
        return rec["name"]
    return (rec["created"] or "9999-99-99", rec["name"])


def plan(cfg: dict, records, today: dt.date):
    pending = [r for r in records if not r["released"]]
    released = [r for r in records if r["released"]]
    due = [r for r in released if r["due"] and r["due"] <= today]
    fresh = [r for r in released if not r["has_history"]]      # 已放行但还没复习过
    mastered = [r for r in released if r["has_history"] and r["due"] and r["due"] > today]

    occupied = len(due) + len(fresh)
    quota = max(0, cfg["daily_target"] - occupied)
    quota = min(quota, cfg["new_cap"], len(pending))

    pending.sort(key=lambda r: sort_key(r, cfg["order"]))
    to_release = pending[:quota]

    return {
        "pending": pending,
        "due": due,
        "fresh": fresh,
        "mastered": mastered,
        "to_release": to_release,
        "occupied": occupied,
        "quota": quota,
    }


def render_today_note(cfg, result, today, dry_run: bool) -> str:
    lines = [
        "---",
        f"created: {today.isoformat()}",
        "tags: [wordnote, daily]",
        "---",
        "",
        f"# 今日复习 · {today.isoformat()}",
        "",
        f"- 每日目标：{cfg['daily_target']}",
        f"- 今天到期复习：{len(result['due'])}",
        f"- 队列里待复习的新卡：{len(result['fresh'])}",
        f"- 今天新放行：{len(result['to_release'])}",
        f"- 还在排队（未放行）：{len(result['pending']) - len(result['to_release'])}",
        "",
        "> 打开左侧「复习卡片」图标开始；本文件只是清单，不用手动勾选。",
        "",
    ]
    if result["to_release"]:
        lines += ["## 🆕 今天新放行", ""]
        lines += [f"- [[{r['name']}]]" for r in result["to_release"]]
        lines += [""]
    if result["fresh"]:
        lines += ["## ⏳ 队列中的新卡（昨天没刷完）", ""]
        lines += [f"- [[{r['name']}]]" for r in result["fresh"]]
        lines += [""]
    if result["due"]:
        lines += ["## 📌 今天到期复习", ""]
        lines += [f"- [[{r['name']}]]（{r['due'].isoformat()}）" for r in result["due"]]
        lines += [""]
    return "\n".join(lines)


def apply_release(cfg, rec) -> str:
    tags = list(rec["tags"])
    if cfg["flashcard_tag"] not in tags:
        tags.append(cfg["flashcard_tag"])
    return write_tags(rec["text"], tags, rec["form"], rec["span"])


def apply_unrelease(cfg, rec) -> str:
    tags = [t for t in rec["tags"] if t != cfg["flashcard_tag"]]
    return write_tags(rec["text"], tags, rec["form"], rec["span"])


def append_log(line: str):
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只看统计，不改动")
    ap.add_argument("--dry-run", action="store_true", help="模拟执行，不写文件")
    ap.add_argument("--init", action="store_true", help="初始化：保留前 N 个在队列，其余收回标签")
    ap.add_argument("--date", help="指定今天日期 YYYY-MM-DD（默认取系统日期）")
    args = ap.parse_args()

    cfg = load_config()
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    records = scan_words(cfg)

    if args.init:
        ordered = sorted(records, key=lambda r: sort_key(r, cfg["order"]))
        keep, rest = ordered[: cfg["daily_target"]], ordered[cfg["daily_target"] :]
        print(f"[init] 队列保留 {len(keep)} 个（前 {cfg['daily_target']} 个），收回 {len(rest)} 个的标签")
        if not args.dry_run:
            for r in keep:
                r["path"].write_text(apply_release(cfg, r), encoding="utf-8")
            for r in rest:
                r["path"].write_text(apply_unrelease(cfg, r), encoding="utf-8")
            append_log(f"{today} init: keep={len(keep)} unreleased={len(rest)}")
        print("保留示例:", ", ".join(r["name"] for r in keep[:10]), "...")
        print("收回示例:", ", ".join(r["name"] for r in rest[:10]), "...")
        return

    result = plan(cfg, records, today)
    print("=== WordNote 每日放行 ===")
    print(f"今天: {today}")
    print(f"词库总数: {len(records)}")
    print(f"  ├─ 队列中已放行: 到期 {len(result['due'])} / 新卡 {len(result['fresh'])} / 已排期 {len(result['mastered'])}")
    print(f"  └─ 未放行(排队): {len(result['pending'])}")
    print(f"每日目标: {cfg['daily_target']} | 今日已占用: {result['occupied']} | 今日可放行: {result['quota']}")

    if args.status:
        print("\n[status] 只读模式，未做任何修改")
        if result["to_release"]:
            print("将要放行:", ", ".join(r["name"] for r in result["to_release"]))
        return

    note_path = VAULT / cfg["review_dir"] / cfg["today_note"]
    if result["to_release"]:
        print("\n放行单词:", ", ".join(r["name"] for r in result["to_release"]))
        if not args.dry_run:
            for r in result["to_release"]:
                r["path"].write_text(apply_release(cfg, r), encoding="utf-8")
    else:
        print("\n今天不需要放行新词（队列已满或没有待放行）")

    if not args.dry_run:
        note_path.write_text(render_today_note(cfg, result, today, args.dry_run), encoding="utf-8")
        append_log(
            f"{today} due={len(result['due'])} fresh={len(result['fresh'])} released={len(result['to_release'])} pending={len(result['pending']) - len(result['to_release'])}"
        )
        print(f"\n已更新: {note_path}")
    else:
        print("\n[dry-run] 未写入任何文件")
        print("--- 今日清单预览 ---")
        print(render_today_note(cfg, result, today, True))


if __name__ == "__main__":
    main()
