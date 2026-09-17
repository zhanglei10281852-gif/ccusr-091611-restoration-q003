"""领域错误，携带 HTTP 状态码便于接口层映射。"""
from __future__ import annotations


class DomainError(Exception):
    status = 400
    code = "domain_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class PermissionDenied(DomainError):
    status = 403
    code = "permission_denied"


class ConflictError(DomainError):
    """乐观锁版本冲突 / 非法状态迁移 / 幂等重传但内容不一致。"""

    status = 409
    code = "conflict"


class GateError(DomainError):
    """确认组合时三道闸门（排他占用、方向一致、必需签名）未通过。"""

    status = 422
    code = "confirmation_gate_failed"
