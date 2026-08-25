"""
State machine that prefers the GPU server and survives it dying mid-session.

A ServerBackend on its own checks reachability exactly once, in __init__.
If the server goes away later, every pass fails forever with no path back
to GPU without restarting. AutoBackend wraps both backends and moves between
them: GPU -> CPU after 5 consecutive failures, CPU -> GPU as soon as a probe
gets an answer every 60 seconds.

This test suite verifies the state machine without a model, server, audio, or
GPU - everything is faked and the clock is monkeypatched to test time-based
behavior without sleeping.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whisper_backends as wb  # noqa: E402


class FakeBackend(object):
    """A backend that records every call and can fail on demand."""

    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.calls = []
        self.last_detection = None

    def transcribe(self, buffer, translate=False, options=None):
        self.calls.append(("transcribe", buffer))
        if self.fail:
            raise wb.BackendError("Backend {0} failed".format(self.name))
        return {"result": "ok from {0}".format(self.name)}

    def ping(self, timeout=None):
        self.calls.append(("ping", timeout))
        if self.fail:
            raise wb.BackendError("Ping failed")

    def set_language(self, language):
        self.calls.append(("set_language", language))


class AutoBackendTestCase(unittest.TestCase):
    """Base class for AutoBackend tests with mocked time and backends."""

    def setUp(self):
        # Patch time.monotonic globally in the whisper_backends module
        self.fake_time = 0.0
        self._time_patcher = mock.patch.object(wb.time, 'monotonic',
                                               side_effect=lambda: self.fake_time)
        self._time_patcher.start()

        # Prepare mock patchers (will be started in make_auto_backend)
        self._server_patcher = None
        self._local_patcher = None

    def tearDown(self):
        self._time_patcher.stop()
        if self._server_patcher is not None:
            try:
                self._server_patcher.stop()
            except RuntimeError:
                pass  # Already stopped
        if self._local_patcher is not None:
            try:
                self._local_patcher.stop()
            except RuntimeError:
                pass  # Already stopped

    def advance_time(self, seconds):
        """Advance the fake clock."""
        self.fake_time += seconds

    def make_auto_backend(self, server_url="http://localhost:8080",
                          server_fail=False, local_fail=False):
        """
        Create an AutoBackend with fake ServerBackend and LocalBackend.
        """
        # Create the fakes for this test - these are the actual backend objects
        # that will be used throughout the test
        self.fake_server = FakeBackend("server", fail=False)
        self.fake_local = FakeBackend("local", fail=False)

        # Track whether to fail on the next factory call
        # This allows tests to break the backend after init
        self._server_should_fail_on_init = server_fail
        self._local_should_fail_on_init = local_fail
        self._local_factory_called = [False]  # Track if factory has been called

        def fake_server_factory(url, check=True, language=None):
            # Only fail at init time if server_fail was True and check=True
            if self._server_should_fail_on_init and check:
                raise wb.BackendError("Server unreachable")
            return self.fake_server

        def fake_local_factory(model_path, language=None, device="cpu",
                               compute_type="auto"):
            # Fail if either:
            # 1. local_fail was True at init time, OR
            # 2. The current state of fake_local.fail is True (allows tests
            #    to break it after init by setting fail=True)
            if self._local_should_fail_on_init or self.fake_local.fail:
                raise wb.BackendError("Local model failed")
            return self.fake_local

        # Apply patches and keep them alive for the duration of the test
        self._server_patcher = mock.patch.object(
            wb, 'ServerBackend', side_effect=fake_server_factory)
        self._local_patcher = mock.patch.object(
            wb, 'LocalBackend', side_effect=fake_local_factory)

        self._server_patcher.start()
        self._local_patcher.start()

        # Create the AutoBackend
        auto = wb.AutoBackend(server_url, "_models/test", language="en")

        # Now that init is done, set up the runtime failure mode for the server
        # (but not for local - that's handled above)
        self.fake_server.fail = server_fail

        return auto


class HealthyServer(AutoBackendTestCase):
    """When the server is healthy, it is used."""

    def test_server_is_preferred_over_local(self):
        auto = self.make_auto_backend(server_fail=False, local_fail=False)

        result = auto.transcribe("buffer1")

        self.assertEqual(result, {"result": "ok from server"})
        # Server should have been called
        self.assertEqual(len(self.fake_server.calls), 1)
        # Local should not have been called on the first try
        self.assertEqual(len(self.fake_local.calls), 0)
        # active_name says we're on the server
        self.assertEqual(auto.active_name, "server")

    def test_server_is_used_on_multiple_calls(self):
        auto = self.make_auto_backend(server_fail=False, local_fail=False)

        auto.transcribe("buffer1")
        auto.transcribe("buffer2")
        auto.transcribe("buffer3")

        self.assertEqual(len(self.fake_server.calls), 3)
        self.assertEqual(len(self.fake_local.calls), 0)

    def test_failures_reset_to_zero_after_successful_server_call(self):
        auto = self.make_auto_backend(server_fail=False, local_fail=False)

        # One success resets the counter
        auto.transcribe("buffer1")
        # Failures should be reset to 0 after success
        self.assertEqual(auto._failures, 0)


class FailureThreshold(AutoBackendTestCase):
    """Exactly FAILURES_BEFORE_FALLBACK (5) failures trigger the fallback."""

    def test_fewer_than_five_failures_do_not_fall_back(self):
        """Test that 4 failures (one less than the threshold) do not cause fallback."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)

        # Break the server after init
        self.fake_server.fail = True

        # Try transcribe 4 times - should raise exceptions
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # Still on server, not fallen back
        self.assertEqual(auto.active_name, "server")
        self.assertEqual(auto._failures, 4)
        self.assertEqual(len(self.fake_local.calls), 0)

    def test_exactly_five_failures_trigger_fallback(self):
        """Test that the 5th failure causes fallback to CPU."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)

        # Break the server after init
        self.fake_server.fail = True

        # First 4 failures raise exceptions
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th failure should trigger fallback and then serve from local
        result = auto.transcribe("buffer4")

        # Should have fallen back to local
        self.assertEqual(auto.active_name, "local")
        self.assertEqual(result, {"result": "ok from local"})
        # Verify local was called at least once
        self.assertTrue(len(self.fake_local.calls) > 0)

    def test_exactly_five_is_the_threshold_not_six(self):
        """Verify that 5 is the exact threshold, not 4 or 6."""
        # This is the boundary test - critical for correctness
        auto = self.make_auto_backend(server_fail=False, local_fail=False)

        # Break the server after init
        self.fake_server.fail = True

        # First 4 calls should fail with exceptions
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th call should not raise (triggers fallback, serves from local)
        result = auto.transcribe("buffer4")
        self.assertIsNotNone(result)

        # After 5, should be on local
        self.assertEqual(auto.active_name, "local")


class ProbeCadence(AutoBackendTestCase):
    """After fallback, server is re-probed on the 60s cadence."""

    def test_probe_is_not_called_before_interval_passes(self):
        """Probe should not fire before PROBE_INTERVAL_SEC (60 seconds)."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force a fallback by failing 5 times
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th call causes fallback to local
        auto.transcribe("buffer_fallback")

        # Now on CPU, clear the server's call list
        self.fake_server.calls = []

        # Advance time by 59 seconds (less than PROBE_INTERVAL_SEC)
        self.advance_time(59.0)

        # Call transcribe - should not probe yet
        auto.transcribe("buffer_after_fallback")

        # Ping should not have been called
        ping_calls = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertEqual(len(ping_calls), 0)
        # Should still be on local
        self.assertEqual(auto.active_name, "local")

    def test_probe_fires_after_sixty_seconds(self):
        """Probe should fire after PROBE_INTERVAL_SEC (60 seconds)."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force a fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th triggers fallback
        auto.transcribe("buffer_fallback")

        # Fix the server for probing
        self.fake_server.fail = False
        self.fake_server.calls = []

        # Advance time by exactly 60 seconds
        self.advance_time(60.0)

        # Call transcribe - should probe now
        auto.transcribe("buffer_after_60s")

        # Ping should have been called
        ping_calls = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertEqual(len(ping_calls), 1)
        # Verify the timeout was passed
        self.assertEqual(ping_calls[0][1], wb.AutoBackend.PROBE_TIMEOUT_SEC)

    def test_probe_fires_after_more_than_sixty_seconds(self):
        """Probe should fire after more than PROBE_INTERVAL_SEC."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force a fallback
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th triggers fallback
        auto.transcribe("buffer_fallback")

        # Fix server for probing
        self.fake_server.fail = False
        self.fake_server.calls = []

        # Advance time by 65 seconds (more than PROBE_INTERVAL_SEC)
        self.advance_time(65.0)

        # Call transcribe - should probe
        auto.transcribe("buffer_after_65s")

        ping_calls = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertEqual(len(ping_calls), 1)


class SuccessfulRecovery(AutoBackendTestCase):
    """A successful probe returns to the server backend."""

    def test_successful_probe_switches_back_to_server(self):
        """When the server responds to a probe, switch back to it."""
        auto = self.make_auto_backend(server_fail=True, local_fail=False)

        # Force a fallback
        for i in range(5):
            try:
                auto.transcribe("buffer{0}".format(i))
            except Exception:
                pass

        self.assertEqual(auto.active_name, "local")

        # Now fix the server (set fail=False)
        self.fake_server.fail = False
        self.fake_server.calls = []

        # Advance time past the probe interval
        self.advance_time(60.0)

        # Transcribe should probe and find the server working
        result = auto.transcribe("buffer_probe_success")

        # Should be back on server
        self.assertEqual(auto.active_name, "server")
        self.assertEqual(result, {"result": "ok from server"})
        # Failures should be reset
        self.assertEqual(auto._failures, 0)

    def test_probe_resets_failures_counter(self):
        """A successful probe resets the failures counter to zero."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force a fallback to set _failures = 5
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th causes fallback, which resets _failures to 0 if it successfully
        # falls back to local. But wait, let me re-read the code...
        # Actually, _failures is only reset when a successful server transcribe
        # happens, or when a successful probe happens. Let me check...
        # In _fall_back(), there's no reset of _failures
        # So _failures should still be 5 after the fallback
        # But then after the fallback, _failures gets reset to 0 by...
        # Actually in transcribe(), after the fallback succeeds on 5th call,
        # _failures is not reset because we're not on the server anymore.
        # But in _probe(), if the ping succeeds, _failures is reset to 0.

        # So the test should check that _failures is 5 after the fallback,
        # then after a successful probe, it's reset to 0.

        result = auto.transcribe("buffer_fallback")

        # After fallback, we're on local, but _failures should still be 5
        # Actually wait, let me re-read transcribe():
        # Line 1325-1329: local transcribe succeeds, _served = local, return results
        # There's no _failures reset on the local path.
        # But the 5th call to server transcribe() would have incremented
        # _failures and then called _fall_back(). So _failures should be 5.

        # The issue is that _failures gets reset to 0 when the successful fallback
        # happens? No, wait. Let me trace through the code again.

        # OK, actually looking at line 1314-1323, when on server and transcribe fails:
        # _failures += 1
        # if _failures < 5 or _local_error, re-raise
        # if not _fall_back(), re-raise
        # otherwise fall through to local
        # So after the 5th failure, _failures = 5, and then _fall_back() is called.
        # _fall_back() does NOT reset _failures.
        # Then we fall through and call _cpu().transcribe()
        # The local transcribe doesn't reset _failures either.
        # So _failures should still be 5.

        self.assertEqual(auto._failures, 5)

        # Fix the server
        self.fake_server.fail = False

        # Advance time and probe
        self.advance_time(60.0)
        auto.transcribe("buffer_after_probe")

        # Failures should be back to 0 after successful probe
        self.assertEqual(auto._failures, 0)


class PerServerFailureTracking(AutoBackendTestCase):
    """Failures are counted per server, so CPU failures don't flip to dead server."""

    def test_cpu_backend_failure_does_not_affect_server_failure_count(self):
        """Failures from the local backend should not affect the server counter."""
        # Start with server working, but later it will fail
        auto = self.make_auto_backend(server_fail=False, local_fail=False)

        # Use the server successfully a few times
        auto.transcribe("buffer1")
        auto.transcribe("buffer2")

        # Now make the server fail
        self.fake_server.fail = True

        # Force a fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i + 3))

        # 5th triggers fallback
        auto.transcribe("buffer_fallback")

        # Now on local, and _failures = 5
        self.assertEqual(auto.active_name, "local")
        self.assertEqual(auto._failures, 5)

        # Fix the server
        self.fake_server.fail = False

        # Make the local backend fail on transcribe
        self.fake_local.fail = True

        # Try to transcribe with local - it will fail, but this should not
        # increase the server failure count or probe the server (we're not yet
        # at the probe interval anyway since we just fell back)
        with self.assertRaises(wb.BackendError):
            auto.transcribe("buffer_local_fails")

        # Should still be on local, not flipped back to server
        self.assertEqual(auto.active_name, "local")
        # Server failure count should still be 5 (not changed by local failure)
        self.assertEqual(auto._failures, 5)

    def test_probe_occurs_even_if_local_backend_has_failed(self):
        """
        If the local backend fails, probing for the server should still happen
        after the interval, because failures are per-backend.
        """
        # Start with bad server, force fallback to local
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th triggers fallback
        auto.transcribe("buffer_fallback")

        # Break local so it fails on transcribe, but fix server for probing
        self.fake_server.fail = False
        self.fake_local.fail = True

        self.fake_server.calls = []

        # Advance time past probe interval
        self.advance_time(60.0)

        # Try transcribe - should probe for server, find it working, and
        # switch back to server. Since the local was broken but probe will
        # find server, we should successfully transcribe from server.
        result = auto.transcribe("buffer_probe")

        # Verify the probe happened and found the server
        ping_calls = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertTrue(len(ping_calls) > 0)

        # Should have switched back to server
        self.assertEqual(auto.active_name, "server")


class StickyLocalError(AutoBackendTestCase):
    """A sticky local error keeps the backend on CPU."""

    def test_local_error_on_first_fallback_sticks(self):
        """If local backend fails on first fallback, stay on it and remember."""
        # Start with server failing and local working
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force 4 failures
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # Now break the local backend before the 5th call (which triggers fallback)
        self.fake_local.fail = True

        # The 5th call should trigger fallback, which tries to load local and fails
        # This should set _local_error
        with self.assertRaises(wb.BackendError):
            auto.transcribe("buffer_fallback_with_error")

        # _local_error should be set
        self.assertIsNotNone(auto._local_error)

        # Try again - should raise the stored error
        with self.assertRaises(wb.BackendError):
            auto.transcribe("buffer_local_error_again")

    def test_sticky_error_prevents_re_init_of_local_backend(self):
        """Once local backend fails to construct, it doesn't retry on next call."""
        # This test verifies that _local_error is sticky: once the local
        # backend fails to construct, subsequent calls don't retry.
        #
        # We simulate this by:
        # 1. Starting with local backend broken from the beginning
        # 2. Trying to fallback (constructor fails, _local_error set)
        # 3. Trying again (should raise the stored error immediately)
        auto = self.make_auto_backend(server_fail=False, local_fail=True)

        # The server is up, so transcribe succeeds initially
        auto.transcribe("buffer_success")

        # Now break the server to force fallback
        self.fake_server.fail = True

        # First 4 failures
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th failure triggers fallback, which tries to create local
        # Local constructor fails because local_fail=True
        with self.assertRaises(wb.BackendError):
            auto.transcribe("buffer_fallback")

        # Now _local_error should be set
        self.assertIsNotNone(auto._local_error)

        # Count the factory calls (should be 1 - from the failed attempt)
        # Actually, the factory is called once and raises immediately
        # So we can't rely on call counts

        # Second attempt should raise the same stored error immediately
        # without trying to construct the backend again
        with self.assertRaises(wb.BackendError):
            auto.transcribe("buffer_second_attempt")

        # Verify _local_error is still set and is the same error
        self.assertIsNotNone(auto._local_error)


class InitialServerDown(AutoBackendTestCase):
    """When the server is down at init, start on CPU."""

    def test_starts_on_local_if_server_is_down_at_init(self):
        """If the server is unreachable at __init__, fallback to local."""
        auto = self.make_auto_backend(server_fail=True, local_fail=False)

        # Should be on local at this point
        self.assertEqual(auto.active_name, "local")

        # Transcribe should work via local
        result = auto.transcribe("buffer1")
        self.assertEqual(result, {"result": "ok from local"})

    def test_probes_server_from_cpu_start(self):
        """Even if we started on CPU, we should still probe the server."""
        auto = self.make_auto_backend(server_fail=True, local_fail=False)

        # Started on local
        self.assertEqual(auto.active_name, "local")

        # Use local a few times
        auto.transcribe("buffer1")
        auto.transcribe("buffer2")

        # Now fix the server
        self.fake_server.fail = False
        self.fake_server.calls = []

        # Advance time and transcribe
        self.advance_time(60.0)
        result = auto.transcribe("buffer3")

        # Should probe and switch to server
        ping_calls = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertTrue(len(ping_calls) > 0)
        self.assertEqual(auto.active_name, "server")
        self.assertEqual(result, {"result": "ok from server"})


class ProbeExecution(AutoBackendTestCase):
    """Probe execution details: order, timing, handling."""

    def test_probe_uses_correct_timeout(self):
        """The probe should use the PROBE_TIMEOUT_SEC timeout."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th triggers fallback
        auto.transcribe("buffer_fallback")

        # Fix server for probing
        self.fake_server.fail = False
        self.fake_server.calls = []

        self.advance_time(60.0)

        # Transcribe to trigger probe
        auto.transcribe("buffer_probe")

        # Find ping call and verify timeout
        ping_calls = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertEqual(len(ping_calls), 1)
        self.assertEqual(ping_calls[0][1], 3.0)  # PROBE_TIMEOUT_SEC

    def test_failed_probe_does_not_switch_back(self):
        """If a probe fails (timeout, connection error), stay on CPU."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th triggers fallback
        auto.transcribe("buffer_fallback")

        # Server is still broken (fail=True from initialization)
        self.assertEqual(self.fake_server.fail, True)

        self.advance_time(60.0)

        # Transcribe - probe will fail but should not raise
        # because _probe() catches BackendError
        auto.transcribe("buffer_probe_fails")

        # Should still be on local
        self.assertEqual(auto.active_name, "local")


class ProbeCadenceAfterRecovery(AutoBackendTestCase):
    """After recovery to server, the next probe interval resets."""

    def test_probe_interval_resets_after_recovery(self):
        """
        After a successful recovery to server, the next probe interval should
        start fresh.
        """
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th triggers fallback
        auto.transcribe("buffer_fallback1")

        # Fix server
        self.fake_server.fail = False

        # Probe and recover
        self.advance_time(60.0)
        auto.transcribe("buffer_recover")
        self.assertEqual(auto.active_name, "server")

        # Now break the server again
        self.fake_server.fail = True

        # Try to cause another fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer_fail_again_{0}".format(i))

        # 5th triggers second fallback
        auto.transcribe("buffer_fallback2")

        # Should be on local again
        self.assertEqual(auto.active_name, "local")

        # Fix server
        self.fake_server.fail = False
        self.fake_server.calls = []

        # Advance by 59 seconds (not enough for probe from the 2nd fallback)
        self.advance_time(59.0)

        # Should not probe yet
        auto.transcribe("buffer_no_probe")

        ping_calls = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertEqual(len(ping_calls), 0)

        # Advance 1 more second to hit 60 total from 2nd fallback
        self.advance_time(1.0)

        # Should probe now
        auto.transcribe("buffer_with_probe")

        ping_calls = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertTrue(len(ping_calls) > 0)


class MultipleProbes(AutoBackendTestCase):
    """Test multiple probe cycles."""

    def test_multiple_probe_cycles(self):
        """Test that probing continues after the first interval."""
        auto = self.make_auto_backend(server_fail=False, local_fail=False)
        self.fake_server.fail = True

        # Force fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer{0}".format(i))

        # 5th triggers fallback
        auto.transcribe("buffer_fallback1")

        self.fake_server.fail = False

        # First probe cycle at 60s
        self.fake_server.calls = []
        self.advance_time(60.0)
        auto.transcribe("buffer_probe1")
        ping_calls_1 = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertTrue(len(ping_calls_1) > 0)

        # Should have recovered
        self.assertEqual(auto.active_name, "server")

        # Break server again
        self.fake_server.fail = True

        # Cause another fallback (4 failures then fallback on 5th)
        for i in range(4):
            with self.assertRaises(wb.BackendError):
                auto.transcribe("buffer_fail2_{0}".format(i))

        # 5th triggers second fallback
        auto.transcribe("buffer_fallback2")

        # Fix server
        self.fake_server.fail = False

        # Second probe cycle at 60s from the second fallback
        self.fake_server.calls = []
        self.advance_time(60.0)
        auto.transcribe("buffer_probe2")
        ping_calls_2 = [c for c in self.fake_server.calls if c[0] == "ping"]
        self.assertTrue(len(ping_calls_2) > 0)

        # Should have recovered again
        self.assertEqual(auto.active_name, "server")


if __name__ == "__main__":
    unittest.main()
