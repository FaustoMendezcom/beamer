"""Secure desktop input (docs/secure-desktop.md): the pipe's messages, who may use it, the
helper's handling, the service's supervision, and the injector passing refused input on."""

import ctypes
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_injector  # noqa: E402
import secure_helper  # noqa: E402
import secure_input  # noqa: E402
import secure_service  # noqa: E402

SIZE = ctypes.sizeof(input_injector.INPUT)
ALLOWED = r"C:\Users\fm\AppData\Local\Beamer\Beamer.exe"


class FramingTests(unittest.TestCase):
    def test_messages_round_trip_and_hold_their_limits(self):
        data = secure_input.frame(secure_input.INPUTS, secure_input.inputs_payload(b"x" * SIZE * 2, SIZE))
        kind, length = secure_input.read_header(data[:secure_input.HEADER.size])
        self.assertEqual(kind, secure_input.INPUTS)
        self.assertEqual(secure_input.read_inputs(data[secure_input.HEADER.size:], SIZE), b"x" * SIZE * 2)
        self.assertEqual(secure_input.read_header(secure_input.frame(secure_input.SAS)), (secure_input.SAS, 0))

    def test_anything_else_is_refused(self):
        for header in (b"XXXX\x01\x00\x00\x00\x00", secure_input.HEADER.pack(b"BSI1", 9, 0),
                       secure_input.HEADER.pack(b"BSI1", 1, secure_input.MAX_PAYLOAD + 1),
                       secure_input.HEADER.pack(b"BSI1", secure_input.SAS, 4), b"BSI1"):
            with self.subTest(header=header), self.assertRaises(secure_input.FrameError):
                secure_input.read_header(header)
        for payload in (b"", secure_input.inputs_payload(b"x" * SIZE, SIZE + 1),
                        secure_input.inputs_payload(b"x" * (SIZE + 3), SIZE), secure_input.inputs_payload(b"", SIZE)):
            with self.subTest(payload=payload), self.assertRaises(secure_input.FrameError):
                secure_input.read_inputs(payload, SIZE)
        with self.assertRaises(secure_input.FrameError):
            secure_input.frame(7)


class WhoMayUseItTests(unittest.TestCase):
    def info(self, **fields):
        return {"session": 1, "path": ALLOWED, "elevated": True, **fields}

    def test_only_the_allowed_elevated_beamer_of_this_session(self):
        self.assertEqual(secure_input.client_allowed(self.info(), ALLOWED.upper(), 1), (True, ""))
        for info, allowed in ((self.info(session=2), ALLOWED), (self.info(path=r"C:\evil\Beamer.exe"), ALLOWED),
                              (self.info(elevated=False), ALLOWED), (self.info(), None), (self.info(path=None), ALLOWED)):
            with self.subTest(info=info, allowed=allowed):
                self.assertFalse(secure_input.client_allowed(info, allowed, 1)[0])

    def test_a_normal_beamer_is_told_why(self):
        self.assertIn("administrator", secure_input.client_allowed(self.info(elevated=False), ALLOWED, 1)[1])


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.sent, self.sas = [], []
        self.helper = secure_helper.Helper(lambda records, count: self.sent.append((records, count)) or count,
                                           lambda: self.sas.append(1) or True, lambda: "Winlogon", SIZE)

    def status(self, reply):
        return reply[0], reply[2:2 + reply[1]].decode()

    def test_inputs_are_sent_whole_and_ctrl_alt_del_and_ping_answered(self):
        reply = self.helper.handle(secure_input.INPUTS, secure_input.inputs_payload(b"r" * SIZE * 3, SIZE))
        self.assertEqual(self.status(reply), (secure_input.OK, ""))
        self.assertEqual(self.sent, [(b"r" * SIZE * 3, 3)])
        self.assertEqual(self.status(self.helper.handle(secure_input.SAS, b"")), (secure_input.OK, ""))
        self.assertEqual(self.sas, [1])
        self.assertEqual(self.status(self.helper.handle(secure_input.PING, b"")), (secure_input.OK, "Winlogon"))

    def test_malformed_input_is_answered_not_sent(self):
        reply = self.helper.handle(secure_input.INPUTS, secure_input.inputs_payload(b"r" * 5, SIZE))
        self.assertEqual(reply[0], secure_input.FAILED)
        self.assertEqual(self.sent, [])


class ClientTests(unittest.TestCase):
    class Pipe:
        def __init__(self, replies):
            self.replies, self.written, self.closed = bytearray(b"".join(replies)), [], False

        def write(self, data):
            self.written.append(data)

        def read(self, count):
            data, self.replies[:] = bytes(self.replies[:count]), self.replies[count:]
            return data

        def close(self):
            self.closed = True

    def test_no_helper_is_asked_again_only_after_a_while(self):
        opened, now = [], [0.0]
        client = secure_input.Client(1, opener=lambda name: opened.append(name), clock=lambda: now[0])
        self.assertFalse(client.sas())
        self.assertFalse(client.sas())
        now[0] = secure_input.RETRY_SECONDS + 1
        self.assertFalse(client.sas())
        self.assertEqual(opened, [secure_input.pipe_name(1)] * 2)

    def test_a_refusal_closes_and_is_remembered(self):
        pipe = self.Pipe([secure_input.answer(secure_input.REFUSED, "Beamer is not running as administrator")])
        client = secure_input.Client(1, opener=lambda name: pipe, clock=lambda: 0.0)
        self.assertFalse(client.send_inputs(b"r" * SIZE, SIZE))
        self.assertTrue(pipe.closed)
        self.assertIn("administrator", client.refused)

    def test_accepted_input_keeps_the_pipe(self):
        pipe = self.Pipe([secure_input.answer(secure_input.OK)] * 2)
        client = secure_input.Client(1, opener=lambda name: pipe, clock=lambda: 0.0)
        self.assertTrue(client.send_inputs(b"r" * SIZE, SIZE))
        self.assertTrue(client.sas())
        self.assertFalse(pipe.closed)


class SupervisorTests(unittest.TestCase):
    def test_one_helper_follows_the_console_session(self):
        sessions, launched, stopped, alive = [1], [], [], {}

        def launch(session):
            launched.append(session)
            alive[session] = True
            return session

        supervisor = secure_service.Supervisor(lambda: sessions[0], launch, lambda process: alive[process],
                                               stopped.append)
        supervisor.check()
        supervisor.check()
        sessions[0] = 2
        supervisor.check()
        alive[2] = False
        supervisor.check()
        sessions[0] = secure_service.NO_SESSION
        supervisor.check()
        self.assertEqual(launched, [1, 2, 2])
        self.assertEqual(stopped, [1, 2, 2])

    def test_a_failed_launch_is_tried_again_next_time(self):
        calls = []

        def launch(session):
            calls.append(session)
            if len(calls) == 1:
                raise OSError("no session yet")
            return "process"

        supervisor = secure_service.Supervisor(lambda: 3, launch, lambda process: True, lambda process: None)
        supervisor.check()
        supervisor.check()
        self.assertEqual(supervisor.process, "process")


class InjectorRoutingTests(unittest.TestCase):
    def test_refused_input_on_the_secure_desktop_goes_to_the_helper(self):
        sent = []
        helper = mock.Mock(send_inputs=lambda records, size: sent.append((len(records), size)) or True)
        with mock.patch.object(input_injector, "_secure", return_value=helper), \
                mock.patch("desktop_win._on_default_desktop", return_value=False):
            array = (input_injector.INPUT * 2)()
            self.assertTrue(input_injector._to_secure_desktop(array, 2))
        self.assertEqual(sent, [(SIZE * 2, SIZE)])

    def test_on_the_default_desktop_a_refusal_stays_a_refusal(self):
        helper = mock.Mock()
        with mock.patch.object(input_injector, "_secure", return_value=helper), \
                mock.patch("desktop_win._on_default_desktop", return_value=True):
            self.assertFalse(input_injector._to_secure_desktop((input_injector.INPUT * 1)(), 1))
        helper.send_inputs.assert_not_called()

    def test_ctrl_alt_delete_is_sent_as_the_secure_attention_sequence(self):
        helper = mock.Mock(sas=mock.Mock(return_value=True))
        with mock.patch.object(input_injector, "_secure", return_value=helper), \
                mock.patch.object(input_injector, "_send_input") as typed, \
                mock.patch.object(input_injector, "_mods_down", {"ctrl", "alt"}):
            input_injector.inject_key("delete", True)
        helper.sas.assert_called_once()
        typed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
