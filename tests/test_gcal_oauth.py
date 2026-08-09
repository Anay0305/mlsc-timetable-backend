"""Scope handling — the reason three production connections silently 403'd."""

from __future__ import annotations

import unittest

from server.gcal import oauth


DECLINED = {  # exactly what the three failing production users granted
    "scope": "https://www.googleapis.com/auth/userinfo.email openid",
}
FULL = {
    "scope": (
        "https://www.googleapis.com/auth/userinfo.email openid "
        "https://www.googleapis.com/auth/calendar"
    ),
}


class GrantedScopeTests(unittest.TestCase):
    def test_reads_a_space_separated_grant(self):
        self.assertEqual(
            oauth.granted_scopes(FULL),
            frozenset({oauth.CALENDAR_SCOPE, oauth.EMAIL_SCOPE, oauth.OPENID_SCOPE}),
        )

    def test_accepts_a_list_form(self):
        self.assertEqual(
            oauth.granted_scopes({"scope": [oauth.CALENDAR_SCOPE]}),
            frozenset({oauth.CALENDAR_SCOPE}),
        )

    def test_missing_or_empty_grant_is_empty_not_an_error(self):
        for payload in (None, {}, {"scope": ""}, {"scope": None}):
            self.assertEqual(oauth.granted_scopes(payload), frozenset())


class RequirementTests(unittest.TestCase):
    def test_the_real_declined_grant_is_rejected(self):
        granted = oauth.granted_scopes(DECLINED)
        self.assertFalse(oauth.has_calendar_access(granted))
        self.assertEqual(oauth.missing_scopes(granted), frozenset({oauth.CALENDAR_SCOPE}))

    def test_a_complete_grant_is_accepted(self):
        self.assertTrue(oauth.has_calendar_access(oauth.granted_scopes(FULL)))
        self.assertEqual(oauth.missing_scopes(oauth.granted_scopes(FULL)), frozenset())

    def test_calendar_alone_is_enough(self):
        # email/openid are only used to label the connection.
        self.assertTrue(oauth.has_calendar_access({oauth.CALENDAR_SCOPE}))

    def test_readonly_calendar_does_not_count(self):
        self.assertFalse(
            oauth.has_calendar_access({"https://www.googleapis.com/auth/calendar.readonly"})
        )

    def test_events_scope_does_not_count(self):
        # calendar.events can write events but cannot create a calendar, which
        # is precisely the call that returned 403.
        self.assertFalse(
            oauth.has_calendar_access({"https://www.googleapis.com/auth/calendar.events"})
        )


class MessageTests(unittest.TestCase):
    def test_message_names_the_tick_box(self):
        message = oauth.consent_error_message({oauth.CALENDAR_SCOPE})
        self.assertIn("Calendar permission", message)
        self.assertIn("tick", message.lower())

    def test_requested_scope_string_includes_calendar(self):
        self.assertIn(oauth.CALENDAR_SCOPE, oauth.SCOPE_PARAM)


if __name__ == "__main__":
    unittest.main()
