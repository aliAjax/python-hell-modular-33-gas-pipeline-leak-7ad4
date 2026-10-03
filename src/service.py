from . import domain, rules
from .domain import DomainError, ConflictError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        source = {
            "source_type": normalized.pop("source_type"),
            "external_id": normalized.pop("external_id"),
            "payload": normalized,
            "observed_at": normalized.pop("observed_at"),
        }
        result = self.repository.upsert_sources_and_reassess(
            item_id, [source], actor, role, reason="source_added"
        )
        return result

    def act(self, item_id, action, payload, actor, role, basis_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_BASIS_VERSION and basis_version is None:
            raise DomainError("basis_version_required", "该操作需要 basis_version", 400)
        submitted_basis = None
        if isinstance(payload.get("basis"), dict):
            submitted_basis = {key: payload["basis"].get(key) for key in rules.BASIS_KEYS}
        # 先校验依据版本（在状态校验之前，避免状态错误掩盖依据冲突）
        current_basis = rules.compute_basis(item, self.repository.list_sources(item_id))
        if basis_version is not None:
            if int(basis_version) != int(item["basis_version"]):
                diff = rules.basis_diff(current_basis, submitted_basis)
                raise ConflictError("basis_version_conflict", "依据版本已更新，请重新读取后再操作", {
                    "current_basis_version": item["basis_version"],
                    "submitted_basis_version": basis_version,
                    "current_basis": current_basis,
                    "submitted_basis": submitted_basis,
                    "diff": diff,
                })
            if submitted_basis is not None:
                diff = rules.basis_diff(current_basis, submitted_basis)
                if diff:
                    raise ConflictError("basis_mismatch", "提交的依据与现场依据不一致", {
                        "current_basis_version": item["basis_version"],
                        "current_basis": current_basis,
                        "submitted_basis": submitted_basis,
                        "diff": diff,
                    })
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload,
            expected_version=item["version"], basis_version=basis_version, submitted_basis=submitted_basis,
        )
        return self.get_item(item_id)

    def upload_batch_page(self, batch_id, page_number, item_id, sources, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        if not isinstance(page_number, int) or page_number < 1:
            raise DomainError("invalid_page", "页码必须是从 1 开始的连续整数", 400)
        if not isinstance(sources, list) or not sources:
            raise DomainError("invalid_batch", "批次页必须包含来源记录", 400)
        self.repository.get_item(item_id)
        normalized_sources = []
        for raw in sources:
            normalized = domain.normalize_source(raw)
            normalized_sources.append({
                "source_type": normalized.pop("source_type"),
                "external_id": normalized.pop("external_id"),
                "payload": normalized,
                "observed_at": normalized.pop("observed_at"),
            })
        return self.repository.store_batch_page(
            batch_id, page_number, item_id, normalized_sources, actor, role
        )

    def batch_status(self, batch_id):
        pages = self.repository.list_batch_pages(batch_id)
        stored = [page["page_number"] for page in pages]
        next_page = (max(stored) + 1) if stored else 1
        return {
            "batch_id": batch_id,
            "stored_pages": stored,
            "next_page": next_page,
            "pages": pages,
        }

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["basis"] = rules.compute_basis(item, item["sources"])
        item["assessment"] = rules.assess(item["basis"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
