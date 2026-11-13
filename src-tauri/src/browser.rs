//! Cookie-jar detection and the Auto candidate order (browser_detect.py).
//! Only directory existence is checked; databases are never opened.

use crate::options::Browser;
use std::path::PathBuf;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Os {
    Windows,
    Macos,
    Linux,
}

pub const CURRENT_OS: Os = if cfg!(windows) {
    Os::Windows
} else if cfg!(target_os = "macos") {
    Os::Macos
} else {
    Os::Linux
};

/// Cookie-jar directory per browser (browser_detect.py:24-73).
pub fn jar_dir(b: Browser, os: Os) -> Option<PathBuf> {
    let home = dirs::home_dir()?;
    match os {
        Os::Windows => {
            let appdata = std::env::var_os("APPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join("AppData").join("Roaming"));
            let local = std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join("AppData").join("Local"));
            Some(match b {
                Browser::Firefox => appdata.join(r"Mozilla\Firefox\Profiles"),
                Browser::Chrome => local.join(r"Google\Chrome\User Data"),
                Browser::Brave => local.join(r"BraveSoftware\Brave-Browser\User Data"),
                Browser::Chromium => local.join(r"Chromium\User Data"),
                Browser::Edge => local.join(r"Microsoft\Edge\User Data"),
                Browser::Opera => appdata.join(r"Opera Software\Opera Stable"),
                Browser::Vivaldi => local.join(r"Vivaldi\User Data"),
                Browser::Safari => return None,
            })
        }
        Os::Macos => {
            let s = home.join("Library").join("Application Support");
            Some(match b {
                Browser::Firefox => s.join("Firefox/Profiles"),
                Browser::Chrome => s.join("Google/Chrome"),
                Browser::Brave => s.join("BraveSoftware/Brave-Browser"),
                Browser::Chromium => s.join("Chromium"),
                Browser::Edge => s.join("Microsoft Edge"),
                Browser::Opera => s.join("com.operasoftware.Opera"),
                Browser::Vivaldi => s.join("Vivaldi"),
                Browser::Safari => home.join("Library/Cookies"),
            })
        }
        Os::Linux => Some(match b {
            Browser::Firefox => home.join(".mozilla/firefox"),
            Browser::Chrome => home.join(".config/google-chrome"),
            Browser::Brave => home.join(".config/BraveSoftware/Brave-Browser"),
            Browser::Chromium => home.join(".config/chromium"),
            Browser::Edge => home.join(".config/microsoft-edge"),
            Browser::Opera => home.join(".config/opera"),
            Browser::Vivaldi => home.join(".config/vivaldi"),
            Browser::Safari => return None,
        }),
    }
}

/// Map an `xdg-settings get default-web-browser` answer (browser_detect.py:81-114).
pub fn map_linux_desktop(desktop: &str) -> Option<Browser> {
    let name = desktop.trim().trim_end_matches(".desktop").to_lowercase();
    Some(match name.as_str() {
        "firefox" | "firefox-esr" | "firefox-developer-edition" | "firefox-nightly" => {
            Browser::Firefox
        }
        "google-chrome" => Browser::Chrome,
        "chromium" | "chromium-browser" => Browser::Chromium,
        "brave-browser" | "brave" => Browser::Brave,
        "microsoft-edge" | "microsoft-edge-stable" => Browser::Edge,
        "opera" => Browser::Opera,
        "vivaldi-stable" | "vivaldi" => Browser::Vivaldi,
        _ => return None,
    })
}

/// Map a Windows `UserChoice\ProgId` by prefix (browser_detect.py:120-150).
pub fn map_windows_progid(progid: &str) -> Option<Browser> {
    let p = progid.to_lowercase();
    [
        ("chromehtml", Browser::Chrome),
        ("firefoxurl", Browser::Firefox),
        ("msedgehtm", Browser::Edge),
        ("bravehtml", Browser::Brave),
        ("bravebhtm", Browser::Brave),
        ("operastable", Browser::Opera),
        ("vivaldihtm", Browser::Vivaldi),
    ]
    .into_iter()
    .find(|(prefix, _)| p.starts_with(prefix))
    .map(|(_, b)| b)
}

/// OS default browser. Runs a helper process on Linux (5 s cap), so call it
/// lazily, off the UI thread.
pub fn default_browser() -> Option<Browser> {
    match CURRENT_OS {
        Os::Linux => {
            let xdg = crate::process::which("xdg-settings")?;
            let (ok, out) = crate::process::run_capture(
                &xdg,
                &["get", "default-web-browser"],
                std::time::Duration::from_secs(5),
            )?;
            ok.then(|| map_linux_desktop(&out)).flatten()
        }
        Os::Windows => windows_progid().and_then(|p| map_windows_progid(&p)),
        Os::Macos => None,
    }
}

#[cfg(windows)]
fn windows_progid() -> Option<String> {
    use windows_sys::Win32::System::Registry::{HKEY_CURRENT_USER, RRF_RT_REG_SZ, RegGetValueW};
    let key: Vec<u16> =
        r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
            .encode_utf16()
            .chain(Some(0))
            .collect();
    let value: Vec<u16> = "ProgId".encode_utf16().chain(Some(0)).collect();
    let mut buf = [0u16; 256];
    let mut size = (buf.len() * 2) as u32;
    // SAFETY: buffers are null-terminated / sized as declared.
    let rc = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            buf.as_mut_ptr().cast(),
            &mut size,
        )
    };
    if rc != 0 {
        return None;
    }
    let len = (size as usize / 2).saturating_sub(1);
    Some(String::from_utf16_lossy(&buf[..len]))
}

#[cfg(not(windows))]
fn windows_progid() -> Option<String> {
    None
}

/// Auto candidate order (browser_detect.py:183-226). On Windows Chromium
/// jars are skipped (App-Bound Encryption), so Auto only ever tries Firefox.
pub fn candidates_with(
    os: Os,
    default: Option<Browser>,
    has_jar: &dyn Fn(Browser) -> bool,
) -> Vec<Browser> {
    let mut out: Vec<Browser> = Vec::new();
    let win = os == Os::Windows;
    if win && default.is_some_and(Browser::is_chromium_family) && has_jar(Browser::Firefox) {
        out.push(Browser::Firefox);
    }
    if let Some(d) = default
        && has_jar(d)
        && !(win && d.is_chromium_family())
        && !out.contains(&d)
    {
        out.push(d);
    }
    for b in [
        Browser::Firefox,
        Browser::Chrome,
        Browser::Brave,
        Browser::Chromium,
        Browser::Edge,
        Browser::Vivaldi,
        Browser::Opera,
        Browser::Safari,
    ] {
        if win && b.is_chromium_family() {
            continue;
        }
        if has_jar(b) && !out.contains(&b) {
            out.push(b);
        }
    }
    out
}

pub fn candidates() -> Vec<Browser> {
    let os = CURRENT_OS;
    candidates_with(os, default_browser(), &|b| {
        jar_dir(b, os).is_some_and(|p| p.is_dir())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use Browser::*;

    #[test]
    fn linux_order_puts_default_first() {
        let jars = [Firefox, Chrome, Vivaldi];
        let got = candidates_with(Os::Linux, Some(Vivaldi), &|b| jars.contains(&b));
        assert_eq!(got, [Vivaldi, Firefox, Chrome]);
        let got = candidates_with(Os::Linux, Some(Edge), &|b| jars.contains(&b));
        assert_eq!(
            got,
            [Firefox, Chrome, Vivaldi],
            "default without a jar is skipped"
        );
    }

    #[test]
    fn windows_only_ever_tries_firefox() {
        let jars = [Firefox, Chrome, Edge];
        assert_eq!(
            candidates_with(Os::Windows, Some(Chrome), &|b| jars.contains(&b)),
            [Firefox]
        );
        assert_eq!(
            candidates_with(Os::Windows, Some(Edge), &|b| b == Edge),
            Vec::<Browser>::new()
        );
        assert_eq!(
            candidates_with(Os::Windows, None, &|b| jars.contains(&b)),
            [Firefox]
        );
    }

    #[test]
    fn macos_includes_safari_last() {
        let got = candidates_with(Os::Macos, None, &|b| matches!(b, Safari | Opera | Firefox));
        assert_eq!(got, [Firefox, Opera, Safari]);
    }

    #[test]
    fn name_mapping() {
        assert_eq!(map_linux_desktop("firefox-esr.desktop\n"), Some(Firefox));
        assert_eq!(
            map_linux_desktop("chromium-browser.desktop"),
            Some(Chromium)
        );
        assert_eq!(map_linux_desktop("org.gnome.Epiphany.desktop"), None);
        assert_eq!(map_windows_progid("ChromeHTML"), Some(Chrome));
        assert_eq!(map_windows_progid("BraveBHTML"), Some(Brave));
        assert_eq!(map_windows_progid("MSEdgeHTM"), Some(Edge));
        assert_eq!(map_windows_progid("IE.HTTP"), None);
    }
}
