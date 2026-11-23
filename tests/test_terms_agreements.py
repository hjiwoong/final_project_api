import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.terms import TERMS
from database.db_connection import get_db
from database.orm import Base
from models.models import SocialAccount, TermType, TermsAgreement, User, UserRole
from routers import alert, chat, child_request, location, notification, parent_bus, rag, social, student, user


class TermsAgreementTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        tables = [t for t in Base.metadata.sorted_tables if t.name != "event_images"]
        Base.metadata.create_all(self.engine, tables=tables)
        self.sessions = sessionmaker(bind=self.engine)
        app = FastAPI()
        for router in (user, social, student, child_request, parent_bus, location, alert, notification, chat, rag):
            app.include_router(router.router)

        def override_db():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app, raise_server_exceptions=False)
        self.agreements = [
            {"term_type": term["type"].value, "term_version": term["version"]}
            for term in TERMS
        ]
        self.signup = {
            "email": "parent@example.com",
            "password": "Pass!",
            "name": "Parent",
            "agreements": self.agreements,
        }

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_terms_are_public_and_signup_saves_server_time(self):
        terms = self.client.get("/terms")
        self.assertEqual(terms.status_code, 200)
        self.assertEqual(
            [(t["type"], t["version"], t["required"]) for t in terms.json()],
            [(t["type"].value, t["version"], True) for t in TERMS],
        )
        self.assertTrue(all(t["title"] for t in terms.json()))
        agreed_at = datetime(2026, 11, 18, 12, 34, 56)
        payload = dict(self.signup)
        payload["agreements"] = [
            {**item, "agreed_at": "2000-01-01T00:00:00"} for item in self.agreements
        ]
        with patch("routers.user.now_kst_naive", return_value=agreed_at):
            response = self.client.post("/auth/signup", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["pending_terms"], [])
        with self.sessions() as db:
            saved_user = db.query(User).filter_by(email=self.signup["email"]).one()
            records = db.query(TermsAgreement).filter_by(user_id=saved_user.id).all()
            self.assertEqual(len(records), 3)
            self.assertEqual({(r.type.value, r.version) for r in records}, {
                (a["term_type"], a["term_version"]) for a in self.agreements
            })
            self.assertTrue(all(r.agreed_at == agreed_at for r in records))
        token = response.json()["access_token"]
        history = self.client.get("/me/agreements", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()), 3)
        self.assertEqual(
            {(item["term_type"], item["term_version"]) for item in history.json()},
            {(item["term_type"], item["term_version"]) for item in self.agreements},
        )
        self.assertTrue(all(item["agreed_at"] == "2026-11-18T12:34:56" for item in history.json()))
        self.assertEqual(self.client.get("/me/agreements").status_code, 401)

    def test_missing_stale_and_duplicate_agreements_are_rejected(self):
        stale = [dict(a) for a in self.agreements]
        stale[0]["term_version"] = "old"
        for agreements in ([], self.agreements[:-1], stale, self.agreements + [self.agreements[0]]):
            with self.subTest(agreements=agreements):
                response = self.client.post("/auth/signup", json={**self.signup, "agreements": agreements})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "필수 약관에 모두 동의해야 가입할 수 있습니다")
                with self.sessions() as db:
                    self.assertEqual(db.query(User).count(), 0)

    def test_agreement_insert_failure_rolls_back_user(self):
        @event.listens_for(TermsAgreement, "before_insert")
        def fail_insert(mapper, connection, target):
            from sqlalchemy.exc import IntegrityError
            raise IntegrityError("insert", {}, Exception("failure"))

        try:
            response = self.client.post("/auth/signup", json=self.signup)
            self.assertEqual(response.status_code, 500)
            with self.sessions() as db:
                self.assertEqual(db.query(User).count(), 0)
                self.assertEqual(db.query(TermsAgreement).count(), 0)
        finally:
            event.remove(TermsAgreement, "before_insert", fail_insert)

    def test_google_agreement_failure_rolls_back_user_and_link(self):
        @event.listens_for(TermsAgreement, "before_insert")
        def fail_insert(mapper, connection, target):
            from sqlalchemy.exc import IntegrityError
            raise IntegrityError("insert", {}, Exception("failure"))

        google = {"email": "google@example.com", "sub": "google-id", "name": "Google"}
        try:
            with patch("routers.social.verify_google_id_token", return_value=google):
                response = self.client.post(
                    "/auth/google",
                    json={"id_token": "valid-token", "agreements": self.agreements},
                )
            self.assertEqual(response.status_code, 500)
            with self.sessions() as db:
                self.assertEqual(db.query(User).count(), 0)
                self.assertEqual(db.query(TermsAgreement).count(), 0)
                self.assertEqual(db.query(SocialAccount).count(), 0)
        finally:
            event.remove(TermsAgreement, "before_insert", fail_insert)

    def test_google_link_failure_rolls_back_user_and_agreements(self):
        @event.listens_for(SocialAccount, "before_insert")
        def fail_insert(mapper, connection, target):
            from sqlalchemy.exc import IntegrityError
            raise IntegrityError("insert", {}, Exception("failure"))

        google = {"email": "google@example.com", "sub": "google-id", "name": "Google"}
        try:
            with patch("routers.social.verify_google_id_token", return_value=google):
                response = self.client.post(
                    "/auth/google",
                    json={"id_token": "valid-token", "agreements": self.agreements},
                )
            self.assertEqual(response.status_code, 500)
            with self.sessions() as db:
                self.assertEqual(db.query(User).count(), 0)
                self.assertEqual(db.query(TermsAgreement).count(), 0)
                self.assertEqual(db.query(SocialAccount).count(), 0)
        finally:
            event.remove(SocialAccount, "before_insert", fail_insert)

    def test_google_new_account_and_existing_login(self):
        google = {"email": "google@example.com", "sub": "google-id", "name": "Google"}
        with patch("routers.social.verify_google_id_token", return_value=google):
            stale = [dict(a) for a in self.agreements]
            stale[0]["term_version"] = "old"
            for agreements in ([], self.agreements[:-1], stale):
                response = self.client.post(
                    "/auth/google", json={"id_token": "valid-token", "agreements": agreements},
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "필수 약관에 모두 동의해야 가입할 수 있습니다")
                with self.sessions() as db:
                    self.assertEqual(db.query(User).count(), 0)
            response = self.client.post(
                "/auth/google", json={"id_token": "valid-token", "agreements": self.agreements},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["is_new"])
            with self.sessions() as db:
                saved_user = db.query(User).one()
                self.assertEqual(saved_user.signup_method, "GOOGLE")
                self.assertEqual(db.query(TermsAgreement).filter_by(user_id=saved_user.id).count(), 3)
                self.assertEqual(db.query(SocialAccount).count(), 1)
            response = self.client.post("/auth/google", json={"id_token": "valid-token"})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["is_new"])

    def test_agreement_history_is_user_scoped(self):
        first = self.client.post("/auth/signup", json=self.signup)
        second = self.client.post(
            "/auth/signup", json={**self.signup, "email": "second@example.com"},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        with self.sessions() as db:
            db.query(TermsAgreement).filter_by(user_id=second.json()["user"]["id"]).delete()
            db.commit()
        token = second.json()["access_token"]
        response = self.client.get("/me/agreements", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_admin_creation_and_google_link_do_not_require_agreements(self):
        with self.sessions() as db:
            admin_created = user.create_user_row(db, "existing@example.com", "Pass!", "Existing", UserRole.PARENT,
                                                 signup_method="ADMIN")
            self.assertIsNotNone(admin_created.id)
            self.assertEqual(db.query(TermsAgreement).count(), 0)
        google = {"email": "existing@example.com", "sub": "google-id", "name": "Google"}
        with patch("routers.social.verify_google_id_token", return_value=google):
            response = self.client.post("/auth/google", json={"id_token": "valid-token"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["linked"])
        with self.sessions() as db:
            self.assertEqual(db.query(TermsAgreement).count(), 0)

    def test_account_deletion_cascades_agreements(self):
        response = self.client.post("/auth/signup", json=self.signup)
        self.assertEqual(response.status_code, 201, response.text)
        with self.sessions() as db:
            db.delete(db.query(User).one())
            db.commit()
            self.assertEqual(db.query(TermsAgreement).count(), 0)

    def test_existing_parent_agrees_idempotently_and_preserves_history(self):
        with self.sessions() as db:
            parent = user.create_user_row(
                db, "legacy@example.com", "Pass!", "Legacy", UserRole.PARENT,
                signup_method="ADMIN",
            )
            parent_id = parent.id
            token = user.create_access_token(parent)
            db.add(TermsAgreement(
                user_id=parent_id, type=TermType.COOKIE, version="old",
                agreed_at=datetime(2020, 1, 1),
            ))
            db.add(TermsAgreement(
                user_id=parent_id, type=TermType.TERMS,
                version=self.agreements[1]["term_version"], agreed_at=datetime(2021, 1, 1),
            ))
            db.commit()
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.post("/me/agreements", json=self.signup).status_code, 401)
        response = self.client.post("/me/agreements", json={"agreements": []}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["pending_terms"]), 2)
        now = datetime(2026, 11, 18, 12, 34, 56)
        with patch("routers.user.now_kst_naive", return_value=now):
            response = self.client.post("/me/agreements", json={
                "user_id": 999, "agreements": [
                    {**item, "agreed_at": "2000-01-01T00:00:00"} for item in self.agreements
                ],
            }, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"pending_terms": []})
        with patch("routers.user.now_kst_naive", return_value=datetime(2027, 1, 1)):
            response = self.client.post(
                "/me/agreements", json={"agreements": self.agreements}, headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        with self.sessions() as db:
            records = db.query(TermsAgreement).filter_by(user_id=parent_id).all()
            self.assertEqual(len(records), 4)
            self.assertEqual(sum(r.agreed_at == now for r in records), 2)
            self.assertEqual(sum(r.agreed_at == datetime(2020, 1, 1) for r in records), 1)
            self.assertEqual(sum(r.agreed_at == datetime(2021, 1, 1) for r in records), 1)

    def test_admin_cannot_save_parent_agreements(self):
        with self.sessions() as db:
            admin = user.create_user_row(
                db, "admin@example.com", "Pass!", "Admin", UserRole.ADMIN,
                signup_method="ADMIN",
            )
            token = user.create_access_token(admin)
        response = self.client.post(
            "/me/agreements", json={"agreements": self.agreements},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 403)

    def test_existing_parent_agreement_failure_is_atomic(self):
        with self.sessions() as db:
            parent = user.create_user_row(
                db, "legacy@example.com", "Pass!", "Legacy", UserRole.PARENT,
                signup_method="ADMIN",
            )
            token = user.create_access_token(parent)

        @event.listens_for(TermsAgreement, "before_insert")
        def fail_insert(mapper, connection, target):
            if target.type == TermType.TERMS:
                from sqlalchemy.exc import IntegrityError
                raise IntegrityError("insert", {}, Exception("failure"))

        try:
            response = self.client.post(
                "/me/agreements", json={"agreements": self.agreements},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 500)
            with self.sessions() as db:
                self.assertEqual(db.query(TermsAgreement).count(), 0)
                self.assertEqual(db.query(User).count(), 1)
        finally:
            event.remove(TermsAgreement, "before_insert", fail_insert)

    def test_existing_parent_blocked_until_partial_agreements_completed(self):
        with self.sessions() as db:
            user.create_user_row(db, "legacy@example.com", "Pass!", "Legacy",
                                 UserRole.PARENT, signup_method="ADMIN")
        login = self.client.post("/auth/login", json={"email": "legacy@example.com", "password": "Pass!"})
        self.assertEqual(login.status_code, 200)
        pending = login.json()["pending_terms"]
        self.assertEqual(
            pending,
            [{"term_type": t["type"].value, "term_version": t["version"], "title": t["title"]}
             for t in TERMS],
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        self.assertEqual(self.client.get("/me", headers=headers).json()["pending_terms"], pending)
        blocked_routes = [
            ("GET", "/me/children", None),
            ("GET", "/me/child-requests", None),
            ("GET", "/me/buses", None),
            ("GET", "/me/bus-status", None),
            ("GET", "/me/alerts", None),
            ("GET", "/me/location/search?address=서울", None),
            ("GET", "/me/notifications", None),
            ("GET", "/chat/hello", None),
            ("POST", "/chat", {"message": "안녕"}),
            ("POST", "/rag/ask", {"question": "안녕"}),
            ("PUT", "/me/password", {"current_password": "Pass!", "new_password": "New!"}),
        ]
        for method, path, body in blocked_routes:
            with self.subTest(path=path):
                result = self.client.request(method, path, headers=headers, json=body)
                self.assertEqual(result.status_code, 403, result.text)
                self.assertEqual(result.json()["detail"], {
                    "code": "TERMS_REQUIRED", "message": "약관 동의가 필요합니다",
                    "pending_terms": pending,
                })
        self.assertEqual(self.client.put("/me/push-token", headers=headers, json={"push_token": "device"}).status_code, 204)
        self.assertEqual(self.client.request("DELETE", "/me/push-token", headers=headers).status_code, 204)
        self.assertEqual(self.client.get("/me/agreements", headers=headers).status_code, 200)
        response = self.client.post(
            "/me/agreements", headers=headers,
            json={"agreements": [self.agreements[0], self.agreements[0]]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pending_terms"], pending[1:])
        self.assertEqual(self.client.get("/me/children", headers=headers).status_code, 403)
        response = self.client.post(
            "/me/agreements", headers=headers, json={"agreements": self.agreements[1:]},
        )
        self.assertEqual(response.json(), {"pending_terms": []})
        self.assertEqual(self.client.get("/me/children", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/me", headers=headers).json()["pending_terms"], [])

    def test_changed_version_and_rejected_stale_request(self):
        response = self.client.post("/auth/signup", json=self.signup)
        self.assertEqual(response.status_code, 201)
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        changed_terms = tuple(
            {**term, "version": "2027-01-01"} if term["type"] == TermType.COOKIE else term
            for term in TERMS
        )
        with patch("core.terms.TERMS", changed_terms):
            pending = self.client.get("/me", headers=headers).json()["pending_terms"]
            self.assertEqual(pending, [{
                "term_type": "COOKIE", "term_version": "2027-01-01", "title": "쿠키 사용",
            }])
            self.assertEqual(self.client.get("/terms").json()[0]["version"], "2027-01-01")
            self.assertEqual(self.client.get("/me/children", headers=headers).json()["detail"]["pending_terms"], pending)
            stale = self.client.post("/me/agreements", headers=headers, json={"agreements": self.agreements})
            self.assertEqual(stale.status_code, 422)
            self.assertEqual(stale.json()["detail"], "약관 버전이 변경되었습니다. 다시 확인해 주세요")
            updated = self.client.post(
                "/me/agreements", headers=headers,
                json={"agreements": [{"term_type": "COOKIE", "term_version": "2027-01-01"}]},
            )
            self.assertEqual(updated.json(), {"pending_terms": []})
            self.assertEqual(self.client.get("/me/children", headers=headers).status_code, 200)

    def test_admin_remains_unblocked_on_shared_and_web_routes(self):
        with self.sessions() as db:
            user.create_user_row(db, "admin@example.com", "Pass!", "Admin",
                                 UserRole.ADMIN, signup_method="ADMIN")
        response = self.client.post("/web/login", json={"email": "admin@example.com", "password": "Pass!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pending_terms"], [])
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        for path in ("/me", "/me/notifications", "/chat/hello", "/web/students"):
            with self.subTest(path=path):
                result = self.client.get(path, headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.get("/me/children", headers=headers).status_code, 403)

    def test_google_login_and_account_recovery_report_pending(self):
        with self.sessions() as db:
            parent = user.create_user_row(db, "legacy@example.com", "Pass!", "Legacy",
                                          UserRole.PARENT, signup_method="ADMIN")
            parent.deletion_requested_at = datetime.now()
            db.commit()
        recovery = self.client.post(
            "/auth/account-deletion/cancel",
            json={"email": "legacy@example.com", "password": "Pass!"},
        )
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(len(recovery.json()["pending_terms"]), 3)
        google = {"email": "legacy@example.com", "sub": "google-id", "name": "Google"}
        with patch("routers.social.verify_google_id_token", return_value=google):
            response = self.client.post("/auth/google", json={"id_token": "valid-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["pending_terms"]), 3)

    def test_optional_term_is_not_required_and_stale_submission_is_atomic(self):
        with self.sessions() as db:
            parent = user.create_user_row(db, "legacy@example.com", "Pass!", "Legacy",
                                          UserRole.PARENT, signup_method="ADMIN")
            token = user.create_access_token(parent)
        headers = {"Authorization": f"Bearer {token}"}
        optional_terms = tuple(
            {**term, "required": False} if term["type"] == TermType.PRIVACY else term
            for term in TERMS
        )
        with patch("core.terms.TERMS", optional_terms):
            pending = self.client.get("/me", headers=headers).json()["pending_terms"]
            self.assertEqual(len(pending), 2)
            invalid = [self.agreements[0], {"term_type": "TERMS", "term_version": "old"}]
            response = self.client.post("/me/agreements", headers=headers, json={"agreements": invalid})
            self.assertEqual(response.status_code, 422)
            with self.sessions() as db:
                self.assertEqual(db.query(TermsAgreement).count(), 0)
            response = self.client.post(
                "/me/agreements", headers=headers, json={"agreements": self.agreements[:2]},
            )
            self.assertEqual(response.json(), {"pending_terms": []})
            self.assertEqual(self.client.get("/me/children", headers=headers).status_code, 200)

    def test_account_deletion_allowed_without_agreement(self):
        with self.sessions() as db:
            parent = user.create_user_row(db, "legacy@example.com", "Pass!", "Legacy",
                                          UserRole.PARENT, signup_method="ADMIN")
            token = user.create_access_token(parent)
        response = self.client.post(
            "/me/account-deletion", json={"current_password": "Pass!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 202, response.text)

if __name__ == "__main__":
    unittest.main()
