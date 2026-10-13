"""无模型依赖的纯业务逻辑：时间格式化、发言人显示名、段落合并、
模板上下文构造、LLM 会议上下文构造。

刻意与 app.py 分离，使这些函数无需加载 FunASR 模型即可被单元测试覆盖。
所有函数均为纯函数，不修改入参（遵循不可变约定）。
"""
from __future__ import annotations

import array
import io
import json
import math
import os
import re
import struct
import sys
from typing import Any


def _clean_env_value(raw: str) -> str:
    """清洗环境变量原始值，容忍常见的 .env 手写瑕疵。

    systemd 的 EnvironmentFile 不支持行尾 `#` 注释——`KEY=524288000  # 500MB`
    会把 `524288000  # 500MB` 整个当成值，导致 int()/float() 解析崩溃。
    这里剥离行尾注释、外围引号和首尾空白，使数值型配置更健壮。
    仅当 `#` 前存在空白时才视为行尾注释，避免误伤值内合法的 `#`。
    """
    s = str(raw).strip()
    m = re.search(r"\s#", s)
    if m:
        s = s[: m.start()].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def env_int(name: str, default: int) -> int:
    """读取整数型环境变量；缺失/空/无法解析时回退到 default（清洗行尾注释后再解析）。"""
    cleaned = _clean_env_value(os.environ.get(name, ""))
    if not cleaned:
        return default
    try:
        return int(cleaned)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    """读取浮点型环境变量；缺失/空/无法解析时回退到 default（清洗行尾注释后再解析）。"""
    cleaned = _clean_env_value(os.environ.get(name, ""))
    if not cleaned:
        return default
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return default


def format_time(seconds: float) -> str:
    """秒 → HH:MM:SS（向下取整）。"""
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def speaker_display(raw: Any, speaker_map: dict[str, str] | None = None) -> str:
    """发言人显示名：优先用 speaker_map；纯数字加“说话人”前缀；其余原样返回。

    与前端 speakerLabel 语义保持一致。
    """
    key = str(raw)
    if speaker_map:
        mapped = speaker_map.get(key, "")
        if mapped and mapped.strip():
            return mapped.strip()
    return f"说话人{key}" if key.isdigit() else key


def resolve_segment_speaker(
    seg: dict, speaker_map: dict[str, str] | None = None
) -> str:
    """单段的发言人显示名，三层解析，优先级从高到低：

      1. seg["speakerName"] —— 行级覆盖（转写列表里对单行的手动修正）
      2. speaker_map[seg["speaker"]] —— 全局「映射发言人」（一个簇整场统一改名）
      3. speaker_display 的默认值 —— “说话人N” / 原样

    行级覆盖存在的理由：cam++ 有时把不同的人聚成同一个簇（张三和李四都成了
    speaker 0），只靠按簇的 speakerMap 无法拆开。此时用户在列表里逐行改名，
    结果只落在那几段的 speakerName 上，簇映射不受影响。

    与前端 segmentLabel 语义保持一致——两处必须同步改，否则界面与导出/问答
    会对同一段给出不同的名字。
    """
    override = str((seg or {}).get("speakerName", "") or "").strip()
    if override:
        return override
    return speaker_display((seg or {}).get("speaker", "?"), speaker_map)


def _join_text(a: str, b: str) -> str:
    """拼接两段文本：仅当衔接处两侧均为 ASCII 字母/数字时补空格。

    与前端 joinText 语义一致：中文之间、中文标点后不加空格。
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a:
        return b
    if not b:
        return a
    need_space = bool(re.search(r"[A-Za-z0-9]$", a)) and bool(re.match(r"[A-Za-z0-9]", b))
    return a + (" " if need_space else "") + b


# 参会人员分隔符：与前端 attendeeList 的 split 正则保持一致
_ATTENDEE_SPLIT = re.compile(r"[、,，;；\n\r\t ]+")
_ATTENDEE_KEYS = ("name", "unit", "title")


def _normalize_attendees(meta: dict) -> list[dict]:
    """把 meta.attendees 归一化为 [{name, unit, title}] 列表（纯函数，不改入参）。

    兼容三种输入：
    - list[dict]：保留 name/unit/title 并去空白
    - str（旧版本）：按分隔符拆成多个姓名，unit/title 留空
    - None/缺失：返回 []
    姓名为空的条目会被丢弃。
    """
    raw = (meta or {}).get("attendees")
    items: list[dict] = []
    if isinstance(raw, str):
        for name in _ATTENDEE_SPLIT.split(raw):
            name = name.strip()
            if name:
                items.append({"name": name, "unit": "", "title": ""})
    elif isinstance(raw, list):
        for a in raw:
            if not isinstance(a, dict):
                continue
            entry = {k: str(a.get(k, "") or "").strip() for k in _ATTENDEE_KEYS}
            if entry["name"]:
                items.append(entry)
    return items


def attendees_to_lines(items: list[dict]) -> str:
    """把结构化参会人渲染为多行字符串，每人一行：姓名（单位·职务）。

    单位/职务缺失时优雅省略：`姓名`、`姓名（单位）`、`姓名（·职务）`→`姓名（职务）`。
    """
    lines = []
    for a in items or []:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        unit = (a.get("unit") or "").strip()
        title = (a.get("title") or "").strip()
        extra = "·".join(x for x in (unit, title) if x)
        lines.append(f"{name}（{extra}）" if extra else name)
    return "\n".join(lines)


def _contact_key(name: str, unit: str) -> tuple[str, str]:
    """判重键：同名+同单位（姓名大小写不敏感、去空白；单位去空白）。"""
    return ((name or "").strip().casefold(), (unit or "").strip())


def contact_is_duplicate(existing: list[dict], name: str, unit: str = "") -> bool:
    """在既有联系人/参会人列表中，是否已存在「同名+同单位」的条目（纯函数）。

    existing 为 [{name, unit, ...}] 列表。姓名大小写不敏感、两端去空白后比较。
    供前端导入判重的对照逻辑参照，也可在后端二次校验；不做拦截，仅判定。
    """
    target = _contact_key(name, unit)
    return any(_contact_key(e.get("name", ""), e.get("unit", "")) == target for e in existing or [])


# 无单位参会人归入的分组名
_NO_UNIT_GROUP = "其他"


def attendees_by_unit(items: list[dict]) -> str:
    """把结构化参会人按单位分组渲染为多行字符串：每单位一行 `单位：张三、李四`。

    - 单位按首次出现顺序排列，单位内成员按出现顺序保留、去重。
    - 无单位者归入“其他”分组，且仅当确有此类成员时才出现该行。
    - 无任何有效参会人时返回空串。
    """
    groups: list[str] = []          # 保序的单位名列表
    members: dict[str, list[str]] = {}
    for a in items or []:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        unit = (a.get("unit") or "").strip() or _NO_UNIT_GROUP
        if unit not in members:
            members[unit] = []
            groups.append(unit)
        if name not in members[unit]:      # 同组内姓名去重
            members[unit].append(name)
    lines = [f"{unit}：{'、'.join(members[unit])}" for unit in groups]
    return "\n".join(lines)


def _speaker_unit_map(items: list[dict]) -> dict[str, str]:
    """由参会人列表构造 姓名→单位 映射，用于给发言人标注所属单位。

    姓名去空白后作键；重名以首次出现的单位为准；单位为空的条目跳过。
    """
    out: dict[str, str] = {}
    for a in items or []:
        name = (a.get("name") or "").strip()
        unit = (a.get("unit") or "").strip()
        if name and unit and name not in out:
            out[name] = unit
    return out


def build_speaker_blocks(
    segments: list[dict],
    speaker_map: dict[str, str] | None = None,
    attendees: list[dict] | None = None,
) -> list[dict]:
    """把段落按连续发言人合并成发言块，供“各发言人意见”分段展示。

    返回 [{speaker, unit, time, text}]：
    - speaker：经 speaker_map 解析后的显示名（如“张三”“说话人0”）。
    - unit：该发言人在参会人中的单位；匹配不到则为空串。
    - time：该发言块的时间范围 HH:MM:SS-HH:MM:SS（模板可按需展示）。
    - text：该发言人连续发言合并后的文本。
    连续同发言人段落合并为一块（复用 merge_segments 的相邻合并语义）。
    """
    speaker_map = speaker_map or {}
    unit_of = _speaker_unit_map(attendees or [])
    merged = merge_segments(segments or [], merge=True, speaker_map=speaker_map)
    blocks: list[dict] = []
    for s in merged:
        name = resolve_segment_speaker(s, speaker_map)
        blocks.append({
            "speaker": name,
            "unit": unit_of.get(name, ""),
            "time": f"{format_time(s.get('start', 0))}-{format_time(s.get('end', 0))}",
            "text": (s.get("text") or "").strip(),
        })
    return blocks


def build_speaker_opinions(
    speakers: list[dict] | None,
    attendees: list[dict] | None = None,
) -> list[dict]:
    """由会议总结的 speakers（[{speaker, points}]）构造模板用的发言人观点列表。

    返回 [{speaker, unit, points}]：
    - speaker：发言人显示名（沿用总结中出现的名称，后端总结已按 speakerMap 映射）。
    - unit：该发言人在参会人中的单位；姓名匹配不到则为空串。
    - points：该发言人的核心观点总结（原样保留，去首尾空白）。
    仅保留 speaker/points 至少其一非空的条目。
    """
    unit_of = _speaker_unit_map(attendees or [])
    out: list[dict] = []
    for s in speakers or []:
        if not isinstance(s, dict):
            continue
        speaker = str(s.get("speaker", "") or "").strip()
        points = _join_points(s.get("points"))
        if not (speaker or points):
            continue
        out.append({
            "speaker": speaker,
            "unit": unit_of.get(speaker, ""),
            "points": points,
        })
    return out


def build_topics(topics: list[dict] | None) -> list[dict]:
    """由会议总结的 topics 构造模板用的议题列表（纯函数）。

    返回 [{topic, discussion, conclusion, open_items}]：
    - 各字段去首尾空白；discussion/open_items 已由 parse_summary_json 归一化为
      \\n 分隔字符串（docxtpl 会把 \\n 自动转 <w:br/>）。
    - conclusion 为空时回退为“未明确”（模板里结论标签始终可见）。
    - 仅保留至少一个字段非空的条目。
    """
    out: list[dict] = []
    for t in topics or []:
        if not isinstance(t, dict):
            continue
        topic = str(t.get("topic", "") or "").strip()
        discussion = str(t.get("discussion", "") or "").strip()
        conclusion = str(t.get("conclusion", "") or "").strip()
        open_items = str(t.get("open_items", "") or "").strip()
        if not (topic or discussion or conclusion or open_items):
            continue
        out.append({
            "topic": topic,
            "discussion": discussion,
            "conclusion": conclusion or "未明确",
            "open_items": open_items,
        })
    return out


# 决议条目行首序号，用于剥离 LLM 误加的前缀：1. / 1、/ 1) / （1）/ (1) / ①-⑳ / 一、二、 / - •
_RESOLUTION_NUM_PREFIX = re.compile(
    r"^\s*(?:"
    r"[（(]?\d+[)）]"          # (1) （1） 1)
    r"|\d+\s*[.、．]"           # 1. 1、 1．
    r"|[①-⑳]"                  # 带圈数字
    r"|[一二三四五六七八九十]+\s*[、.．]"  # 一、二、
    r"|[-•*]\s"                # - • * 列表符
    r")\s*"
)

# 行内序号断点：把挤在同一行的多条决议（如“1. 甲 2. 乙”）拆开
_RESOLUTION_INLINE_SPLIT = re.compile(
    r"(?<!^)\s+(?=(?:[（(]?\d+[)）]|\d+\s*[.、．]|[①-⑳]|[一二三四五六七八九十]+\s*[、.．]))"
)


def _strip_item_prefix(item: str) -> str:
    """去掉单条决议开头的序号/列表符（若有），返回条目正文。"""
    return _RESOLUTION_NUM_PREFIX.sub("", item.strip(), count=1).strip()


def normalize_resolutions(raw: Any) -> list[str]:
    """把会议决议规范为“每条一项”的字符串数组（纯函数）。

    兼容三种输入：
    - list：逐元素转字符串，去空白丢空项；元素内含多条（换行/行内序号）时再拆分。
    - str（旧数据 / LLM 未按数组返回）：按换行 + 行内序号拆条。
    - None/其它：返回 []。
    每条会剥离 LLM 误加的行首序号/列表符（展示与导出统一由前端/模板加序号或换行）。
    """
    raw_items: list[str]
    if isinstance(raw, list):
        raw_items = [str(x or "") for x in raw]
    elif isinstance(raw, str):
        raw_items = [raw]
    else:
        return []

    out: list[str] = []
    for chunk in raw_items:
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            for part in _RESOLUTION_INLINE_SPLIT.sub("\n", line).split("\n"):
                item = _strip_item_prefix(part)
                if item:
                    out.append(item)
    return out


def format_resolutions(raw: Any) -> str:
    """把会议决议规范为“按序号逐条换行”的字符串（供 docx 模板 {{ resolutions }}）。

    在 normalize_resolutions 的基础上重新编号为 1. 2. 3.，用 \\n 连接
    （docxtpl 会把 \\n 自动转 <w:br/>）。无决议时返回空串。
    """
    items = normalize_resolutions(raw)
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))


def merge_segments(
    segments: list[dict],
    merge: bool = True,
    speaker_map: dict[str, str] | None = None,
) -> list[dict]:
    """合并连续的同发言人段落，返回全新列表（不修改入参）。

    merge=False 时仅返回逐段浅拷贝（text 去空白），不做合并。

    相邻判断用**解析后的显示名**（resolve_segment_speaker）而不是原始
    seg["speaker"]：行级覆盖正是用来把「被 cam++ 并成同一个簇的两个人」拆开的，
    若仍按簇合并，张三和李四的文本会被拼成一块、只显示块首那一行的名字，
    列表上改完看着对了，导出与问答里却还是错的。
    speaker_map 为 None 且各段都没有 speakerName 时，两种口径完全等价（旧行为不变）。
    """
    if not merge:
        return [{**seg, "text": (seg.get("text") or "").strip()} for seg in segments]
    out: list[dict] = []
    for seg in segments:
        last = out[-1] if out else None
        if last is not None and (
            resolve_segment_speaker(last, speaker_map)
            == resolve_segment_speaker(seg, speaker_map)
        ):
            out[-1] = {
                **last,
                "end": seg.get("end", last.get("end")),
                "text": _join_text(last.get("text", ""), seg.get("text", "")),
            }
        else:
            out.append({**seg, "text": (seg.get("text") or "").strip()})
    return out


# datetime-local 存储为 "YYYY-MM-DDTHH:mm"，展示时把 T 换成空格
def _display_date(raw: str) -> str:
    return (raw or "").replace("T", " ")


# 标量 meta 键（host、recorder 已移除；attendees 改为结构化，单独处理）
_META_KEYS = ("title", "date", "place", "agenda")


def build_template_context(
    meta: dict, segments: list[dict], options: dict, summary: dict | None = None
) -> dict:
    """构造 docxtpl 渲染上下文。

    options: {mergeSpeaker, timestamp, speaker, speakerMap}
    summary: {overview, topics:[{topic,discussion,conclusion,open_items}], speakers:[{speaker,points}], resolutions}
             （会议总结，可选；缺失时对应占位符渲染为空）
    - meta 缺失字段填空串；title 缺失回退为“会议纪要”
    - segments 依据 mergeSpeaker 合并，发言人经 speakerMap 解析显示名
    - 参会人员按单位分组（{{ attendees }}）
    - 议题纪要取自 summary.topics（{{ topics }}，每项 {topic,discussion,conclusion,open_items}）
    - 各发言人意见取自 summary.speakers 的“核心观点总结”（{{ speaker_opinions }}，
      每项 {speaker, unit, points}），不再放原始转写内容
    - 会议概要 {{ overview }}；会议决议 {{ resolutions }}（已按序号逐条换行）
    - 兼容保留 {{ speaker_blocks }}（原始转写分段），供旧模板/回退使用
    """
    meta = meta or {}
    options = options or {}
    summary = summary or {}
    speaker_map = options.get("speakerMap") or {}
    attendees = _normalize_attendees(meta)

    merged = merge_segments(
        segments or [],
        merge=bool(options.get("mergeSpeaker", True)),
        speaker_map=speaker_map,
    )
    seg_ctx = [
        {
            "time": f"{format_time(s.get('start', 0))}-{format_time(s.get('end', 0))}",
            "speaker": resolve_segment_speaker(s, speaker_map),
            "text": s.get("text", ""),
        }
        for s in merged
    ]

    ctx: dict[str, Any] = {k: (meta.get(k) or "").strip() for k in _META_KEYS}
    ctx["date"] = _display_date(ctx["date"])
    if not ctx["title"]:
        ctx["title"] = "会议纪要"
    # 参会人员：结构化 → 按单位分组的多行字符串（{{ attendees }}）
    ctx["attendees"] = attendees_by_unit(attendees)
    ctx["show_speaker"] = bool(options.get("speaker", True))
    ctx["show_timestamp"] = bool(options.get("timestamp", False))
    ctx["segments"] = seg_ctx
    # 各发言人意见：取自会议总结的核心观点（{%p for b in speaker_opinions %}）
    ctx["speaker_opinions"] = build_speaker_opinions(summary.get("speakers"), attendees)
    # 议题纪要：取自会议总结的 topics（{%p for t in topics %}）；旧模板无此占位符时被忽略
    ctx["topics"] = build_topics(summary.get("topics"))
    # 兼容保留：原始转写分段（旧模板 {%p for b in speaker_blocks %} 仍可用）
    ctx["speaker_blocks"] = build_speaker_blocks(segments or [], speaker_map, attendees)
    # 会议概要 / 会议决议：来自会议总结（缺失时留空，模板占位符渲染为空串）
    ctx["overview"] = str(summary.get("overview", "") or "").strip()
    # 会议决议：数组 → 重新编号逐条换行（docxtpl 会把 \n 自动转 <w:br/>）；兼容旧字符串
    ctx["resolutions"] = format_resolutions(summary.get("resolutions"))
    return ctx


def build_meeting_context(
    meta: dict,
    segments: list[dict],
    budget: int = 300000,
    speaker_map: dict[str, str] | None = None,
    keep_tail: bool = False,
) -> str:
    """把会议信息 + 转写文本拼装为注入 LLM 的上下文文本，按字符预算裁剪。

    speaker_map：发言人映射（如 {"0": "张三"}），使注入 LLM 的转写文本用
    映射后的姓名而非“说话人0”，否则用户按姓名提问时模型找不到对应发言。
    超预算时默认保留**开头**（问答场景，前部信息密度高）；keep_tail=True 时
    保留开头+结尾（总结场景，结论/决议常在会议后半段），并标注“已截断”。
    """
    meta = meta or {}
    speaker_map = speaker_map or {}
    header_parts = []
    # 标量字段（不含 attendees，参会人单独结构化渲染）
    label_map = {
        "title": "会议名称", "date": "时间", "place": "地点",
        "agenda": "议程",
    }
    for k, label in label_map.items():
        v = (meta.get(k) or "").strip()
        if v:
            header_parts.append(f"{label}：{_display_date(v) if k == 'date' else v}")
    attendees_text = attendees_to_lines(_normalize_attendees(meta))
    if attendees_text:
        header_parts.append("参会人员：\n" + attendees_text)
    header = "【会议信息】\n" + ("\n".join(header_parts) if header_parts else "（无）")

    merged = merge_segments(segments or [], merge=True, speaker_map=speaker_map)
    lines = [f"{resolve_segment_speaker(s, speaker_map)}：{s.get('text', '')}" for s in merged]
    body = "\n".join(lines) if lines else "（暂无转写内容）"

    truncated = False
    if len(body) > budget:
        if keep_tail:
            body = _truncate_head_tail(body, budget)
        else:
            cut = body[:budget]
            # 尽量在行边界截断，避免切断一句话；找不到换行则硬截
            nl = cut.rfind("\n")
            body = cut[:nl] if nl > 0 else cut
        truncated = True

    parts = [header, "\n【会议记录】\n" + body]
    if truncated:
        note = (
            "（注：会议记录较长，以上保留了开头与结尾部分，中间内容已省略。）"
            if keep_tail else
            "（注：会议记录较长，以上仅为按时间顺序保留的前段内容，后续部分已省略。）"
        )
        parts.append("\n" + note)
    return "\n".join(parts)


def _truncate_head_tail(body: str, budget: int) -> str:
    """超预算时保留开头（60%）+结尾（40%），掐掉中间，并在断点处标注省略。

    两段都在行边界裁剪，避免从半句断开。结尾段至关重要：会议决议/待办通常
    出现在后半段，只留开头会丢掉结论。
    """
    head_chars = int(budget * 0.6)
    tail_chars = budget - head_chars

    head = body[:head_chars]
    nl = head.rfind("\n")
    head = head[:nl] if nl > 0 else head

    tail = body[len(body) - tail_chars:]
    nl = tail.find("\n")
    tail = tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail

    return head + "\n……（中间部分内容已省略）……\n" + tail


SYSTEM_PROMPT_TEMPLATE = (
    "你是一个会议助手。请依据下面提供的会议记录回答用户的问题，"
    "回答要准确、简洁，忠于会议内容；若会议记录中没有相关信息，请如实说明。\n\n{context}"
)


def build_llm_messages(payload: dict, budget: int = 300000) -> list[dict]:
    """把会议上下文注入 system，与前端传来的对话历史拼接。

    payload: { meta, segments, speakerMap, messages:[{role,content}] }
    仅保留 user/assistant 历史，避免前端注入额外 system 消息。
    speakerMap 用于把注入 LLM 的转写文本发言人替换为映射后的姓名。
    """
    meta = (payload or {}).get("meta") or {}
    segments = (payload or {}).get("segments") or []
    speaker_map = (payload or {}).get("speakerMap") or {}
    history = (payload or {}).get("messages") or []
    context = build_meeting_context(meta, segments, budget=budget, speaker_map=speaker_map)
    system = {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(context=context)}
    convo = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return [system, *convo]


# ---------- 会议总结：LLM 提示词 / 消息构造 / JSON 解析 ----------

_SUMMARY_SYSTEM_PROMPT = (
    "你是一个专业的会议纪要助手。用户会提供一场会议的完整记录（含发言人与发言内容）。"
    "请通读全部记录，撰写一份**详实、准确、忠于原文**的会议总结，并**只输出一个 JSON 对象**，"
    "不要输出任何解释、前后缀或 Markdown 代码块围栏。\n"
    "JSON 结构如下：\n"
    "{\n"
    '  "overview": "会议概要（字符串，200-400字：会议背景与目标、讨论了哪些议题、达成的主要共识、存在的分歧、核心结论）",\n'
    '  "topics": [ { "topic": "议题名称（字符串）", "discussion": ["讨论要点一", "讨论要点二"], "conclusion": "该议题的结论（字符串）", "open_items": "该议题遗留的未决事项（字符串，无则空串）" } ],\n'
    '  "speakers": [ { "speaker": "发言人姓名", "points": ["观点要点一", "观点要点二"] } ],\n'
    '  "resolutions": [ "事项｜责任人｜时间节点" ]\n'
    "}\n"
    "规则：\n"
    "- **topics（议题）是重点，务必完整**：优先按会议议程或讨论脉络归纳议题（通常 2-8 个），"
    "不要只做按发言人的流水账。每个议题都要给出 discussion（分点讨论要点，含各方观点与争论焦点，"
    "字符串数组，通常 3-6 点）与 conclusion（该议题达成的结论/共识；未达成一致时明确写“未达成共识”）；"
    "确有遗留问题的填 open_items，否则为空字符串。\n"
    "- **提炼而非复述**：用自己的话概括，不逐字抄录转写；但不得编造记录中不存在的事实、数字或承诺，"
    "信息不足时如实说明。\n"
    "- speakers 逐一覆盖每位有实质发言的发言人，不要遗漏；points 用字符串数组（通常 3-6 条），"
    "每条一句话概括一个观点/主张/依据/疑虑。\n"
    "- resolutions 每条一个决议或待办，统一格式“事项｜责任人｜时间节点”；某字段缺失用“待定”占位；"
    "无明确决议时返回 [\"本次会议未形成明确决议\"]。\n"
    "- 发言人标识沿用会议记录中出现的名称（如“张三”“说话人0”）。\n"
    "- discussion/points 必须以字符串数组输出（不要输出含换行符的单个大字符串）；"
    "整体必须是可被 JSON.parse 直接解析的纯 JSON。"
)


_SUMMARY_HOTWORDS_MAX_CHARS = 4000  # 注入总结提示词的热词表长度上限，防止超长列表挤占上下文预算

_SUMMARY_HOTWORD_GUIDANCE = (
    "热词表（可能是语音识别误写的专有名词、人名、地名或术语，供你校正）：{hotwords}\n"
    "注意：会议转写文本可能存在语音识别误差（同音错字、专有名词误写）。"
    "请在总结时结合上下文语境与上述热词表，对明显写错的词做**局部校正**"
    "（例如把同音错字还原为热词表中的正确写法）；"
    "只做有把握的修正，不得因此改动发言的实质内容，也不得凭空编造记录中不存在的信息。"
)


def _summary_hotword_guidance(hotwords: str, max_chars: int = _SUMMARY_HOTWORDS_MAX_CHARS) -> str:
    """把系统热词表渲染成注入总结提示词的校正说明；空/无热词返回空串。

    热词先用 normalize_hotword 归一化为空格分隔单行（与 ASR 同一约定），再按
    max_chars 截断，防止超长热词表挤占总结上下文预算。纯函数，便于单测。
    """
    normalized = normalize_hotword(hotwords or "")
    if not normalized:
        return ""
    if len(normalized) > max_chars:
        normalized = normalized[:max_chars] + "…"
    return _SUMMARY_HOTWORD_GUIDANCE.format(hotwords=normalized)


def build_summary_messages(
    meta: dict,
    segments: list[dict],
    budget: int = 300000,
    speaker_map: dict[str, str] | None = None,
    keep_tail: bool = True,
    hotwords: str = "",
) -> list[dict]:
    """构造会议总结的 LLM 消息（system 约束 JSON 输出 + user 提供会议上下文）。

    复用 build_meeting_context 注入会议信息与转写文本，speaker_map 使发言人
    以映射后的姓名出现（如 0→张三），与问答 /export 行为一致。
    keep_tail=True 时超预算截断保留开头+结尾（议题结论/决议常在会议后半段，
    不能像问答那样只留开头）。
    hotwords：系统配置的热词表（原始文本）；非空时追加到 system 提示词末尾，
    提示模型结合语境对 ASR 误识别的专有名词做局部校正。空串时不追加（旧行为不变）。
    """
    context = build_meeting_context(
        meta or {}, segments or [], budget=budget, speaker_map=speaker_map or {}, keep_tail=keep_tail
    )
    user = context + "\n\n请依据以上会议内容进行总结，并按要求只输出 JSON。"
    system = _SUMMARY_SYSTEM_PROMPT
    guidance = _summary_hotword_guidance(hotwords)
    if guidance:
        system = system + "\n\n" + guidance
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _join_points(points: Any) -> str:
    """把 points/discussion 归一化为 \\n 分隔的多行字符串（兼容 list/str/None）。

    LLM 被要求以字符串数组输出（JSON 更不易出错），此处立即折叠回字符串，
    使下游（前端 renderMarkdown、模板 {{ b.points }} / {{ t.discussion }}、
    历史存档）始终拿到字符串，存储契约不变。
    """
    if isinstance(points, (list, tuple)):
        return "\n".join(str(p or "").strip() for p in points if str(p or "").strip())
    return str(points or "").strip()


def parse_summary_json(raw: str) -> dict:
    """解析 LLM 返回的总结 JSON，返回归一化后的结构。

    容错：去代码围栏、抽取首个平衡 {}、严格校验。解析失败抛 ValueError。
    返回：{overview:str, speakers:[{speaker,points}], resolutions:[str], topics:[...]}
    - points：LLM 以字符串数组输出（JSON 更稳），此处归一化为 \\n 分隔字符串。
    - topics：新增议题纪要 [{topic,discussion,conclusion,open_items}]；旧数据/未输出时为空列表。
    """
    try:
        obj = json.loads(_extract_json_object(raw), strict=False)
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"LLM 返回无法解析为 JSON：{e}") from e
    if not isinstance(obj, dict):
        raise ValueError("LLM 返回的不是 JSON 对象")

    speakers: list[dict] = []
    raw_speakers = obj.get("speakers")
    if isinstance(raw_speakers, list):
        for s in raw_speakers:
            if not isinstance(s, dict):
                continue
            speaker = str(s.get("speaker", "") or "").strip()
            points = _join_points(s.get("points"))
            if speaker or points:
                speakers.append({"speaker": speaker, "points": points})

    topics: list[dict] = []
    raw_topics = obj.get("topics")
    if isinstance(raw_topics, list):
        for t in raw_topics:
            if not isinstance(t, dict):
                continue
            topic = str(t.get("topic", "") or "").strip()
            discussion = _join_points(t.get("discussion"))
            conclusion = str(t.get("conclusion", "") or "").strip()
            open_items = _join_points(t.get("open_items"))
            if not (topic or discussion or conclusion or open_items):
                continue
            topics.append({
                "topic": topic,
                "discussion": discussion,
                "conclusion": conclusion,
                "open_items": open_items,
            })

    return {
        "overview": str(obj.get("overview", "") or "").strip(),
        "topics": topics,
        "speakers": speakers,
        "resolutions": normalize_resolutions(obj.get("resolutions")),
    }


# ---------- 议程导入：Word 文本提取 / LLM 提示词 / JSON 解析 ----------

AGENDA_TEXT_MAX_CHARS = 20000  # 送入 LLM 的文档文本上限


def extract_docx_text(data: bytes) -> str:
    """从 .docx 字节流提取纯文本（段落 + 表格单元格），供 LLM 提取字段。

    议程通知常用表格排版，故必须读取表格单元格文本，否则会漏掉参会人员等信息。
    解析失败抛 ValueError。
    """
    try:
        from docx import Document  # docxtpl 的传递依赖，无需额外安装
    except ImportError as e:  # pragma: no cover - 环境缺失时的兜底
        raise ValueError(f"缺少 python-docx 依赖：{e}") from e
    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:
        raise ValueError(f"无法读取 Word 文档（请确认为 .docx 格式）：{e}") from e

    lines: list[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            lines.append(t)
    for table in doc.tables:
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            cells = [c for c in cells if c]
            if cells:
                lines.append(" | ".join(cells))

    text = "\n".join(lines).strip()
    if not text:
        raise ValueError("文档内容为空，未能提取到文本")
    return text[:AGENDA_TEXT_MAX_CHARS]


_AGENDA_SYSTEM_PROMPT = (
    "你是一个会议信息提取助手。用户会给你一份“会议议程通知”文档的纯文本，"
    "请从中提取会议关键信息，并**只输出一个 JSON 对象**，不要输出任何解释、前后缀或 Markdown 代码块围栏。\n"
    "JSON 结构如下：\n"
    "{\n"
    '  "title": "会议名称（字符串）",\n'
    '  "date": "会议时间（字符串，尽量保留原文，如 2026-07-30 14:00）",\n'
    '  "place": "会议地点（字符串）",\n'
    '  "agenda": "会议议程（字符串，多项议程用换行分隔）",\n'
    '  "attendees": [ { "name": "姓名", "unit": "单位", "title": "职务" } ]\n'
    "}\n"
    "规则：\n"
    "- 未提及的字段填空字符串 \"\"，attendees 未提及时填空数组 []。\n"
    "- attendees 中每个人的 unit/title 若文档未写明则填空字符串。\n"
    "- 不要编造文档中不存在的信息。\n"
    "- 再次强调：输出必须是可被 JSON.parse 直接解析的纯 JSON，不含任何额外文字。"
)


def build_agenda_extract_messages(text: str) -> list[dict]:
    """构造议程提取的 LLM 消息（system 约束 JSON 输出 + user 提供文档文本）。"""
    user = "以下是会议议程通知文档的文本内容：\n---\n" + (text or "") + "\n---\n请提取并输出 JSON。"
    return [
        {"role": "system", "content": _AGENDA_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _extract_json_object(raw: str) -> str:
    """从可能包含围栏/前后缀说明的 LLM 输出中抽出 JSON 对象子串。"""
    s = (raw or "").strip()
    # 去 ```json ... ``` 或 ``` ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    # 扫描第一个平衡的 {...}，字符串字面量内的花括号不计入深度
    start = s.find("{")
    if start == -1:
        raise ValueError("LLM 返回中未找到 JSON 对象")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    raise ValueError("LLM 返回的 JSON 对象不完整")


def parse_agenda_json(raw: str) -> dict:
    """解析 LLM 返回的议程 JSON，返回归一化后的 meta 结构。

    容错：去代码围栏、抽取首个平衡 {}、严格校验。解析失败抛 ValueError。
    返回：{title, date, place, agenda, attendees:[{name,unit,title}]}
    """
    try:
        # strict=False 容忍 LLM 在 JSON 字符串里直接输出的原始换行等控制字符
        obj = json.loads(_extract_json_object(raw), strict=False)
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"LLM 返回无法解析为 JSON：{e}") from e
    if not isinstance(obj, dict):
        raise ValueError("LLM 返回的不是 JSON 对象")

    meta = {
        "title": str(obj.get("title", "") or "").strip(),
        "date": str(obj.get("date", "") or "").strip(),
        "place": str(obj.get("place", "") or "").strip(),
        "agenda": str(obj.get("agenda", "") or "").strip(),
    }
    # 复用统一的参会人归一化（同时兼容对象数组或误返回的字符串）
    meta["attendees"] = _normalize_attendees(obj)
    return meta


# ---------- 会议历史：payload 构造 / 冗余列抽取 / 校验 / 按日期分组 ----------

HISTORY_SCHEMA_VERSION = 2  # payload 结构版本，供将来迁移兼容
HISTORY_SNIPPET_CHARS = 120  # 列表页摘要字符数
_HISTORY_FILE_KEYS = ("id", "name", "duration", "elapsed", "audio_name", "has_audio")

# 会议级状态（DB 冗余列 + payload.status）
MEETING_DRAFT = "draft"            # 已建档，尚无任何转写结果
MEETING_TRANSCRIBING = "transcribing"  # 至少一个文件排队/转写中
MEETING_DONE = "done"             # 无进行中转写（全部完成或本就无需转写）

# 文件级状态（仅存于 payload.files[]）
# recording=服务端正在采集麦克风。它与 queued/processing 同属“进行中”，
# 但走的是完全不同的收尾路径（停止录音 → 补 WAV 头 → 再提交转写）。
_FILE_ACTIVE = {"queued", "processing", "recording"}  # 视为“进行中”的文件态

# deleted=已删除的槽位。文件索引（file_index）同时是数组下标、路由参数、SSE 帧键、
# 音频文件名前缀与在途任务身份（见 CLAUDE.md），因此删除**不做数组重排**——重排会
# 让后续每个下标与磁盘上 {index}_ 开头的文件名全部错位，并让在途转写结果写回错误的
# 槽位。删除改为原地留一个墓碑：槽位保留，内容清空。索引空间因此是「稳定的」，代价
# 是数组里可能存在空洞，所有按下标遍历的地方都要容忍 deleted。
FILE_DELETED = "deleted"


def tombstone_file(entry: Any) -> dict:
    """把一个文件条目原地改成墓碑（纯函数，返回新 dict）。

    保留 id/name（列表里若还要显示「已删除」占位，名字仍可读），清空音频引用与段落，
    置 status=deleted。清空 audio_name 是必须的：音频文件随删除一起从磁盘移除，
    留着旧名会让 audio_path 去查一个已不存在的文件，且会让 `has_audio` 与事实不符。
    """
    raw = entry or {}
    return {
        "id": str(raw.get("id", "") or ""),
        "name": str(raw.get("name", "") or ""),
        "duration": None,
        "elapsed": None,
        "audio_name": "",
        "has_audio": False,
        "status": FILE_DELETED,
        "segments": [],
        "deleted": True,
    }


def is_deleted_file(entry: Any) -> bool:
    """条目是否为墓碑（删除后遗留的空槽）。容忍 status 缺失的旧数据。"""
    if not entry:
        return False
    if entry.get("deleted"):
        return True
    return str(entry.get("status", "") or "").strip() == FILE_DELETED


def derive_meeting_status(files: list[dict]) -> str:
    """由文件级 status 汇总出会议级 status（纯函数）。

    - 任一文件处于 queued/processing/recording → transcribing
    - 否则若存在任何已完成的转写结果（有 segments 的文件）→ done
    - 否则（无文件，或文件均无结果且未在转写）→ draft
    兼容旧 payload：文件无 status 字段时按“有无 segments”判定完成/草稿。
    """
    files = files or []
    if any((f.get("status") in _FILE_ACTIVE) for f in files):
        return MEETING_TRANSCRIBING
    has_result = any((f.get("segments") or []) for f in files)
    return MEETING_DONE if has_result else MEETING_DRAFT


def _normalize_history_file(raw: Any) -> dict:
    """把单个文件条目归一化为 payload 内的 file 结构（纯函数，不改入参）。

    保留 id/name/duration/elapsed/audio_name/has_audio 与 segments（逐段浅拷贝）。
    音频引用 audio_name 为服务端相对文件名；has_audio 缺失时按 audio_name 是否存在推断。
    """
    raw = raw or {}
    segs_in = raw.get("segments") or []
    segments = []
    for s in segs_in:
        if not isinstance(s, dict):
            continue
        seg = {
            "start": float(s.get("start", 0) or 0),
            "end": float(s.get("end", 0) or 0),
            "speaker": s.get("speaker", "?"),
            "text": str(s.get("text", "") or ""),
            "flagged": bool(s.get("flagged", False)),
        }
        # 行级发言人覆盖（可选）：用户在转写列表里改单行发言人时写入，
        # 优先级高于 speakerMap，只影响这一段。空值不写键，旧的 payload 保持原样。
        speaker_name = str(s.get("speakerName", "") or "").strip()
        if speaker_name:
            seg["speakerName"] = speaker_name
        segments.append(seg)
    audio_name = str(raw.get("audio_name", "") or "").strip()
    has_audio = raw.get("has_audio")
    # 文件级状态：pending=待转写 / queued=排队 / processing=转写中 / recording=录制中
    # / done / error / deleted=已删除的槽位（墓碑，见 tombstone_file）。
    # 兼容旧 payload（无 status）：有 segments 视为 done，否则 pending。
    status = str(raw.get("status", "") or "").strip()
    if not status:
        status = "done" if segments else "pending"
    # 墓碑槽位：前端 PUT 是整份覆盖，保留这个标记才能让「删除」在后续 autosave 中
    # 存活——否则下一次 PUT 会把墓碑归一化成一个空的 pending 卡片，删掉的文件复活。
    deleted = bool(raw.get("deleted", False))
    if deleted:
        # 归一化边界即权威：墓碑一律清空运行期字段，不接受外部传入的段落/音频引用。
        segments = []
        audio_name = ""
        has_audio = False
        status = FILE_DELETED
    return {
        "id": str(raw.get("id", "") or ""),
        "name": str(raw.get("name", "") or ""),
        "duration": raw.get("duration"),
        "elapsed": raw.get("elapsed"),
        "audio_name": audio_name,
        "has_audio": bool(has_audio) if has_audio is not None else bool(audio_name),
        "status": status,
        "segments": segments,
        "deleted": deleted,
    }


def build_history_payload(
    meta: dict,
    files: list[dict],
    speaker_map: dict[str, str] | None = None,
    summary: dict | None = None,
    status: str | None = None,
) -> dict:
    """组装一条会议历史的完整 payload（纯函数）。

    - meta 经参会人归一化，剔除非法条目。
    - files 每项保留元信息 + 音频引用 + segments + 文件级 status。
    - status：会议级状态；缺省由 derive_meeting_status(files) 推导。
      显式传入用于建"草稿档"（无文件也标 draft）等场景。
    - schema_version 内嵌，供将来迁移兼容。
    """
    meta = meta or {}
    norm_meta = {
        "title": str(meta.get("title", "") or "").strip(),
        "date": str(meta.get("date", "") or "").strip(),
        "place": str(meta.get("place", "") or "").strip(),
        "agenda": str(meta.get("agenda", "") or "").strip(),
        "attendees": _normalize_attendees(meta),
    }
    norm_files = [_normalize_history_file(f) for f in (files or [])]
    meeting_status = (status or "").strip() or derive_meeting_status(norm_files)
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "meta": norm_meta,
        "speakerMap": dict(speaker_map or {}),
        "summary": summary or {},
        "status": meeting_status,
        "files": norm_files,
    }


def extract_history_columns(payload: dict) -> dict:
    """从 payload 计算入库的冗余列（纯函数）。

    返回 {title, meeting_date, duration, segment_count, speaker_count, snippet}：
    - duration：各文件 duration 求和（None 记 0）。
    - segment_count：全部文件 segments 总数。
    - speaker_count：去重发言人数（按经映射解析后的显示名去重，与展示一致）。
    - snippet：合并后转写文本前 N 字（供列表摘要）。
    """
    payload = payload or {}
    meta = payload.get("meta") or {}
    files = payload.get("files") or []
    speaker_map = payload.get("speakerMap") or {}

    duration = 0.0
    all_segments: list[dict] = []
    for f in files:
        d = f.get("duration")
        if isinstance(d, (int, float)):
            duration += float(d)
        all_segments.extend(f.get("segments") or [])

    speakers = {resolve_segment_speaker(s, speaker_map) for s in all_segments}
    merged = merge_segments(all_segments, merge=True, speaker_map=speaker_map)
    snippet = " ".join((s.get("text") or "").strip() for s in merged).strip()
    snippet = snippet[:HISTORY_SNIPPET_CHARS]

    # 会议级 status 冗余列：优先用 payload 显式值，否则由文件态推导（兼容旧数据）
    status = str(payload.get("status", "") or "").strip() or derive_meeting_status(files)

    return {
        "title": str(meta.get("title", "") or "").strip(),
        "meeting_date": str(meta.get("date", "") or "").strip(),
        "duration": round(duration, 1),
        "segment_count": len(all_segments),
        "speaker_count": len(speakers),
        "snippet": snippet,
        "status": status,
    }


def validate_history_payload(raw: Any, max_bytes: int | None = None) -> dict:
    """校验并归一化外来 payload（入库前的边界防护）。

    - 接受 dict 或 JSON 字符串；结构非法抛 ValueError。
    - 超过 max_bytes（若给出，按 UTF-8 JSON 字节计）抛 ValueError。
    - 返回经 build_history_payload 重建的规范 payload（丢弃未知字段）。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw, strict=False)
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError(f"payload 不是合法 JSON：{e}") from e
    if not isinstance(raw, dict):
        raise ValueError("payload 必须是 JSON 对象")

    files = raw.get("files")
    if files is not None and not isinstance(files, list):
        raise ValueError("payload.files 必须是数组")

    payload = build_history_payload(
        meta=raw.get("meta") or {},
        files=files or [],
        speaker_map=raw.get("speakerMap") or {},
        summary=raw.get("summary") or {},
        status=raw.get("status"),
    )
    if max_bytes is not None:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if size > max_bytes:
            raise ValueError(f"payload 过大（{size} 字节，上限 {max_bytes}）")
    return payload


def group_by_date(items: list[dict]) -> list[dict]:
    """把列表项按 meeting_date 的“天”分组（纯函数，供前端亦可复用的分组逻辑参考）。

    - 分组键取 meeting_date 的日期部分（"YYYY-MM-DDTHH:mm" / "YYYY-MM-DD ..." → "YYYY-MM-DD"）。
    - 无法解析日期的归入 "未标注日期" 组，排在最后。
    - 组按日期倒序；组内保持传入顺序（调用方应已按时间倒序）。
    返回 [{date, items:[...]}]。
    """
    UNKNOWN = "未标注日期"
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for it in items or []:
        raw = str((it or {}).get("meeting_date", "") or "").strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw.replace("T", " "))
        key = m.group(1) if m else UNKNOWN
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)
    # 已知日期倒序，未知组始终置尾
    known = sorted((k for k in order if k != UNKNOWN), reverse=True)
    ordered = known + ([UNKNOWN] if UNKNOWN in groups else [])
    return [{"date": k, "items": groups[k]} for k in ordered]


# ---------- 声纹匹配：向量归一化 / 余弦相似度 / 说话人匹配（纯函数） ----------
# 刻意用纯 Python 数学实现，使这些函数无需 numpy 即可单测（与本模块“无重依赖”一致）。
# 向量入参接受任意可迭代的数值序列（list / numpy 一维数组皆可）。


def l2_normalize(vec: Any) -> list[float]:
    """L2 归一化一维向量，返回新的 list[float]（不修改入参）。

    零向量（或近零）原样返回全 0，避免除零；配合余弦：归一化后点积即余弦相似度。
    """
    v = [float(x) for x in (vec or [])]
    norm = math.sqrt(sum(x * x for x in v))
    if norm <= 1e-12:
        return [0.0 for _ in v]
    return [x / norm for x in v]


def cosine_similarity(a: Any, b: Any) -> float:
    """两向量余弦相似度 ∈ [-1, 1]。任一为零向量或维度不匹配时返回 0.0。

    不假设入参已归一化：内部各自除以模长，故可直接传原始声纹向量。
    """
    va = [float(x) for x in (a or [])]
    vb = [float(x) for x in (b or [])]
    if not va or not vb or len(va) != len(vb):
        return 0.0
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(x * x for x in vb))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    return dot / (na * nb)


def match_speaker(embedding: Any, enrolled: list[dict], threshold: float) -> dict:
    """把单个说话人簇的声纹向量匹配到登记库中最相似的人。

    - embedding：该说话人簇的代表性声纹向量。
    - enrolled：登记库 [{id, name, embedding:[...]}, ...]（embedding 为一维数值序列）。
    - threshold：余弦相似度阈值；最高分 ≥ 阈值才算命中。
    返回 {matched:bool, id, name, score}；未命中时 matched=False、id/name 为 None，
    score 仍给出最高分（便于日志/调阈值）。库为空或向量非法时 matched=False、score=0。
    """
    best_id = None
    best_name = None
    best_score = -1.0
    for person in enrolled or []:
        if not isinstance(person, dict):
            continue
        score = cosine_similarity(embedding, person.get("embedding"))
        if score > best_score:
            best_score = score
            best_id = person.get("id")
            best_name = person.get("name")
    if best_score < 0:  # 库为空（未进入循环）：无最高分
        return {"matched": False, "id": None, "name": None, "score": 0.0}
    matched = best_score >= threshold
    return {
        "matched": matched,
        "id": best_id if matched else None,
        "name": best_name if matched else None,
        "score": best_score,
    }


def match_speakers(
    cluster_embeddings: dict, enrolled: list[dict], threshold: float
) -> dict:
    """把多个说话人簇一次性匹配到登记库，供转写后填充 speakerMap。

    - cluster_embeddings：{原始 spk 值(str): 该簇声纹向量}。
    - enrolled / threshold：同 match_speaker。
    返回 {spk(str): {name, score}}，**仅包含命中的簇**（未命中的不出现，
    使前端可原样保留“说话人N”）。姓名冲突不去重——同一人匹配到多个簇属正常
    （聚类可能把一个人拆成两簇），由前端/用户决定是否合并。
    """
    out: dict[str, dict] = {}
    for spk, emb in (cluster_embeddings or {}).items():
        res = match_speaker(emb, enrolled, threshold)
        if res["matched"]:
            out[str(spk)] = {"name": res["name"], "score": round(res["score"], 3)}
    return out


def build_representative_segments(
    sentence_info: list,
    max_segments: int,
    min_seconds: float,
) -> dict:
    """把 FunASR 的 sentence_info 按说话人聚类编号分组，各取**最长的若干段**，
    作为提取该簇代表性声纹的时间区间。纯函数（无音频/模型依赖，便于单测）。

    - sentence_info：[{start(ms), end(ms), spk, ...}]，start/end 为**毫秒**（原始单位）。
    - max_segments：每个说话人最多取几段（按时长降序）。
    - min_seconds：短于此秒数的段直接丢弃（太短向量不可靠）。

    返回 {spk(int/原值): [(start_sec, end_sec), ...]}，时间已换算成**秒**并按起点升序。
    某说话人所有段都过短则不出现在结果里（该簇无法可靠提向量）。
    """
    by_spk: dict = {}
    for seg in sentence_info or []:
        spk = seg.get("spk")
        st, en = seg.get("start"), seg.get("end")
        if spk is None or st is None or en is None:
            continue
        start_sec = st / 1000.0
        end_sec = en / 1000.0
        if end_sec - start_sec < min_seconds:
            continue
        by_spk.setdefault(spk, []).append((start_sec, end_sec))

    out: dict = {}
    for spk, spans in by_spk.items():
        top = sorted(spans, key=lambda p: p[1] - p[0], reverse=True)[: max(1, max_segments)]
        out[spk] = sorted(top, key=lambda p: p[0])
    return out


def normalize_hotword(hotword: str) -> str:
    """把 SeACo 热词输入归一化为空格分隔的单行文本。

    换行、英文逗号、中文逗号都会让 SeACo 的热词解析错位（它期望单行、空格分隔），
    统一替换成空格再去首尾空白。纯函数，便于单测。
    """
    if not hotword:
        return ""
    return (
        str(hotword)
        .replace("\r", " ")
        .replace("\n", " ")
        .replace(",", " ")
        .replace("，", " ")
        .strip()
    )


# ---------- 麦克风录音：设备枚举 / 裸流收尾 / 电平（纯函数） ----------
# 采集本身在 recorder.py（子进程 + 事件循环），此处只放不需要模型、不需要 GPU、
# 不依赖服务进程即可单测的解析与封装逻辑，与上面的纯函数区同一约定。
# 沿用本模块的“无 numpy”约束：RMS 用 stdlib 的 array 解 int16。

# 采集格式：**按回放质量优先**选 48k 立体声 16bit，而不是迁就 ASR 的 16k 单声道。
#
# 为什么不再直接录 16k：录音停止后它同时还要被回放（「录音停止后就是一个普通文件」
# 这条不变式），而 16k 的奈奎斯特上限只有 8k——清辅音（s/sh/f/x/c/q、t/k/p 的爆破段）
# 的能量重心恰好在 4–8k 以上，被砍掉后现场听得清的话，回放就"听不清"了。
# 元音几乎不受影响，所以听上去仍是人声，只是字与字分不开——这正是当初的故障现象。
#
# 转 16k 不再由采集端负责：ASR 侧本来就无条件转码（app.py 的 _ensure_audio_input
# 对任何输入都跑一遍 ffmpeg → 16k 单声道 + 降噪/响度链），录音与上传走同一条路。
# 而且 ffmpeg 的 swr 重采样器（多相滤波，带抗混叠低通）质量优于 ALSA plughw 的
# 默认转换器——旧方案等于"先用差转换器损失一次，再让好转换器无事可做"。
#
# 代价：码率从 32KB/s 涨到 192KB/s（48k×2ch×2B），同一条 RECORD_MAX_BYTES 能录的
# 时长缩到 1/6（默认 10GB 约 15.5 小时，改之前 2GB 约 18.6 小时）。见 .env 的说明。
REC_SAMPLE_RATE = 48000
REC_CHANNELS = 2
REC_BITS = 16
REC_RAW_EXT = ".raw"  # 采集期间的无头裸流
REC_WAV_EXT = ".wav"  # 收尾后的成品（即 payload.audio_name）


def format_plughw(dev: dict) -> str:
    """把 parse_asound_cards 的一条设备写成 ffmpeg 可用的 ALSA 串。

    优先用稳定的 CARD=<id>（USB 设备号重启会变，卡名不变）；卡名为空时退回数字
    索引 `plughw:<card>,<dev>`。用 plughw 而非 hw：允许 ALSA 插件层替我们把设备
    的原生格式对齐到 REC_* 那套 48k/立体声——设备本身就是 48k 立体声时这层空转，
    是单声道或别的采样率时由它补齐，采集端不必为每种硬件写分支。
    """
    card_id = str((dev or {}).get("id", "") or "").strip()
    if card_id:
        return f"plughw:CARD={card_id},DEV={int(dev.get('device', 0))}"
    return f"plughw:{int(dev.get('card', 0))},{int(dev.get('device', 0))}"


# SoC 内部音频中枢的卡名标记（大小写不敏感的子串匹配）。
# 这些卡在 /proc/asound 里同样报 capture>0——片上音频中枢能把任意内部通路
# （ADMAIF / I2S / DMIC）路由成采集流——但它们背后没有任何物理输入，选中只会录到静音。
# 一路列进下拉框，就把那唯一一个真麦克风淹没了（Jetson AGX Orin 上一个 APE 卡
# 就能贡献十几行「NVIDIA Jetson AGX Orin APE」）。
# 适配其它硬件时改这一行即可；两条兜底规则见 filter_capture_devices。
_HUB_MARKERS = ("nvidia jetson", "tegra", "admaif", "dummy", "loopback")
# 单独的 "APE" 按词匹配而不是子串：卡名里含 ape 的正常设备是存在的（如 Shaper）。
_HUB_WORD = re.compile(r"\bape\b")


def _is_internal_hub(dev: dict) -> bool:
    """该设备是否属于 SoC 内部音频中枢（即：不是任何物理输入）。"""
    hay = f"{dev.get('id', '')} {dev.get('name', '')}".lower()
    return any(m in hay for m in _HUB_MARKERS) or bool(_HUB_WORD.search(hay))


def filter_capture_devices(devices: list[dict], keep: str = "") -> list[dict]:
    """滤掉 SoC 内部音频中枢，只留下真正接着麦克风的设备。

    两条兜底规则保证「过滤」永远不会把录音入口本身弄没：
    1. keep（即 RECORD_DEVICE）指向的设备永不滤除——用户显式配置了就尊重它，哪怕它
       的名字看起来像内部设备；
    2. 过滤后一个不剩时原样返回入参——宁可多显示几个噪音设备，也不能因为一份不认识的
       卡名把功能整体变成「未检测到可用设备」。

    因此这个函数只会「少显示」，不会让本来能录的机器变得不能录。
    """
    devs = list(devices or [])
    out = [
        d for d in devs
        if not _is_internal_hub(d) or same_device(d.get("plughw"), keep)
    ]
    return out or devs


def list_capture_devices(keep: str = "") -> list[dict]:
    """枚举可采集的音频设备，附上 plughw 串（读 /proc/asound，Linux 专用）。

    非 Linux（如开发机）没有 /proc/asound → 返回空表，上层据此隐藏录音 UI。
    任何读取异常都降级为空表：录音是可选功能，不得影响服务其它部分。

    解析后先过 filter_capture_devices 再返回。放在这里而不是各调用点，是为了让每个
    调用方都自动拿到同一份「用户真正可以选的东西」——包括 pick_capture_device 的
    devs[0] 兜底：不滤的话那个兜底恰好会选中卡号最小的内部中枢，录出一片静音。
    keep 传 RECORD_DEVICE，使其显式配置的设备不被滤除。
    """
    try:
        with open("/proc/asound/cards", "r", encoding="utf-8", errors="replace") as f:
            cards_text = f.read()
        with open("/proc/asound/pcm", "r", encoding="utf-8", errors="replace") as f:
            pcm_text = f.read()
    except OSError:
        return []
    out = []
    for d in parse_asound_cards(cards_text, pcm_text):
        out.append({**d, "plughw": format_plughw(d)})
    return filter_capture_devices(out, keep)


def recording_raw_name(file_index: int) -> str:
    """录音采集期间的裸流文件名（相对会议目录）。"""
    return f"{int(file_index)}_mic{REC_RAW_EXT}"


def recording_wav_name(file_index: int) -> str:
    """录音收尾后的 WAV 文件名（相对会议目录）；也是 payload.audio_name。"""
    return f"{int(file_index)}_mic{REC_WAV_EXT}"


def _norm_device(s: Any) -> str:
    """plughw 串的归一化形式（去掉所有空白并转小写），用于设备串比较。"""
    return "".join(str(s or "").split()).lower()


def same_device(a: Any, b: Any) -> bool:
    """两个 plughw 串是否指向同一设备（忽略大小写与空白）。

    卡名的大小写由驱动决定，而用户是照着 `arecord -D <串>` 的写法手填 RECORD_DEVICE
    的；严格相等会让只差大小写的配置静默落空、退到「第一个设备」——用户以为在录 USB
    麦，实际录的是板载声卡，且没有任何提示。归一化后比较可消除这一类错配。
    两边都为空时不算命中（否则未配置会被当成命中了空配置）。
    """
    na = _norm_device(a)
    return bool(na) and na == _norm_device(b)


def pick_capture_device(devices: list[dict], preferred: str = "") -> dict | None:
    """从枚举结果里挑一个默认录音设备（纯函数，返回 None 表示无可用设备）。

    优先级：RECORD_DEVICE 指定的 plughw 串命中 → 名称里带 USB 的（USB 麦是本职场景，
    板载声卡多用于输出）→ 第一个可用设备。preferred 命不中时**不报错**而是继续回退：
    设备换口/改名是常态，宁可回退也不该让录音入口整个不可用。

    一个例外：**枚举结果为空**（非 Linux、或 /proc/asound 解析不出）而用户又显式配置了
    RECORD_DEVICE 时，直接采信该串——用户能用 `arecord -D <串>` 录出音频就说明设备真实
    存在，此时不可信的恰恰是枚举，而不是用户的配置。否则「arecord 能录、网页却说没有
    可用设备」会成为一个无解的死角。
    """
    devs = list(devices or [])
    want = (preferred or "").strip()
    if want:
        for d in devs:
            if same_device(d.get("plughw"), want):
                return d
        if not devs:
            return {"card": None, "device": None, "id": "", "name": want, "plughw": want}
    if not devs:
        return None
    for d in devs:
        if "usb" in str(d.get("name", "")).lower() or "usb" in str(d.get("id", "")).lower():
            return d
    return devs[0]


_CARDS_LINE = re.compile(r"^\s*(\d+)\s+\[(.+?)\]\s*:\s*(.*)$")
_PCM_LINE = re.compile(r"^\s*(\d+)-(\d+)\s*:\s*(.*)$")
_CAPTURE_COUNT = re.compile(r"capture\s+(\d+)")


def parse_asound_cards(cards_text: str, pcm_text: str) -> list[dict]:
    """解析 /proc/asound/cards + /proc/asound/pcm，列出**可采集**的设备。

    - 卡名列形如 ` 1 [Device   ]: USB-Audio - USB Audio Device`：取 `[..]` 内的
      卡 id（供 `plughw:CARD=<id>,DEV=<n>` 寻址，比数字索引稳定，重启/换口不变），
      以及 ` - ` 之后的友好描述作为显示名。
    - PCM 列形如 `01-00: USB Audio : USB Audio : playback 1 : capture 1`：只保留
      `capture N` 且 N>0 的设备——纯播放设备录不了音。
    - 不做任何 IO，两份文本由调用方读入，便于用真实抓取内容做 fixture 单测。
      解析不出内容时返回空表，调用方据此判定“无可用设备”。
    返回 [{card, device, id, name}]，按 (card, device) 升序。
    """
    cards: dict[int, tuple[str, str]] = {}
    for line in (cards_text or "").splitlines():
        m = _CARDS_LINE.match(line)
        if not m:
            continue
        card_id = m.group(2).strip()
        desc = m.group(3).strip()
        # `USB-Audio - USB Audio Device` → 破折号后才是友好名；没有则用整串
        name = desc.split(" - ", 1)[1].strip() if " - " in desc else desc
        cards[int(m.group(1))] = (card_id, name or card_id)

    out: list[dict] = []
    for line in (pcm_text or "").splitlines():
        m = _PCM_LINE.match(line)
        if not m:
            continue
        cap = _CAPTURE_COUNT.search(m.group(3))
        if not cap or int(cap.group(1)) <= 0:
            continue
        card_no, dev_no = int(m.group(1)), int(m.group(2))
        card_id, name = cards.get(card_no, ("", ""))
        out.append({
            "card": card_no,
            "device": dev_no,
            "id": card_id,
            "name": name or f"card{card_no}",
        })
    out.sort(key=lambda d: (d["card"], d["device"]))
    return out


def build_wav_header(
    data_bytes: int,
    sample_rate: int = REC_SAMPLE_RATE,
    channels: int = REC_CHANNELS,
    bits: int = REC_BITS,
) -> bytes:
    """生成 44 字节 canonical PCM WAV 头，data 长度按入参给定。

    录音期间写的是无头裸流（崩溃安全，见 wrap_raw_to_wav），停止时按文件**实际**
    大小补上这个头。数据量远低于 4GB，标准 RIFF 足够，不需要 RF64 变体。
    """
    block_align = max(1, channels * bits // 8)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + int(data_bytes), b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        sample_rate * block_align, block_align, bits,
        b"data", int(data_bytes),
    )


def wrap_raw_to_wav(
    raw_path: str,
    wav_path: str,
    sample_rate: int = REC_SAMPLE_RATE,
    channels: int = REC_CHANNELS,
    bits: int = REC_BITS,
) -> int:
    """把裸 PCM 流封成 WAV（写标准头 + 流式拷贝数据），返回数据字节数。

    长度以**实际文件大小**为准，而不是任何已有头部字段——正因如此，对「进程被杀、
    头部从未回填」的残留文件同样成立，这正是选裸流采集的兑现点。分块拷贝，不整份
    读入内存；目标已存在则覆盖，故重复调用幂等。raw 文件不删除，由调用方决定。
    """
    frame = max(1, channels * bits // 8)
    size = os.path.getsize(raw_path)
    data_bytes = size - (size % frame)  # 丢弃可能的半帧尾巴
    with open(raw_path, "rb") as src, open(wav_path, "wb") as dst:
        dst.write(build_wav_header(data_bytes, sample_rate, channels, bits))
        remaining = data_bytes
        while remaining > 0:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            dst.write(chunk)
    return data_bytes


def raw_pcm_seconds(
    size_bytes: int,
    sample_rate: int = REC_SAMPLE_RATE,
    channels: int = REC_CHANNELS,
    bits: int = REC_BITS,
) -> float:
    """由裸流字节数换算已录时长（秒）。按 REC_* 的 48k/立体声/16bit 即 192000 字节每秒。"""
    frame = max(1, channels * bits // 8)
    frames = max(0, int(size_bytes)) // frame
    return frames / float(max(1, sample_rate))


def pcm_rms_tail(raw_path: str, tail_bytes: int = 196608) -> float:
    """读裸流**末尾**若干字节算 RMS（0.0–1.0），供电平表使用。

    只看尾部是因为电平表要的是“此刻多大声”，读整个文件既慢又无意义。文件不存在、
    短于一帧或读取失败一律返回 0.0（电平表静默归零，不打断录音）。用 stdlib 的
    array 解 int16，不引入 numpy。

    tail_bytes 默认 192000（≈1 秒）：48k/立体声下 192000 字节/秒，取满一秒才和
    16k/单声道时代 32768 字节的观感一致。立体声的交错采样在这里无差别——RMS 求的是
    左右两路混在一起的能量均值，与先下混再求一样。
    """
    try:
        size = os.path.getsize(raw_path)
    except OSError:
        return 0.0
    n = min(int(tail_bytes), size)
    n -= n % 2  # int16 → 丢弃奇数字节
    if n <= 0:
        return 0.0
    try:
        with open(raw_path, "rb") as f:
            f.seek(size - n)
            data = f.read(n)
    except OSError:
        return 0.0
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return min(1.0, math.sqrt(total / len(samples)) / 32768.0)


def build_recording_name(now: Any) -> str:
    """生成录音条目显示名，如「麦克风录音 2026-09-24 14:30」。

    now 接受 datetime，也接受 ISO 字符串（取到分钟、把 T 换成空格），便于路由直接
    把 `_now_iso()` 的返回值传进来，避免两处各自格式化日期。
    """
    if hasattr(now, "strftime"):
        stamp = now.strftime("%Y-%m-%d %H:%M")
    else:
        stamp = str(now or "").replace("T", " ").strip()[:16]
    return f"麦克风录音 {stamp}".strip()


def preserve_recording_file(payload: dict, file_index: int, entry: dict) -> dict:
    """把仍在录制的文件条目强制保留进将要落库的 payload（纯函数，返回新 dict）。

    前端 autosave 会整份 PUT 它内存里的 files 数组；若那份内存态早于录音开始，
    这次 PUT 会把录音条目整个抹掉（服务端唯一会被前端覆盖的写入口就在这里）。护栏
    刻意做窄：仅在 index 落在范围内时用服务端已知的条目覆盖该槽位，其余字段一律照旧。
    entry 为空或越界则原样返回（不修改入参）。
    """
    if not entry or file_index < 0:
        return payload
    files = list((payload or {}).get("files") or [])
    if file_index >= len(files):
        return payload
    files[file_index] = dict(entry)
    out = dict(payload or {})
    out["files"] = files
    out["status"] = derive_meeting_status(files)
    return out


def preserve_deleted_files(payload: dict, stored_files: list[dict]) -> dict:
    """把服务端已标记为墓碑的槽位强制保留进将要落库的 payload（纯函数，返回新 dict）。

    与 preserve_recording_file 同源的护栏，防的是同一种竞争：删除请求（DELETE
    路由）与前端 autosave（整份 PUT）可能同时在路上，若那次 PUT 基于删除之前的
    内存态，就会把刚删掉的卡片原样写回来——表现为「删了又自己长出来」。

    删除在语义上是**不可逆**的（没有撤销入口），因此这里可以对墓碑做单调保护：
    只要服务端当前存的是墓碑，就无条件盖回去。反过来说，前端任何一次合法 PUT 都
    不可能把一个墓碑槽位写回成有内容的条目，这条护栏不会挡住正常操作。
    stored_files 为 nil 或与 payload.files 长度不符时原样返回（不修改入参）。
    """
    files = list((payload or {}).get("files") or [])
    stored = stored_files or []
    if len(stored) != len(files):
        return payload
    changed = False
    for i, cur in enumerate(stored):
        if not is_deleted_file(cur):
            continue
        if is_deleted_file(files[i]):
            continue
        files[i] = tombstone_file(cur)
        changed = True
    if not changed:
        return payload
    out = dict(payload or {})
    out["files"] = files
    out["status"] = derive_meeting_status(files)
    return out
