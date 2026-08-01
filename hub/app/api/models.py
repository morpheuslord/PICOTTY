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


class ExpectBody(BaseModel):
    # A list of steps: action steps ({"type":"send"/"type"/"keys", ...}),
    # {"delay_ms": n}, or {"wait_for": {"regex", "timeout_ms", "on_timeout"}}.
    steps: List[dict]


class QueueBody(BaseModel):
    command: CmdBody
    ttl_ms: Optional[int] = 3_600_000  # default 1h; 0/None -> no expiry


class RunbookCreate(BaseModel):
    name: str
    yaml: str


class RunbookPatch(BaseModel):
    name: Optional[str] = None
    yaml: Optional[str] = None


class RunbookRun(BaseModel):
    node_ids: Optional[List[str]] = None
    group: Optional[str] = None
    stagger_ms: Optional[int] = 0


class OTAFile(BaseModel):
    path: str
    content_b64: str


class OTABundleCreate(BaseModel):
    name: str
    files: List[OTAFile]


class OTAPush(BaseModel):
    bundle: str


class OTARollout(BaseModel):
    node_ids: List[str]
    bundle: str
    stagger_ms: Optional[int] = 0


class SettingsPatch(BaseModel):
    heartbeat_interval_ms: Optional[int] = None
    stale_timeout_ms: Optional[int] = None
    warn_timeout_ms: Optional[int] = None
    output_retention_days: Optional[int] = None
    event_retention_days: Optional[int] = None
    require_confirm_dangerous: Optional[bool] = None
    auth_enabled: Optional[bool] = None
    serial_bridge_enabled: Optional[bool] = None
    alerts_enabled: Optional[bool] = None
    alerts_webhook_url: Optional[str] = None
    alerts_ntfy_url: Optional[str] = None


class LoginBody(BaseModel):
    password: str
