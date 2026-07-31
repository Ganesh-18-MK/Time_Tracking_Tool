"""app/auth.py's department-scoping helper (Ganesh, 2026-07-31) — the
single place that decides whether an admin sees every department (super
admin) or only their own (department-scoped admin/team lead). Dashboard,
Leave Requests, and Reports all key off this one function; a bug here
would silently under- or over-scope every one of those screens at once."""
from types import SimpleNamespace

from app.auth import admin_department_scope


def _admin(is_super_admin, department):
    return SimpleNamespace(is_super_admin=is_super_admin, department=department)


class TestAdminDepartmentScope:
    def test_super_admin_has_no_restriction(self):
        assert admin_department_scope(_admin(True, "Ops")) is None

    def test_department_scoped_admin_gets_their_own_department(self):
        assert admin_department_scope(_admin(False, "Frontdesk")) == "Frontdesk"

    def test_blank_department_falls_back_to_em_dash(self):
        # matches the "—" fallback used everywhere else in the app for a
        # blank Employee.department (dashboard grouping, roster pills, etc.)
        assert admin_department_scope(_admin(False, "")) == "—"

    def test_super_admin_with_blank_department_is_still_unrestricted(self):
        # is_super_admin wins regardless of what department they're set to
        assert admin_department_scope(_admin(True, "")) is None
