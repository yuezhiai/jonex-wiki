"""Shared guard: detect a model emitting a tool call as XML-ish text.

A model under tool pressure sometimes writes its tool intent as a pseudo-XML
block inside free-text ``content`` or inside a structured ``answer`` field
instead of using the native ``tool_calls`` channel.  This reads like a final
answer to the caller and pollutes the answer with garbage.  Both the query
algorithm (traversal + synthesis) and the model port's free-text fallback use
this predicate to recognise and reject such blocks before they become an
answer.
"""

from __future__ import annotations

import re

# Opening tags of tool-call XML shapes seen from DeepSeek under tool pressure:
#   <tool_calls><invoke name="wiki_search"><parameter name="query" ...>   (plain)
#   <｜｜DSML｜｜tool_calls｜｜><｜｜DSML｜｜invoke｜｜ name=...>            (DeepSeek
#     SGLang DSML markers — ASCII '<' + FULLWIDTH vertical bar U+FF5C '｜' around
#     the tag word).  Both serialisations mean "the model still wants a tool".
#   <function_call><tool>...</tool>  ...
# Any '<' ... '>' on one line whose body carries a tool-call vocabulary word is
# treated as a tool-call block; prose answers rarely contain these words inside
# angle brackets.
_XML_TOOL_CALL_TAG = re.compile(
    r"<[^\n>]*?\b(?:tool_calls?|tool_call|invoke|function_call|wiki_search|wiki_read|search|read|parameter|dsml)\b[^\n>]*>",
    re.IGNORECASE,
)


def is_xml_tool_call_text(text: str) -> bool:
    """True when *text* looks like a tool call serialised as XML, not an answer.

    A real answer rarely contains these opening tags (they are tooling
    vocabulary), so a match is a strong signal the model is trying to call a
    tool with the budget gone.  Fails open (returns False) on empty/None so a
    blank or prose answer is never misclassified.
    """
    if not text:
        return False
    return _XML_TOOL_CALL_TAG.search(text) is not None
