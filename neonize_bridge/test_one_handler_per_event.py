"""neonize keeps ONE handler per event type (EventsManager.list_func):
registering the same type twice silently erases the first. This test
prevents two bridge modules from registering the same event."""
import collections
import pathlib
import re

HERE = pathlib.Path(__file__).parent
REGISTRATION = re.compile(r"cli\.event\(\s*nev\.([A-Za-z]+)\s*\)")


def registrations():
    found = collections.defaultdict(list)
    for file in sorted(HERE.glob("*.py")):
        if file.name.startswith("test_"):
            continue
        for line, text in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            match = REGISTRATION.search(text)
            if match:
                found[match.group(1)].append(f"{file.name}:{line}")
    return found


def test_no_event_type_is_registered_twice():
    repeated = {kind: where for kind, where in registrations().items() if len(where) > 1}
    assert repeated == {}, (
        "neonize keeps one handler per type; the last erases the previous: "
        f"{repeated}")


def test_the_test_sees_the_registrations_that_exist():
    found = registrations()
    assert "MessageEv" in found and "GroupInfoEv" in found
    assert "JoinedGroupEv" in found and "PictureEv" in found
    assert "BlocklistEv" in found
    assert "PrivacySettingsEv" in found
