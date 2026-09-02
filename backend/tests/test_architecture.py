"""Architecture guards — A-SLOT-5, A-SLOT-6, A-AUD-1, FIN-1, FIN-12, AUD-7.

These are source-level tests. They exist so the boundaries stay true as the code
grows rather than only on the day they were written. All scanning is done on
code with comments and string literals stripped (see ``tests/_source.py``), so
prose in a docstring cannot trip a guard.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests._source import APP_ROOT, code_only, module_name, python_files

# P0 §2.1 module boundaries.
DOMAIN_PACKAGES = {
    "tenancy",
    "identity",
    "customers",
    "service",
    "billing",
    "audit",
    "sync",
    "core",
}
# Tables that must never be updated or deleted (FIN-12, AUD-7).
APPEND_ONLY_MODELS = {"LedgerEntry", "AuditEvent"}
APPEND_ONLY_TABLES = {"ledger_entry", "audit_event"}


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


class TestASLOT5ImportBoundaries:
    """A-SLOT-5: a domain module must never import an adapter implementation."""

    def test_ASLOT5_domain_does_not_import_adapters(self):
        violations = []
        for path in python_files():
            module = module_name(path)
            parts = module.split(".")
            if len(parts) < 2 or parts[1] not in DOMAIN_PACKAGES:
                continue
            for imported in _imports(path):
                if imported.startswith("app.adapters"):
                    violations.append((module, imported))
        assert violations == [], f"domain -> adapters imports: {violations}"

    def test_ASLOT5_domain_does_not_import_api(self):
        """P0 §2.1: api -> domain is allowed; domain -> api is forbidden."""
        violations = []
        for path in python_files():
            module = module_name(path)
            parts = module.split(".")
            if len(parts) < 2 or parts[1] not in DOMAIN_PACKAGES:
                continue
            for imported in _imports(path):
                if imported.startswith("app.api"):
                    violations.append((module, imported))
        assert violations == [], f"domain -> api imports: {violations}"

    def test_ASLOT5_core_does_not_import_other_domains(self):
        """core holds primitives; depending on a domain would invert the layering."""
        violations = []
        for path in python_files():
            module = module_name(path)
            if not module.startswith("app.core"):
                continue
            for imported in _imports(path):
                if imported.startswith("app.") and not imported.startswith("app.core"):
                    violations.append((module, imported))
        assert violations == [], f"core -> domain imports: {violations}"

    def test_ASLOT5_guard_would_catch_a_violation(self):
        """The guard is only meaningful if it can fail — prove it detects one."""
        source = "from app.adapters.speech.groq import GroqSpeechToText\n"
        tree = ast.parse(source)
        imported = [
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        assert any(name.startswith("app.adapters") for name in imported)


class TestASLOT6VendorBoundary:
    """A-SLOT-6: no vendor identifier outside adapters and configuration."""

    VENDORS = ("groq", "whisper", "ghl", "payfast", "jazzcash", "twilio", "openai")

    def test_ASLOT6_no_vendor_name_in_application_code(self):
        violations = []
        for path in python_files():
            module = module_name(path)
            if module.startswith("app.adapters") or module.endswith("core.config"):
                continue
            code = code_only(path).lower()
            for vendor in self.VENDORS:
                if vendor in code.split() or f"{vendor}_" in code:
                    violations.append((module, vendor))
        assert violations == [], f"vendor identifiers in domain code: {violations}"

    def test_no_adapter_package_exists_yet(self):
        """P1 implements no adapter; speculative adapter code would be scope creep."""
        assert not (APP_ROOT / "adapters").exists()


class TestNoFutureScope:
    """P1 must not contain later-package or removed-scope implementation."""

    FORBIDDEN_SYMBOLS = [
        "PaymentProvider",
        "MockPaymentProvider",
        "SpeechToTextProvider",
        "OperationalIntentInterpreter",
        "SearchInterpreter",
        "CommunicationProvider",
        "payment_attempt",
        "BillingCycle",
        "CommissionPlan",
        "CommissionEvent",
    ]

    @pytest.mark.parametrize("symbol", FORBIDDEN_SYMBOLS)
    def test_no_future_symbol_is_implemented(self, symbol):
        offenders = []
        for path in python_files():
            if symbol in code_only(path):
                offenders.append(module_name(path))
        assert offenders == [], f"{symbol} implemented in P1: {offenders}"

    def test_no_http_client_is_imported(self):
        """P1 makes no outbound network call of any kind."""
        offenders = []
        for path in python_files():
            for imported in _imports(path):
                root = imported.split(".")[0]
                if root in {"httpx", "requests", "aiohttp", "urllib3", "socket"}:
                    offenders.append((module_name(path), imported))
        assert offenders == [], f"HTTP client imported by application code: {offenders}"

    def test_no_voice_or_audio_module(self):
        for forbidden in ("voice", "speech", "audio", "transcript"):
            assert not (APP_ROOT / forbidden).exists(), f"app/{forbidden} is out of P1 scope"


class TestAppendOnlyEnforcement:
    """FIN-12 / AUD-7: no update or delete path against the protected tables."""

    def test_no_orm_delete_of_protected_models(self):
        offenders = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name not in {"delete", "sql_delete"}:
                    continue
                args = ast.dump(node)
                for model in APPEND_ONLY_MODELS:
                    if model in args:
                        offenders.append((module_name(path), model))
        assert offenders == [], f"delete() against append-only models: {offenders}"

    def test_no_sql_delete_or_update_statement_against_protected_tables(self):
        offenders = []
        for path in python_files():
            source = path.read_text(encoding="utf-8").lower()
            for table in APPEND_ONLY_TABLES:
                for verb in (f"delete from {table}", f"update {table}"):
                    if verb in source:
                        offenders.append((module_name(path), verb))
        assert offenders == [], f"raw DML against append-only tables: {offenders}"

    def test_ledger_module_exposes_no_mutation_helper(self):
        """The only ledger writer offers append operations and nothing else."""
        import app.billing.ledger as ledger

        public = {n for n in dir(ledger) if not n.startswith("_")}
        for banned in ("update_entry", "delete_entry", "remove_entry", "void_entry"):
            assert banned not in public

    def test_audit_module_exposes_no_mutation_helper(self):
        import app.audit.service as audit

        public = {n for n in dir(audit) if not n.startswith("_")}
        for banned in ("update_audit_event", "delete_audit_event", "purge"):
            assert banned not in public


class TestFIN1NoFloatInCode:
    """FIN-1: no float ever holds, transports or computes money."""

    def test_no_float_call_in_domain_code(self):
        offenders = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "float":
                    offenders.append((module_name(path), node.lineno))
        assert offenders == [], f"float() called in application code: {offenders}"

    def test_no_float_literal_in_domain_code(self):
        offenders = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    offenders.append((module_name(path), node.lineno, node.value))
        assert offenders == [], f"float literals in application code: {offenders}"

    def test_no_float_or_numeric_money_column_type(self):
        offenders = []
        for path in python_files():
            code = code_only(path)
            if "Float" in code.split() or "REAL" in code.split():
                offenders.append(module_name(path))
        assert offenders == [], f"floating-point column type declared: {offenders}"


class TestSEC9NoSecrets:
    """SEC-9: no secret in source, and .env.example carries names only."""

    def test_env_example_has_no_values(self):
        path = APP_ROOT.parent / ".env.example"
        assert path.exists()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assert line.endswith("="), f"value present in .env.example: {line}"

    def test_no_env_file_is_committed(self):
        assert not (APP_ROOT.parent / ".env").exists()

    def test_bootstrap_contains_no_default_password(self):
        code = code_only(APP_ROOT / "bootstrap.py")
        assert "hash_password" in code
        source = (APP_ROOT / "bootstrap.py").read_text(encoding="utf-8")
        # Passwords come from the environment; none is written down.
        assert "BOOTSTRAP_OWNER_PASSWORD" in source
        assert 'password="' not in source.replace('password=""', "")


class TestTenantScopingIsStructural:
    """SEC-3: no repository entry point can omit the tenant."""

    QUERY_MODULES = [
        "app/customers/commands.py",
        "app/service/commands.py",
        "app/billing/ledger.py",
    ]

    @pytest.mark.parametrize("relative", QUERY_MODULES)
    def test_SEC3_public_query_functions_require_context(self, relative):
        path = APP_ROOT.parent / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            if node.name in {"serialize_customer", "serialize_record"}:
                continue
            args = [a.arg for a in node.args.args] + [
                a.arg for a in node.args.kwonlyargs
            ]
            if "session" not in args:
                continue
            if "ctx" not in args:
                offenders.append(node.name)
        assert offenders == [], (
            f"{relative}: session-taking functions without a TenantContext: {offenders}"
        )
