//! Metadata preview before queueing (`yt-dlp -J --flat-playlist`).

use serde::Serialize;
use serde_json::Value;

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PreviewEntry {
    pub id: Option<String>,
    pub title: Option<String>,
    pub duration: Option<f64>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SizeAtHeight {
    pub height: u32,
    /// Largest video-only (or muxed) format at that height plus the best audio.
    pub bytes: u64,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Preview {
    pub url: String,
    pub is_playlist: bool,
    pub title: Option<String>,
    pub uploader: Option<String>,
    pub thumbnail: Option<String>,
    /// Seconds.
    pub duration: Option<f64>,
    /// yt-dlp's estimate for its default format selection.
    pub filesize_approx: Option<u64>,
    /// Available video heights, highest first.
    pub heights: Vec<u32>,
    pub sizes: Vec<SizeAtHeight>,
    pub audio_bytes: Option<u64>,
    /// Playlist item count.
    pub entry_count: Option<u32>,
    /// First 500 playlist entries.
    pub entries: Vec<PreviewEntry>,
}

fn size(f: &Value) -> Option<u64> {
    f.get("filesize")
        .and_then(Value::as_f64)
        .or_else(|| f.get("filesize_approx").and_then(Value::as_f64))
        .map(|x| x as u64)
}

fn s(v: &Value, k: &str) -> Option<String> {
    v.get(k).and_then(Value::as_str).map(str::to_owned)
}

fn thumb(v: &Value) -> Option<String> {
    s(v, "thumbnail")
        .or_else(|| {
            v.get("thumbnails")?
                .as_array()?
                .iter()
                .rev()
                .find_map(|t| s(t, "url"))
        })
        .filter(|u| u.starts_with("https://"))
}

pub fn parse(url: &str, json: &str) -> Result<Preview, String> {
    let v: Value = serde_json::from_str(json).map_err(|e| format!("bad yt-dlp JSON: {e}"))?;
    let is_playlist = s(&v, "_type").as_deref() == Some("playlist");
    let entries: Vec<PreviewEntry> = v
        .get("entries")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .take(500)
        .map(|e| PreviewEntry {
            id: s(e, "id"),
            title: s(e, "title"),
            duration: e.get("duration").and_then(Value::as_f64),
        })
        .collect();
    let formats: Vec<&Value> = v
        .get("formats")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .collect();
    let is_none = |f: &Value, k: &str| f.get(k).and_then(Value::as_str) == Some("none");
    let audio_bytes = formats
        .iter()
        .filter(|f| is_none(f, "vcodec") && !is_none(f, "acodec"))
        .filter_map(|f| size(f))
        .max();
    let mut heights: Vec<u32> = Vec::new();
    let mut sizes: Vec<SizeAtHeight> = Vec::new();
    for f in formats.iter().filter(|f| !is_none(f, "vcodec")) {
        let Some(h) = f.get("height").and_then(Value::as_u64).map(|h| h as u32) else {
            continue;
        };
        if !heights.contains(&h) {
            heights.push(h);
        }
        if let Some(b) = size(f) {
            let total = b + if is_none(f, "acodec") {
                audio_bytes.unwrap_or(0)
            } else {
                0
            };
            match sizes.iter_mut().find(|x| x.height == h) {
                Some(x) => x.bytes = x.bytes.max(total),
                None => sizes.push(SizeAtHeight {
                    height: h,
                    bytes: total,
                }),
            }
        }
    }
    heights.sort_unstable_by(|a, b| b.cmp(a));
    sizes.sort_unstable_by_key(|s| std::cmp::Reverse(s.height));
    Ok(Preview {
        url: url.to_owned(),
        is_playlist,
        title: s(&v, "title"),
        uploader: s(&v, "uploader").or_else(|| s(&v, "channel")),
        thumbnail: thumb(&v).or_else(|| v.get("entries")?.as_array()?.first().and_then(thumb)),
        duration: v.get("duration").and_then(Value::as_f64),
        filesize_approx: v
            .get("filesize_approx")
            .and_then(Value::as_f64)
            .map(|x| x as u64),
        heights,
        sizes,
        audio_bytes,
        entry_count: v
            .get("playlist_count")
            .and_then(Value::as_u64)
            .map(|n| n as u32)
            .or_else(|| is_playlist.then_some(entries.len() as u32)),
        entries,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn video_preview() {
        let j = r#"{"_type":"video","title":"T","uploader":"U","thumbnail":"https://i/x.jpg","duration":60.5,"filesize_approx":1000,
          "formats":[{"vcodec":"none","acodec":"opus","filesize":100},
                     {"vcodec":"vp9","acodec":"none","height":1080,"filesize":900},
                     {"vcodec":"avc1","acodec":"none","height":1080,"filesize_approx":700},
                     {"vcodec":"avc1","acodec":"mp4a","height":360,"filesize":50},
                     {"vcodec":"none","acodec":"none","height":90}]}"#;
        let p = parse("u", j).unwrap();
        assert!(!p.is_playlist);
        assert_eq!(p.heights, [1080, 360]);
        assert_eq!(
            p.sizes,
            [
                SizeAtHeight {
                    height: 1080,
                    bytes: 1000
                },
                SizeAtHeight {
                    height: 360,
                    bytes: 50
                }
            ]
        );
        assert_eq!(p.audio_bytes, Some(100));
        assert_eq!(p.thumbnail.as_deref(), Some("https://i/x.jpg"));
        assert_eq!(p.entry_count, None);
    }

    #[test]
    fn playlist_preview() {
        let j = r#"{"_type":"playlist","title":"L","playlist_count":3,"entries":[{"id":"a","title":"A","duration":5,"thumbnails":[{"url":"http://insecure"},{"url":"https://t/a"}]},{"id":"b"}]}"#;
        let p = parse("u", j).unwrap();
        assert!(p.is_playlist);
        assert_eq!(p.entry_count, Some(3));
        assert_eq!(p.entries.len(), 2);
        assert_eq!(p.thumbnail.as_deref(), Some("https://t/a"));
    }
}
