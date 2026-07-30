"""Request bodies for the REST API. Responses are plain dicts with an `ok` flag."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class NodePatch(BaseModel):
    label: Optional[str] = None
    group: Optional[str] = None
    notes: Optional[str] = None


class CmdBody(BaseModel):
    type: str  # "type" | "keys" | "sequence" | "read" | "send"
    text: Optional[str] = None
    char_delay_ms: Optional[int] = None
    chord: Optional[List[str]] = None
    steps: Optional[List[dict]] = None
    stop_on_error: Optional[bool] = None
    # send: write bytes to the target's serial getty. Exactly one of data/raw.
    data: Optional[str] = None   # UTF-8 text
    raw: Optional[str] = None    # hex bytes, e.g. "03" for Ctrl+C, "0d" for CR
    # UI hints, accepted and ignored by the node:
    expect_output: Optional[bool] = None
    confirm: Optional[bool] = None


class KeysBody(BaseModel):
    chord: List[str]


class SequenceBody(BaseModel):
    steps: List[dict]
    stop_on_error: Optional[bool] = False


class BulkCmd(BaseModel):
    node_ids: List[str]
    command: CmdBody
    stagger_ms: Optional[int] = 0
    skip_offline: Optional[bool] = True


class MacroCreate(BaseModel):
    name: str
    steps: List[dict]
    group: Optional[str] = ""
    dangerous: Optional[bool] = False


class MacroPatch(BaseModel):
    name: Optional[str] = None
    steps: Optional[List[dict]] = None
    group: Optional[str] = None
    dangerous: Optional[bool] = None


class MacroRun(BaseModel):
    node_ids: List[str]
    stagger_ms: Optional[int] = 0


class SettingsPatch(BaseModel):
    heartbeat_interval_ms: Optional[int] = None
    stale_timeout_ms: Optional[int] = None
    warn_timeout_ms: Optional[int] = None
    output_retention_days: Optional[int] = None
    event_retention_days: Optional[int] = None
    require_confirm_dangerous: Optional[bool] = None
    auth_enabled: Optional[bool] = None


class LoginBody(BaseModel):
    password: str
