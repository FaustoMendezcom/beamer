"""core/file_transfer.py: the manifest, the names a peer may write, and files fetched over a data
connection that a responder hands to its `files_server`, never taking a link."""

import os
import shutil
import tempfile
import threading
import unittest

from core import file_transfer, protocol
from core.tests.responder_harness import B, CAPS, Machine, TOKENS, entry, wait_for

OFFER = bytes(range(200, 216))


class PathTests(unittest.TestCase):
    def test_ordinary_names_and_nested_folders_pass(self):
        self.assertEqual(file_transfer.safe_path("Photos/2026/IMG 01.jpg"), ["Photos", "2026", "IMG 01.jpg"])

    def test_anything_that_could_leave_the_folder_is_refused(self):
        for path in ("../x", "a/../b", "/etc/passwd", "C:/Windows", "C:x", "a\\b", "a//b", "", ".",
                     "file.txt:stream", "CON", "nul.txt", "com1.log", "trailing.", "trailing ",
                     "a" * 256, "x\x01y", "/".join(["d"] * 40)):
            with self.subTest(path=path):
                self.assertIsNone(file_transfer.safe_path(path))

    def test_read_manifest_wants_each_folder_before_its_files_and_no_repeats(self):
        good = [{"path": "f", "size": 0, "mtime": 1, "dir": True},
                {"path": "f/a.txt", "size": 3, "mtime": 1, "dir": False}]
        self.assertEqual(file_transfer.read_manifest(good)[1], 3)
        for items in ([good[1]], good + [good[1]], [{**good[0], "size": 5}], [{**good[1], "path": "x", "size": -1}],
                      [{**good[1], "path": "x", "size": 1.5}], [], "x"):
            with self.subTest(items=items), self.assertRaises(file_transfer.TransferError):
                file_transfer.read_manifest(items)

    def test_read_manifest_holds_the_size_limit(self):
        with self.assertRaises(file_transfer.TooLarge):
            file_transfer.read_manifest([{"path": "big", "size": 11, "mtime": 1, "dir": False}], max_bytes=10)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, relative, data=b""):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_files_and_folders_become_relative_entries_folders_first(self):
        one = self.write("one.txt", b"abc")
        self.write("dir/inner/two.bin", b"12345")
        items, sources, total = file_transfer.build_manifest([one, os.path.join(self.root, "dir")])
        self.assertEqual([(item["path"], item["dir"], item["size"]) for item in items],
                         [("one.txt", False, 3), ("dir", True, 0), ("dir/inner", True, 0),
                          ("dir/inner/two.bin", False, 5)])
        self.assertEqual(total, 8)
        self.assertEqual(sources[1], None)
        self.assertTrue(sources[3].endswith("two.bin"))

    def test_two_copied_items_with_one_name_are_told_apart(self):
        first = self.write("a/same.txt", b"1")
        second = self.write("b/same.txt", b"2")
        items, _sources, _total = file_transfer.build_manifest([first, second])
        self.assertEqual([item["path"] for item in items], ["same.txt", "same (2).txt"])

    def test_too_much_is_refused_before_anything_is_sent(self):
        path = self.write("big.bin", b"x" * 100)
        with self.assertRaises(file_transfer.TooLarge):
            file_transfer.build_manifest([path], max_bytes=50)


class FetchTests(unittest.TestCase):
    """A real responder on loopback: the fetch arrives as a `_DataRequest`, takes no link, and the
    files land with their sizes and times."""

    def setUp(self):
        self.source = tempfile.mkdtemp()
        self.target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.source, True)
        self.addCleanup(shutil.rmtree, self.target, True)
        os.makedirs(os.path.join(self.source, "set", "deep"))
        self.payload = os.urandom(3 * file_transfer.CHUNK_BYTES + 17)
        with open(os.path.join(self.source, "set", "deep", "big.bin"), "wb") as handle:
            handle.write(self.payload)
        with open(os.path.join(self.source, "note.txt"), "wb") as handle:
            handle.write(b"hello")
        os.utime(os.path.join(self.source, "note.txt"), (1_700_000_000, 1_700_000_000))
        self.items, self.sources, _ = file_transfer.build_manifest(
            [os.path.join(self.source, "note.txt"), os.path.join(self.source, "set")])
        self.served = []

        def server(request):
            self.served.append(request.offer)
            if request.offer != OFFER:
                file_transfer.refuse(request.sock, request.session, "no such offer")
                return
            file_transfer.serve(request.sock, request.session, self.items, self.sources)

        self.machine = Machine([entry(B, "Peer")], caps=tuple(CAPS) + ("files",)).start()
        self.addCleanup(self.machine.stop)
        self.machine.responder.files_server = server

    def fetch(self, offer=OFFER, **kwargs):
        return file_transfer.fetch("127.0.0.1", self.machine.port, TOKENS[B], B, offer, self.target, **kwargs)

    def test_a_fetch_writes_every_file_and_folder_and_takes_no_link(self):
        marked = []
        tops = self.fetch(written=marked.append)
        self.assertEqual(sorted(os.path.basename(path) for path in tops), ["note.txt", "set"])
        with open(os.path.join(self.target, "set", "deep", "big.bin"), "rb") as handle:
            self.assertEqual(handle.read(), self.payload)
        self.assertEqual(int(os.path.getmtime(os.path.join(self.target, "note.txt"))), 1_700_000_000)
        self.assertEqual(len(marked), 2)
        self.assertEqual(self.machine.responder.links(), set() if isinstance(self.machine.responder.links(), set) else [])
        self.assertIsNone(self.machine.responder.owner)

    def test_an_unknown_offer_is_refused_in_words(self):
        with self.assertRaises(file_transfer.TransferError) as caught:
            self.fetch(offer=bytes(range(1, 17)))
        self.assertIn("no such offer", str(caught.exception))
        self.assertEqual(os.listdir(self.target), [])

    def test_a_fetch_claiming_another_machines_id_is_closed(self):
        with self.assertRaises((file_transfer.TransferError, OSError, protocol.ProtocolError, protocol.ConnectionClosed)):
            file_transfer.fetch("127.0.0.1", self.machine.port, TOKENS[B], bytes(range(16)), OFFER, self.target)
        self.assertEqual(self.served, [])

    def test_cancelling_stops_partway(self):
        cancelled = threading.Event()

        def progress(done, total):
            if done >= file_transfer.CHUNK_BYTES:
                cancelled.set()

        with self.assertRaises(file_transfer.TransferError):
            self.fetch(cancelled=cancelled, progress=progress)


class FilesMessageTests(unittest.TestCase):
    def test_offer_and_cancel_round_trip(self):
        message = protocol.files_msg("offer", OFFER, count=3, total=1234, label="3 files")
        self.assertEqual(protocol.read_files(message), ("offer", OFFER, 3, 1234, "3 files", "paste"))
        self.assertEqual(protocol.read_files(protocol.files_msg("cancel", OFFER)), ("cancel", OFFER, 0, 0, "", ""))
        for mode in ("drop", "send"):
            self.assertEqual(protocol.read_files(protocol.files_msg("offer", OFFER, 1, 1, "a", mode))[5], mode)
        legacy = {"type": "files", "data": {"action": "offer", "id": protocol.id_text(OFFER), "count": 1, "total": 1,
                                            "label": "a"}}
        self.assertEqual(protocol.read_files(legacy)[5], "paste")

    def test_malformed_offers_are_refused(self):
        for data in ({"action": "offer", "id": "x", "count": 1, "total": 1, "label": ""},
                     {"action": "offer", "id": protocol.id_text(OFFER), "count": 0, "total": 1, "label": ""},
                     {"action": "offer", "id": protocol.id_text(OFFER), "count": 1, "total": -1, "label": ""},
                     {"action": "send", "id": protocol.id_text(OFFER)},
                     {"action": "offer", "id": protocol.id_text(OFFER), "count": 1, "total": 1, "label": "", "mode": "x"}):
            with self.subTest(data=data):
                self.assertIsNone(protocol.read_files({"type": "files", "data": data}))

    def test_fetch_request_round_trips(self):
        self.assertEqual(protocol.read_files_fetch(protocol.files_fetch_msg(OFFER, B)), (OFFER, B))


class QaHardeningTests(unittest.TestCase):
    """From the adversarial review: names Windows still opens as devices, names that read
    backwards, file times no file system takes, and an elevated sender reading for its user."""

    def test_more_device_names_and_reversed_names_are_refused(self):
        for name in ("COM\u00b9", "LPT\u00b2.txt", "CONIN$", "conout$.log", "con .txt", "a\u202egpj.exe",
                     "b\u2066x", "c\u200fd"):
            with self.subTest(name=name):
                self.assertFalse(file_transfer.safe_part(name))
        for name in ("com10.txt", "Report (2).pdf", "console.txt", "\u00e9t\u00e9.jpg"):
            with self.subTest(name=name):
                self.assertTrue(file_transfer.safe_part(name))

    def test_a_file_time_out_of_range_refuses_the_manifest(self):
        for mtime in (10 ** 30, 10 ** 12, -1):
            with self.subTest(mtime=mtime), self.assertRaises(file_transfer.TransferError):
                file_transfer.read_manifest([{"path": "a", "size": 1, "mtime": mtime, "dir": False}])

    def test_what_the_sender_may_not_read_is_left_out(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for name in ("open.txt", "secret.txt"):
            with open(os.path.join(root, name), "wb") as handle:
                handle.write(b"x")
        items, _sources, _total = file_transfer.build_manifest(
            [os.path.join(root, "open.txt"), os.path.join(root, "secret.txt")],
            readable=lambda path: not path.endswith("secret.txt"))
        self.assertEqual([item["path"] for item in items], ["open.txt"])

class NotInUseFetchTests(FetchTests):
    def setUp(self):
        super().setUp()
        self.machine.stop()
        self.machine = Machine([entry(B, "Peer", in_use=False)], caps=tuple(CAPS) + ("files",)).start()
        self.addCleanup(self.machine.stop)
        self.machine.responder.files_server = lambda request: self.served.append(request.offer)

    def test_a_pairing_not_in_use_fetches_nothing(self):
        with self.assertRaises((file_transfer.TransferError, OSError, protocol.ProtocolError, protocol.ConnectionClosed)):
            self.fetch()
        self.assertEqual(self.served, [])

    test_a_fetch_writes_every_file_and_folder_and_takes_no_link = None
    test_an_unknown_offer_is_refused_in_words = None
    test_a_fetch_claiming_another_machines_id_is_closed = None
    test_cancelling_stops_partway = None

if __name__ == "__main__":
    unittest.main()
