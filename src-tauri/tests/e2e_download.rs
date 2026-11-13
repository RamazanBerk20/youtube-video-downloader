//! Opt-in end-to-end test with the real yt-dlp (managed download from GitHub,
//! falling back to PATH) and a short public video:
//! `cargo test --test e2e_download -- --ignored --nocapture`

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use ytdl_lib::backend::{AppEvent, Backend};
use ytdl_lib::settings::Paths;
use ytdl_lib::task::TaskState;

#[test]
#[ignore = "network: downloads yt-dlp and a short public video"]
fn downloads_a_short_video_and_converts_container() {
    let dir = std::env::temp_dir().join(format!("ytdl-e2e-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let paths = Paths {
        config_dir: dir.join("cfg"),
        data_dir: dir.join("data"),
    };
    let events = Arc::new(Mutex::new(0usize));
    let ev = events.clone();
    let (b, _) = Backend::new(
        paths,
        Box::new(move |e: &AppEvent| {
            *ev.lock().unwrap() += 1;
            if let AppEvent::Log { entry } = e {
                eprintln!("{:?}", entry.message);
            }
        }),
    );
    let out = dir.join("out");
    let patch = serde_json::json!({
        "outputDir": out.to_string_lossy(), "quality": "360p", "container": "MKV",
        "cookiesBrowser": "Off", "embedMetadata": true
    });
    b.update_settings(patch.as_object().unwrap()).unwrap();
    // "Me at the zoo": 19 s, the first YouTube video.
    let ids = b
        .add_tasks("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(600);
    let task = loop {
        let t = b.tasks().into_iter().find(|t| t.id == ids[0]).unwrap();
        if t.state.is_terminal() {
            break t;
        }
        assert!(Instant::now() < deadline, "timed out in {:?}", t.state);
        std::thread::sleep(Duration::from_millis(200));
    };
    let TaskState::Done { files, .. } = &task.state else {
        panic!("task ended {:?}", task.state)
    };
    assert_eq!(files.len(), 1);
    assert!(
        files[0].ends_with(".mkv"),
        "container conversion ran: {files:?}"
    );
    assert!(std::fs::metadata(&files[0]).unwrap().len() > 10_000);
    assert_eq!(task.title.as_deref(), Some("Me at the zoo"));
    let leftovers: Vec<_> = std::fs::read_dir(&out)
        .unwrap()
        .flatten()
        .map(|e| e.file_name())
        .collect();
    assert_eq!(leftovers.len(), 1, "no partial files left: {leftovers:?}");
    assert!(*events.lock().unwrap() > 5);
    let _ = std::fs::remove_dir_all(&dir);
}
