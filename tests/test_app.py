# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 the Klepsydra authors
"""Browser tests for Klepsydra.

Run with:  ~/.venv/bin/python -m pytest tests
Serves a copy of the app with tests/fixtures/timers.json on 127.0.0.1 and drives it
headless with Playwright's fake clock, so no test waits in real time.
"""
import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_TEXT = (ROOT / "tests" / "fixtures" / "timers.json").read_text()

# Records the frequency of every tone the page plays (the silent unlock tone has gain 0 and 30 ms, skip it).
RECORD_TONES = """
window.__tones = [];
const origStart = OscillatorNode.prototype.start;
OscillatorNode.prototype.start = function (...args) {
  if (this.frequency.value !== 440) window.__tones.push(this.frequency.value);
  return origStart.apply(this, args);
};
"""


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    # Serve what build.sh publishes (so a file missing from its list fails here), with the fixture timers.
    src = tmp_path_factory.mktemp("src") / "app"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", "tests", "public", ".pytest_cache", "__pycache__"))
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_SHA"}
    subprocess.run(["sh", "build.sh"], cwd=src, env=env, check=True)
    site = src / "public"
    shutil.copy(ROOT / "tests" / "fixtures" / "timers.json", site / "timers.json")
    handler = functools.partial(QuietHandler, directory=str(site))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()

# A stand-in for the Wake Lock API that logs requests and releases (headless Chromium refuses the real one).
FAKE_WAKE_LOCK = """
window.__wake = [];
Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: {
  request(type) {
    window.__wake.push('request:' + type);
    const handlers = [];
    return Promise.resolve({
      released: false,
      addEventListener(name, fn) { if (name === 'release') handlers.push(fn); },
      release() { window.__wake.push('release'); this.released = true; handlers.forEach((f) => f()); return Promise.resolve(); },
    });
  },
}});
"""
NO_WAKE_LOCK = "delete Navigator.prototype.wakeLock;"


class Dialogs:
    """Answers confirm() prompts from a queue; a dialog nobody queued an answer for fails the test."""

    def __init__(self, errors):
        self.answers, self.seen, self.errors = [], [], errors

    def handle(self, dialog):
        self.seen.append(dialog.message)
        if dialog.type == "confirm" and self.answers:
            dialog.accept() if self.answers.pop(0) else dialog.dismiss()
        else:
            self.errors.append(f"unexpected {dialog.type}: {dialog.message}")
            dialog.dismiss()

    def wait(self, page, count):
        for _ in range(100):
            if len(self.seen) >= count:
                return
            page.wait_for_timeout(20)
        raise AssertionError(f"expected {count} dialogs, saw {self.seen}")


def new_page(browser, server, fake_clock, wake_lock=FAKE_WAKE_LOCK, version=None):
    # With `version`, serve that as version.txt (routes can't see service-worker requests, so block it).
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              service_workers="block" if version is not None else "allow")
    if version is not None:
        ctx.route("**/version.txt", lambda route: route.fulfill(status=200, body=version, content_type="text/plain"))
    pg = ctx.new_page()
    errors = []
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.console_errors = errors
    pg.dialogs = Dialogs(errors)
    pg.on("dialog", pg.dialogs.handle)
    pg.add_init_script(RECORD_TONES)
    pg.add_init_script(wake_lock)
    if fake_clock:
        pg.clock.install(time=0)
    pg.goto(server)
    if fake_clock:
        pg.clock.pause_at(1000)  # from here on time only moves when a test advances it
    expect(pg.locator(".timer-item").first).to_be_visible()
    return ctx, pg, errors


@pytest.fixture
def page(browser, server):
    ctx, pg, errors = new_page(browser, server, fake_clock=True)
    yield pg
    ctx.close()
    assert errors == []


@pytest.fixture
def live_page(browser, server):
    """Real clock, for the service worker."""
    ctx, pg, errors = new_page(browser, server, fake_clock=False)
    yield pg
    ctx.close()
    assert errors == []


def tones(page):
    return page.evaluate("window.__tones.splice(0)")


def ring_emptied(page):
    """How much of the ring has emptied: 0 = full, 1 = empty."""
    return page.locator("#ring-arc").evaluate(
        "el => (parseFloat(el.style.strokeDashoffset) || 0) / (2 * Math.PI * 46)")


def open_timer(page, name):
    page.locator(".timer-item .name", has_text=name).click()
    expect(page.locator("#timer-view")).to_be_visible()


def test_list_shows_valid_timers_and_errors(page):
    items = page.locator(".timer-item")
    expect(items).to_have_count(10)
    expect(items.nth(0)).to_contain_text("00:09 · 2 intervals")
    expect(items.nth(1)).to_contain_text("00:35 · 5 intervals")
    expect(items.nth(0)).to_be_enabled()
    for i, msg in [(3, "color must be a CSS colour string"), (4, "duration must be"), (5, "more than 2000 intervals"),
                   (6, "intervals must be a non-empty list"), (7, "must be an object"),
                   (8, "description must be a string"), (9, "#1: comment must be a string")]:
        expect(items.nth(i)).to_be_disabled()
        expect(items.nth(i)).to_contain_text(msg)


def test_logo_and_manifest_load(page, server):
    logo = page.locator(".brand img")
    expect(logo).to_be_visible()
    assert logo.evaluate("img => img.complete && img.naturalWidth") == 192
    manifest = page.request.get(server + "manifest.json").json()
    assert manifest["name"] == "Klepsydra"
    for icon in manifest["icons"]:
        assert page.request.get(server + icon["src"]).ok
    for href in page.locator("link[rel=icon], link[rel=apple-touch-icon]").evaluate_all("ls => ls.map(l => l.href)"):
        assert page.request.get(href).ok


def test_help_links_to_readme(page):
    help_link = page.locator("#open-help")
    expect(help_link).to_be_visible()
    expect(help_link).to_have_attribute("href", "https://github.com/n03l/klepsydra#readme")
    expect(help_link).to_have_attribute("target", "_blank")
    expect(help_link).to_have_attribute("rel", "noopener noreferrer")


def test_no_referrer_and_refuses_to_run_in_a_frame(browser, server):
    ctx = browser.new_context(service_workers="block")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server)
    expect(pg.locator(".timer-item").first).to_be_visible()
    assert pg.evaluate("document.referrer === '' && document.querySelector('meta[name=referrer]').content") \
        == "no-referrer"
    host = ctx.new_page()  # not pg: its document keeps the app's CSP, which blocks frames
    host.on("pageerror", lambda e: errors.append(str(e)))
    host.set_content(f'<iframe src="{server}" width="390" height="600"></iframe>')
    framed = host.frame_locator("iframe")
    expect(framed.locator("body")).to_have_text("Klepsydra can't run inside a frame. Open it directly.")
    expect(framed.locator(".timer-item")).to_have_count(0)
    ctx.close()
    assert errors == []


def test_description_between_name_and_timeline(page):
    item = page.locator(".timer-item").nth(0)
    expect(item.locator(".description")).to_have_text("Two quick intervals")
    order = item.evaluate("el => [...el.children].map(c => c.className)")
    assert order == ["name", "description", "meta", "strip"]
    long_desc = page.locator(".timer-item").nth(1).locator(".description").text_content()
    assert len(long_desc) == 200 and long_desc.endswith("…")
    expect(page.locator(".timer-item").nth(3).locator(".description")).to_have_count(0)


def test_hostile_names_render_as_text(page):
    item = page.locator(".timer-item").nth(2)
    expect(item.locator(".name")).to_have_text("<img src=x onerror=alert(1)>")
    expect(item.locator(".description")).to_have_text("<b onmouseover=alert(3)>bold</b>")
    assert page.locator("#timer-list").locator("img, b, script").count() == 0
    item.click()
    expect(page.locator("#cur-name")).to_have_text("<script>alert(2)</script>")
    page.locator("#btn-play").click()
    page.clock.run_for(3000)
    expect(page.locator("#cur-name")).to_have_text("Done")


def test_runs_through_intervals_with_beeps(page):
    open_timer(page, "Short")
    expect(page.locator("#time")).to_have_text("00:05")
    expect(page.locator("#cur-name")).to_have_text("Alpha")
    expect(page.locator("#next-name")).to_have_text("Beta")
    expect(page.locator("#summary")).to_have_text("Set 1/2 · 00:09 left")
    expect(page.locator("#cur-dur")).to_have_text("of 00:05")
    assert ring_emptied(page) == 0
    expect(page.locator("#timer-view")).to_have_css("background-color", "rgb(229, 57, 53)")

    page.locator("#btn-play").click()
    page.clock.run_for(2500)
    expect(page.locator("#time")).to_have_text("00:03")
    expect(page.locator("#summary")).to_have_text("Set 1/2 · 00:07 left")
    assert abs(ring_emptied(page) - 0.5) < 0.01  # halfway through Alpha
    assert tones(page) == []

    page.clock.run_for(2600)  # through the 2-second countdown into Beta
    expect(page.locator("#cur-name")).to_have_text("Beta")
    expect(page.locator("#next-name")).to_have_text("Finish")
    expect(page.locator("#summary")).to_have_text("Set 2/2 · 00:04 left")
    assert ring_emptied(page) < 0.05  # refilled for the new interval
    # Yellow background gets dark text.
    expect(page.locator("#timer-view")).to_have_css("background-color", "rgb(255, 255, 0)")
    expect(page.locator("#time")).to_have_css("color", "rgb(17, 17, 17)")
    assert tones(page) == [880, 880, 1320]

    page.clock.run_for(4000)
    expect(page.locator("#cur-name")).to_have_text("Done")
    expect(page.locator("#time")).to_have_text("00:00")
    expect(page.locator("#summary")).to_have_text("Total 00:09")
    expect(page.locator("#cur-dur")).to_be_hidden()
    assert ring_emptied(page) == 0  # a full ring when done
    expect(page.locator("#next-card")).to_be_hidden()
    assert tones(page) == [880, 880, 1320, 1320, 1760]


def test_pause_next_prev_reset(page):
    open_timer(page, "Short")
    page.locator("#btn-play").click()
    page.clock.run_for(1500)
    page.locator("#btn-play").click()  # pause
    expect(page.locator("#btn-play")).to_have_attribute("aria-label", "Play")
    page.clock.run_for(10000)
    expect(page.locator("#time")).to_have_text("00:04")
    expect(page.locator("#cur-name")).to_have_text("Alpha")

    page.locator("#btn-next").click()
    expect(page.locator("#cur-name")).to_have_text("Beta")
    expect(page.locator("#time")).to_have_text("00:04")
    page.locator("#btn-prev").click()
    expect(page.locator("#cur-name")).to_have_text("Alpha")
    expect(page.locator("#time")).to_have_text("00:05")

    page.locator("#time").click()  # tapping the digits also plays
    page.clock.run_for(1200)
    expect(page.locator("#time")).to_have_text("00:04")
    page.locator("#btn-reset").click()
    expect(page.locator("#btn-play")).to_have_attribute("aria-label", "Play")
    expect(page.locator("#time")).to_have_text("00:05")
    expect(page.locator("#summary")).to_have_text("Set 1/2 · 00:09 left")

    page.locator("#btn-next").click()
    page.locator("#btn-next").click()  # past the last interval finishes
    expect(page.locator("#cur-name")).to_have_text("Done")
    page.locator("#btn-prev").click()
    expect(page.locator("#cur-name")).to_have_text("Beta")


def test_prepare_rounds_and_halfway(page):
    open_timer(page, "Rounds")
    expect(page.locator("#cur-name")).to_have_text("Get ready")
    expect(page.locator("#summary")).to_have_text("Set 1/5 · 00:35 left")  # no round outside a repeat
    page.keyboard.press(" ")
    page.clock.run_for(3100)
    expect(page.locator("#cur-name")).to_have_text("Work")
    expect(page.locator("#summary")).to_have_text("Round 1/2 · Set 2/5 · 00:32 left")
    assert tones(page) == [1320]
    page.clock.run_for(5000)  # halfway through Work
    assert tones(page) == [660, 660]
    page.clock.run_for(5000)  # into Rest, which has halfway off
    expect(page.locator("#cur-name")).to_have_text("Rest")
    page.clock.run_for(6000)
    expect(page.locator("#summary")).to_contain_text("Round 2/2 · Set 4/5")
    assert tones(page) == [1320, 1320]
    page.keyboard.press("ArrowRight")
    expect(page.locator("#cur-name")).to_have_text("Rest")
    page.keyboard.press("Escape")
    expect(page.locator("#list-view")).to_be_visible()


def test_background_catch_up(page):
    """If the tab slept through several intervals, the timer lands in the right one."""
    open_timer(page, "Rounds")
    page.locator("#btn-play").click()
    page.clock.run_for(100)
    page.clock.fast_forward(3000 + 16000 + 4000)  # jumps without running every tick
    page.clock.run_for(100)
    expect(page.locator("#cur-name")).to_have_text("Work")
    expect(page.locator("#summary")).to_contain_text("Round 2/2")
    expect(page.locator("#time")).to_have_text("00:06")


def test_back_button_returns_to_list(page):
    open_timer(page, "Short")
    page.go_back()
    expect(page.locator("#list-view")).to_be_visible()
    expect(page.locator("#timer-view")).to_be_hidden()
    open_timer(page, "Short")
    page.locator("#btn-close").click()
    expect(page.locator("#list-view")).to_be_visible()


# ---------------------------------------------------------------- timers file: storage, editor, upload, download

CUSTOM = json.dumps({"timers": [{"name": "Custom", "description": "Mine",
                                 "intervals": [{"name": "Go", "duration": 5}]}]})


def names(page):
    return page.locator(".timer-item .name")


def stored(page):
    return page.evaluate("JSON.parse(localStorage.getItem('klepsydra:timers') || 'null')")


def open_settings(page):
    page.locator("#open-settings").click()
    expect(page.locator("#settings-view")).to_be_visible()


def choose_file(page, input_id, name, text):
    page.locator(input_id).set_input_files(
        files=[{"name": name, "mimeType": "application/json", "buffer": text.encode()}])


def upload(page, name, text):
    """Settings → Restore from backup."""
    open_settings(page)
    choose_file(page, "#file-input", name, text)


def open_json_editor(page):
    open_settings(page)
    page.locator("#edit").click()
    expect(page.locator("#editor-view")).to_be_visible()


def save_in_editor(page, text):
    open_json_editor(page)
    page.locator("#editor-text").fill(text)
    page.locator("#editor-save").click()
    expect(page.locator("#list-view")).to_be_visible()


def test_default_timers_and_no_file_name(page):
    expect(page.locator(".timer-item")).to_have_count(10)
    expect(page.locator(".list-header")).not_to_contain_text("timers.json")
    expect(page.locator("#source")).to_have_count(0)
    assert stored(page) is None  # the default is used, not copied into storage


def test_upload_replaces_and_persists(page):
    page.dialogs.answers.append(True)
    upload(page, "mine.json", CUSTOM)
    expect(names(page)).to_have_text(["Custom"])
    expect(page.locator("#list-view")).to_be_visible()  # back on the list, with the restored timers
    assert page.dialogs.seen == ["Replace all your timers with mine.json?"]
    page.reload()
    expect(names(page)).to_have_text(["Custom"])
    expect(page.locator(".timer-item .description")).to_have_text("Mine")


def test_upload_rejected_or_declined_keeps_timers(page):
    upload(page, "bad.json", "{")
    expect(page.locator("#settings-message")).to_contain_text("bad.json: Not valid JSON")
    choose_file(page, "#file-input", "list.json", '{"x": 1}')
    expect(page.locator("#settings-message")).to_contain_text("list.json: The file must be a list of timers")
    assert page.dialogs.seen == []

    page.dialogs.answers.append(False)
    choose_file(page, "#file-input", "mine.json", CUSTOM)
    page.dialogs.wait(page, 1)
    expect(page.locator("#settings-view")).to_be_visible()
    page.go_back()
    expect(page.locator(".timer-item")).to_have_count(10)
    assert stored(page) is None


def test_edit_validate_format_save(page):
    open_json_editor(page)
    expect(page.locator("#editor-text")).to_have_value(FIXTURE_TEXT)

    page.locator("#editor-text").fill("{")
    page.locator("#editor-save").click()
    expect(page.locator("#editor-error")).to_contain_text("Not valid JSON")
    page.locator("#editor-format").click()
    expect(page.locator("#editor-error")).to_contain_text("Not valid JSON")
    page.locator("#editor-text").fill('{"x": 1}')
    page.locator("#editor-save").click()
    expect(page.locator("#editor-error")).to_contain_text("must be a list of timers")
    expect(page.locator("#editor-view")).to_be_visible()
    assert stored(page) is None

    page.locator("#editor-text").fill(CUSTOM)
    page.locator("#editor-format").click()
    assert '\n  "timers": [' in page.locator("#editor-text").input_value()
    page.locator("#editor-save").click()
    expect(page.locator("#list-view")).to_be_visible()
    expect(names(page)).to_have_text(["Custom"])
    assert json.loads(stored(page)["text"]) == json.loads(CUSTOM)
    page.reload()
    expect(names(page)).to_have_text(["Custom"])


def test_editor_cancel_and_back_button(page):
    open_json_editor(page)
    page.locator("#editor-cancel").click()  # no changes: no question asked
    expect(page.locator("#list-view")).to_be_visible()  # it took the place of Settings

    open_json_editor(page)
    page.locator("#editor-text").fill(CUSTOM)
    page.dialogs.answers.append(False)
    page.locator("#editor-cancel").click()
    page.dialogs.wait(page, 1)
    assert page.dialogs.seen == ["Discard your changes?"]
    expect(page.locator("#editor-view")).to_be_visible()
    expect(page.locator("#editor-text")).to_have_value(CUSTOM)

    page.dialogs.answers.append(True)
    page.go_back()  # the phone's back button
    expect(page.locator("#list-view")).to_be_visible()
    expect(page.locator(".timer-item")).to_have_count(10)
    assert stored(page) is None


def test_restore_default(page):
    save_in_editor(page, CUSTOM)
    expect(names(page)).to_have_text(["Custom"])
    open_json_editor(page)
    page.dialogs.answers.append(True)
    page.locator("#editor-default").click()
    expect(page.locator("#editor-text")).to_have_value(FIXTURE_TEXT)
    expect(names(page)).to_have_text(["Custom"])  # nothing saved yet
    page.locator("#editor-save").click()
    expect(page.locator(".timer-item")).to_have_count(10)


def test_back_up_all_timers(page):
    open_settings(page)
    with page.expect_download() as info:
        page.locator("#download").click()
    d = info.value
    assert re.fullmatch(r"klepsydra-backup-\d{4}-\d{2}-\d{2}\.json", d.suggested_filename)
    assert Path(d.path()).read_text() == FIXTURE_TEXT
    expect(page.locator("#settings-view")).to_be_visible()  # a backup changes nothing, so Settings stays open

    page.locator("#settings-back").click()
    save_in_editor(page, CUSTOM)
    open_settings(page)
    with page.expect_download() as info:
        page.locator("#download").click()
    assert Path(info.value.path()).read_text() == CUSTOM


def test_hostile_json_from_editor_renders_as_text(page):
    hostile = json.dumps({"timers": [{"name": "<img src=x onerror=alert(1)>",
                                      "description": "<svg onload=alert(2)></svg>",
                                      "intervals": [{"name": "<iframe srcdoc=x>", "duration": 1,
                                                     "comment": "<img src=z onerror=alert(5)>"}]}]})
    save_in_editor(page, hostile)
    expect(names(page)).to_have_text(["<img src=x onerror=alert(1)>"])
    expect(page.locator(".timer-item .description")).to_have_text("<svg onload=alert(2)></svg>")
    assert page.locator(".timer-item").locator("img, svg, iframe").count() == 0
    page.locator(".card-info").click()
    expect(page.locator("#details-name")).to_have_text("<img src=x onerror=alert(1)>")
    expect(page.locator("#details-outline .outline-name")).to_have_text(["<iframe srcdoc=x>"])
    expect(page.locator("#details-outline .outline-comment")).to_have_text(["<img src=z onerror=alert(5)>"])
    assert page.locator("#details").locator("img, iframe").count() == 0
    page.locator("#details-close").click()
    names(page).click()
    expect(page.locator("#cur-name")).to_have_text("<iframe srcdoc=x>")
    expect(page.locator("#cur-comment")).to_have_text("<img src=z onerror=alert(5)>")
    assert page.locator("#timer-view").locator("img, iframe").count() == 0


def test_broken_saved_file_can_be_fixed(page):
    page.evaluate("localStorage.setItem('klepsydra:timers', JSON.stringify({text: '{oops'}))")
    page.reload()
    expect(page.locator("#message")).to_contain_text("Not valid JSON")
    expect(page.locator(".timer-item")).to_have_count(0)
    expect(page.locator("#empty")).to_be_hidden()  # the error says what's wrong instead
    open_settings(page)
    expect(page.locator("#settings-message")).to_contain_text("Your timers file has an error")
    for button in ("#manage", "#new-timer", "#import"):
        expect(page.locator(button)).to_be_disabled()
    page.locator("#edit").click()
    expect(page.locator("#editor-text")).to_have_value("{oops")
    page.locator("#editor-text").fill(CUSTOM)
    page.locator("#editor-save").click()
    expect(names(page)).to_have_text(["Custom"])
    expect(page.locator("#message")).to_be_empty()


def test_legacy_storage_is_migrated(page):
    page.evaluate("t => localStorage.setItem('interval-timer:last-file', JSON.stringify({name: 'old.json', text: t}))",
                  CUSTOM)
    page.reload()
    expect(names(page)).to_have_text(["Custom"])
    assert page.evaluate("localStorage.getItem('interval-timer:last-file')") is None
    assert stored(page)["text"] == CUSTOM


def test_works_offline_after_first_visit(live_page):
    page = live_page
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    page.context.set_offline(True)
    # Prove the network is really gone for the service worker: an uncached URL must fail, not 404.
    assert page.evaluate("fetch('not-cached.txt').then(r => r.status, () => 'failed')") == "failed"
    page.wait_for_timeout(200)
    page.console_errors[:] = [e for e in page.console_errors if "ERR_FAILED" not in e]  # the probe's own log line
    page.reload()
    expect(page.locator(".timer-item")).to_have_count(10)
    page.locator(".timer-item .name", has_text="Short").click()
    expect(page.locator("#cur-name")).to_have_text("Alpha")
    page.context.set_offline(False)


# ---------------------------------------------------------------- keep screen on

def wake_log(page):
    return page.evaluate("window.__wake.splice(0)")


def test_keep_awake_off_by_default(page):
    expect(page.locator("#keep-awake")).not_to_be_checked()
    expect(page.locator("#keep-awake")).to_be_enabled()
    open_timer(page, "Short")
    page.locator("#btn-play").click()
    page.clock.run_for(1000)
    assert wake_log(page) == []


def test_keep_awake_setting_persists_and_holds_lock_while_running(page):
    open_settings(page)
    page.locator(".setting").click()  # the whole row toggles it
    expect(page.locator("#keep-awake")).to_be_checked()
    page.reload()
    expect(page.locator("#list-view")).to_be_visible()
    expect(page.locator("#keep-awake")).to_be_checked()
    assert page.evaluate("localStorage.getItem('klepsydra:keep-awake')") == "on"
    assert wake_log(page) == []  # not while just looking at the list

    open_timer(page, "Short")
    assert wake_log(page) == []  # opened but not started
    page.locator("#btn-play").click()
    page.clock.run_for(100)
    assert wake_log(page) == ["request:screen"]
    page.locator("#btn-play").click()  # pause
    page.clock.run_for(100)
    assert wake_log(page) == ["release"]
    page.locator("#btn-play").click()
    page.clock.run_for(10_000)  # runs to the end
    expect(page.locator("#cur-name")).to_have_text("Done")
    assert wake_log(page) == ["request:screen", "release"]

    page.locator("#btn-close").click()
    open_settings(page)
    page.locator("#keep-awake").uncheck()
    page.reload()
    expect(page.locator("#keep-awake")).not_to_be_checked()


def test_pause_during_lock_request_does_not_leave_it_held(page):
    open_settings(page)
    page.locator("#keep-awake").check()
    page.locator("#settings-back").click()
    open_timer(page, "Short")
    page.locator("#btn-play").click()
    page.locator("#btn-play").click()  # pause before the request settles
    page.clock.run_for(100)
    log = wake_log(page)
    assert log.count("request:screen") == log.count("release")


def test_keep_awake_unsupported(browser, server):
    ctx, page, errors = new_page(browser, server, fake_clock=True, wake_lock=NO_WAKE_LOCK)
    expect(page.locator("#keep-awake")).to_be_disabled()
    expect(page.locator("#keep-awake-note")).to_contain_text("Not available")
    open_timer(page, "Short")
    page.locator("#btn-play").click()
    page.clock.run_for(2000)
    expect(page.locator("#time")).to_have_text("00:03")
    ctx.close()
    assert errors == []


# ---------------------------------------------------------------- deployed version

def test_version_hidden_without_commit(page):
    expect(page.locator(".build-info .version")).to_have_count(4)
    for link in page.locator(".build-info .version").all():
        expect(link).to_be_hidden()


@pytest.mark.parametrize("body, shown", [
    ("a8af90f\n", "a8af90f"),
    ("0123456789abcdef0123456789abcdef01234567\n", "0123456"),
    ("", None),                        # CLI deploy: no commit known
    ("<img src=x onerror=alert(1)>", None),
    ("<!doctype html><title>404</title>", None),
])
def test_version_shown_only_if_it_looks_like_a_commit(browser, server, body, shown):
    ctx, page, errors = new_page(browser, server, fake_clock=False, version=body)
    if shown:
        expect(page.locator("#list-view .version")).to_have_text(shown)
        expect(page.locator("#list-view .version")).to_be_visible()
        commit = body.strip()
        expect(page.locator("#list-view .version")).to_have_attribute(
            "href", f"https://github.com/n03l/klepsydra/commit/{commit}")
        expect(page.locator("#list-view .version")).to_have_attribute("title", f"Commit {commit}")
        # at the bottom of the list, not in the header
        last_card = page.locator(".timer-item").last.bounding_box()
        assert page.locator("#list-view .version").bounding_box()["y"] > last_card["y"] + last_card["height"]
        assert page.locator(".list-header .version").count() == 0
    else:
        page.wait_for_timeout(200)
        expect(page.locator("#list-view .version")).to_be_hidden()
    assert page.locator(".list-view img").count() == 1  # just the logo
    expect(page.locator("#list-view .repo-link")).to_have_attribute("href", "https://github.com/n03l/klepsydra")
    expect(page.locator("#list-view .repo-link")).to_be_visible()
    ctx.close()
    assert errors == []


def test_github_link_and_commit_on_every_screen_but_the_timer(browser, server):
    ctx, page, errors = new_page(browser, server, fake_clock=False, version="d863f16caef8f95c7b7e9bee571266a58700241e\n")
    expect(page.locator(".build-info .version")).to_have_text(["d863f16"] * 4)

    def check(screen):
        footer = page.locator(f"{screen} .build-info")
        expect(footer.locator(".repo-link")).to_be_visible()
        expect(footer.locator(".repo-link")).to_have_attribute("href", "https://github.com/n03l/klepsydra")
        expect(footer.locator(".version")).to_be_visible()
        repo, version = footer.locator(".repo-link").bounding_box(), footer.locator(".version").bounding_box()
        assert repo["x"] + repo["width"] <= version["x"]  # the commit to the right of the logo
        assert footer.bounding_box()["y"] + footer.bounding_box()["height"] <= 844 + 0.5 or screen in ("#list-view", "#settings-view")  # these scroll

    check("#list-view")
    open_settings(page)
    check("#settings-view")
    page.locator("#new-timer").click()
    check("#timer-editor")
    page.locator("#te-cancel").click()
    open_json_editor(page)
    check("#editor-view")
    page.locator("#editor-cancel").click()
    open_timer(page, "Short")
    expect(page.locator(".build-info:visible")).to_have_count(0)
    ctx.close()
    assert errors == []


def test_build_publishes_only_the_app(tmp_path):
    src = tmp_path / "app"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", "public", ".pytest_cache", "__pycache__"))
    subprocess.run(["sh", "build.sh"], cwd=src, env={**os.environ, "GITHUB_SHA": "9293fe4" + "0" * 33},
                   check=True)
    published = sorted(str(p.relative_to(src / "public")) for p in (src / "public").rglob("*") if p.is_file())
    assert published == sorted([
        "LICENSE", "app.js", "icons/NOTICE", "icons/apple-touch-icon.png", "icons/favicon.png",
        "icons/icon-192.png", "icons/icon-512.png", "index.html", "manifest.json", "style.css", "sw.js",
        "timers.json", "version.txt"])
    assert (src / "public" / "version.txt").read_text() == "9293fe4" + "0" * 33 + "\n"

    # Built locally from a checkout: its commit.
    git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
    head = subprocess.run(git[:3] + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_SHA"}
    subprocess.run(["sh", "build.sh"], cwd=src, env=env, check=True)
    assert (src / "public" / "version.txt").read_text() == head
    # everything the service worker precaches must be published
    sw = (src / "public" / "sw.js").read_text()
    shell = json.loads(re.search(r"const SHELL = (\[.*?\]);", sw, re.S).group(1).replace("'", '"').replace(",\n]", "]"))
    for path in shell:
        assert path == "./" or (src / "public" / path).is_file(), path


# ---------------------------------------------------------------- details (ⓘ)

def outline_rows(page):
    return page.locator("#details-outline .outline-row").evaluate_all(
        "rows => rows.map(r => [...r.querySelectorAll('.outline-name, .outline-dur')].map(e => e.textContent).join(' '))")


def test_info_button_on_each_valid_card(page):
    expect(page.locator(".card-info")).to_have_count(3)  # Short, Rounds and the hostile one; not the broken ones
    for li in page.locator("#timer-list li").all():
        assert li.locator(".card-info").count() == (0 if li.locator(".timer-item").is_disabled() else 1)
    box, card = page.locator(".card-info").first.bounding_box(), page.locator(".timer-item").first.bounding_box()
    assert box["x"] + box["width"] > card["x"] + card["width"] - 12 and box["y"] < card["y"] + 12  # top right


def test_details_from_list_show_the_structure(page):
    page.locator("#timer-list li", has_text="Rounds").locator(".card-info").click()
    expect(page.locator("#details")).to_be_visible()
    expect(page.locator("#list-view")).to_be_visible()  # opening details doesn't open the timer
    expect(page.locator("#details-name")).to_have_text("Rounds")
    expect(page.locator("#details-meta")).to_have_text("00:35 · 5 intervals")
    assert outline_rows(page) == ["Get ready 00:03", "2 rounds 00:32", "Work 00:10", "Rest 00:06"]
    expect(page.locator("#details .is-current")).to_have_count(0)
    page.locator("#details-close").click()
    expect(page.locator("#details")).to_be_hidden()

    page.locator("#timer-list li", has_text="Short").locator(".card-info").click()
    expect(page.locator("#details-desc")).to_have_text("Two quick intervals")
    assert outline_rows(page) == ["Alpha 00:05", "Beta 00:04"]
    page.go_back()  # the phone's back button
    expect(page.locator("#details")).to_be_hidden()
    expect(page.locator("#list-view")).to_be_visible()


def test_details_from_timer_highlight_current_and_keep_running(page):
    open_timer(page, "Rounds")
    page.locator("#btn-play").click()
    page.clock.run_for(3000 + 16000 + 1000)  # get ready, round 1, then 1 s into round 2's Work
    expect(page.locator("#summary")).to_contain_text("Round 2/2")
    page.locator("#btn-info").click()
    expect(page.locator("#details .is-current .outline-name")).to_have_text("Work")
    expect(page.locator("#details .is-current .outline-dur")).to_have_text("00:10")
    names_x = page.locator("#details .outline-block li.outline-row .outline-name").evaluate_all(
        "els => els.map(e => Math.round(e.getBoundingClientRect().x))")
    assert len(set(names_x)) == 1  # the highlighted row is laid out like its neighbours
    page.keyboard.press("ArrowRight")  # timer shortcuts are off while the dialog is open
    page.clock.run_for(5000)
    expect(page.locator("#cur-name")).to_have_text("Work")
    page.keyboard.press("Escape")
    expect(page.locator("#details")).to_be_hidden()
    expect(page.locator("#timer-view")).to_be_visible()  # Esc closed only the dialog
    expect(page.locator("#time")).to_have_text("00:04")  # kept running underneath
    page.locator("#btn-info").click()
    page.locator("#details").click(position={"x": 5, "y": 5})  # inside the dialog: stays open
    expect(page.locator("#details")).to_be_visible()
    page.mouse.click(5, 5)  # the backdrop
    expect(page.locator("#details")).to_be_hidden()
    expect(page.locator("#timer-view")).to_be_visible()


def test_toolbar_centres_play_and_fits(page):
    open_timer(page, "Short")
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 800})
        play = page.locator("#btn-play").bounding_box()
        assert abs(play["x"] + play["width"] / 2 - width / 2) < 1
        for button in page.locator(".toolbar button").all():
            box = button.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width
        order = [page.locator(f"#btn-{b}").bounding_box() for b in ("close", "reset", "prev", "play", "next", "info")]
        for a, b in zip(order, order[1:]):
            assert a["x"] + a["width"] <= b["x"], "toolbar buttons overlap"
        assert page.evaluate("document.documentElement.scrollWidth") == width


# ---------------------------------------------------------------- interval comments

def test_comments_on_the_timer_screen(page):
    open_timer(page, "Short")
    expect(page.locator("#cur-comment")).to_have_text("Keep it steady")  # trimmed, spaces collapsed
    expect(page.locator("#cur-comment")).to_be_visible()
    expect(page.locator("#next-name")).to_have_text("Beta")
    expect(page.locator("#next-comment")).to_be_hidden()  # Beta has none
    page.locator("#btn-next").click()
    expect(page.locator("#cur-comment")).to_be_hidden()
    page.locator("#btn-prev").click()
    expect(page.locator("#cur-comment")).to_have_text("Keep it steady")
    page.locator("#btn-next").click()
    page.locator("#btn-next").click()
    expect(page.locator("#cur-name")).to_have_text("Done")
    expect(page.locator("#cur-comment")).to_be_hidden()


def test_next_card_shows_the_next_comment_cut_to_the_cap(page):
    open_timer(page, "Rounds")  # Get ready, then Work with a 134-character comment
    expect(page.locator("#next-name")).to_have_text("Work")
    expect(page.locator("#next-comment")).to_be_visible()
    comment = page.locator("#next-comment").text_content()
    assert len(comment) == 100 and comment.endswith("…")
    page.locator("#btn-next").click()
    expect(page.locator("#cur-comment")).to_have_text(comment)
    expect(page.locator("#next-comment")).to_be_hidden()  # Rest has none


def test_comments_in_details(page):
    page.locator("#timer-list li", has_text="Short").locator(".card-info").click()
    rows = page.locator("#details-outline li.outline-row")
    expect(rows.nth(0).locator(".outline-comment")).to_have_text("Keep it steady")
    expect(rows.nth(1).locator(".outline-comment")).to_have_count(0)


def test_timer_layout_with_a_long_comment_fits(page):
    open_timer(page, "Rounds")
    page.locator("#btn-next").click()  # Work: longest comment; next card: Rest
    for width, height in ((390, 844), (320, 640), (844, 390)):
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate("document.documentElement.scrollWidth") == width
        for sel in ("#cur-comment", "#next-card", ".toolbar"):
            box = page.locator(sel).bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, (width, sel)
            assert box["y"] >= 0 and box["y"] + box["height"] <= height + 0.5, (width, sel)
        comment, section = page.locator("#cur-comment").bounding_box(), page.locator(".current").bounding_box()
        assert comment["y"] + comment["height"] <= section["y"] + section["height"] + 0.5, (width, "comment clipped")
        if width < height:  # portrait: the Next card is compact, the current interval gets the space
            assert page.locator("#next-card").bounding_box()["height"] < height * 0.3


def time_text_width(page):
    return page.locator("#time").evaluate(
        "el => { const r = document.createRange(); r.selectNodeContents(el); return r.getBoundingClientRect().width; }")


def test_ring_is_big_and_the_digits_fill_it(page):
    open_timer(page, "Short")
    for width, height in ((412, 839), (390, 844), (320, 640), (844, 390)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(50)
        ring = page.locator(".ring-wrap").bounding_box()
        if width < height:
            assert ring["width"] >= 0.9 * width, (width, ring)  # the ring nearly spans the phone
        assert ring["y"] >= 0 and ring["y"] + ring["height"] <= height
        inner = page.locator(".ring-inner").bounding_box()
        text = time_text_width(page)
        assert 0.9 * inner["width"] <= text <= inner["width"] + 0.5, (width, text, inner)


def test_countdown_digits_refit_for_hours(page):
    long = json.dumps({"timers": [{"name": "Long", "intervals": [{"name": "Hike", "duration": "1:30:00"}]}]})
    save_in_editor(page, long)
    open_timer(page, "Long")
    expect(page.locator("#time")).to_have_text("1:30:00")
    inner = page.locator(".ring-inner").bounding_box()
    assert 0.9 * inner["width"] <= time_text_width(page) <= inner["width"] + 0.5


def test_next_strip_edge_takes_the_next_colour(page):
    open_timer(page, "Short")  # Alpha (red) → Beta (yellow)
    expect(page.locator("#next-card")).to_have_css("border-left-color", "rgb(255, 255, 0)")
    expect(page.locator("#next-name")).to_have_text("Beta")
    expect(page.locator("#next-dur")).to_have_text("00:04")
    page.locator("#btn-next").click()
    expect(page.locator("#next-name")).to_have_text("Finish")
    expect(page.locator("#next-dur")).to_have_text("")


# ---------------------------------------------------------------- settings, edit mode, import

def stored_timers(page):
    data = json.loads(stored(page)["text"])
    return data["timers"] if isinstance(data, dict) else data


def manage(page):
    open_settings(page)
    page.locator("#manage").click()
    expect(page.locator("#manage-done")).to_be_visible()


def manage_names(page):
    return page.locator(".manage-card .name")


def test_settings_open_and_close(page):
    expect(page.locator("#list-view button", has_text="Upload")).to_have_count(0)  # backup lives in Settings
    expect(page.locator("#keep-awake")).to_be_hidden()
    open_settings(page)
    expect(page.locator("#list-view")).to_be_hidden()
    expect(page.locator("#settings-message")).to_be_empty()
    page.go_back()  # the phone's back button
    expect(page.locator("#list-view")).to_be_visible()
    expect(page.locator("#settings-view")).to_be_hidden()
    open_settings(page)
    page.locator("#settings-back").click()
    expect(page.locator("#list-view")).to_be_visible()


def test_edit_mode_reorders_and_deletes(page):
    manage(page)
    expect(page.locator("#settings-view")).to_be_hidden()
    expect(page.locator("#open-settings")).to_be_hidden()
    expect(page.locator("#open-help")).to_be_hidden()
    expect(page.locator(".timer-item")).to_have_count(0)
    expect(page.locator(".manage-card")).to_have_count(10)  # broken timers too, so they can be fixed or removed
    expect(page.locator(".manage-card").nth(4)).to_contain_text("duration must be")
    expect(page.get_by_role("button", name="Move Short up")).to_be_disabled()
    expect(page.get_by_role("button", name="Move Bad comment down")).to_be_disabled()

    page.get_by_role("button", name="Move Rounds up").click()
    expect(manage_names(page).nth(0)).to_have_text("Rounds")
    expect(page.get_by_role("button", name="Move Rounds down")).to_be_focused()
    assert [t["name"] for t in stored_timers(page)[:2]] == ["Rounds", "Short"]

    page.dialogs.answers.append(False)
    page.get_by_role("button", name="Delete Short").click()
    page.dialogs.wait(page, 1)
    expect(page.locator(".manage-card")).to_have_count(10)
    page.dialogs.answers.append(True)
    page.get_by_role("button", name="Delete Short").click()
    expect(page.locator(".manage-card")).to_have_count(9)
    assert page.dialogs.seen == ['Delete "Short"?', 'Delete "Short"?']
    assert "Short" not in [t.get("name") for t in stored_timers(page) if isinstance(t, dict)]

    page.go_back()  # leaves edit mode for the list, not Settings
    expect(page.locator(".timer-item")).to_have_count(9)
    expect(page.locator("#settings-view")).to_be_hidden()
    expect(names(page).nth(0)).to_have_text("Rounds")

    manage(page)
    page.locator("#manage-done").click()
    expect(page.locator(".timer-item")).to_have_count(9)


def test_delete_the_last_timer_leaves_an_empty_list(page):
    save_in_editor(page, CUSTOM)
    manage(page)
    page.dialogs.answers.append(True)
    page.get_by_role("button", name="Delete Custom").click()
    expect(page.locator(".manage-card")).to_have_count(0)
    page.locator("#manage-done").click()
    expect(page.locator("#empty")).to_be_visible()
    expect(page.locator("#message")).to_be_hidden()
    page.reload()
    expect(page.locator("#empty")).to_be_visible()
    assert stored_timers(page) == []


def test_download_one_timer_and_import_it_back(page):
    manage(page)
    with page.expect_download() as info:
        page.get_by_role("button", name="Download Short").click()
    d = info.value
    assert d.suggested_filename == "klepsydra-short.json"
    one = json.loads(Path(d.path()).read_text())
    assert one == {"timers": [json.loads(FIXTURE_TEXT)["timers"][0]]}

    with page.expect_download() as info:
        page.get_by_role("button", name="Download <img src=x onerror=alert(1)>").click()
    assert info.value.suggested_filename == "klepsydra-img-src-x-onerror-alert-1.json"

    page.locator("#manage-done").click()
    open_settings(page)
    choose_file(page, "#import-input", "short.json", json.dumps(one))
    expect(page.locator("#list-view")).to_be_visible()
    expect(page.locator(".timer-item")).to_have_count(11)
    expect(names(page).nth(10)).to_have_text("Short (2)")  # same name: renamed, nothing replaced
    expect(page.locator("#message")).to_have_text('Imported "Short (2)".')
    expect(page.locator("#message")).to_have_class(re.compile(r"\bok\b"))
    assert stored_timers(page)[10]["intervals"] == one["timers"][0]["intervals"]


def test_import_adds_valid_timers_and_reports_skipped_ones(page):
    open_settings(page)
    incoming = [{"name": "Tabata", "intervals": [{"repeat": 8, "intervals": [
                    {"name": "Work", "duration": 20}, {"name": "Rest", "duration": 10}]}]},
                {"name": "Broken", "intervals": [{"name": "X", "duration": "abc"}]},
                {"name": "Rounds", "intervals": [{"name": "Y", "duration": 3}]}]
    choose_file(page, "#import-input", "mine.json", json.dumps(incoming))  # a bare list works too
    expect(page.locator(".timer-item")).to_have_count(12)
    expect(names(page).nth(10)).to_have_text("Tabata")
    expect(names(page).nth(11)).to_have_text("Rounds (2)")
    expect(page.locator("#message")).to_contain_text('Imported "Tabata", "Rounds (2)".')
    expect(page.locator("#message")).to_contain_text('"Broken" #1: duration must be')
    page.reload()
    expect(page.locator(".timer-item")).to_have_count(12)
    assert page.dialogs.seen == []


@pytest.mark.parametrize("text, error", [
    ("{", "Not valid JSON"),
    ('{"x": 1}', "The file must be a list of timers"),
    ('{"timers": []}', "The file must be a list of timers"),
    ('[{"name": "Nope", "intervals": []}]', "nothing to import"),
    (json.dumps([{"name": f"T{i}", "intervals": [{"name": "X", "duration": 1}]} for i in range(191)]),
     "at most 200 timers"),
])
def test_import_rejected(page, text, error):
    open_settings(page)
    choose_file(page, "#import-input", "x.json", text)
    expect(page.locator("#settings-message")).to_contain_text(error)
    expect(page.locator("#settings-view")).to_be_visible()
    assert stored(page) is None


def test_restore_default_timers_from_settings(page):
    save_in_editor(page, CUSTOM)
    open_settings(page)
    page.dialogs.answers.append(True)
    page.locator("#reset-default").click()
    expect(page.locator("#list-view")).to_be_visible()
    expect(page.locator(".timer-item")).to_have_count(10)
    assert page.dialogs.seen == ["Replace all your timers with the default timers?"]
    assert stored(page) is None  # back on the bundled file, so updates to it arrive


# ---------------------------------------------------------------- timer editor

def editor_rows(page):
    return page.locator("#te-items .te-row .te-name")


def test_create_a_timer_with_the_form(page):
    open_settings(page)
    page.locator("#new-timer").click()
    expect(page.locator("#te-title")).to_have_text("New timer")
    expect(page.locator("#te-name")).to_be_focused()
    expect(editor_rows(page)).to_have_text(["Work", "Rest"])
    expect(page.locator("#te-summary")).to_contain_text("name must be a non-empty string")
    page.locator("#te-save").click()
    expect(page.locator("#te-error")).to_have_text("Give the timer a name.")

    page.locator("#te-name").fill("Tabata")
    page.locator("#te-desc").fill("Eight rounds")
    expect(page.locator("#te-summary")).to_have_text("00:40 · 2 intervals")
    page.locator("#te-items > .te-items > .te-adders").get_by_role("button", name="+ Repeat block").click()
    block = page.locator(".te-block")
    block.get_by_role("button", name="More rounds").click()
    expect(block.locator(".te-count")).to_have_text("× 3")
    expect(page.locator("#te-summary")).to_contain_text("8 intervals")

    block.locator(".te-row").first.click()  # its Work
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#iv-duration")).to_have_value("30")
    expect(page.locator("#iv-countdown")).to_have_value("")  # inherits
    expect(page.locator("#iv-countdown option").first).to_have_text("Inherit (3 s)")
    page.locator("#iv-name").fill("Sprint")
    page.locator("#iv-duration").fill("abc")
    expect(page.locator("#sheet-error")).to_contain_text("duration must be seconds")
    page.locator("#iv-duration").fill("0:20")
    expect(page.locator("#sheet-error")).to_be_empty()
    page.locator("#iv-comment").fill("All out")
    page.locator("#iv-countdown").select_option("5")
    page.get_by_role("radio", name="Red").click()
    expect(page.get_by_role("radio", name="Red")).to_have_attribute("aria-checked", "true")
    page.locator("#sheet-done").click()
    expect(page.locator("#sheet")).to_be_hidden()
    expect(block.locator(".te-row").first).to_contain_text("Sprint")
    expect(block.locator(".te-row").first).to_contain_text("All out")
    expect(block.locator(".te-row").first).to_contain_text("00:20")

    editor_rows(page).nth(1).click()  # the top-level Rest: move it first, then delete it
    page.locator("#sheet-up").click()
    expect(page.locator("#sheet-up")).to_be_disabled()
    page.locator("#sheet-delete").click()
    expect(editor_rows(page)).to_have_text(["Work", "Sprint", "Rest"])

    page.locator("#te-save").click()
    expect(page.locator("#list-view")).to_be_visible()  # New timer took the place of Settings
    expect(names(page).nth(10)).to_have_text("Tabata")
    assert stored_timers(page)[10] == {
        "name": "Tabata", "description": "Eight rounds",
        "intervals": [{"name": "Work", "duration": 30},
                      {"repeat": 3, "intervals": [
                          {"name": "Sprint", "duration": 20, "comment": "All out", "countdown": 5, "color": "#e53935"},
                          {"name": "Rest", "duration": 10}]}]}
    page.reload()
    expect(page.locator(".timer-item").nth(10)).to_contain_text("02:00 · 7 intervals")


def test_edit_keeps_fields_the_form_does_not_know(page):
    custom = {"timers": [{"name": "Custom", "x-note": "keep me", "halfway": True,
                          "intervals": [{"name": "Go", "duration": 5, "x-tag": [1, 2]},
                                        {"repeat": 2, "halfway": False, "intervals": [{"name": "In", "duration": 3}]}]}]}
    save_in_editor(page, json.dumps(custom))
    manage(page)
    page.get_by_role("button", name="Edit Custom").click()
    expect(page.locator("#te-title")).to_have_text("Edit timer")
    expect(page.locator("#te-name")).to_have_value("Custom")
    expect(page.locator("#te-halfway")).to_have_value("true")
    expect(page.locator("#te-options-summary")).to_contain_text("halfway on")
    page.locator("#te-name").fill("Custom 2")
    editor_rows(page).nth(0).click()
    expect(page.locator("#iv-halfway option").first).to_have_text("Inherit (on)")
    page.locator("#iv-duration").fill("90")
    page.locator("#sheet-done").click()
    page.locator(".te-block").get_by_role("button", name="Repeat block options").click()
    expect(page.locator("#sheet-title")).to_have_text("Repeat block")
    expect(page.locator("#bl-halfway")).to_have_value("false")
    page.locator("#bl-repeat").fill("4")
    page.go_back()  # closes the sheet; the change stays in the draft
    expect(page.locator("#sheet")).to_be_hidden()
    expect(page.locator(".te-count")).to_have_text("× 4")

    page.dialogs.answers.append(False)
    page.locator("#te-cancel").click()
    page.dialogs.wait(page, 1)
    assert page.dialogs.seen == ["Discard your changes?"]
    expect(page.locator("#timer-editor")).to_be_visible()

    page.locator("#te-save").click()
    expect(page.locator(".manage-card")).to_have_count(1)  # back in edit mode
    expect(manage_names(page)).to_have_text(["Custom 2"])
    t = stored_timers(page)[0]
    assert t["x-note"] == "keep me" and t["intervals"][0]["x-tag"] == [1, 2]
    assert t["intervals"][0]["duration"] == "1:30"
    assert t["intervals"][1]["repeat"] == 4


def test_cancel_without_changes_and_back_button_discard(page):
    manage(page)
    page.get_by_role("button", name="Edit Short").click()
    page.locator("#te-cancel").click()  # unchanged: no question
    expect(page.locator(".manage-card")).to_have_count(10)
    page.get_by_role("button", name="Edit Short").click()
    page.locator("#te-name").fill("Changed")
    page.dialogs.answers.append(True)
    page.go_back()
    expect(page.locator(".manage-card")).to_have_count(10)
    expect(manage_names(page).nth(0)).to_have_text("Short")
    assert stored(page) is None


def test_json_tab_edits_just_this_timer(page):
    manage(page)
    page.get_by_role("button", name="Edit Short").click()
    page.locator("#te-tab-json").click()
    expect(page.locator("#te-tab-json")).to_have_attribute("aria-pressed", "true")
    assert json.loads(page.locator("#te-json").input_value()) == json.loads(FIXTURE_TEXT)["timers"][0]
    page.locator("#te-json").fill("{")
    page.locator("#te-tab-form").click()
    expect(page.locator("#te-error")).to_contain_text("Not valid JSON")
    expect(page.locator("#te-json")).to_be_visible()
    page.locator("#te-json").fill('{"name": "No intervals"}')
    page.locator("#te-tab-form").click()
    expect(page.locator("#te-error")).to_contain_text('"intervals" list')
    page.locator("#te-save").click()
    expect(page.locator("#te-error")).to_contain_text("intervals must be a non-empty list")

    page.locator("#te-json").fill('{"name": "From JSON", "intervals": [{"name": "Z", "duration": "1:00"}]}')
    page.locator("#te-tab-form").click()
    expect(page.locator("#te-form")).to_be_visible()
    expect(page.locator("#te-name")).to_have_value("From JSON")
    expect(editor_rows(page)).to_have_text(["Z"])
    page.locator("#te-tab-json").click()
    page.locator("#te-json").fill('{"name": "Saved from JSON", "intervals": [{"name": "Z", "duration": 2}]}')
    page.locator("#te-save").click()
    expect(manage_names(page).nth(0)).to_have_text("Saved from JSON")


def test_broken_timers_open_where_they_can_be_fixed(page):
    manage(page)
    page.get_by_role("button", name="Edit Timer 8").click()  # "not an object": only JSON can show it
    expect(page.locator("#te-json")).to_be_visible()
    expect(page.locator("#te-json")).to_have_value('"not an object"')
    expect(page.locator("#te-error")).to_contain_text("must be an object")
    page.locator("#te-cancel").click()

    page.get_by_role("button", name="Edit Bad duration").click()  # right shape, wrong value: the form shows it
    expect(page.locator("#te-form")).to_be_visible()
    expect(page.locator("#te-summary")).to_contain_text("duration must be seconds")
    expect(page.locator("#te-items .te-dur")).to_have_text(["—"])
    editor_rows(page).first.click()
    expect(page.locator("#iv-duration")).to_have_value("abc")
    page.locator("#iv-duration").fill("7")
    page.locator("#sheet-done").click()
    page.locator("#te-save").click()
    expect(page.locator(".manage-card").nth(4)).to_contain_text("00:07 · 1 interval")


def test_hostile_values_in_the_editor_render_as_text(page):
    hostile = {"timers": [{"name": "<img src=x onerror=alert(1)>", "description": "<b onmouseover=alert(3)>b</b>",
                           "intervals": [{"name": "<script>alert(2)</script>", "duration": 2,
                                          "comment": "<img src=y onerror=alert(4)>",
                                          "color": "red;background:url(https://evil.example/)"}]}]}
    save_in_editor(page, json.dumps(hostile))
    manage(page)
    expect(manage_names(page)).to_have_text(["<img src=x onerror=alert(1)>"])
    page.get_by_role("button", name="Edit <img src=x onerror=alert(1)>").click()
    expect(page.locator("#te-name")).to_have_value("<img src=x onerror=alert(1)>")
    expect(editor_rows(page)).to_have_text(["<script>alert(2)</script>"])
    expect(page.locator(".te-comment")).to_have_text(["<img src=y onerror=alert(4)>"])
    expect(page.locator(".te-dot")).to_have_css("background-color", "rgba(0, 0, 0, 0)")  # the bad colour isn't used
    editor_rows(page).first.click()
    expect(page.locator("#iv-name")).to_have_value("<script>alert(2)</script>")
    expect(page.locator("#sheet-error")).to_contain_text("color must be a CSS colour string")
    expect(page.locator("#iv-palette [aria-checked='true']")).to_have_count(0)
    assert page.locator("#timer-editor, #sheet, #timer-list").locator("img, script, b, iframe").count() == 0


def test_editor_screens_fit_a_small_phone(page):
    page.set_viewport_size({"width": 320, "height": 640})
    manage(page)
    assert page.evaluate("document.documentElement.scrollWidth") == 320
    page.get_by_role("button", name="Edit Rounds").click()
    assert page.evaluate("document.documentElement.scrollWidth") == 320
    editor_rows(page).first.click()
    for sel in ("#sheet-done", "#iv-halfway", "#iv-palette"):
        box = page.locator(sel).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 320, sel
