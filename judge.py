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
    r"\b(i'?ll|i will|i'?m going to|let me) (report back|follow up|check|get back|look into|circle back|update you|keep you posted|monitor|ping you)"
    r"|\bwill (follow up|report back|get back to you|update you)\b"
    r"|\b(stand by|hang tight|working on it|in a (moment|minute|bit))\b"
    r"|稍后|马上|接下来|待会|一会儿|随后|回头|我去(查|看|跑|试)|我会(跟进|汇报|回复|检查|继续)",
    re.I)


def _last_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


_SENT = re.compile(r"[^。！!？?.\n]*[。！!？?.\n]?")


def _asks_question(text: str, n: int = 2) -> bool:
    """True if any of the final n sentences ends with a question mark."""
    sents = [x.strip() for x in _SENT.findall((text or "").strip()) if x.strip()]
    return any(x.rstrip(")）]*_`\"'”」 ").endswith(("?", "？")) for x in sents[-n:])


def heuristic(text: str) -> dict:
    """-> {"ball", "promise", "note"}. Question/decision ask -> human; promise w/o ask -> ai + promise."""
    text = text or ""
    tail = text[-600:]
    last = _last_line(text)
    if not text.strip():
        return {"ball": "human", "promise": None, "note": "empty reply"}
    if QUESTION_END.search(last) or _asks_question(tail) or DECISION.search(tail):
        return {"ball": "human", "promise": None, "note": "asked: " + last[:120]}
    m = PROMISE.search(tail)
    if m:
        return {"ball": "ai", "promise": m.group(0), "note": "promised: " + m.group(0)}
    return {"ball": "human", "promise": None, "note": "replied"}


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
