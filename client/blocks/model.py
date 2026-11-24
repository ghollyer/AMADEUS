"""Action blocks and sequences: the saved, vendor-neutral program format.

A *block* is one node in a tree. ``Call`` runs one MCP tool, and every other
node is control flow around calls. A ``Sequence`` is a named, saveable tree
(``<data_dir>/sequences/*.amseq.json``). The Blockly editor in the web console
only edits this format; it is never the storage format.

Argument values in ``Call.args`` are plain JSON, with two special forms:

* ``{"$expr": "target.x_nm * 2"}`` is evaluated with :mod:`client.blocks.expr`
  against the sequence variables just before the call.
* ``{"$role": "detector:HAADF"}`` is resolved through the instrument's role
  table (``instrument.roles``) to that microscope's index or id. Writing
  roles instead of indices is what makes a sequence portable.

Conditions (``If.cond``, ``While.cond``) are expression strings.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class _Node(BaseModel):
    id: str | None = None          # stable id from the editor (optional)
    label: str | None = None       # user-facing name; defaults to the tool name


class Call(_Node):
    type: Literal["call"] = "call"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    as_var: str | None = None      # store the result under this variable
    requires: list[str] = Field(default_factory=list)   # extra, beyond the tool's own
    ensure_mode: bool = False      # switch to the tool's imaging mode first


class Seq(_Node):
    type: Literal["seq"] = "seq"
    children: list["Block"] = Field(default_factory=list)


class If(_Node):
    type: Literal["if"] = "if"
    cond: str
    then: list["Block"] = Field(default_factory=list)
    orelse: list["Block"] = Field(default_factory=list, alias="else")

    model_config = {"populate_by_name": True}


class Repeat(_Node):
    type: Literal["repeat"] = "repeat"
    count: int = Field(ge=0, le=100_000)
    var: str | None = None         # loop index (0-based), if wanted
    body: list["Block"] = Field(default_factory=list)


class While(_Node):
    type: Literal["while"] = "while"
    cond: str
    max_iter: int = Field(default=100, ge=1, le=100_000)   # always bounded
    body: list["Block"] = Field(default_factory=list)


class ForEach(_Node):
    type: Literal["for_each"] = "for_each"
    var: str
    items: str                      # expression yielding a list
    max_iter: int = Field(default=1000, ge=1, le=100_000)
    body: list["Block"] = Field(default_factory=list)


class SetVar(_Node):
    type: Literal["set"] = "set"
    var: str
    value: str                      # expression


class Ask(_Node):
    type: Literal["ask"] = "ask"
    question: str
    options: list[str] = Field(default_factory=list)
    as_var: str | None = None


class Pause(_Node):
    """Stop the queue here until the operator resumes it."""
    type: Literal["pause"] = "pause"
    message: str = ""


class Fail(_Node):
    """Abort the run with a message (e.g. after a failed check)."""
    type: Literal["fail"] = "fail"
    message: str


Block = Annotated[
    Union[Call, Seq, If, Repeat, While, ForEach, SetVar, Ask, Pause, Fail],
    Field(discriminator="type"),
]

for _cls in (Seq, If, Repeat, While, ForEach):
    _cls.model_rebuild()


class Param(BaseModel):
    """A sequence input, set when the sequence is queued."""
    name: str
    default: Any = None
    description: str = ""


class Sequence(BaseModel):
    schema_version: int = SCHEMA_VERSION
    name: str
    description: str = ""
    params: list[Param] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)   # declared, beyond derived
    body: list[Block] = Field(default_factory=list)
    on_error: Literal["abort", "continue"] = "abort"
    editor: dict[str, Any] | None = None   # opaque editor state (Blockly XML/JSON)

    def dump(self) -> dict:
        return self.model_dump(by_alias=True, exclude_none=True)


def load_sequence(data: dict) -> Sequence:
    version = int(data.get("schema_version", SCHEMA_VERSION))
    if version > SCHEMA_VERSION:
        raise ValueError(f"sequence schema_version {version} is newer than this "
                         f"AMADEUS ({SCHEMA_VERSION}); update the client")
    return Sequence.model_validate(data)


def children_of(node) -> list[tuple[str, list]]:
    """(slot name, child list) pairs of a control node, for tree walks."""
    if isinstance(node, Seq):
        return [("children", node.children)]
    if isinstance(node, If):
        return [("then", node.then), ("else", node.orelse)]
    if isinstance(node, (Repeat, While, ForEach)):
        return [("body", node.body)]
    return []
