from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass


_TAGGED_PROCESS_RE = re.compile(
    r"\A<process_output>\n"
    r"<stdout>(?P<stdout>.*?)</stdout>\n"
    r"<stderr>(?P<stderr>.*?)</stderr>\n"
    r"<return_code>(?P<return_code>-?\d+)</return_code>\n"
    r"<offload>(?P<offload>.*?)</offload>\n"
    r"</process_output>\Z",
    re.DOTALL,
)


@dataclass(frozen=True)
class TaggedProcessOutput:
    stdout: str
    stderr: str
    return_code: int
    offload: dict[str, object]


def render_tagged_process_output(
    *,
    stdout: str,
    stderr: str,
    return_code: int,
    offload: Mapping[str, object],
) -> str:
    return "\n".join(
        [
            "<process_output>",
            f"<stdout>{html.escape(stdout, quote=False)}</stdout>",
            f"<stderr>{html.escape(stderr, quote=False)}</stderr>",
            f"<return_code>{return_code}</return_code>",
            f"<offload>{html.escape(json.dumps(offload, ensure_ascii=False), quote=False)}</offload>",
            "</process_output>",
        ]
    )


def parse_tagged_process_output(content: str) -> TaggedProcessOutput | None:
    match = _TAGGED_PROCESS_RE.match(content)
    if match is None:
        return None
    try:
        offload = json.loads(html.unescape(match.group("offload")))
        if not isinstance(offload, dict):
            return None
        return TaggedProcessOutput(
            stdout=html.unescape(match.group("stdout")),
            stderr=html.unescape(match.group("stderr")),
            return_code=int(match.group("return_code")),
            offload={str(key): value for key, value in offload.items()},
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
