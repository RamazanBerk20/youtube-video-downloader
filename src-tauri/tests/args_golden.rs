//! Golden tests for the yt-dlp argument builder over the option matrix of
//! spec section 2. Regenerate with `UPDATE_GOLDEN=1 cargo test --test args_golden`
//! and review the diff.

use std::fmt::Write;
use std::path::PathBuf;
use ytdl_lib::args::{AttemptArgs, TaskSpec, build, post_plan};
use ytdl_lib::options::*;
use ytdl_lib::settings::Settings;

fn spec() -> TaskSpec {
    TaskSpec::snapshot(
        "https://www.youtube.com/watch?v=abc",
        std::path::Path::new("/out"),
        &Settings::default(),
    )
}

fn tail_after(argv: &[String], flag: &str) -> String {
    let i = argv.iter().position(|a| a == flag).expect("flag present");
    argv[i..].join(" ")
}

fn golden(name: &str, actual: &str) {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/golden")
        .join(name);
    if std::env::var_os("UPDATE_GOLDEN").is_some() {
        std::fs::write(&path, actual).unwrap();
    }
    let expected =
        std::fs::read_to_string(&path).expect("golden file exists; run with UPDATE_GOLDEN=1");
    assert!(
        expected == actual,
        "golden mismatch for {name}; rerun with UPDATE_GOLDEN=1 and review the diff"
    );
}

#[test]
fn video_matrix() {
    let mut out = String::new();
    for q in Quality::VIDEO {
        for c in [
            VideoCodec::Auto,
            VideoCodec::H264,
            VideoCodec::Vp9,
            VideoCodec::Av1,
        ] {
            for container in [
                Container::None,
                Container::Mp4,
                Container::Mkv,
                Container::Webm,
                Container::Mov,
                Container::Avi,
                Container::Flv,
                Container::Mpeg,
                Container::Wmv,
            ] {
                for reencode in [false, true] {
                    let mut s = spec();
                    s.quality = q;
                    s.video_codec = c;
                    s.container = container;
                    s.reencode = reencode;
                    let argv = build(&s, &AttemptArgs::default());
                    let f = &argv[argv.iter().position(|a| a == "-f").unwrap() + 1];
                    let plan = serde_json::to_string(&post_plan(&s)).unwrap();
                    writeln!(
                        out,
                        "{q:?} {c:?} {container:?} reencode={reencode}\n  -f {f}\n  post {plan}"
                    )
                    .unwrap();
                }
            }
        }
    }
    golden("video_matrix.txt", &out);
}

#[test]
fn audio_matrix() {
    let mut out = String::new();
    for c in [
        AudioCodec::M4a,
        AudioCodec::Mp3,
        AudioCodec::Opus,
        AudioCodec::Vorbis,
        AudioCodec::Flac,
        AudioCodec::Wav,
        AudioCodec::Alac,
        AudioCodec::Ac3,
        AudioCodec::Original,
    ] {
        for q in Quality::AUDIO {
            let mut s = spec();
            s.mode = Mode::Audio;
            s.audio_codec = c;
            s.quality = q;
            let argv = build(&s, &AttemptArgs::default());
            let plan = serde_json::to_string(&post_plan(&s)).unwrap();
            writeln!(
                out,
                "{c:?} {q:?}\n  {}\n  post {plan}",
                tail_after(&argv, "-f")
            )
            .unwrap();
        }
    }
    golden("audio_matrix.txt", &out);
}

#[test]
fn full_argv_cases() {
    let mut out = String::new();
    let mut case = |name: &str, s: TaskSpec, a: AttemptArgs| {
        let argv = build(&s, &a);
        writeln!(out, "## {name}").unwrap();
        for x in argv {
            writeln!(out, "  {x}").unwrap();
        }
    };
    case("defaults", spec(), AttemptArgs::default());
    let mut p = spec();
    p.playlist = true;
    p.fragments = 16;
    case("playlist, 16 fragments", p, AttemptArgs::default());
    case(
        "browser cookies, js runtimes, ffmpeg dir",
        spec(),
        AttemptArgs {
            cookie_browser: Some(Browser::Firefox),
            js_runtimes: vec![
                ("deno".into(), "/usr/bin/deno".into()),
                ("node".into(), "/usr/bin/node".into()),
            ],
            ffmpeg_dir: Some("/usr/bin".into()),
            ..Default::default()
        },
    );
    case(
        "cookies.txt copy + archive",
        spec(),
        AttemptArgs {
            cookie_copy: Some("/data/tmp/cookies-1.txt".into()),
            archive_file: Some("/data/download-archive.txt".into()),
            ..Default::default()
        },
    );
    let mut x = spec();
    x.extras.embed_subtitles = true;
    x.extras.subtitle_langs = "en,tr".into();
    x.extras.embed_chapters = true;
    x.extras.embed_thumbnail = true;
    x.extras.embed_metadata = true;
    x.extras.sponsorblock_remove = true;
    case("all extras", x.clone(), AttemptArgs::default());
    x.mode = Mode::Audio;
    case(
        "all extras in audio mode (no subs)",
        x,
        AttemptArgs::default(),
    );
    let mut d = spec();
    d.url = "https://example.com/--exec=rm -rf".into();
    case(
        "hostile-looking url stays after --",
        d,
        AttemptArgs::default(),
    );
    golden("full_argv.txt", &out);
}

#[test]
fn invariants() {
    // No user path or proxy ever reaches argv; URL is always last after `--`.
    let mut s = spec();
    s.out_dir = "/home/u/%(id)s/$HOME".into();
    let argv = build(&s, &AttemptArgs::default());
    assert!(!argv.iter().any(|a| a.contains("/home/u")));
    assert!(!argv.iter().any(|a| a.contains("anyip") || a == "--proxy"));
    assert_eq!(
        &argv[argv.len() - 2..],
        ["--", "https://www.youtube.com/watch?v=abc"]
    );
    assert!(argv.contains(&"--ignore-config".to_owned()));
    assert!(!argv.contains(&"--merge-output-format".to_owned()));
    assert!(
        !argv.contains(&"--recode-video".to_owned()),
        "BUG #3: no hidden re-encode"
    );
}
