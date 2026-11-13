# Parity checklist: Python app → Tauri rewrite

Source of truth: the rewrite specification (`ytdl-spec.md`, sections 1–7) and the
old Python sources, which stay in the repository until parity is verified.

Status legend:

- **B** — implemented in the backend (`src-tauri/src`); the UI only renders it or calls the named command.
- **U** — belongs to the interface; the UI engineer builds exactly what the "UI must" column says.
- **B+U** — backend part done (symbol given); the UI part is listed and still to build.
- **C** — cannot be kept; reason given.

Contract files: `src/lib/types.ts` (types and option lists), `src/lib/api.ts` (commands, events,
dialogs, clipboard), `src/lib/mockBackend.ts` (used automatically outside Tauri), `src/i18n/`
(106 keys × 10 languages, converted from `i18n.py`).

Totals: **B 49 · B+U 26 · U 34 · C 1** (110 items: 1–109 plus 62a).

## 1.1 Start-up and the window

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 1 | Windows AppUserModelID `RamazanBerk20.YouTubeDownloader`, errors ignored | B | `lib::set_app_user_model_id` (Windows, unverified here) |
| 2 | Strip `__NV_PRIME_RENDER_OFFLOAD`, `__GLX_VENDOR_LIBRARY_NAME`, `__VK_LAYER_NV_optimus` before the GUI; re-inject only into ffmpeg | B | `transcode::capture_and_strip_nvidia_env` (first line of `lib::run`), `transcode::ffmpeg_env` |
| 3 | Child cleanup armed before any work | B | `process::command`/`process::spawn` (own process group + `PR_SET_PDEATHSIG` on Linux, kill-on-close Job Object per child on Windows), `Backend::shutdown` → `process::kill_all` on `RunEvent::Exit` |
| 4 | GUI construction failure → `Failed to start GUI: <e>` on stderr, exit 1 | B | `lib::run` |
| 5 | Dark mode only, no theme switch | B+U | `tauri.conf.json` window `theme: "Dark"`. UI must: dark palette only, no light mode, no switch. **Built (changed on request):** dark *and* light palettes as CSS tokens (`styles.css`), following the system with a manual override (header toggle in `App`, Theme select in `SettingsSheet`, remembered in localStorage). `tauri.conf.json` no longer forces `theme: "Dark"`; `api.setWindowTheme` (capability `core:window:allow-set-theme`) keeps the native title bar in step. The owner's brief asked for this, overriding the old dark-only rule. |
| 6 | 1100×880 default, 820×700 minimum | B | `tauri.conf.json` |
| 7 | Window/taskbar icon from `icon.ico`/`icon.png` | B | `src-tauri/icons/` generated from `icon.png`; original `icon.ico` reused |
| 8 | Title = localised `app.title`, live on language change | U | On bootstrap and every language change: `api.setWindowTitle(t(lang, "app.title"))`. **Built:** `App` (language effect → `api.setWindowTitle`). |
| 9 | No hardware-encoder probe at start-up; lazy on first encode | B | `transcode::EncoderSession::encoders` / `cuda` (probed on the task thread at the first encode, cached) |
| 10 | Start-up order (settings → language → UI → ffmpeg banner → update check → poll) | B+U | `Backend::new` (settings, autodetect, `log.ready`), `lib::start_background` (password migration, ffmpeg banner, restored queue, update check, yt-dlp install/update) — none blocks the first paint. UI must: subscribe with `onEvent` **before** `api.getBootstrap()`, apply `settings.language`, then the enable states of items 50 and 52. **Built:** `state/store.ts` `start()` subscribes, then bootstraps, replaying events that raced the call; the shell paints at once with a loading state (`Queue` `queue-loading`). |

## 1.2 Layout

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 11 | Banner stack shown only when non-empty | U | Render `Bootstrap.banners` then `bannerShown` (replace same `kind` in place, else append) / `bannerDismissed`. Collapse the area when empty. **Built:** `components/Banners.tsx` (renders nothing when empty). |
| 12 | Header: bold 22 pt `header.title`; language menu (width 140) with native names | U | Options `LANGUAGES` in that order labelled `NATIVE_NAMES[code]` (from `src/i18n`). On change: `api.updateSettings({ language })`, re-render everything (item 109). **Built:** `App` top bar: title + language `<select>` with native names (header title is 15 px semibold in the new design, not 22 pt). |
| 13 | URL box: multi-line, 78 px, no wrap, 12 pt, label `field.url`, placeholder `field.url.placeholder` | U | `<textarea wrap="off">`; use the real `placeholder` attribute (never placeholder text inside the value — fixes BUG #5). **Built:** `components/Composer.tsx` (`<textarea wrap="off">`, real `placeholder`). |
| 14 | Paste button (width 90) | U | `api.readClipboard()`; null/empty after trim → nothing; else append to the existing text on a new line (if text is non-empty) and add a trailing newline. No validation. **Built:** `Composer` Paste + `lib/urls.ts` `appendPasted` (existing text stripped first, as app.py:1738-1741; corrected in verification). |
| 15 | Add button (width 90, height 38, bold) | U | Calls the Add flow of 1.4. **Built:** `Composer` Add (primary button). |
| 16 | Ctrl+Return in the URL box = Add | U | `keydown` Ctrl+Enter (also ⌘+Enter on macOS) in the URL box. **Built:** `Composer` (Ctrl+Enter and ⌘+Enter). |
| 17 | Save-to entry; initial = saved `output_dir` else `~/Downloads` | B+U | Backend resolves blank to `~/Downloads` (`Backend::output_dir`). UI must: show `settings.outputDir` or, when blank, `Bootstrap.defaultOutputDir` as placeholder; commit with `updateSettings({ outputDir })` on blur/Enter. **Built:** `components/OptionsBar.tsx` Save-to (`TextSetting`, placeholder = `defaultOutputDir`, commit on blur/Enter). |
| 18 | Browse opens a folder picker at the current value or ~/Downloads; Cancel keeps the value | U | `api.pickFolder(current || defaultOutputDir)`; null = unchanged. **Built:** `OptionsBar` Browse → `api.pickFolder`. |
| 19 | Options row 1: `label.format`, radios `radio.video`/`radio.audio`, Quality menu (140), Max-concurrent editable combo (110) | U | Quality options: `VIDEO_QUALITIES` / `AUDIO_QUALITIES` by mode. Max concurrent: editable combobox with `CONCURRENCY_PRESETS` (show 0 as `∞`); on commit `updateSettings({ maxConcurrent: parseConcurrency(text) })` and re-display the parsed value. Label `label.max_concurrent`. **Built:** `OptionsBar` (format segmented control, Quality) and the Max-concurrent `Combo` in the `Queue` toolbar (changeable while the queue runs). |
| 20 | Options row 2: Codec menu (180), `check.playlist`, Advanced toggle (120, grey `#3a3a3a`) | U | Codec options `VIDEO_CODECS` / `AUDIO_CODECS` by mode, label `label.codec`. **Built:** `OptionsBar` (Codec, playlist checkbox, Advanced button). |
| 21 | Advanced collapsed at every launch, not persisted; label `button.advanced` / `button.hide_advanced`; values apply while collapsed | U | Local component state only. **Built:** Advanced opens `components/SettingsSheet.tsx`; open state lives in the UI reducer (`state/ui.ts`), never persisted; label toggles `button.advanced`/`button.hide_advanced`. |
| 22 | Advanced row 0: Parallel parts editable combo (110); Browser cookies menu (159) | U | Parts: `FRAGMENT_PRESETS`, `updateSettings({ concurrentFragments: parseFragments(text) })`, label `label.fragments`. Cookies: `COOKIE_CHOICES` (`Auto`, `Off` localised via `optionLabel`), `cookiesBrowser`, label `label.cookies`. **Built:** `SettingsSheet` Download (Parallel parts `Combo`) and Cookies (browser menu). |
| 23 | Advanced row 1, video only: Container (170), Re-encode checkbox, Preset (130); row hidden in audio mode | U | `CONTAINERS` (`None` shows `option.none`), `label.container`; `check.reencode_h264` → `reencode`; `PRESETS` localised, `label.reencode_preset`. Hide the whole row when `mode === "audio"`. **Built:** `SettingsSheet` video-only group (hidden in audio mode, with a hint instead). |
| 24 | Advanced row 2: cookies.txt entry (487) + Browse | U | `label.cookie_file`; Browse `api.pickCookieFile(current)` (dialog titled `dialog.cookie_file.title` where the platform allows; filters already set: "Cookie files" `*.txt`, "All files"). Commit `updateSettings({ cookieFile })`. **Built:** `SettingsSheet` cookies.txt field + Browse. *Verification:* the dialog title and the localised type name were not passed (and `*.cookies` was missing from the filter); now `api.pickCookieFile(initial, title, typeName)` passes both. |
| 25 | Advanced row 3: anyIP checkbox, Username (150), Password (150, masked), Network menu (not localised), Session (150) | B+U | Password is **write-only**: `api.setAnyipPassword(value)` stores it in the keyring (`secrets::set`); the UI never receives it. Show `••••••` placeholder when `anyipPasswordSet`; an empty commit clears it. Other fields: `anyipEnabled`, `anyipUsername` (trimmed by backend), `anyipNetworkType` from `NETWORK_TYPES`, `anyipSessionName` (blank → default by backend). Labels `check.anyip_rescue`, `label.anyip_username/password/network/session`. **Built:** `SettingsSheet` anyIP section; password is write-only (`store.setPassword`, `••••••` placeholder, commits only after typing, explicit "Remove saved password"). |
| 26 | Action row: Cancel all (red `#a02929`, 140), Clear finished (grey, 170), Open folder (140); right: Auto-update checkbox, Check for updates (grey, 150) | B+U | `api.cancelAll()`, `api.clearFinished()`, `api.openOutputFolder()`, `updateSettings({ autoUpdateCheck })` (backend logs `log.auto_update_toggled`), `api.checkAppUpdate()`. Labels `button.cancel_all`, `button.clear_done`, `button.open_folder`, `check.auto_update`, `button.check_updates`. **Built:** Cancel all / Clear finished in the `Queue` toolbar; Open folder beside Browse in `OptionsBar`; Auto-update checkbox and Check for updates in the drawer's Tools tab (`components/Drawer.tsx`). |
| 27 | "Queue" label above a scroll area (bg `#1e1e1e`), `queue.empty` when no rows | U | `label.queue`, `queue.empty`. **Built:** `components/Queue.tsx` (heading, designed empty/loading/all-done states). |
| 28 | Mouse wheel over any row scrolls the queue | U | Native scrolling; do not stop wheel propagation inside rows. **Built:** `Queue` (native scrolling; rows never stop wheel events). |
| 29 | "Log" label above a read-only, 140 px, word-wrapped monospace (11 pt) box | U | `label.log`; render entries as in item 90. **Built:** `Drawer` Log tab (monospace, wrapped, read-only, auto-scroll unless scrolled up). |

## 1.3 Text editing quality-of-life

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 30 | Ctrl+A / Ctrl+Shift+A select all (URL box, output, cookie file, anyIP fields, log) | U | Native in inputs; for the log, select the log's text only. **Built:** native Ctrl+A; Ctrl+Shift+A in `App`'s key handler; in the log, Ctrl+A selects the log only (`lib/textEdit.ts` `selectContents`). |
| 31 | Ctrl+V, Ctrl+Shift+V, Shift+Insert paste replacing the selection | U | Native; add Ctrl+Shift+V handler (insert plain text replacing the selection). **Built:** native Ctrl+V/Shift+Insert; Ctrl+Shift+V in `App` (`replaceRange`, keeps undo). |
| 32 | Ctrl+Backspace deletes the previous word (trailing whitespace first, then alphanumerics/`_`) | U | `keydown` handler implementing exactly that rule (browser default differs slightly on punctuation). **Built:** `lib/textEdit.ts` `wordStartBefore` / `deleteWord`, bound in `App`. |
| 33 | Ctrl+Delete deletes the next word, same rule | U | As 32, forward. **Built:** `lib/textEdit.ts` `wordEndAfter`. |
| 34 | Right-click context menu Cut/Copy/Paste/—/Select all, one at a time, closes on outside click/Escape/blur/after a command | U | Custom menu with `menu.cut`, `menu.copy`, `menu.paste`, `menu.select_all` on `contextmenu` **only** — do not bind middle-click (fixes BUG #23). **Built:** `components/Dialogs.tsx` `ContextMenu` on `contextmenu` only; closes on outside click, Escape, blur, resize, after a command. |
| 35 | Log supports copy/select-all; paste/cut do nothing | U | Read-only element; menu shows Cut/Paste disabled. **Built:** `ContextMenu` on the log: Cut/Paste disabled, Copy/Select all work. |

## 1.4 Adding downloads

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 36 | Placeholder counts as empty | B+U | Real placeholder attribute (item 13); backend receives the raw text. **Built:** `Composer` (real placeholder attribute). |
| 37 | Trim lines, drop empties, keep only `http://`/`https://` (case-insensitive), no de-dup, any site | B | `args::extract_urls` (also strips control characters, BUG #24) |
| 38 | No URL → warning `msg.no_url.title` / `msg.no_url.body`, nothing else | B+U | `Backend::add_tasks` rejects with `AddError { kind: "noUrls" }`; UI shows the warning dialog. **Built:** `Composer` add → `DialogView` `noUrls`. |
| 39 | Output dir = entry or ~/Downloads, `expanduser`, `mkdir -p`; failure → `msg.bad_folder.title` / `msg.bad_folder.body {error}` | B+U | `Backend::add_tasks` → `AddError { kind: "badFolder", error }`; UI shows the error dialog. **Built:** `DialogView` `badFolder`. |
| 40 | Immutable per-task snapshot; container forced None and re-encode off in audio mode; max-concurrent, Auto cookie source and anyIP proxy read at start | B | `args::TaskSpec::snapshot`, `Backend::schedule` (cookie source + proxy per attempt), `log.task_added`. UI must `await` pending `updateSettings` calls before `addTasks` (the snapshot is taken from the backend's settings). **Built:** `Composer` awaits `store.settled()` before `api.addTasks`. |
| 41 | Successful Add clears the URL box and persists preferences | B+U | Settings are saved on every `updateSettings` (`settings::save`). UI clears the box only when `addTasks` resolves. **Built:** UI reducer `addSucceeded` clears the box only after `addTasks` resolves. |
| 42 | Last Add folder remembered for Open folder | B | `Backend::last_out_dir` |

## 1.5 Format options

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 43 | Mode video/audio, default video | B | `options::Mode`, `Settings::default` |
| 44 | Video qualities in order | B | `options::Quality::VIDEO` = `VIDEO_QUALITIES` |
| 45 | Audio qualities in order | B | `options::Quality::AUDIO` = `AUDIO_QUALITIES` |
| 46 | One shared quality; on mode switch reset to Best if not in the new list | U | Send `{ mode, quality }` in one `updateSettings` patch. **Built:** `lib/options.ts` `modePatch` (one patch). |
| 47 | Video codecs, default Auto | B | `options::VideoCodec` = `VIDEO_CODECS` |
| 48 | Audio codecs, default m4a (AAC) | B | `options::AudioCodec` = `AUDIO_CODECS` (`ac3` now works: `transcode::to_ac3`, BUG #2) |
| 49 | Codec remembered per mode | B+U | `Settings.videoCodec` / `audioCodec`; UI shows and edits the one for the current mode. **Built:** `lib/options.ts` `currentCodec` / `codecPatch`. |
| 50 | Quality disabled in audio mode for flac/wav/alac/Original | B+U | Backend ignores quality for them (`args::build`). UI disables the menu when `mode === "audio" && !AUDIO_CODECS_WITH_BITRATE.includes(audioCodec)`. **Built:** `lib/options.ts` `qualityEnabled` → `OptionsBar`. |
| 51 | Containers, default None ("None (keep original)") | B | `options::Container` = `CONTAINERS` |
| 52 | Re-encode checkbox enables Preset | U | Disable Preset while `!reencode`. **Built:** `lib/options.ts` `presetEnabled` → `SettingsSheet`. |
| 53 | Presets Fast/Balanced/Quality, default Balanced | B | `options::Preset` |
| 54 | Container/re-encode/preset hidden in audio mode and forced off in audio tasks | B+U | `TaskSpec::snapshot`; UI hides (item 23). **Built:** `lib/options.ts` `videoOnlyVisible` → `SettingsSheet`. |
| 55 | Playlist checkbox, default off | B | `Settings.playlist` |
| 56 | Only Best/Worst/Auto/Original/Off/None/Fast/Balanced/Quality are localised; values persisted as English keys | B+U | Values are the English keys end to end. UI labels with `optionLabel(lang, key)`. **Built:** every option `<select>` labels with `optionLabel`. |
| 57 | Language switch keeps every dropdown's selection | U | Satisfied by storing keys, not display strings; re-render labels only. **Built:** selections are stored as keys; only labels re-render. |

## 1.6 Concurrency and fragments

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 58 | Max-concurrent presets 1,2,3,4,5,10,∞, editable | U | `CONCURRENCY_PRESETS` (0 = ∞), editable combobox. **Built:** `controls.tsx` `Combo` with `CONCURRENCY_PRESETS` (0 shown as ∞). |
| 59 | `∞`/`inf`/`infinity`/`unlimited` → unlimited; int → max(1,n); garbage → 2 | B | `options::parse_concurrency`; same rule in TS `parseConcurrency` (tested). |
| 60 | Applies immediately; lowering never stops running tasks | B | `Backend::update_settings` → `Backend::schedule` |
| 61 | Saved ≥ 9999 or ≤ 0 shows ∞; default 2 | B+U | Migration maps ≥ 9999 to 0 (`Settings::from_json`); UI shows 0 as `∞`. **Built:** `lib/options.ts` `concurrencyLabel`. |
| 62 | Fragment presets 1–64, editable, max(1,n), garbage → 4, no cap; snapshotted | B | `options::parse_fragments`, `TaskSpec.fragments` → `-N` |
| 62a | Sequential ids; rows in id order | B+U | `Queue.next_id` (from 1 each session; continues across restarts only when the opt-in `restoreQueue` is on); UI sorts rows by `id`. **Built:** `store` keeps `ids` sorted by id. |

## 1.7 Queue model

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 63 | Start queued tasks in id order up to the limit; a retried task keeps its position | B | `Backend::schedule` |
| 64 | Each task on its own thread | B | `supervisor::run` (one thread per attempt) |
| 65 | Row: status icon, grey `#id`, title, status text (110, right-aligned), always-enabled `×`, 8 px bar, 11 pt grey detail line | U | Render from `Task`. **Built:** `Queue` `TaskRow` (icon, `#id`, title, status, retry, ×, bar, detail; thumbnail when a preview has one). |
| 66 | Icons/colours | U | `queued` ⏸ `#888a8e`; `starting`/`fetching`/`downloading`/`postProcessing`/`cancelling` ▶ `#4aa3df`; `done` ✓ `#54b25f`; `failed` ✗ `#d04848`; `cancelled` ⊘ `#888a8e`. **Built:** `Queue` icon map + `.s-*` colour tokens (same hues, tuned per theme). |
| 67 | Title = URL until yt-dlp reports it; ellipsised to 80 chars with `…` | B+U | `Backend::set_title` (from the `before_dl` print); UI shows `task.title ?? task.spec.url`, cut at 80 + `…`. **Built:** `TaskRow` (`title ?? preview title ?? url`, cut at 80 + `…`, full text in the tooltip). |
| 68 | Status text `status.<status>`; encode → `status.encoding`, remux → `status.remuxing`, else running = `status.running` (also during merge/extract/convert) | U | `queued`→`status.queued`; `starting`/`fetching`/`downloading` → `status.running`; `postProcessing` with kind `encode` → `status.encoding`, `remux` → `status.remuxing`, other kinds → `status.running` (new labels optional); `cancelling` → **new key needed** (e.g. `status.cancelling`); `done`/`failed`/`cancelled` → `status.done/failed/cancelled`. **Built:** `lib/format.ts` `statusKey` (new keys for cancelling, merging, extracting, converting, embedding). |
| 69 | Detail line | U | downloading: `` `${percent.toFixed(1)}%   ·   ${speed}   ·   ETA ${eta}` `` ("ETA" not localised; formats items 77–78); postProcessing with `percent`: same with speed `speedX.toFixed(2)+"x"`; indeterminate: empty or a spinner; queued: empty; done: `100%`; failed: `message` with newlines → spaces, cut at 110 + `…`; cancelled: empty. Optional additions from the data: `item.index/item.count`, `downloaded/total`. **Built:** `lib/format.ts` `detailLine` (+ sizes, file n, item n of m, attempt/cookies/proxy). |
| 70 | `×` cancels queued/running; removes done/failed/cancelled; stale row just disappears | B+U | `api.cancelTask(id)` for non-terminal, `api.removeTask(id)` for terminal (`Backend::cancel`, `Backend::remove`); rows leave on `tasksRemoved`. **Built:** `TaskRow` × (cancel when active, remove when terminal). |
| 71 | Cancel all cancels every queued/running task, removes nothing | B | `Backend::cancel_all` |
| 72 | Cancel queued → cancelled at once; running → honoured later | B | `Backend::cancel`: queued → `cancelled`; active → `cancelling` and the supervisor kills the whole tree within ~0.2 s + 2 s grace, then removes partial files (`supervisor::cleanup_partials`) → `cancelled { cleanedUp }`. Works in extraction and merging too (BUG #7). |
| 73 | Clear finished removes done/failed/cancelled in one operation | B | `Backend::clear_finished` |
| 74 | Open folder: last Add dir else entry; missing → nothing; `startfile`/`open`/`xdg-open`; error dialog titled "Open folder" | B+U | `commands::open_output_folder` (`process::open_with_system`, opener reaped — no zombie). UI shows a rejected promise as an error dialog titled "Open folder" (not localised). **Built:** `OptionsBar` Open folder; rejection → `DialogView` titled "Open folder". |
| 75 | ABSENT: pause, reorder, retry, open file, notifications | B | Still absent, except a per-row retry is now available: `api.retryTask(id)` (optional UI). **Built:** per-row Retry in `TaskRow` for failed/cancelled. |

## 1.8 Progress

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 76 | Percent per stream file (video then audio), restarts per playlist item | B | `progress::DlProgress::percent`, `TaskState::Downloading { stream, item }`, `supervisor::run` |
| 77 | Speed: base 1024, `B/s KB/s MB/s GB/s TB/s`, one decimal; missing/0/non-finite/negative → `—` | U | Format `speed` (bytes/s, already null when invalid). **Built:** `lib/format.ts` `formatSpeed`. |
| 78 | ETA `"{h}h {mm}m"` when ≥ 1 h, else `"{m}m {ss}s"`; missing → `—` | U | Format `eta` (seconds). **Built:** `lib/format.ts` `formatEta`. |
| 79 | Finished stream file names logged as `#id  <name>` | B | `supervisor::run` (progress `finished`) |
| 80 | Remux/encode phases with ffmpeg `-progress`: 99.5 cap, 0.5 steps, `N.NNx`, ETA | B | `transcode::run_ffmpeg`, `progress::FfProgress`; remux now also has real progress (duration probed). |
| 81 | Done = 100 % | U | Render `done` as 100 %. **Built:** `lib/format.ts` `barPercent` / `detailLine` (done = 100 %). |
| 82 | ABSENT: item n of m, sizes, merge/extract phases | B | Now provided (`ItemPos`, `downloaded/total`, `PostKind`); showing them is optional. **Built:** shown in `detailLine` (item n of m, downloaded/total, post-processing kind as status). |

## 1.9 Playlists and naming

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 83 | Playlist off → `--no-playlist` | B | `args::build` |
| 84 | Playlist on → `%(playlist_title\|playlist)s/%(playlist_index)03d - %(title).200B [%(id)s].%(ext)s` | B | `args::output_template` |
| 85 | Single video in playlist mode → folder `playlist`, file `NA - …` | B | Same template (kept as is). |
| 86 | Playlist off → `%(title).200B [%(id)s].%(ext)s` | B | `args::output_template` |
| 87 | `--windows-filenames` on every OS, `--no-restrict-filenames` | B | `args::build` |
| 88 | First failing playlist entry aborts the rest | B | Deliberately changed (BUG #12): `--no-abort-on-error`; partial success → `done { itemErrors }`, each logged as `error: …`; a bot-check/cookie error still fails the task so the rescue chain runs. |
| 89 | ABSENT: output template, item selection, archive | B | Download archive now optional (`useDownloadArchive` → `--download-archive`). |

## 1.10 Session log

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 90 | `[HH:MM:SS] message`, auto-scroll, read-only, never trimmed | B+U | `log::SessionLog` (bounded at 5000, BUG #17). UI: `[HH:MM:SS]` from `tsMs` in local time, then `#<taskId>  ` when set, then `t(lang, key, args)` or `text`; auto-scroll; keep at most ~5000 lines. Arg values that are message keys (`state` of `log.auto_update_toggled`) must be translated too: `t(lang, "log.auto_update_toggled", { state: t(lang, args.state) })`. **Built:** `Drawer` Log + `lib/format.ts` `logLine`; `store` keeps ≤ 5000 entries. |
| 91 | `log.ready`, `log.task_added`, `log.task_started`, `log.task_done`, `log.task_failed` (first line, rest indented 3 spaces), `log.task_cancelled` | B | `Backend::new`, `Backend::add_tasks`, `Backend::schedule`, `Backend::finish`, `Backend::cancel` |
| 92 | Per-task `warning: …` / `error: …` lines (ANSI stripped), postprocessor decisions; yt-dlp info/debug hidden | B | `supervisor::run`, `progress::parse_yt_line`, `log::strip_ansi`, `transcode::transcode` messages (same English strings as spec 2.6) |
| 93 | Install/update/cookie/anyIP messages | B | `commands::install`, `commands::check_app`, `commands::apply_app_update`, `Backend::finish` (rescue notes) |

## 1.11 Settings persistence

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 94 | Per-OS location `…/youtube-downloader/config.json` | B | `settings::Paths::detect` (same directory); the new file is `settings.json` beside it; the old `config.json` is left for the old app (password scrubbed). |
| 95 | JSON; bad file → defaults; unknown keys dropped; write errors silent | B | `Settings::from_json` (per-field tolerant), `settings::write_atomic` (temp + rename, BUG #11); write errors are returned to `updateSettings`. |
| 96 | Keys, defaults, coercions | B | `settings::Settings` (same defaults; `max_concurrent` 9999 → 0; password moved out, see 98) |
| 97 | Persist points (Add, close, language, auto-update toggle, restart) | B+U | Superset: every `updateSettings` saves immediately. UI must call `updateSettings` when a control commits (no batching needed). **Built:** `store.updateSettings` per control commit (optimistic, serialised). |
| 98 | No migration; `force_mp4` lost | B | `settings::load` reads the old file once, maps `force_mp4: true` → `MP4`, moves `anyip_password` to the keyring (`lib::start_background`, `secrets::set`, `settings::scrub_legacy_password`). Fixture test: `settings::tests::migrates_old_config_shape`. |
| 99 | Autodetected language written on first persist | B | `Backend::new` + `settings::detect_language` |

## 1.12 Banners

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 100 | Coloured frame, message (wrap 600), optional action button (130, `#3a6dd0`), `×` dismiss; stacked; same key replaces | B+U | `Backend::show_banner` (replaces in place, BUG #10), `api.dismissBanner(kind)`. UI draws them. **Built:** `Banners` (tone colour, wrapped text, action button, × → `api.dismissBanner`). |
| 101 | Kinds and triggers | B+U | Backend triggers: `ffmpegMissing` (red `#7a3030`) at start-up and on an ffmpeg-missing failure; `jsRuntimeMissing` (red) once per session after "requested format is not available" with no deno/node/bun, re-armed after a failed install; `chromiumCookies`, `anyipNotConfigured`, `botDetection` (amber `#7a5a30`) from `rescue::decide`; `appUpdate` (blue `#2a4a78`) from `commands::check_app`; new `ytdlpMissing`. Texts: `banner.ffmpeg_missing`, `banner.js_runtime_missing`, `banner.chromium_cookies`, `banner.anyip_not_configured`, `banner.bot_detection`; for a manual `install` plan show the text plus `banner.manual_install` and `install.hint` (use `banner.no_pm` when the hint starts with "No supported package manager"). Buttons: `button.install` when `install.type === "auto"` → `api.installTool("ffmpeg" | "deno")`; `appUpdate`: `button.update_now` → `applyAppUpdate()` if `canApply` else `openReleasePage()`. **New keys needed:** app-update text with `{version}` (old `banner.update_available` speaks of commits), `ytdlpMissing` text. `git-init` banner: see 106. **Built:** `Banners` (all kinds; install / manual hint / `banner.no_pm`; app update → apply or release page; yt-dlp missing → `api.updateYtdlp`; amber rescue banners open the right settings section). |
| 102 | Install: dismiss, `log.installing`, `log.install_running`, stream output, re-probe, `log.install_done` / `log.install_failed` + banner again; manual → `banner.manual_install` + hint | B | `commands::install`, `tools::plan_for`, `tools::run_plan` (600 s cap over the whole run, `sudo -n`, BUG #8); `install` events for a spinner. |

## 1.13 Updates

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 103 | Background check at start-up when enabled; silent on error/up-to-date; banner when behind | B | `lib::start_background` → `commands::check_app(manual = false)` against GitHub releases (replaces git; see deviations). |
| 104 | Check for updates: `log.update_checking`, then errors `log.update_failed` / `log.update_uptodate` | B | `commands::check_app_update` (`UpdateState::busy` refuses concurrent runs, BUG #13) |
| 105 | Update now: `log.update_running`, run update, `log.update_done` + restart prompt / `log.update_failed` | B+U | `commands::apply_app_update`: download, SHA-256 check against the release's `SHA256SUMS`, replace the AppImage / stage the Windows installer, `log.update_done`, state `readyToRestart`. For deb/rpm/dev installs `canApply` is false → `openReleasePage()`. UI shows the restart prompt on `readyToRestart`. **Built:** `Banners` / `Drawer` Tools "Update now"; restart prompt on `readyToRestart` (`store` signal). |
| 106 | Enable (git-init): `git init` … `reset --hard FETCH_HEAD` | C | Obsolete: the app is a compiled binary, not a git checkout, so connecting a folder to git cannot update it; release-based updates (103–105) replace it. Keys `banner.no_git_checkout`, `button.enable_auto_update`, `log.auto_update_enabl*` stay in the catalogs unused. |
| 107 | Restart prompt `msg.restart.title`/`msg.restart.body`; yes → spawn detached, exit; spawn failure logged | B+U | `commands::restart_app` → `process::spawn_detached_restart` (new session / detached, never registered for exit cleanup, so it survives — BUG #1). UI: yes/no dialog, yes → `api.restartApp()`. **Built:** `DialogView` `restart` → `api.restartApp()`. |

## 1.14 Closing

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 108 | Close never asks; 2 s watchdog; cancel all; partial files left | B+U | Changed per BUG #16: with active tasks the close is held and `closeRequested { activeTasks }` is emitted (`lib::run` `on_window_event`); UI asks (**new key needed**), then `api.confirmExit()`. Exit kills every child tree (`Backend::shutdown`). Only with the opt-in `restoreQueue` setting is the queue kept (interrupted tasks come back queued and yt-dlp resumes their `.part` files); the dialog text says which (`msg.close.body` / `msg.close.body_stop`). *Verification:* the queue used to be restored and auto-started on every launch with no opt-in. With nothing active the window closes at once. **Built:** `closeRequested` → `DialogView` `confirmClose` → `api.confirmExit()`. |

## 1.15 Localisation

| # | Behaviour | Status | Backend symbol / UI must |
|---|---|---|---|
| 109 | Native-name picker; live re-translation of every label, placeholder, option, Advanced button and row; saved at once | B+U | Catalogs `src/i18n/locales/*.json` (106 keys × 10 languages; the 15 keys eight languages lacked are filled), `t()`, `optionLabel()`, `LANGUAGES`, `NATIVE_NAMES`. Do **not** pre-shape Arabic (the webview shapes bidi natively). Unknown language → English. **Built:** language picker (top bar and settings); non-English catalogs load on demand (`i18n` `loadLanguage`, `store.syncLang`); Arabic sets `dir="rtl"` and the CSS uses logical properties only. |

## Spec sections 2–7 (not numbered in the checklist)

- **yt-dlp contract (2.1–2.5):** `args::build` with `--ignore-config`, `--color never`, `--no-mtime`, `-R 5`, `--fragment-retries 5`, `--remote-components ejs:github`, explicit `--js-runtimes`, `--abort-on-error` / `--no-abort-on-error`, selectors incl. the source-preference overrides (`args::video_format`), audio `-x --audio-format --audio-quality`, no `--merge-output-format`, `--` before the URL. Golden files: `src-tauri/tests/golden/*.txt`.
- **Transcode (2.6):** `transcode::{decide, remux_args, copy_video_args, encode_args, encoder_order, transcode, encode, run_ffmpeg, free_output_path}`.
- **Error capture and rescue (3):** `task::classify`, `task::friendly_message`, `rescue::decide`, `Backend::finish`.
- **Browser detection (4.1):** `browser::{jar_dir, map_linux_desktop, map_windows_progid, candidates_with}`.
- **Installer (4.2):** `tools::{plan_for, run_plan}`; deno hint no longer pipes curl into sh (BUG #26).
- **Updater (4.3):** replaced by `updater` (app) and `ytdlp` (managed yt-dlp).
- **start.sh / start.bat (4.4–4.5):** obsolete for the packaged app (Tauri bundles; yt-dlp is managed). Their tool checks live on as the ffmpeg/JS-runtime banners and `get_tools_status`. The old scripts remain in the repo for the Python app.
- **State model (6.8):** `task::TaskState` + `task::transition` (exhaustive table test), `task::Attempt`, `rescue::SessionRescue`, `transcode::EncoderSession`, `updater::UpdateState`, `ytdlp::YtdlpPhase`.

### Section 7 bugs

| Bug | Fix |
|---|---|
| 1 restart killed by cleanup | `process::spawn_detached_restart` (not registered; own session / breakaway) |
| 2 ac3 fails | `args::post_plan` → `PostPlan::Ac3` → `transcode::to_ac3` |
| 3 "remux" re-encodes | `PostPlan::Transcode { remuxFirst: true }`: stream copy first, encode only if the copy fails. *Verification:* Opus/Vorbis audio is never copied into MP4/MOV (the old app avoided that on purpose); it is converted to AAC while the video is copied (`transcode::container_change_args`). |
| 4 plaintext password | `secrets` (keyring / 0600 file), proxy only in child env (`args::proxy_env`), `log::redact` everywhere |
| 5 placeholder corrupts input | UI uses a real placeholder attribute (item 13) |
| 6 phase not reset between items | `transition` on `TaskEvent::Item` → `fetching` |
| 7 cancel unreliable | tree kill in every phase + `supervisor::cleanup_partials`; `cancelling` always ends `cancelled` |
| 8 installer hangs | `tools::run_plan` overall timeout, `sudo -n` |
| 9 rescue races | `rescue::decide` (shared pending candidate, per-attempt proxy flag, task's own cookie choice) |
| 10 banner overlap | `Backend::show_banner` replaces in place |
| 11 non-atomic config | `settings::write_atomic` |
| 12 playlist aborts | `--no-abort-on-error` + `itemErrors` |
| 13 no integrity, unmanaged yt-dlp, concurrent updates | `ytdlp::Ytdlp` (SHA2-256SUMS, per-session re-verify, daily update, busy lock), `updater` (SHA256SUMS), `UpdateState::busy` |
| 14 start.bat PATH | obsolete; `process::which` also searches winget's `Links` dir |
| 15 start.sh auto-confirms | obsolete (no launcher script) |
| 16 close asks nothing | `closeRequested` + `confirm_exit` |
| 17 unbounded log | `log::SessionLog` (5000) |
| 18 ffmpeg message misses cases | `task::classify` matches both messages |
| 19 `%` in output dir | yt-dlp runs with the output dir as cwd; template is relative |
| 20 encodes overwrite | `transcode::free_output_path` |
| 21 ffmpeg error lines dropped | stderr read separately from `-progress` |
| 22 orphans/zombies | PDEATHSIG, process groups, Job Objects, reaped openers |
| 23 middle-click hijacked | UI: context menu on right-click only |
| 24 argument injection | argv lists only, `--` before URL, http(s) check, control chars stripped, `.exe` only on Windows |
| 25 remote text in the UI | UI must render titles/errors/log as text (React escapes; never `dangerouslySetInnerHTML`); CSP forbids inline script |
| 26 curl \| sh hint | `tools::plan_for` deno hint |

## UI build status

All 60 UI items (26 B+U, 34 U) are built; each row above names its component. None is marked C on the UI side.
The three additions the backend supports are built too:

- **Metadata preview** before queueing: `Composer` Preview button → `PreviewCard` (thumbnail, title, uploader, duration, approx. size, sizes per height, audio size, playlist entries); results are cached in `store.previews` so queue rows show the thumbnail/title.
- **Persistent queue / history**: opt-in (`restoreQueue`, off by default, `SettingsSheet` › Extras); when on, restored tasks arrive in `Bootstrap.tasks` and render like any row. The download-archive option is in the same section.
- **Embed options**: subtitles (+ languages, enabled only with subtitles), chapters, thumbnail, metadata, SponsorBlock in `SettingsSheet` › Extras.
- **Tools**: yt-dlp version/source/latest/last check/phase, "Update yt-dlp now", daily-update checkbox, ffmpeg/ffprobe/JS-runtime status with Install, app version and update state in the drawer's Tools tab.

Contract addition: `api.setWindowTheme` (transport only, Tauri `setTheme`; capability `core:window:allow-set-theme`). No Rust command was needed.

## New strings the UI engineer must add

**Done:** 100 keys added to all ten catalogs (206 keys each; `check.restore_queue` and `msg.close.body_stop` added in verification), translated, placeholders checked by `src/test/contract.test.ts`.


`status.cancelling`; labels for the new post-processing kinds if shown (merging, extracting audio, converting, embedding); app-update banner with `{version}`; yt-dlp-missing banner; close-while-downloading confirmation; retry button; preview panel; the extras (archive, embed subtitles + languages, chapters, thumbnail, metadata, SponsorBlock); "Update yt-dlp now" button and the yt-dlp daily-update checkbox (`ytdlpAutoUpdate`); yt-dlp status (version, source). Add them to all ten catalogs.

## Independent verification (corrections)

What the ledger above had overstated, and what was fixed:

- Persistent queue was always on (restored and auto-started at launch, ids continued): now opt-in `restoreQueue`, default off.
- Container MP4/MOV without re-encode stream-copied Opus audio into MP4: now converted to AAC (video still copied).
- Item 24: cookie dialog had no title, an English type name and no `*.cookies` filter: fixed.
- Item 14: Paste kept leading/trailing whitespace of the box: now stripped as before.
- Spec 5.3: autodetect ignored `LC_CTYPE` (what Python's `locale.getlocale()` reads): now the LC_ALL/LC_CTYPE/LANG candidate comes first (`settings::posix_locale_candidates`).
- Spec 2.6: the info line "Retrying NVENC with CPU decode" before the CPU-decode retry was missing: added.
- Spec 2.6 says `ffmpeg -encoders` is probed on each encode; the rewrite caches it per session (`EncoderSession`). Kept; harmless.

yt-dlp contract checked mechanically: 9 288 argument vectors (mode × codec × quality × container × re-encode × playlist × cookie source) parsed with yt-dlp 2026.10.26's own `parse_options` and compared with the old `_build_opts` dictionaries. Identical in format selector (apart from de-duplicating `best/best`), output template, retries, fragment retries, fragments, `windowsfilenames`/`restrictfilenames`, `noplaylist`, JS runtimes, remote components, cookies and mtime. Intended differences only: `ignoreerrors` for playlists (BUG #12), no `FFmpegVideoConvertor` (BUG #3), no ac3 `FFmpegExtractAudio` (BUG #2), progress output on (needed for the template).
