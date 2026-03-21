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


def _resolve_schema_ref(schema: dict, all_schemas: dict[str, dict], max_depth: int = 3) -> dict:
    current = schema if isinstance(schema, dict) else {}
    depth = 0
    while depth < max_depth and isinstance(current, dict) and "$ref" in current:
        ref = str(current.get("$ref") or "")
        if not ref.startswith("#/components/schemas/"):
            break
        schema_name = ref.rsplit("/", 1)[-1]
        resolved = all_schemas.get(schema_name)
        if not isinstance(resolved, dict):
            break
        current = resolved
        depth += 1
    return current if isinstance(current, dict) else {}


def _extract_media_from_content(content: dict) -> tuple[str, dict]:
    if not isinstance(content, dict) or not content:
        return "application/json", {}
    if "application/json" in content and isinstance(content["application/json"], dict):
        return "application/json", content["application/json"]
    first_key = next(iter(content.keys()))
    media = content.get(first_key) if isinstance(content.get(first_key), dict) else {}
    return str(first_key), media


def _extract_media_examples(media: dict) -> tuple[object, dict[str, object]]:
    example = media.get("example")
    examples_raw = media.get("examples", {})
    parsed_examples: dict[str, object] = {}
    if isinstance(examples_raw, dict):
        for key, payload in examples_raw.items():
            if isinstance(payload, dict):
                if "value" in payload:
                    parsed_examples[str(key)] = payload["value"]
                elif "externalValue" in payload:
                    parsed_examples[str(key)] = payload["externalValue"]
            elif payload is not None:
                parsed_examples[str(key)] = payload

    if example is None and parsed_examples:
        example = next(iter(parsed_examples.values()))
    return example, parsed_examples


def _extract_request_body_hints(
    request_body: dict | None,
    all_schemas: dict[str, dict],
) -> tuple[list[str], object]:
    if not isinstance(request_body, dict):
        return [], None

    content = request_body.get("content", {})
    _content_type, media = _extract_media_from_content(content)
    schema = _resolve_schema_ref(media.get("schema", {}), all_schemas)
    required_fields = schema.get("required", []) if isinstance(schema, dict) else []
    if not isinstance(required_fields, list):
        required_fields = []

    example, _examples_map = _extract_media_examples(media)
    return [str(field) for field in required_fields], example


def _merge_parameters(path_level: list[ParsedParameter], operation_level: list[ParsedParameter]) -> list[ParsedParameter]:
    merged: list[ParsedParameter] = []
    seen: set[tuple[str, str]] = set()
    for param in [*path_level, *operation_level]:
        key = (param.location, param.name)
        if key in seen:
            continue
        seen.add(key)
        merged.append(param)
    return merged


def _detect_requires_auth(
    operation_security: object,
    global_security: object,
    parameters: list[ParsedParameter],
) -> tuple[list[dict[str, object]], bool]:
    effective_security = operation_security if operation_security is not None else global_security
    normalized_security: list[dict[str, object]] = []
    if isinstance(effective_security, list):
        for item in effective_security:
            if isinstance(item, dict):
                normalized_security.append(item)

    if normalized_security:
        return normalized_security, True

    auth_header_names = {"authorization", "api_key", "x-api-key"}
    for parameter in parameters:
        if parameter.location != "header":
            continue
        if parameter.name.lower() in auth_header_names:
            return normalized_security, True

    return normalized_security, False


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
        content_type, media = _extract_media_from_content(content)
        schema = media.get("schema", {})
        schema_ref = schema.get("$ref", None)
        example, examples = _extract_media_examples(media)
        links = resp_data.get("links", {}) if isinstance(resp_data, dict) else {}
        result.append(
            ParsedResponse(
                status_code=str(status_code),
                description=resp_data.get("description", ""),
                content_type=content_type,
                schema_ref=schema_ref,
                example=example,
                examples=examples,
                links=links if isinstance(links, dict) else {},
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
    global_security = spec.get("security")
    all_schemas = spec.get("components", {}).get("schemas", {}) or {}

    endpoints: list[ParsedEndpoint] = []
    for path, path_data in spec.get("paths", {}).items():
        path_level_parameters = _extract_parameters(path_data.get("parameters", []))
        for method_str, op_data in path_data.items():
            if method_str.upper() not in HttpMethod.__members__:
                continue

            op_parameters = _extract_parameters(op_data.get("parameters", []))
            merged_parameters = _merge_parameters(path_level_parameters, op_parameters)
            responses = _extract_responses(op_data.get("responses", {}))
            security, requires_auth = _detect_requires_auth(op_data.get("security"), global_security, merged_parameters)
            request_body = op_data.get("requestBody")
            required_fields, request_body_example = _extract_request_body_hints(request_body, all_schemas)
            response_examples = {
                response.status_code: response.example
                for response in responses
                if response.example is not None
            }

            endpoints.append(
                ParsedEndpoint(
                    path=path,
                    method=HttpMethod(method_str.upper()),
                    summary=op_data.get("summary", ""),
                    description=op_data.get("description", ""),
                    operation_id=op_data.get("operationId", ""),
                    tags=op_data.get("tags", []),
                    parameters=merged_parameters,
                    responses=responses,
                    security=security,
                    requires_auth=requires_auth,
                    request_body=request_body,
                    request_body_required_fields=required_fields,
                    request_body_example=request_body_example,
                    response_examples=response_examples,
                )
            )

    schemas = {}
    for schema_name, schema_data in all_schemas.items():
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
