"""Which arrays make up which volume, and what /sys says about their disks.

SHR on drives of different sizes is several md arrays joined by LVM into one
volume, so mdstat alone shows "two raid5 arrays, one with 7 of the 8 disks"
on a NAS with one volume. These build a /sys the way DSM lays it out and check
the doctor reads it back as the one volume it is.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import raid  # noqa: E402
from app.briefing import storage_facts  # noqa: E402
from app.detector import Detector  # noqa: E402

TB = 1000 ** 4
BIG, SMALL = int(5.5 * TB), int(2.7 * TB)

# Seven big drives and one small one, drive 6: md2 is the layer every drive
# has room for, md3 the rest of the seven big ones.
MDSTAT = """Personalities : [raid1] [raid6] [raid5] [raid4]
md3 : active raid5 sata1p6[0] sata8p6[6] sata7p6[5] sata5p6[4] sata4p6[3] sata3p6[2] sata2p6[1]
      17578127616 blocks super 1.2 level 5, 64k chunk, algorithm 2 [7/7] [UUUUUUU]

md2 : active raid5 sata1p5[0] sata8p5[7] sata7p5[6] sata6p5[5] sata5p5[4] sata4p5[3] sata3p5[2] sata2p5[1]
      18433744896 blocks super 1.2 level 5, 64k chunk, algorithm 2 [8/8] [UUUUUUUU]

md1 : active raid1 sata1p2[0] sata8p2[7] sata7p2[6] sata6p2[5] sata5p2[4] sata4p2[3] sata3p2[2] sata2p2[1]
      2097088 blocks [8/8] [UUUUUUUU]

md0 : active raid1 sata1p1[0] sata8p1[7] sata7p1[6] sata6p1[5] sata5p1[4] sata4p1[3] sata3p1[2] sata2p1[1]
      8388544 blocks [8/8] [UUUUUUUU]

unused devices: <none>
"""


class FakeSys:
    """A /sys/block and /proc/self/mountinfo, written into a temp dir."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp()
        self.block = os.path.join(self.root, "block")
        self.mountinfo = os.path.join(self.root, "mountinfo")

    def write(self, *parts: str, text: str) -> None:
        path = os.path.join(self.block, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def mkdir(self, *parts: str) -> None:
        os.makedirs(os.path.join(self.block, *parts), exist_ok=True)

    def disk(self, name: str, size: int, model: str, partitions: list[str]) -> None:
        self.write(name, "size", text=str(size // 512))
        self.write(name, "device", "model", text=model + "   ")  # padded, as SATA models are
        for part in partitions:
            self.mkdir(name, part)

    def md(self, name: str, members: list[str], errors: dict[str, int] | None = None,
           mismatches: int = 0, last: str = "check") -> None:
        self.write(name, "md", "mismatch_cnt", text=str(mismatches))
        self.write(name, "md", "last_sync_action", text=last)
        for member in members:
            self.write(name, "md", f"dev-{member}", "errors", text=str((errors or {}).get(member, 0)))
        self.mkdir(name, "holders")

    def dm(self, device: str, name: str, dev: str, slaves: list[str],
           size: int = 35 * TB) -> None:
        self.write(device, "dm", "name", text=name)
        self.write(device, "dev", text=dev)
        self.write(device, "size", text=str(size // 512))
        self.mkdir(device, "holders")
        for slave in slaves:
            self.mkdir(device, "slaves", slave)
            self.mkdir(slave, "holders", device)

    def filesystem(self, *parts: str) -> None:
        """/sys/fs/btrfs/<uuid>/devices/dm-N, or /sys/fs/ext4/dm-N."""
        os.makedirs(os.path.join(self.root, "fs", *parts), exist_ok=True)

    def mounts(self, *lines: str) -> None:
        with open(self.mountinfo, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def shr_nas(errors: dict[str, int] | None = None, mismatches: int = 0,
            fstype: str = "ext4") -> FakeSys:
    fake = FakeSys()
    for n in range(1, 9):
        parts = [f"sata{n}p1", f"sata{n}p2", f"sata{n}p5"] + ([] if n == 6 else [f"sata{n}p6"])
        fake.disk(f"sata{n}", SMALL if n == 6 else BIG,
                  "HAT5300-3T" if n == 6 else "HAT5300-6T", parts)
    bays = [n for n in range(1, 9)]
    fake.md("md0", [f"sata{n}p1" for n in bays], last="resync")
    fake.md("md1", [f"sata{n}p2" for n in bays], mismatches=128, last="check")
    fake.md("md2", [f"sata{n}p5" for n in bays], errors=errors)
    fake.md("md3", [f"sata{n}p6" for n in bays if n != 6], errors=errors, mismatches=mismatches)
    fake.dm("dm-0", "vg1-volume_1", "253:0", ["md2", "md3"])
    fake.dm("dm-1", "vg1-syno_vg_reserved_area", "253:2", ["md2"])
    fake.dm("dm-2", "cachedev_0", "253:1", ["dm-0"])
    device = "0:45" if fstype == "btrfs" else "253:1"
    fake.mounts(
        "600 500 0:52 / / ro,relatime - overlay overlay rw",
        "601 600 0:53 / /proc rw - proc proc rw",
        f"650 600 {device} /@docker/container-doctor /data rw,relatime - {fstype} "
        "/dev/mapper/cachedev_0 rw",
    )
    return fake


def inspect(fake: FakeSys, paths=("/data",)):
    arrays = raid.parse(MDSTAT)
    pools = raid.inspect(arrays, fake.root, fake.mountinfo, list(paths))
    return {a.name: a for a in arrays}, pools


class SizesFromMdstat(unittest.TestCase):
    def test_every_array_has_its_size(self):
        arrays = {a.name: a for a in raid.parse(MDSTAT)}
        self.assertEqual(arrays["md2"].size_bytes, 18433744896 * 1024)
        self.assertEqual(arrays["md0"].size_bytes, 8388544 * 1024)

    def test_the_size_line_still_reads_the_status(self):
        md3 = {a.name: a for a in raid.parse(MDSTAT)}["md3"]
        self.assertEqual((md3.slots, md3.active), (7, 7))


class OneVolumeOnTwoArrays(unittest.TestCase):
    def test_md2_and_md3_are_one_pool_with_one_volume(self):
        _, pools = inspect(shr_nas())
        self.assertEqual(len(pools), 1)
        [pool] = pools
        self.assertEqual(pool["arrays"], ["md2", "md3"])
        self.assertEqual(pool["pool"], "Storage Pool 1")
        self.assertEqual([v["name"] for v in pool["volumes"]], ["Volume 1"])

    def test_the_watched_path_is_placed_on_its_volume(self):
        _, [pool] = inspect(shr_nas())
        self.assertEqual(pool["volumes"][0]["paths"], ["/data"])

    def test_btrfs_is_found_by_its_source_not_its_anonymous_device(self):
        _, [pool] = inspect(shr_nas(fstype="btrfs"))
        self.assertEqual(pool["volumes"][0]["paths"], ["/data"])

    def test_dsms_reserved_area_is_not_a_volume(self):
        _, [pool] = inspect(shr_nas())
        self.assertNotIn("vg1-syno_vg_reserved_area", [v["device"] for v in pool["volumes"]])

    def test_the_system_arrays_are_in_no_pool(self):
        _, [pool] = inspect(shr_nas())
        self.assertNotIn("md0", pool["arrays"])

    def test_the_pool_size_adds_up_its_layers(self):
        arrays, [pool] = inspect(shr_nas())
        self.assertEqual(pool["size_bytes"], arrays["md2"].size_bytes + arrays["md3"].size_bytes)

    def test_two_separate_volumes_are_two_pools(self):
        fake = FakeSys()
        for n in (1, 2, 3, 4):
            fake.disk(f"sata{n}", BIG, "X", [f"sata{n}p5"])
        fake.md("md2", ["sata1p5", "sata2p5"])
        fake.md("md3", ["sata3p5", "sata4p5"])
        fake.dm("dm-0", "vg1-volume_1", "253:0", ["md2"])
        fake.dm("dm-1", "vg2-volume_2", "253:1", ["md3"])
        text = """md3 : active raid1 sata3p5[0] sata4p5[1]
      100 blocks super 1.2 [2/2] [UU]

md2 : active raid1 sata1p5[0] sata2p5[1]
      100 blocks super 1.2 [2/2] [UU]
"""
        pools = raid.inspect(raid.parse(text), fake.root, fake.mountinfo, [])
        self.assertEqual([(p["arrays"], p["volumes"][0]["name"]) for p in pools],
                         [(["md2"], "Volume 1"), (["md3"], "Volume 2")])


# The NAS from the screenshots: Storage Pool 1 is DSM 6's SHR, two layers in
# vg1000 with no volume_N name; Storage Pool 2 came later, a raid1 of two
# SSDs with one partial volume on it and a third SSD as its read cache.
TWO_POOLS = """md5 : active raid1 sata7p1[0]
      3905109440 blocks super 1.2 [1/1] [U]

md4 : active raid1 sata5p3[0] sata6p3[1]
      1855409088 blocks super 1.2 [2/2] [UU]
      [==============>......]  resync = 70.1% (1300654080/1855409088) finish=38.0min speed=243241K/sec

md3 : active raid5 sata1p6[0] sata2p6[1] sata3p6[2]
      100 blocks super 1.2 level 5, 64k chunk, algorithm 2 [3/3] [UUU]

md2 : active raid5 sata1p5[0] sata2p5[1] sata3p5[2]
      100 blocks super 1.2 level 5, 64k chunk, algorithm 2 [3/3] [UUU]

md1 : active raid1 sata1p2[0] sata2p2[1] sata3p2[2] sata5p2[3] sata6p2[4]
      2097088 blocks [5/5] [UUUUU]
"""


def two_pools() -> FakeSys:
    fake = FakeSys()
    for n in (1, 2, 3):
        fake.disk(f"sata{n}", BIG, "WDC WD60EFAX", [f"sata{n}p2", f"sata{n}p5", f"sata{n}p6"])
    for n in (5, 6):
        fake.disk(f"sata{n}", 2 * TB, "Samsung SSD 870 EVO 2TB", [f"sata{n}p2", f"sata{n}p3"])
    fake.disk("sata7", 4 * TB, "Samsung SSD 870 EVO 4TB", ["sata7p1"])
    fake.md("md1", ["sata1p2", "sata2p2", "sata3p2", "sata5p2", "sata6p2"])
    fake.md("md2", ["sata1p5", "sata2p5", "sata3p5"])
    fake.md("md3", ["sata1p6", "sata2p6", "sata3p6"])
    fake.md("md4", ["sata5p3", "sata6p3"])
    fake.md("md5", ["sata7p1"])
    fake.dm("dm-0", "vg1000-lv", "253:0", ["md2", "md3"])
    fake.dm("dm-1", "cachedev_0", "253:1", ["dm-0"])
    fake.dm("dm-2", "vg1-volume_2", "253:2", ["md4"], size=1000 * 1024 ** 3)
    fake.dm("dm-3", "vg1-syno_vg_reserved_area", "253:3", ["md4"])
    fake.dm("dm-4", "shared_cache_vg1-alloc_cache_1_1", "253:4", ["md5"])
    fake.dm("dm-5", "shared_cache_vg1-syno_vg_reserved_area", "253:5", ["md5"])
    fake.dm("dm-6", "cachedev_1", "253:6", ["dm-2", "dm-4"], size=1000 * 1024 ** 3)
    fake.filesystem("btrfs", "0f3c9a5e-5d2b-4c1e-9a77-2f1d3c4b5a69", "devices", "dm-6")
    fake.mounts("650 600 253:1 /docker/container-doctor /data rw,relatime - ext4 "
                "/dev/mapper/cachedev_0 rw")
    return fake


class TwoPoolsAndAnSsdCache(unittest.TestCase):
    def setUp(self):
        fake = two_pools()
        self.pools = raid.inspect(raid.parse(TWO_POOLS), fake.root, fake.mountinfo, ["/data"])

    def test_the_cache_is_no_layer_and_joins_no_pool(self):
        self.assertEqual([p["arrays"] for p in self.pools], [["md2", "md3"], ["md4"]])
        self.assertEqual([p["caches"] for p in self.pools], [[], ["md5"]])

    def test_shr_layers_are_told_from_a_single_raid(self):
        self.assertEqual([p["layout"] for p in self.pools], ["shr", "single"])

    def test_pools_are_counted_when_dsm_6_left_one_without_a_number(self):
        # vg1 is Storage Pool 2 here: vg1000 was there first.
        self.assertEqual([p["pool"] for p in self.pools], ["Storage Pool 1", "Storage Pool 2"])

    def test_dsm_6s_volume_gets_the_number_left_free(self):
        self.assertEqual([[v["name"] for v in p["volumes"]] for p in self.pools],
                         [["Volume 1"], ["Volume 2"]])

    def test_each_volume_says_its_filesystem_mounted_here_or_not(self):
        self.assertEqual([p["volumes"][0]["filesystem"] for p in self.pools], ["ext4", "btrfs"])

    def test_the_cache_is_on_the_volume_it_serves(self):
        self.assertEqual([p["volumes"][0]["cache"] for p in self.pools], [[], ["md5"]])

    def test_a_partial_volume_is_smaller_than_its_pool(self):
        volume = self.pools[1]["volumes"][0]
        self.assertEqual(volume["size_bytes"], 1000 * 1024 ** 3)
        self.assertGreater(self.pools[1]["size_bytes"], volume["size_bytes"])

    def test_elsewhere_pools_keep_no_synology_names(self):
        fake = two_pools()
        pools = raid.inspect(raid.parse(TWO_POOLS), fake.root, fake.mountinfo, ["/data"],
                             synology=False)
        self.assertEqual([p["pool"] for p in pools], ["", ""])
        self.assertEqual([p["layout"] for p in pools], ["lvm", "single"])
        self.assertEqual(pools[0]["volumes"][0]["name"], "cachedev_0")

    def test_older_dsm_hands_the_cache_array_to_cachedev_directly(self):
        fake = FakeSys()
        for n in (1, 2, 3):
            fake.disk(f"sata{n}", BIG, "X", [f"sata{n}p3"])
        fake.md("md2", ["sata1p3", "sata2p3"])
        fake.md("md3", ["sata3p3"])
        fake.dm("dm-0", "vg1-volume_1", "253:0", ["md2"])
        fake.dm("dm-1", "cachedev_0", "253:1", ["dm-0", "md3"])
        text = """md3 : active raid1 sata3p3[0]
      100 blocks super 1.2 [1/1] [U]

md2 : active raid1 sata1p3[0] sata2p3[1]
      100 blocks super 1.2 [2/2] [UU]
"""
        [pool] = raid.inspect(raid.parse(text), fake.root, fake.mountinfo, [])
        self.assertEqual((pool["arrays"], pool["caches"], pool["pool"]),
                         (["md2"], ["md3"], "Storage Pool 1"))


class TheDrivesBehindEachArray(unittest.TestCase):
    def test_members_are_named_by_drive_bay(self):
        arrays, _ = inspect(shr_nas())
        self.assertEqual([d["label"] for d in arrays["md3"].drives],
                         ["Drive 1", "Drive 2", "Drive 3", "Drive 4", "Drive 5", "Drive 7", "Drive 8"])

    def test_drive_size_and_model(self):
        arrays, _ = inspect(shr_nas())
        drive6 = next(d for d in arrays["md2"].drives if d["disk"] == "sata6")
        self.assertEqual((drive6["size_bytes"] // 512 * 512, drive6["model"]),
                         (SMALL // 512 * 512, "HAT5300-3T"))


class ScrubsAndReadErrors(unittest.TestCase):
    def test_a_clean_nas_has_no_warnings(self):
        arrays, _ = inspect(shr_nas())
        self.assertEqual({n: raid.warnings(a) for n, a in arrays.items() if raid.warnings(a)}, {})

    def test_swap_mismatches_are_not_news(self):
        arrays, _ = inspect(shr_nas())
        self.assertEqual(arrays["md1"].mismatches, 128)
        self.assertEqual(raid.warnings(arrays["md1"]), [])

    def test_a_scrub_that_found_mismatches_is_a_warning(self):
        arrays, _ = inspect(shr_nas(mismatches=64))
        [(key, text)] = raid.warnings(arrays["md3"])
        self.assertEqual(key, "mismatch")
        self.assertIn("64 sectors", text)

    def test_read_errors_are_the_drives_not_the_arrays(self):
        arrays, _ = inspect(shr_nas(errors={"sata3p5": 7}))
        self.assertEqual(raid.warnings(arrays["md2"]), [])
        [drive] = [d for d in raid.drives(list(arrays.values())) if d["errors"]]
        self.assertEqual((drive["label"], drive["errors"], drive["by_array"]),
                         ("Drive 3", 7, {"md2": 7}))
        self.assertEqual(raid.drive_warning(drive, 10, 7),
                         ("warning", "Drive 3 has had 7 read errors corrected in md2"))

    def test_one_drive_in_two_layers_is_one_warning(self):
        arrays, _ = inspect(shr_nas(errors={"sata7p5": 32, "sata7p6": 8}))
        warned = [d for d in raid.drives(list(arrays.values())) if raid.drive_warning(d, 10, 7)]
        self.assertEqual([d["label"] for d in warned], ["Drive 7"])
        _, text = raid.drive_warning(warned[0], 10, 7)
        self.assertEqual(text, "Drive 7 has had 40 read errors corrected (32 in md2, 8 in md3)")

    def test_a_drive_warning_is_an_incident_per_drive(self):
        drive = {"disk": "sata3", "label": "Drive 3", "errors": 7, "by_array": {"md2": 7},
                 "severity": "warning", "summary": "Drive 3 has had 7 read errors", "grew": True}
        from app.config import Settings
        [signal] = Detector(Settings()).check_raid([], [drive])
        self.assertEqual((signal.kind, signal.severity), ("raid_errors", "warning"))
        self.assertEqual(signal.container["name"], "host:Drive 3")
        self.assertFalse(signal.unchanged)
        [again] = Detector(Settings()).check_raid([], [{**drive, "errors": 32, "grew": False}])
        self.assertEqual(again.fingerprint, signal.fingerprint)
        self.assertTrue(again.unchanged)

    def test_a_scrub_result_read_again_is_unchanged(self):
        arrays, _ = inspect(shr_nas(mismatches=64))
        md3 = {**arrays["md3"].to_dict(), "healthy": True,
               "warnings": [{"key": k, "summary": t} for k, t in raid.warnings(arrays["md3"])]}
        from app.config import Settings
        detector = Detector(Settings())
        [first] = detector.check_raid([md3])
        [second] = detector.check_raid([md3])
        self.assertEqual((first.unchanged, second.unchanged), (False, True))

    def test_the_briefing_mentions_it_and_the_layout(self):
        arrays, pools = inspect(shr_nas(errors={"sata3p5": 7}))
        dicts = [{**a.to_dict(), "healthy": True,
                  "warnings": [{"key": k, "summary": t} for k, t in raid.warnings(a)]}
                 for a in arrays.values()]
        drives = raid.drives(list(arrays.values()))
        for drive in drives:
            drive["severity"], drive["summary"] = raid.drive_warning(drive, 10, 7) or ("", "")
        facts = storage_facts([], dicts, {}, 21, 5, pools, drives)
        self.assertEqual(facts.level, "attention")
        self.assertTrue(any("Drive 3" in line for line in facts.lines))
        self.assertFalse(any("All" in line and "healthy" in line for line in facts.lines))
        self.assertTrue(any("Volume 1 (/data) is built from md2 + md3" in line
                            for line in facts.lines))


class ErrorHistory(unittest.TestCase):
    """What past readings of a drive's count add to today's."""

    from datetime import datetime, timedelta, timezone
    NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    DAY = NOW - timedelta(hours=24)
    WEEK = NOW - timedelta(days=7)

    def at(self, hours_ago: float):
        return self.NOW - self.timedelta(hours=hours_ago)

    def history(self, samples, current):
        return raid.error_history(samples, current, self.DAY, self.WEEK)

    def test_no_history_counts_nothing_as_recent_but_all_as_new(self):
        # First sight of a drive with errors: no "getting worse" yet, but a
        # warning for one window.
        self.assertEqual(self.history([], 40),
                         {"seen": 40, "new": 40, "recent": 0, "reset": False})

    def test_errors_before_the_doctor_watched_are_not_recent(self):
        samples = [(self.at(3), 40)]
        self.assertEqual(self.history(samples, 42)["recent"], 2)

    def test_recent_is_measured_from_the_last_reading_before_the_window(self):
        samples = [(self.at(72), 5), (self.at(30), 20), (self.at(10), 30)]
        history = self.history(samples, 40)
        self.assertEqual((history["seen"], history["recent"]), (40, 20))

    def test_new_counts_only_the_errors_inside_the_window(self):
        # Drive 7: 40 corrected by 29 September, 8 more today.
        samples = [(self.at(8 * 24), 40), (self.at(5 * 24), 40), (self.at(2), 40)]
        history = self.history(samples, 48)
        self.assertEqual((history["new"], history["recent"]), (8, 8))

    def test_old_errors_with_none_since_are_quiet(self):
        samples = [(self.at(8 * 24), 48), (self.at(3 * 24), 48), (self.at(1), 48)]
        drive = {"label": "Drive 7", "errors": 48, "by_array": {"md2": 40, "md3": 8},
                 **self.history(samples, 48)}
        self.assertEqual(drive["new"], 0)
        self.assertIsNone(raid.drive_warning(drive, 10, 7))

    def test_a_reboot_does_not_forget_the_errors(self):
        samples = [(self.at(50), 40), (self.at(30), 40), (self.at(2), 0)]
        history = self.history(samples, 3)
        self.assertEqual(history, {"seen": 43, "new": 43, "recent": 3, "reset": True})

    def test_after_a_reset_the_warning_stays_and_says_why(self):
        drive = {"label": "Drive 7", "errors": 0, "by_array": {}, "seen": 40, "new": 40,
                 "recent": 0}
        severity, text = raid.drive_warning(drive, 10, 7)
        self.assertEqual(severity, "warning")
        self.assertIn("40 read errors corrected in the last 7 days, 0 since", text)

    def test_some_old_some_new_says_both(self):
        drive = {"label": "Drive 7", "errors": 48, "by_array": {"md2": 40, "md3": 8},
                 "seen": 48, "new": 8, "recent": 8}
        severity, text = raid.drive_warning(drive, 10, 7)
        self.assertEqual(severity, "warning")
        self.assertEqual(text, "Drive 7 has had 8 read errors corrected in the last 7 days, "
                               "48 since its arrays were last assembled (40 in md2, 8 in md3), "
                               "8 of them in the last 24 hours")

    def test_errors_that_keep_coming_are_critical(self):
        drive = {"label": "Drive 7", "errors": 52, "by_array": {"md2": 52},
                 "seen": 52, "new": 52, "recent": 12}
        severity, text = raid.drive_warning(drive, 10, 7)
        self.assertEqual(severity, "critical")
        self.assertTrue(text.endswith(", 12 of them in the last 24 hours"))
        self.assertEqual(raid.drive_warning(drive, 0, 7)[0], "warning")

    def test_a_clean_drive_says_nothing(self):
        drive = {"label": "Drive 1", "errors": 0, "by_array": {}, "seen": 0, "new": 0,
                 "recent": 0}
        self.assertIsNone(raid.drive_warning(drive, 10, 7))


class ThroughTheSupervisor(unittest.TestCase):
    def test_the_snapshot_carries_pools_and_warnings(self):
        from types import SimpleNamespace

        from app.supervisor import Supervisor

        fake = shr_nas(errors={"sata3p5": 7})
        mdstat = os.path.join(fake.root, "mdstat")
        with open(mdstat, "w", encoding="utf-8") as handle:
            handle.write(MDSTAT)
        host = SimpleNamespace(
            platform=SimpleNamespace(synology=True),
            settings=SimpleNamespace(mdstat_path=mdstat, sysfs_path=fake.root, disk_paths=[],
                                     raid_errors_per_day=10, raid_errors_days=7),
            store=SimpleNamespace(raid_most_members=lambda name, active: active,
                                  drive_error_samples=lambda disk, since: [],
                                  record_drive_errors=lambda disk, errors: None),
        )
        host._drive_errors = lambda drives: Supervisor._drive_errors(host, drives)
        report = Supervisor._read_raid(host)
        self.assertEqual([p["arrays"] for p in report["pools"]], [["md2", "md3"]])
        md2 = next(a for a in report["arrays"] if a["name"] == "md2")
        self.assertTrue(md2["healthy"])
        self.assertEqual(md2["warnings"], [])
        self.assertEqual(len(md2["drives"]), 8)
        [drive] = [d for d in report["drives"] if d["summary"]]
        self.assertEqual((drive["label"], drive["severity"], drive["grew"]),
                         ("Drive 3", "warning", True))


class WithoutSys(unittest.TestCase):
    def test_no_sys_is_no_detail_not_an_error(self):
        arrays = raid.parse(MDSTAT)
        self.assertEqual(raid.inspect(arrays, "/nonexistent/sys"), [])
        self.assertEqual(raid.inspect(arrays, ""), [])
        md2 = {a.name: a for a in arrays}["md2"]
        self.assertEqual((md2.drives, md2.mismatches), ([], None))


if __name__ == "__main__":
    unittest.main()
