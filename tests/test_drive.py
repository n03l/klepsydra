# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 the Klepsydra authors
"""Google Drive sync, against a stubbed Google sign-in script and a fake Drive with one store per account.

Real Google behaviour (status codes, the account chooser, the consent screen) can't be checked offline: do one
real sync after deploying.
"""
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

FIXTURE_TEXT = (Path(__file__).resolve().parent / "fixtures" / "timers.json").read_text()
CLIENT_ID = "123-test.apps.googleusercontent.com"

# Google Identity Services, as far as Klepsydra uses it. The account signing in is window.__account.
FAKE_GIS = """
window.__prompts = [];
window.__revoked = [];
window.google = { accounts: { oauth2: {
  initTokenClient(cfg) {
    return { requestAccessToken(opts) {
      window.__prompts.push(opts && opts.prompt);
      const account = window.__account || 'alice';
      setTimeout(() => cfg.callback({ access_token: 'token-' + account, expires_in: 3599, scope: cfg.scope }), 0);
    } };
  },
  hasGrantedAllScopes() { return true; },
  revoke(token, done) { window.__revoked.push(token); if (done) done(); },
} } };
"""


def timers_text(*names):
    return json.dumps({"timers": [{"name": n, "intervals": [{"name": "Work", "duration": 5}]} for n in names]},
                      indent=2) + "\n"


class FakeDrive:
    """Files per Google account, reached with 'token-<account>'. Records every write."""

    def __init__(self):
        self.files = {}  # id -> {account, text, version, trashed}
        self.writes = []  # (account, file id)
        self.next_id = 1

    def add(self, account, text):
        fid = f"file{self.next_id}"
        self.next_id += 1
        self.files[fid] = {"account": account, "text": text, "version": 1, "trashed": False}
        return fid

    def edit(self, fid, text):
        """Another device saves."""
        f = self.files[fid]
        f["text"] = text
        f["version"] += 1

    def of(self, account):
        return {fid: f for fid, f in self.files.items() if f["account"] == account and not f["trashed"]}

    def handle(self, route):
        req = route.request
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "authorization, content-type",
                "Access-Control-Allow-Methods": "GET, POST, PATCH"}
        if req.method == "OPTIONS":
            return route.fulfill(status=204, headers=cors)
        auth = req.headers.get("authorization", "")
        m = re.fullmatch(r"Bearer token-(\w+)", auth)
        if not m:
            return route.fulfill(status=401, headers=cors, body="{}")
        account = m.group(1)
        url = urlparse(req.url)
        qs = parse_qs(url.query)

        def reply(obj, status=200):
            route.fulfill(status=status, headers=cors, content_type="application/json", body=json.dumps(obj))

        def desc(fid):
            f = self.files[fid]
            return {"id": fid, "version": str(f["version"]), "trashed": f["trashed"]}

        if url.path == "/drive/v3/about":
            return reply({"user": {"permissionId": f"perm-{account}"}})
        if url.path == "/drive/v3/files" and req.method == "GET":
            assert "appProperties has { key='klepsydra' and value='timers' }" in qs["q"][0]
            return reply({"files": [desc(fid) for fid in reversed(list(self.of(account)))]})
        fm = re.fullmatch(r"/(upload/)?drive/v3/files/(\w+)", url.path)
        if fm:
            fid = fm.group(2)
            if fid not in self.files or self.files[fid]["account"] != account:
                return reply({"error": "notFound"}, 404)
            if req.method == "GET" and qs.get("alt") == ["media"]:
                return route.fulfill(status=200, headers=cors, content_type="application/json",
                                     body=self.files[fid]["text"])
            if req.method == "GET":
                return reply(desc(fid))
            if req.method == "PATCH":
                self.edit(fid, req.post_data)
                self.writes.append((account, fid))
                return reply(desc(fid))
        if url.path == "/upload/drive/v3/files" and req.method == "POST":
            boundary = re.search(r"boundary=(\S+)", req.headers["content-type"]).group(1)
            parts = req.post_data.split(f"--{boundary}")
            meta = json.loads(parts[1].split("\r\n\r\n", 1)[1])
            assert meta["appProperties"] == {"klepsydra": "timers"}
            assert meta["name"] == "Klepsydra timers.json"
            text = parts[2].split("\r\n\r\n", 1)[1][:-2]  # drop the CRLF before the closing boundary
            fid = self.add(account, text)
            self.writes.append((account, fid))
            return reply(desc(fid))
        raise AssertionError(f"unexpected Drive request {req.method} {req.url}")


@pytest.fixture
def fake_drive():
    return FakeDrive()


def device(browser, server, drive, client_id=CLIENT_ID, ctx=None):
    """A browser profile (one device) with Google stubbed; returns (ctx, page, errors)."""
    if ctx is None:
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
        ctx.route("**/google-client-id.txt",
                  lambda r: r.fulfill(status=200, body=client_id + "\n", content_type="text/plain"))
        ctx.route("https://accounts.google.com/gsi/client",
                  lambda r: r.fulfill(status=200, body=FAKE_GIS, content_type="text/javascript"))
        ctx.route("https://www.googleapis.com/**", drive.handle)
        ctx.errors = []
    pg = ctx.new_page()
    pg.on("console", lambda m: m.type == "error" and ctx.errors.append(m.text))
    pg.on("pageerror", lambda e: ctx.errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    pg.goto(server)
    expect(pg.locator(".timer-item").first).to_be_visible()
    return ctx, pg, ctx.errors


def wait_until(pg, check, timeout_ms):
    """Polls `check` while the page runs (route handlers only run while Playwright waits)."""
    for _ in range(timeout_ms // 100):
        if check():
            return
        pg.wait_for_timeout(100)
    assert check()


def settings(pg):
    pg.locator("#open-settings").click()
    expect(pg.locator("#settings-view")).to_be_visible()


def connect(pg):
    settings(pg)
    pg.locator("#drive-connect").click()


def synced(pg):
    expect(pg.locator("#drive-status")).to_contain_text("Synced")


def names(pg):
    return pg.locator(".timer-item .name").all_text_contents()


def stored(pg):
    return pg.evaluate("JSON.parse(localStorage.getItem('klepsydra:timers')).text")


def save_json(pg, text):
    """Edit all timers as JSON and save (from Settings)."""
    pg.locator("#edit").click()
    pg.locator("#editor-text").fill(text)
    pg.locator("#editor-save").click()
    expect(pg.locator("#list-view")).to_be_visible()


def test_hidden_without_a_client_id(browser, server, fake_drive):
    ctx, pg, errors = device(browser, server, fake_drive, client_id="")
    settings(pg)
    expect(pg.locator("#drive-section")).to_be_hidden()
    expect(pg.locator("#drive-note")).to_be_hidden()
    ctx.close()
    assert errors == []


def test_connect_uploads_then_second_device_takes_it(browser, server, fake_drive):
    ctx, pg, errors = device(browser, server, fake_drive)
    settings(pg)
    expect(pg.locator("#drive-note")).to_be_visible()
    expect(pg.locator("#drive-connect")).to_contain_text("Keeps a copy of your timers")
    pg.locator("#drive-connect").click()
    synced(pg)
    [(fid, f)] = fake_drive.of("alice").items()
    assert f["text"] == FIXTURE_TEXT  # the bundled timers, as shown
    state = pg.evaluate("JSON.parse(localStorage.getItem('klepsydra:drive'))")
    assert state["fileId"] == fid and state["account"] == "perm-alice" and state["version"] == "1"
    assert "token" not in json.dumps(pg.evaluate("Object.assign({}, localStorage)"))  # tokens stay in memory

    # A change is uploaded a little later, while the token lasts.
    pg.locator("#settings-back").click()
    settings(pg)
    save_json(pg, timers_text("Mine"))
    wait_until(pg, lambda: fake_drive.files[fid]["text"] == timers_text("Mine"), 15000)

    # A fresh device (nothing saved) connects: it takes Drive's timers without asking.
    ctx2, pg2, errors2 = device(browser, server, fake_drive)
    connect(pg2)
    synced(pg2)
    pg2.locator("#settings-back").click()
    expect(pg2.locator(".timer-item .name")).to_have_text(["Mine"])
    assert stored(pg2) == timers_text("Mine")
    assert len(fake_drive.of("alice")) == 1

    # Disconnect: the token is revoked, the state forgotten, the file left in Drive.
    settings(pg2)
    pg2.locator("#drive-disconnect").click()
    expect(pg2.locator("#drive-connect")).to_be_visible()
    assert pg2.evaluate("localStorage.getItem('klepsydra:drive')") is None
    assert pg2.evaluate("window.__revoked") == ["token-alice"]
    assert len(fake_drive.of("alice")) == 1
    ctx.close()
    ctx2.close()
    assert errors == [] and errors2 == []


def test_launch_asks_for_a_tap_and_pulls_other_device_changes(browser, server, fake_drive):
    ctx, pg, errors = device(browser, server, fake_drive)
    connect(pg)
    synced(pg)
    [fid] = fake_drive.of("alice")
    fake_drive.edit(fid, timers_text("From phone"))
    pg.reload()
    notice = pg.locator("#sync-notice")
    expect(notice).to_contain_text("waiting for you to sign in")
    notice.locator("button", has_text="Sync now").click()
    expect(notice).to_be_hidden()
    expect(pg.locator(".timer-item .name")).to_have_text(["From phone"])

    # Drive's version moving without a content change is not a change.
    fake_drive.files[fid]["version"] += 5
    writes = len(fake_drive.writes)
    settings(pg)
    pg.locator("#drive-sync").click()
    synced(pg)
    assert len(fake_drive.writes) == writes
    ctx.close()
    assert errors == []


def test_another_account_is_refused(browser, server, fake_drive):
    ctx, pg, errors = device(browser, server, fake_drive)
    connect(pg)
    synced(pg)
    fake_drive.add("bob", timers_text("Bob's"))
    writes = list(fake_drive.writes)
    pg.reload()
    pg.evaluate("window.__account = 'bob'")
    pg.locator("#sync-notice button").click()
    expect(pg.locator("#sync-notice")).to_contain_text("different Google account")
    assert fake_drive.writes == writes  # nothing uploaded to Bob's Drive
    expect(pg.locator(".timer-item").first).to_contain_text("Short")  # and nothing taken from it
    # The next sign-in shows the account chooser.
    pg.evaluate("window.__account = 'alice'")
    pg.locator("#sync-notice button", has_text="Try again").click()
    expect(pg.locator("#sync-notice")).to_be_hidden()
    assert pg.evaluate("window.__prompts")[-1] == "select_account"
    ctx.close()
    assert errors == []


@pytest.mark.parametrize("choice", ["merge", "drive", "device"])
def test_both_changed_asks(browser, server, fake_drive, choice):
    ctx, pg, errors = device(browser, server, fake_drive)
    settings(pg)
    save_json(pg, timers_text("A", "B"))
    connect(pg)
    synced(pg)
    [fid] = fake_drive.of("alice")
    fake_drive.edit(fid, timers_text("A", "C"))
    pg.locator("#settings-back").click()
    settings(pg)
    # Restore default timers on a synced device is a change too: it must not be undone silently.
    pg.locator("#reset-default").click()
    expect(pg.locator("#list-view")).to_be_visible()
    pg.reload()
    pg.locator("#sync-notice button").click()
    notice = pg.locator("#sync-notice")
    expect(notice).to_contain_text("Drive has changes from another device")
    expect(pg.locator(".timer-item").first).to_contain_text("Short")  # still this device's
    notice.locator("button", has_text="Review").click()
    sheet = pg.locator("#sync-sheet")
    expect(sheet).to_be_visible()
    expect(sheet).to_contain_text("Drive has 2 timers; this device has 10 timers.")
    pg.locator(f"#sync-{choice}").click()
    expect(sheet).to_be_hidden()
    expect(notice).to_be_hidden()
    default_names = [json.loads(FIXTURE_TEXT)["timers"][i]["name"] for i in range(3)]
    if choice == "merge":
        assert names(pg)[-2:] == ["A", "C"] and names(pg)[:3] == default_names
        assert fake_drive.files[fid]["text"] == stored(pg)
    elif choice == "drive":
        assert names(pg) == ["A", "C"]
        assert fake_drive.files[fid]["text"] == timers_text("A", "C")
    else:
        assert names(pg)[:3] == default_names
        assert fake_drive.files[fid]["text"] == FIXTURE_TEXT
    # Back from the sheet left no stray history entry: back now leaves nothing open.
    ctx.close()
    assert errors == []


def test_merge_keeps_a_timer_changed_on_both_sides_twice(browser, server, fake_drive):
    ctx, pg, errors = device(browser, server, fake_drive)
    settings(pg)
    save_json(pg, timers_text("A"))
    connect(pg)
    synced(pg)
    [fid] = fake_drive.of("alice")
    other = json.loads(timers_text("A"))
    other["timers"][0]["intervals"][0]["duration"] = 9
    fake_drive.edit(fid, json.dumps(other))
    pg.locator("#settings-back").click()
    settings(pg)
    save_json(pg, timers_text("A", "B"))
    pg.reload()
    pg.locator("#sync-notice button").click()
    pg.locator("#sync-notice button", has_text="Review").click()
    pg.locator("#sync-merge").click()
    expect(pg.locator("#sync-sheet")).to_be_hidden()
    assert names(pg) == ["A", "B", "A (2)"]
    ctx.close()
    assert errors == []


def test_drive_changing_while_the_choice_is_open_asks_again(browser, server, fake_drive):
    ctx, pg, errors = device(browser, server, fake_drive)
    connect(pg)
    synced(pg)
    [fid] = fake_drive.of("alice")
    fake_drive.edit(fid, timers_text("Phone 1"))
    pg.locator("#settings-back").click()
    settings(pg)
    save_json(pg, timers_text("Here"))
    pg.reload()
    pg.locator("#sync-notice button").click()
    pg.locator("#sync-notice button", has_text="Review").click()
    fake_drive.edit(fid, timers_text("Phone 2"))
    writes = list(fake_drive.writes)
    pg.locator("#sync-device").click()
    expect(pg.locator("#sync-sheet-error")).to_contain_text("Drive changed again on another device")
    assert fake_drive.writes == writes
    assert fake_drive.files[fid]["text"] == timers_text("Phone 2")
    expect(pg.locator("#sync-sheet-text")).to_contain_text("Drive has 1 timer;")
    pg.locator("#sync-drive").click()
    expect(pg.locator("#sync-sheet")).to_be_hidden()
    assert names(pg) == ["Phone 2"]
    ctx.close()
    assert errors == []


@pytest.mark.parametrize("bad", ["not json", json.dumps({"no": "timers"}), "[" + " " * (1024 * 1024) + "]"])
def test_a_bad_drive_file_changes_nothing(browser, server, fake_drive, bad):
    fake_drive.add("alice", bad)
    ctx, pg, errors = device(browser, server, fake_drive)
    connect(pg)
    expect(pg.locator("#drive-message")).to_contain_text("Couldn't sync with Drive: The file in Drive")
    assert pg.evaluate("localStorage.getItem('klepsydra:timers')") is None
    expect(pg.locator(".timer-item").first).to_contain_text("Short")
    assert fake_drive.writes == []
    ctx.close()
    assert errors == []


def test_hostile_drive_file_is_shown_as_text(browser, server, fake_drive):
    fid = fake_drive.add("alice", timers_text('<img src=x onerror="window.__xss=1">'))
    ctx, pg, errors = device(browser, server, fake_drive)
    connect(pg)
    synced(pg)
    pg.locator("#settings-back").click()
    expect(pg.locator(".timer-item .name")).to_have_text(['<img src=x onerror="window.__xss=1">'])
    assert pg.evaluate("window.__xss") is None
    assert fake_drive.files[fid]["version"] == 1
    ctx.close()
    assert errors == []


def test_older_tab_stops_saving_and_syncing(browser, server, fake_drive):
    ctx, old, errors = device(browser, server, fake_drive)
    connect(old)
    synced(old)
    _, new, _ = device(browser, server, fake_drive, ctx=ctx)
    expect(old.locator("#settings-message")).to_contain_text("open in another tab")
    expect(old.locator("#drive-sync")).to_be_disabled()
    old.locator("#settings-back").click()
    settings(old)
    old.locator("#edit").click()
    old.locator("#editor-text").fill(timers_text("Stale"))
    old.locator("#editor-save").click()
    expect(old.locator("#editor-error")).to_contain_text("open in another tab")
    assert new.evaluate("localStorage.getItem('klepsydra:timers')") is None
    ctx.close()
    assert errors == []


def test_conflict_sheet_and_notice_layout(browser, server, fake_drive, tmp_path):
    """Screenshots for a look (in pytest's tmp dir); fails on overflow."""
    ctx, pg, errors = device(browser, server, fake_drive)
    connect(pg)
    synced(pg)
    pg.screenshot(path=str(tmp_path / "settings.png"), full_page=True)
    [fid] = fake_drive.of("alice")
    fake_drive.edit(fid, timers_text("Phone"))
    pg.locator("#settings-back").click()
    settings(pg)
    save_json(pg, timers_text("Here"))
    pg.reload()
    expect(pg.locator("#sync-notice")).to_be_visible()
    pg.screenshot(path=str(tmp_path / "signin.png"))
    pg.set_viewport_size({"width": 1280, "height": 800})
    pg.screenshot(path=str(tmp_path / "signin-desktop.png"))
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.locator("#sync-notice button").click()
    expect(pg.locator("#sync-notice")).to_contain_text("Drive has changes")
    pg.screenshot(path=str(tmp_path / "notice.png"))
    pg.locator("#sync-notice button", has_text="Review").click()
    pg.screenshot(path=str(tmp_path / "sheet.png"))
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
    pg.go_back()
    expect(pg.locator("#sync-sheet")).to_be_hidden()
    expect(pg.locator("#list-view")).to_be_visible()
    print("screenshots in", tmp_path)
    ctx.close()
    assert errors == []
