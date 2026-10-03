from . import domain, rules
from .domain import DomainError, NotFoundError
from .rules import build_basis, check_basis


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
        item, created = self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )
        return item, created

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        normalized = domain.normalize_source(payload)
        source_type = normalized.pop("source_type")
        external_id = normalized.pop("external_id")
        observed_at = normalized.pop("observed_at")
        source, item, meta = self.repository.add_source(
            item_id, source_type, external_id, normalized, observed_at, actor, role
        )
        return source, self.get_item(item_id), meta

    def upload_batch_page(self, item_id, payload, actor, role):
        """离线批次按连续编号上传一页。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        batch_id = domain.require_text(payload, "batch_id")
        page_no = domain.integer(payload, "page_no", 1)
        total_pages = domain.integer(payload, "total_pages", 1)
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise DomainError("entries_required", "每页至少包含一条来源记录")
        normalized_entries = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise DomainError("invalid_entry", "第 %d 条来源格式无效" % (index + 1))
            normalized = domain.normalize_source(entry)
            normalized_entries.append({
                "source_type": normalized.pop("source_type"),
                "external_id": normalized.pop("external_id"),
                "observed_at": normalized.pop("observed_at"),
                "payload": normalized,
            })
        return self.repository.ingest_batch_page(
            item_id, batch_id, page_no, total_pages,
            payload.get("content_hash") or self._entries_hash(normalized_entries),
            normalized_entries, actor, role,
        )

    @staticmethod
    def _entries_hash(entries):
        from .audit import canonical_json
        import hashlib
        return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()

    def act(self, item_id, action, payload, actor, role, expected_version=None,
            expected_basis_revision=None, expected_basis_digest=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        item = self._decorate(item)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        if action in rules.ACTION_REQUIRES_BASIS:
            # 对外处置必须带依据版本；版本落后或依据对不上返回冲突和差异
            if expected_basis_revision is None:
                from .domain import DomainError as _DE
                raise _DE("basis_version_required", "该操作需要携带依据版本 expected_basis_revision", 400)
            expected_snapshot = self.repository.get_basis(item_id, int(expected_basis_revision))
            check_basis(
                item["payload"],
                expected_snapshot,
                item["basis_current"],
                expected_basis_revision,
                expected_basis_digest,
            )
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload,
            expected_version, expected_basis_revision if action in rules.ACTION_REQUIRES_BASIS else None,
        )
        return self.get_item(item_id)

    def _basis_current(self, item):
        sources = self.repository.list_sources(item["id"])
        basis = build_basis(item["payload"], sources)
        basis["revision"] = item["payload"].get("basis_revision", 1)
        basis["digest"] = item["payload"].get("basis_digest")
        return basis

    def _decorate(self, item):
        item["sources"] = self.repository.list_sources(item["id"])
        item["basis_current"] = self._basis_current(item)
        return item

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item = self._decorate(item)
        item["audit"] = self.repository.audit_trail(item_id)
        item["basis_history"] = self.repository.basis_history(item_id)
        item["audit_chain_valid"] = self.repository.verify_audit_chain(item_id)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
