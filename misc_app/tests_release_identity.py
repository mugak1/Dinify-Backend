"""The loaded-process release identity (D08 B3): what each plane's route says about the
process that answered, and what it refuses to say.

The routes are unauthenticated on purpose (the transition verifier reads them over loopback
with no credential), so every test here also pins what they must NEVER disclose.
"""

import json

from django.test import SimpleTestCase, override_settings

from dinify_backend import release_identity as ri

VERIFIED = {
    "plane": "customer", "state": "verified", "reason": None,
    "release": {"id": "a" * 40 + "-" + "b" * 16, "commit": "a" * 40, "tree": "c" * 40, "environmentDigest": "d" * 64,
                "recordSha256": "e" * 64, "ciRun": "36261585225", "ciAttempt": "1", "installedAt": "2026-09-27T01:02:03Z"},
    "process": {"instance": "f" * 32, "loadedAt": "2026-09-27T01:02:04Z", "python": "3.12.3", "modWsgi": "5.0.0",
                "processGroup": "dinify-customer"},
}


class IdentityModuleTests(SimpleTestCase):
    def setUp(self):
        ri._reset_for_tests()
        self.addCleanup(ri._reset_for_tests)

    def test_a_process_not_started_by_the_launcher_says_unavailable_never_a_guess(self):
        for plane in ri.PLANES:
            doc = ri.current(plane)
            self.assertEqual((doc["state"], doc["reason"], doc["release"], doc["process"]),
                             ("unavailable", "not_started_by_release_launcher", None, None))

    def test_installed_once_and_reported_verbatim(self):
        ri.install(VERIFIED)
        self.assertEqual(ri.current("customer")["release"]["id"], VERIFIED["release"]["id"])
        with self.assertRaises(RuntimeError):
            ri.install(VERIFIED)   # a process loads one release in its lifetime

    def test_the_other_plane_is_never_lent_this_identity(self):
        ri.install(VERIFIED)
        doc = ri.current("admin")
        self.assertEqual((doc["state"], doc["reason"], doc["release"]), ("mismatch", "plane_mismatch", None))

    def test_a_returned_document_cannot_relabel_the_process(self):
        ri.install(VERIFIED)
        ri.current("customer")["release"]["id"] = "0" * 40 + "-" + "0" * 16
        self.assertEqual(ri.current("customer")["release"]["id"], VERIFIED["release"]["id"])

    def test_unbounded_or_unknown_fields_are_refused_not_dropped(self):
        bad = [
            dict(VERIFIED, path="/var/www/x"),
            dict(VERIFIED, release=dict(VERIFIED["release"], source="/home/ubuntu/app")),
            dict(VERIFIED, release=dict(VERIFIED["release"], id="../../etc")),
            dict(VERIFIED, process=dict(VERIFIED["process"], python="3.12.3 /usr/bin/python")),
            dict(VERIFIED, process=dict(VERIFIED["process"], secret="x")),
            dict(VERIFIED, reason="interpreter_outside_release"),        # verified never carries a reason
            dict(VERIFIED, state="mismatch", reason=None),               # a mismatch always names one
            dict(VERIFIED, plane="kitchen"),
        ]
        for doc in bad:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                ri.install(doc)
        self.assertEqual(ri.current("customer")["state"], "unavailable")

    def test_a_mismatch_that_could_not_read_its_receipt_carries_no_release(self):
        doc = ri.install(dict(VERIFIED, state="mismatch", reason="receipt_unreadable", release=None))
        self.assertIsNone(doc["release"])


@override_settings(ALLOWED_HOSTS=["*"])
class IdentityRouteTests(SimpleTestCase):
    def setUp(self):
        ri._reset_for_tests()
        self.addCleanup(ri._reset_for_tests)

    def _get(self, path, urlconf, **headers):
        with self.settings(ROOT_URLCONF=urlconf):
            return self.client.get(path, **headers)

    def test_both_planes_answer_unavailable_today_and_are_not_cached(self):
        for path, urlconf, plane in (("/api/v1/release/", "dinify_backend.urls", "customer"),
                                     ("/admin/v1/release/", "dinify_backend.urls_admin", "admin")):
            with self.subTest(plane=plane):
                response = self._get(path, urlconf)
                self.assertEqual(response.status_code, 200)
                body = json.loads(response.content)
                self.assertEqual((body["schema"], body["plane"], body["state"]), (ri.SCHEMA, plane, "unavailable"))
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["Pragma"], "no-cache")

    def test_the_customer_route_reports_the_installed_identity_and_no_path(self):
        ri.install(VERIFIED)
        body = json.loads(self._get("/api/v1/release/", "dinify_backend.urls").content)
        self.assertEqual(body["release"], VERIFIED["release"])
        self.assertNotIn("/", json.dumps(body["release"]) + json.dumps(body["process"]))

    def test_the_admin_route_does_not_borrow_the_customer_identity(self):
        ri.install(VERIFIED)
        body = json.loads(self._get("/admin/v1/release/", "dinify_backend.urls_admin").content)
        self.assertEqual((body["state"], body["reason"]), ("mismatch", "plane_mismatch"))

    def test_credentials_are_neither_needed_nor_consulted(self):
        response = self._get("/api/v1/release/", "dinify_backend.urls", HTTP_AUTHORIZATION="Bearer not-a-token")
        self.assertEqual(response.status_code, 200)

    def test_only_get_is_served(self):
        with self.settings(ROOT_URLCONF="dinify_backend.urls"):
            self.assertEqual(self.client.post("/api/v1/release/").status_code, 405)
