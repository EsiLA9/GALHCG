from __future__ import annotations

from uuid import UUID, uuid5


_NAMESPACE = UUID("e6c0e829-4e4e-41c1-94f8-2ba5c6d776b1")


def project_node_id(project_id: str) -> str:
    return f"project:{project_id}"


def file_node_id(project_id: str, path: str) -> str:
    return f"file:{uuid5(_NAMESPACE, project_id + chr(0) + path).hex}"


def managed_edge_id(project_id: str, relation: str, source_id: str, target_id: str) -> str:
    key = f"{project_id}\0{relation}\0{source_id}\0{target_id}"
    return f"edge:{uuid5(_NAMESPACE, key).hex}"
