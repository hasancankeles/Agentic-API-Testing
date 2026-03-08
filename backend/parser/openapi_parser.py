from __future__ import annotations

import re

import yaml
import requests as http_requests

from models.schemas import (
    HttpMethod,
    ParsedAPI,
    ParsedEndpoint,
    ParsedParameter,
    ParsedResponse,
    ParsedWebSocketMessage,
)


def load_spec(source: str) -> dict:
    """Load an OpenAPI spec from a file path, URL, or raw YAML/JSON string."""
    if source.startswith(("http://", "https://")):
        resp = http_requests.get(source, timeout=15)
        resp.raise_for_status()
        return yaml.safe_load(resp.text)
    try:
        with open(source, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return yaml.safe_load(source)


def _extract_parameters(params_raw: list[dict]) -> list[ParsedParameter]:
    result: list[ParsedParameter] = []
    for p in params_raw:
        schema = p.get("schema", {})
        result.append(
            ParsedParameter(
                name=p.get("name", ""),
                location=p.get("in", "query"),
                required=p.get("required", False),
                schema_type=schema.get("type", schema.get("format", "string")),
                description=p.get("description", ""),
            )
        )
    return result


def _extract_responses(responses_raw: dict) -> list[ParsedResponse]:
    result: list[ParsedResponse] = []
    for status_code, resp_data in responses_raw.items():
        content = resp_data.get("content", {})
        content_type = next(iter(content.keys()), "application/json") if content else "application/json"
        media = content.get(content_type, {}) if content else {}
        schema = media.get("schema", {})
        schema_ref = schema.get("$ref", None)
        example = media.get("example", None)
        result.append(
            ParsedResponse(
                status_code=str(status_code),
                description=resp_data.get("description", ""),
                content_type=content_type,
                schema_ref=schema_ref,
                example=example,
            )
        )
    return result


def _extract_ws_messages_from_description(description: str) -> list[ParsedWebSocketMessage]:
    """Parse WebSocket message types from the spec description tables."""
    messages: list[ParsedWebSocketMessage] = []

    client_pattern = r"Client → Server Message Types.*?\n((?:\s*\|.*\n)+)"
    server_pattern = r"Server → Client Message Types.*?\n((?:\s*\|.*\n)+)"

    for pattern, direction in [
        (client_pattern, "client_to_server"),
        (server_pattern, "server_to_client"),
    ]:
        match = re.search(pattern, description, re.DOTALL)
        if not match:
            continue
        table_text = match.group(1)
        for line in table_text.strip().split("\n"):
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) < 3 or cols[0].startswith("---") or cols[0] == "Type":
                continue
            msg_type = cols[0].strip("`")
            fields_str = cols[1] if len(cols) > 1 else ""
            desc = cols[2] if len(cols) > 2 else ""

            fields = {}
            if fields_str and fields_str != "_(none)_":
                for f in fields_str.split(","):
                    f = f.strip().strip("`")
                    if f:
                        fields[f] = "string"

            messages.append(
                ParsedWebSocketMessage(
                    type=msg_type,
                    direction=direction,
                    fields=fields,
                    description=desc,
                )
            )

    return messages


def parse_openapi(source: str) -> ParsedAPI:
    """Parse an OpenAPI spec into a structured ParsedAPI model."""
    spec = load_spec(source)
    info = spec.get("info", {})
    servers = spec.get("servers", [])
    base_url = servers[0].get("url", "") if servers else ""

    endpoints: list[ParsedEndpoint] = []
    for path, path_data in spec.get("paths", {}).items():
        for method_str, op_data in path_data.items():
            if method_str.upper() not in HttpMethod.__members__:
                continue
            endpoints.append(
                ParsedEndpoint(
                    path=path,
                    method=HttpMethod(method_str.upper()),
                    summary=op_data.get("summary", ""),
                    description=op_data.get("description", ""),
                    operation_id=op_data.get("operationId", ""),
                    tags=op_data.get("tags", []),
                    parameters=_extract_parameters(op_data.get("parameters", [])),
                    responses=_extract_responses(op_data.get("responses", {})),
                    request_body=op_data.get("requestBody"),
                )
            )

    schemas = {}
    for schema_name, schema_data in spec.get("components", {}).get("schemas", {}).items():
        schemas[schema_name] = schema_data

    ws_messages = _extract_ws_messages_from_description(info.get("description", ""))

    return ParsedAPI(
        title=info.get("title", ""),
        description=info.get("description", ""),
        version=info.get("version", ""),
        base_url=base_url,
        endpoints=endpoints,
        schemas=schemas,
        websocket_messages=ws_messages,
    )
