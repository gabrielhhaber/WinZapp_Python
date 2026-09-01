"""WPPConnect's browser options: the two halves that have to agree.

Both of the things checked here are the same class of mistake — a setting
written down in one of the two places WinZapp configures WPPConnect from, while
the other place quietly kept the old behaviour. Neither failed loudly; both
were found by reading a log against the source.

  * ``autoClose: 0`` does not mean "nothing closes the page". host.layer.js has
    a second timer, ``deviceSyncTimeout`` (default 180000), started right after
    a successful login by ``startAutoClose(this.options.deviceSyncTimeout)``
    and closing the page through the same tryAutoClose() path — under the same
    log wording, so wppconnect.log prints "Auto close configured to 180s" for a
    session configured with autoClose: 0.

  * ``browserArgs`` in ``src/config.ts`` is UNIONED with start.js's list by
    merge-deep (``mergeDeep({}, config, serverOptions)`` in index.ts), never
    replaced. A flag deleted from start.js therefore still reaches Chrome if it
    also appears here — which is what happened to the video-send fix: the three
    rasterizer flags were removed from start.js, stayed in config.ts, and every
    video send kept failing with "video loaded with duration but no dims".
"""

import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PATCHES = REPO_ROOT / "client" / "api_patches"

CONFIG_TS = (PATCHES / "src" / "config.ts").read_text(encoding="utf-8")
#: config.ts explains at length why the rasterizer flags must not come back,
#: naming them — so the checks below read the code with the comments stripped,
#: or the explanation would fail the test it exists to justify.
CONFIG_TS_CODE = "\n".join(
    line for line in CONFIG_TS.splitlines() if not line.lstrip().startswith("//")
)
CONFIG_JSON = json.loads((PATCHES / "config.json").read_text(encoding="utf-8"))


class TestNothingClosesTheBrowserOnUs:
    """Both close timers must be off, not just the well-known one."""

    def test_config_json_pins_both_timers_to_zero(self):
        create_options = CONFIG_JSON["createOptions"]
        assert create_options["autoClose"] == 0
        assert create_options["deviceSyncTimeout"] == 0

    def test_config_ts_pins_both_timers_to_zero(self):
        assert "autoClose: 0," in CONFIG_TS
        assert "deviceSyncTimeout: 0," in CONFIG_TS


class TestRasterizerFlagsAreGoneFromBothLists:
    """start.js removed these; config.ts has to remove them too or the union
    puts them straight back."""

    REMOVED = (
        "--disable-software-rasterizer",
        "--disable-3d-apis",
        "--disable-webgl",
    )

    def test_config_ts_no_longer_lists_them(self):
        for flag in self.REMOVED:
            assert f"'{flag}'," not in CONFIG_TS_CODE, (
                f"{flag} is back in config.ts's browserArgs; merge-deep unions "
                "the arrays, so it reaches Chrome no matter what start.js does."
            )

    def test_start_js_no_longer_lists_them(self):
        start_js = "\n".join(
            line
            for line in (PATCHES / "start.js").read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("//")
        )
        for flag in self.REMOVED:
            assert f"'{flag}'," not in start_js

    def test_disable_gpu_is_kept(self):
        # The one that actually saves CPU/memory in a windowless session, and
        # on its own still leaves the software rasterizer fallback available.
        assert "'--disable-gpu'," in CONFIG_TS_CODE
