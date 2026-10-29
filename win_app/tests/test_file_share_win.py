"""file_share_win.FileShare with a fake clipboard and fetch: offering what Explorer copied,
fetching on arrival, the paste window, and never offering back what it pasted itself."""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import file_share_win  # noqa: E402
from core import file_transfer, protocol  # noqa: E402

PEER = bytes(range(40, 56))
OWN = bytes(range(1, 17))


class FakeClipboard:
    def __init__(self):
        self.seq, self.files, self.marked, self.cleared = 1, None, [], []

    def sequence(self):
        return self.seq

    def read_files(self):
        return self.files

    def copy(self, files):
        self.seq += 1
        self.files = files

    def write_files(self, paths):
        self.seq += 1
        self.files = list(paths)
        return self.seq

    def clear_if(self, expected):
        if self.seq != expected:
            return False
        self.cleared.append(expected)
        self.seq += 1
        self.files = None
        return True

    def mark_from_another_computer(self, path):
        self.marked.append(path)


class FileShareTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.staging = os.path.join(self.root, "incoming")
        os.makedirs(self.staging)
        self.clipboard = FakeClipboard()
        self.sent, self.notes, self.timers, self.fetched = [], [], [], []
        self.share = file_share_win.FileShare(
            self.clipboard, own_id=OWN, staging=self.staging,
            send=lambda peer, message: self.sent.append((peer, message)), targets=lambda: [PEER],
            peer=lambda ident: ("10.0.0.2", 24820, "token", "PC2"), notify=self.notes.append, fetch=self.fake_fetch)
        self.share._later = lambda seconds, action: self.timers.append((seconds, action))
        self.share._spawn = lambda target, *args: target(*args)

    def fake_fetch(self, host, port, token, own_id, offer, destination, **kwargs):
        self.fetched.append((host, offer, destination))
        path = os.path.join(destination, "report.pdf")
        with open(path, "wb") as handle:
            handle.write(b"pdf")
        kwargs["written"](path)
        return [path]

    def file(self, name, data=b"x"):
        path = os.path.join(self.root, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_sending_offers_to_that_peer_alone_and_lands_in_its_downloads_opened(self):
        received = os.path.join(self.root, "Downloads", "Beamer")
        opened = []
        self.share._received = lambda: received
        self.share._opened = opened.append
        self.assertEqual(self.share.offer_paths([self.file("a.txt")], PEER, "send"), "a.txt")
        (peer, message), = self.sent
        self.assertEqual((peer, protocol.read_files(message)[5]), (PEER, "send"))
        self.share.on_message(PEER, ("offer", bytes(range(70, 86)), 1, 3, "report.pdf", "send"))
        self.assertEqual(os.listdir(received), ["report.pdf"])
        self.assertEqual(opened, [received])
        self.assertIsNone(self.clipboard.files)
        self.assertEqual(os.listdir(self.staging), [])

    def test_a_dropped_set_waits_for_the_landing_and_takes_a_free_name_there(self):
        armed = []
        self.share._dropped = armed.append
        self.share.on_message(PEER, ("offer", bytes(range(70, 86)), 1, 3, "report.pdf", "drop"))
        self.assertEqual(self.fetched, [])
        folder = os.path.join(self.root, "Target")
        os.makedirs(folder)
        self.file("Target/report.pdf")
        placed = self.share.deliver(armed[0], folder)
        self.assertEqual([os.path.basename(path) for path in placed], ["report (2).pdf"])
        self.assertIn("copied to", self.notes[-1])

    def test_a_drop_with_no_room_is_refused_before_fetching(self):
        incoming = {"peer": PEER, "offer": bytes(range(70, 86)), "count": 1, "total": 10 ** 18, "label": "huge",
                    "mode": "drop", "at": time.time()}
        self.assertIsNone(self.share.deliver(incoming, self.root))
        self.assertEqual(self.fetched, [])
        self.assertIn("Not enough room", self.notes[-1])

    def test_copying_files_offers_their_names_and_sizes_to_each_peer(self):
        self.clipboard.copy([self.file("a.txt", b"12345")])
        self.share.check()
        (peer, message), = self.sent
        action, _offer, count, total, label, _mode = protocol.read_files(message)
        self.assertEqual((peer, action, count, total, label), (PEER, "offer", 1, 5, "a.txt"))

    def test_copying_something_else_withdraws_the_offer(self):
        self.clipboard.copy([self.file("a.txt")])
        self.share.check()
        self.clipboard.copy(None)
        self.share.check()
        self.assertEqual([protocol.read_files(message)[0] for _peer, message in self.sent], ["offer", "cancel"])

    def test_the_offer_is_served_and_an_unknown_one_refused(self):
        self.clipboard.copy([self.file("a.txt")])
        self.share.check()
        offer = protocol.read_files(self.sent[0][1])[1]
        served, refused = [], []
        file_share_win.file_transfer.serve = lambda sock, session, items, sources, stop: served.append(items)
        file_share_win.file_transfer.refuse = lambda sock, session, reason: refused.append(reason)
        self.addCleanup(setattr, file_share_win.file_transfer, "serve", file_transfer.serve)
        self.addCleanup(setattr, file_share_win.file_transfer, "refuse", file_transfer.refuse)
        request = type("Request", (), {"offer": offer, "sock": None, "session": None, "entry": {"name": "PC2"}})
        self.share.serve(request)
        request.offer = bytes(range(9, 25))
        self.share.serve(request)
        self.assertEqual(len(served), 1)
        self.assertEqual(len(refused), 1)

    def test_arriving_fetches_marks_and_puts_the_files_on_the_clipboard_for_the_paste_window(self):
        offer = bytes(range(60, 76))
        self.share.on_message(PEER, ("offer", offer, 1, 3, "report.pdf", "paste"))
        self.share.activate()
        self.assertEqual(len(self.fetched), 1)
        self.assertTrue(self.clipboard.files[0].endswith("report.pdf"))
        self.assertEqual(self.clipboard.marked, self.clipboard.files)
        self.assertIn("paste within 2 min", self.notes[-1])
        (seconds, expire), = self.timers
        self.assertEqual(seconds, 120)
        expire()
        self.assertIsNone(self.clipboard.files)
        self.assertEqual(self.timers[-1][0], file_share_win.DELETE_AFTER_SECONDS)

    def test_an_expiry_leaves_anything_copied_since(self):
        self.share.on_message(PEER, ("offer", bytes(range(60, 76)), 1, 3, "report.pdf", "paste"))
        self.share.activate()
        self.clipboard.copy(["C:/other.txt"])
        self.timers[0][1]()
        self.assertEqual(self.clipboard.files, ["C:/other.txt"])
        self.assertEqual(self.clipboard.cleared, [])

    def test_what_it_pasted_itself_is_never_offered_back(self):
        self.share.on_message(PEER, ("offer", bytes(range(60, 76)), 1, 3, "report.pdf", "paste"))
        self.share.activate()
        self.share.check()
        self.assertEqual(self.sent, [])

    def test_a_cancelled_stale_or_too_large_offer_is_not_fetched(self):
        self.share.on_message(PEER, ("offer", bytes(range(60, 76)), 1, 3, "x", "paste"))
        self.share.on_message(PEER, ("cancel", bytes(range(60, 76)), 0, 0, "", ""))
        self.share.activate()
        self.share.on_message(PEER, ("offer", bytes(range(61, 77)), 1, 3, "x", "paste"))
        self.share._incoming["at"] -= file_share_win.OFFER_SECONDS + 1
        self.share.activate()
        self.share.on_message(PEER, ("offer", bytes(range(62, 78)), 1, file_transfer.PASTE_MAX_BYTES + 1, "big", "paste"))
        self.share.activate()
        self.assertEqual(self.fetched, [])
        self.assertIn("too large", self.notes[-1])

    def test_old_staging_folders_are_purged_at_start(self):
        old = os.path.join(self.staging, "old")
        os.makedirs(old)
        past = time.time() - file_share_win.PURGE_OLDER_SECONDS - 10
        os.utime(old, (past, past))
        os.makedirs(os.path.join(self.staging, "new"))
        self.share.purge()
        self.assertEqual(os.listdir(self.staging), ["new"])


class QaFileShareTests(FileShareTests):
    def test_a_delivery_is_held_to_the_size_it_claimed(self):
        limits = []

        def fetch(host, port, token, own_id, offer, destination, **kwargs):
            limits.append(kwargs["max_bytes"])
            return self.fake_fetch(host, port, token, own_id, offer, destination, **kwargs)

        self.share._fetch = fetch
        incoming = {"peer": PEER, "offer": bytes(range(70, 86)), "count": 1, "total": 5, "label": "a",
                    "mode": "send", "at": time.time()}
        self.share.deliver(incoming, os.path.join(self.root, "in"))
        self.assertEqual(limits, [5])

    def test_no_more_than_a_few_transfers_at_once(self):
        for _ in range(file_share_win.FETCHES_AT_ONCE):
            self.share._fetching.acquire()
        incoming = {"peer": PEER, "offer": bytes(range(70, 86)), "count": 1, "total": 5, "label": "a",
                    "mode": "send", "at": time.time()}
        self.assertIsNone(self.share._receive(incoming, 10))
        self.assertEqual(self.fetched, [])

    def test_an_elevated_sender_offers_only_what_its_user_may_read(self):
        self.share._may_read = lambda path: not path.endswith("secret.txt")
        self.clipboard.copy([self.file("secret.txt")])
        self.share.check()
        self.assertEqual(self.sent, [])

if __name__ == "__main__":
    unittest.main()
