"""Turn-end judge: deterministic heuristic first, optional LLM second."""
from __future__ import annotations

import json
import re
from typing import Optional

QUESTION_END = re.compile(r"[?？]\s*[)）\]\*_`\"'”」]*\s*$")
DECISION = re.compile(
    r"\b(should i|shall i|do you want|would you like|want me to|let me know|please (confirm|approve|choose|decide)"
    r"|can you (confirm|approve)|approve|your call|which (one|option) do you|waiting for your|need your)\b"
    r"|要不要|是否需要|是否|请确认|请批准|请选择|你决定|你来定|需要你|等你|你看呢|可以吗|行吗|好吗"
    r"|给我|告诉我|你选|需你拍板|需要你拍板|你拍板|请提供|发我|你定",
    re.I)
PROMISE = re.compile(
    r"\b(?:i'?ll|i will|i'?m going to|i am going to|let me|we'?ll|we will)\s+(?:now\s+|then\s+|also\s+)?"
    r"(?:report(?: back)?|post|update you|update (?:this|here)|let you know|get back|follow up|circle back"
    r"|check and (?:tell|let|report|get back)|check|look into|keep you posted|monitor|ping you|send (?:you )?(?:the )?results)"
    r"|\bwill (?:post (?:it |them |the \w+ )?(?:here|back)|report back|follow up|get back to you|update you)\b"
    r"|\b(?:stand by|hang tight|working on it|in a (?:moment|minute|bit))\b"
    r"|稍后|马上|接下来|待会|一会儿|随后|回头|我去(?:查|看|跑|试)|我会(?:跟进|汇报|回复|检查|继续|告诉你|同步)",
    re.I)
PROMISE_SENTENCES = 2  # a promise only counts in the final sentences: earlier ones were usually kept


def _last_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


_SENT = re.compile(r"[^。！!？?.\n]*[。！!？?.\n]?")


def _last_sentences(text: str, n: int) -> list:
    protected = re.sub(r"\.(?=\S)", "\x00", (text or "").strip())
    sents = [x.replace("\x00", ".").strip() for x in _SENT.findall(protected)]
    return [x for x in sents if x.strip(" 。！？!?.")][-n:]


def _asks_question(text: str, n: int = 2) -> bool:
    """True if any of the final n sentences ends with a question mark."""
    sents = [x.strip() for x in _SENT.findall((text or "").strip()) if x.strip()]
    return any(x.rstrip(")）]*_`\"'”」 ").endswith(("?", "？")) for x in sents[-n:])


_SPLIT = re.compile(r"[^。！？!?.\n]*[。！？!?.\n]?")
_MD = re.compile(r"`+|\*\*|__|~~|^#+\s*|^\s*(?:[-*+>]|\d+[.)])\s+|\[([^\]]*)\]\([^)]*\)")
_LEGACY = re.compile(r"^(asked|promised)\s*:\s*", re.I)
NOTE_LEN = 40
REPLIED = "replied"


def _clean(s: str) -> str:
    s = _MD.sub(lambda m: m.group(1) or "", s.strip())
    return " ".join(s.split()).strip(" \"'“”「」()（）")


def _cut(s: str, n: int = NOTE_LEN) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def extract_note(text: str) -> str:
    """Last question sentence of a reply (≤40 chars, markdown stripped), else 'replied'. Deterministic."""
    text = (text or "").strip()
    legacy_ask = text.lower().startswith("asked")
    text = _LEGACY.sub("", text)
    if not text:
        return REPLIED
    # keep '.' inside words/numbers/filenames (judge.py, 1.5) from splitting: only split '.' before space/end
    protected = re.sub(r"\.(?=\S)", "\x00", text)
    sents = [x.replace("\x00", ".").strip() for x in _SPLIT.findall(protected)]
    sents = [x for x in sents if x.strip(" 。！？!?.")]
    pick = next((x for x in reversed(sents) if x.rstrip(")）]*_`\"'”」 ").endswith(("?", "？"))), None)
    if pick is None:
        pick = next((x for x in reversed(sents) if DECISION.search(x)), None)
    if pick is None and legacy_ask:  # old rows stored "asked: <last line>"
        pick = sents[-1] if sents else None
    if pick is None:
        return REPLIED
    pick = _clean(pick)
    if len(pick) > NOTE_LEN:  # long question: keep the trailing clauses that fit (the actual ask)
        parts = re.split(r"(?<=[，,；;：:])\s*", pick)
        tail = ""
        for part in reversed(parts):
            if len(part + tail) > NOTE_LEN:
                break
            tail = part + tail
        pick = tail or pick
    if not pick:
        return REPLIED
    if pick[-1] in "。.!！":
        pick = pick[:-1]
    if len(pick) > NOTE_LEN and pick[-1] in "?？":  # keep the question mark visible after truncation
        return pick[: NOTE_LEN - 2].rstrip() + "…" + pick[-1]
    return _cut(pick)


ASKED, REPLIED_STATE, WORKING, INTERRUPTED = "asked", "replied", "working", "interrupted"


def heuristic(text: str) -> dict:
    """-> {"ball", "promise", "note", "state"}. Decided from the FULL text, before any truncation.

    Question/decision ask -> human/asked (a question always beats a promise); promise phrase in the last
    PROMISE_SENTENCES sentences w/o ask -> ai/working + promise; else human/replied."""
    text = text or ""
    tail = text[-600:]
    last = _last_line(text)
    if not text.strip():
        return {"ball": "human", "promise": None, "note": "empty reply", "state": REPLIED_STATE}
    if QUESTION_END.search(last) or _asks_question(tail) or DECISION.search(tail):
        return {"ball": "human", "promise": None, "note": extract_note(text), "state": ASKED}
    for sent in reversed(_last_sentences(tail, PROMISE_SENTENCES)):
        m = PROMISE.search(sent)
        if m:
            return {"ball": "ai", "promise": m.group(0), "note": _cut(m.group(0)), "state": WORKING}
    return {"ball": "human", "promise": None, "note": "replied", "state": REPLIED_STATE}


def legacy_state(row: dict) -> str:
    """State for rows written before the ``state`` column. A truncated legacy question may have lost its
    '?', so anything that is not clearly a plain reply is treated as *asked* (never silently Done)."""
    if row.get("ball") == "ai":
        return WORKING
    note = (row.get("note") or "").strip()
    low = note.lower()
    if low.startswith("interrupted"):
        return INTERRUPTED
    if low in ("", "replied", "empty reply", "judge failed", "you spoke") or low.endswith(("resolved", "approved",
                                                                                          "denied", "timeout")):
        return REPLIED_STATE
    return ASKED

JUDGE_PROMPT = (
    "You judge a chat thread's turn-taking. Given the assistant's final message, decide whose turn it is. "
    "ball='human' if the assistant answered, asked a question, or needs a decision/approval. "
    "ball='ai' only if the assistant promised further action it has not done yet; then put the promise phrase in "
    "'promise'. Reply with ONLY JSON: {\"ball\":\"ai|human\",\"since\":\"<iso8601>\",\"promise\":\"<str>|null\",\"note\":\"<=80 chars\"}")


def parse_contract(raw: str) -> Optional[dict]:
    """Parse the LLM contract JSON; None if invalid."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("ball") not in ("ai", "human"):
        return None
    promise = data.get("promise")
    if promise is not None and not isinstance(promise, str):
        return None
    note = data.get("note") or ""
    return {"ball": data["ball"], "since": data.get("since") if isinstance(data.get("since"), str) else None,
            "promise": promise or None, "note": str(note)[:120]}


def llm_judge(llm, text: str, timeout: float) -> Optional[dict]:
    res = llm.complete(
        [{"role": "system", "content": JUDGE_PROMPT},
         {"role": "user", "content": (text or "")[-2000:]}],
        max_tokens=200, temperature=0, timeout=timeout, purpose="ball turn judge")
    return parse_contract(getattr(res, "text", "") or "")
