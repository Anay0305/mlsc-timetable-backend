"""Reconciliation — converging on Google without duplicating or destroying."""

from __future__ import annotations

import unittest

from server.gcal.projection import DesiredEvent
from server.gcal.reconcile import (
    MirrorRow,
    RemoteEvent,
    plan_sync,
    to_google_event,
)


def desired(slot="s_a", summary="Software Engineering (LT102)", start="09:40:00"):
    return DesiredEvent(
        slot_id=slot, kind="class", summary=summary, description="d",
        start_date="2026-07-27", start_time=start, end_time="10:30:00",
        recurrence=("RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261219T235959Z",),
        color_id="9",
    )


def remote_of(event: DesiredEvent, event_id="g1", fingerprint=None):
    return RemoteEvent(event_id=event_id, slot_id=event.slot_id,
                       fingerprint=fingerprint if fingerprint is not None else event.fingerprint)


class MirrorOnlyTests(unittest.TestCase):
    """The steady state: no Google listing, just what we recorded."""

    def test_first_ever_sync_creates(self):
        plan = plan_sync([desired()], mirror=[])
        self.assertEqual(len(plan.create), 1)
        self.assertEqual(plan.writes, 1)

    def test_unchanged_content_does_nothing(self):
        event = desired()
        plan = plan_sync([event], mirror=[MirrorRow(event.slot_id, "g1", event.fingerprint)])
        self.assertTrue(plan.is_noop)
        self.assertEqual(plan.unchanged, [event.slot_id])

    def test_changed_content_patches_rather_than_recreating(self):
        old = desired(summary="Software Engineering (LT102)")
        new = desired(summary="Software Engineering (LT999)")
        plan = plan_sync([new], mirror=[MirrorRow(old.slot_id, "g1", old.fingerprint)])
        self.assertEqual(plan.patch, [("g1", new)])
        self.assertEqual(plan.create, [])
        self.assertEqual(plan.delete, [])

    def test_dropped_class_is_deleted(self):
        gone = desired()
        plan = plan_sync([], mirror=[MirrorRow(gone.slot_id, "g1", gone.fingerprint)])
        self.assertEqual(plan.delete, ["g1"])

    def test_repeated_sync_converges(self):
        event = desired()
        first = plan_sync([event], mirror=[])
        self.assertEqual(len(first.create), 1)
        # After applying, the mirror holds the row; a second run must be a no-op.
        second = plan_sync([event], mirror=[MirrorRow(event.slot_id, "g1", event.fingerprint)])
        self.assertTrue(second.is_noop)


class AdoptionTests(unittest.TestCase):
    """First run after deploy: the mirror is empty but Google already has events."""

    def test_existing_events_are_adopted_not_recreated(self):
        event = desired()
        plan = plan_sync([event], mirror=[], remote=[remote_of(event)])
        self.assertEqual(plan.adopt, [("g1", event)])
        self.assertEqual(plan.create, [])
        self.assertEqual(plan.delete, [])
        self.assertEqual(plan.writes, 0, "adoption must not touch Google")

    def test_adopted_event_that_drifted_is_patched(self):
        event = desired()
        plan = plan_sync([event], mirror=[], remote=[remote_of(event, fingerprint="stale")])
        self.assertEqual(plan.patch, [("g1", event)])
        self.assertEqual(plan.create, [])

    def test_google_wins_over_a_stale_mirror(self):
        event = desired()
        plan = plan_sync(
            [event],
            mirror=[MirrorRow(event.slot_id, "OLD", "whatever")],
            remote=[remote_of(event, event_id="g1")],
        )
        # The mirror pointed at a different id; adopt the real one.
        self.assertEqual(plan.adopt, [("g1", event)])
        self.assertEqual(plan.delete, [])


class DuplicateTests(unittest.TestCase):
    def test_two_remote_events_for_one_slot_leaves_exactly_one(self):
        event = desired()
        plan = plan_sync([event], mirror=[], remote=[
            remote_of(event, event_id="g1"),
            remote_of(event, event_id="g2"),
        ])
        self.assertEqual(plan.delete, ["g2"])
        self.assertEqual(plan.create, [])
        self.assertEqual(len(plan.adopt), 1)

    def test_duplicate_desired_events_create_once(self):
        event = desired()
        plan = plan_sync([event, event], mirror=[])
        self.assertEqual(len(plan.create), 1)


class SafetyTests(unittest.TestCase):
    def test_a_users_own_event_is_never_deleted(self):
        plan = plan_sync([], mirror=[], remote=[RemoteEvent(event_id="mine", slot_id=None)])
        self.assertEqual(plan.delete, [])
        self.assertEqual(plan.foreign, ["mine"], "should be reported, not silently ignored")

    def test_foreign_events_can_be_removed_when_explicitly_asked(self):
        plan = plan_sync([], mirror=[], remote=[RemoteEvent(event_id="mine", slot_id=None)],
                         delete_foreign=True)
        self.assertEqual(plan.delete, ["mine"])

    def test_stale_event_of_ours_is_removed(self):
        gone = desired()
        plan = plan_sync([], mirror=[], remote=[remote_of(gone)])
        self.assertEqual(plan.delete, ["g1"])


class GoogleBodyTests(unittest.TestCase):
    def test_timed_event_carries_identity_and_timezone(self):
        event = desired()
        body = to_google_event(event, timezone="Asia/Kolkata")
        private = body["extendedProperties"]["private"]
        self.assertEqual(private["mlscSlotId"], event.slot_id)
        self.assertEqual(private["mlscFingerprint"], event.fingerprint)
        self.assertEqual(body["start"]["dateTime"], "2026-07-27T09:40:00")
        self.assertEqual(body["start"]["timeZone"], "Asia/Kolkata")
        self.assertIn("recurrence", body)

    def test_all_day_event_ends_the_next_day(self):
        banner = DesiredEvent(
            slot_id="s_b", kind="all_day", summary="MST week", description="d",
            start_date="2026-09-14", start_time=None, end_time=None,
        )
        body = to_google_event(banner, timezone="Asia/Kolkata")
        self.assertEqual(body["start"], {"date": "2026-09-14"})
        self.assertEqual(body["end"], {"date": "2026-09-15"})
        self.assertEqual(body["transparency"], "transparent")



class LegacyAdoptionTests(unittest.TestCase):
    """Events created before the rewrite must not be wiped and recreated."""

    def _legacy(self, slot="s_new", legacy="oldsha1"):
        return DesiredEvent(
            slot_id=slot, kind="class", summary="Software Engineering (LT102)",
            description="d", start_date="2026-07-27", start_time="09:40:00",
            end_time="10:30:00", legacy_slot_id=legacy,
        )

    def test_event_with_the_old_identity_is_restamped_not_recreated(self):
        event = self._legacy()
        plan = plan_sync([event], mirror=[],
                         remote=[RemoteEvent(event_id="g1", slot_id="oldsha1", fingerprint="x")])
        self.assertEqual(plan.patch, [("g1", event)])
        self.assertEqual(plan.create, [], "must not create a second copy")
        self.assertEqual(plan.delete, [], "must not delete the existing event")

    def test_the_new_identity_wins_when_both_are_present(self):
        event = self._legacy()
        plan = plan_sync([event], mirror=[], remote=[
            RemoteEvent(event_id="new", slot_id="s_new", fingerprint=event.fingerprint),
            RemoteEvent(event_id="old", slot_id="oldsha1", fingerprint="x"),
        ])
        self.assertEqual(len(plan.adopt), 1)
        self.assertEqual(plan.adopt[0][0], "new")
        self.assertEqual(plan.delete, ["old"], "the superseded copy is removed")
        self.assertEqual(plan.create, [])

    def test_a_second_run_after_restamping_is_a_noop(self):
        event = self._legacy()
        plan = plan_sync([event], mirror=[MirrorRow(event.slot_id, "g1", event.fingerprint)],
                         remote=[RemoteEvent("g1", event.slot_id, event.fingerprint)])
        self.assertTrue(plan.is_noop)

if __name__ == "__main__":
    unittest.main()
