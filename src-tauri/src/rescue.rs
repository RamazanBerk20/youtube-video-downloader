//! Rescue policy (spec 3.3) as a pure function, with the races of BUG #9
//! fixed by design:
//! - a browser counts as "tried" only once an attempt that really used it
//!   has ended, and concurrent failures share the newly chosen candidate
//!   instead of each burning one;
//! - a task whose failed attempt ran without the proxy may still be retried
//!   through it after another task activated it;
//! - the banner decision uses the task's own cookie choice.

use crate::args::CookieSource;
use crate::options::{Browser, CookieChoice};
use crate::task::{ErrorClass, TaskRescue};
use serde::{Deserialize, Serialize};

/// Session-wide Auto cookie source (spec 2.4; setting one clears the other).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum AutoSource {
    #[default]
    None,
    Browser {
        browser: Browser,
    },
    File {
        path: String,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum BannerKind {
    FfmpegMissing,
    JsRuntimeMissing,
    ChromiumCookies,
    AnyipNotConfigured,
    BotDetection,
    AppUpdate,
    YtdlpMissing,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SessionRescue {
    pub auto_source: AutoSource,
    /// Browsers an ended attempt really used.
    pub tried: Vec<Browser>,
    pub anyip_active: bool,
    /// One-shot banners already shown this session.
    pub shown: Vec<BannerKind>,
}

impl SessionRescue {
    pub fn once(&mut self, b: BannerKind) -> Option<BannerKind> {
        (!self.shown.contains(&b)).then(|| {
            self.shown.push(b);
            b
        })
    }
}

/// The attempt that just failed.
#[derive(Clone, Debug)]
pub struct Failure {
    pub class: ErrorClass,
    pub used_cookies: CookieSource,
    pub used_proxy: bool,
}

/// Read-only inputs gathered by the caller.
#[derive(Clone, Debug)]
pub struct Inputs<'a> {
    pub task_choice: CookieChoice,
    /// Task snapshot path, else the current setting (app.py:1465), expanded.
    pub cookie_file: Option<&'a str>,
    pub cookie_file_exists: bool,
    pub candidates: &'a [Browser],
    pub anyip_enabled: bool,
    /// Username and password both present.
    pub anyip_configured: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Note {
    AutoCookiesSwitched(Browser),
    AutoCookiesNoBrowser,
    CookieFileMissing(String),
    CookieFileRetry(String),
    AnyipActivated,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    pub retry: bool,
    pub notes: Vec<Note>,
    pub banner: Option<BannerKind>,
}

/// Decide what happens after a failed attempt. Mutates the session and task
/// bookkeeping it owns; everything else is in the returned outcome.
pub fn decide(
    session: &mut SessionRescue,
    task: &mut TaskRescue,
    f: &Failure,
    i: &Inputs,
) -> Outcome {
    let mut out = Outcome {
        retry: false,
        notes: vec![],
        banner: None,
    };
    let bot = f.class == ErrorClass::BotCheck;
    if !bot && f.class != ErrorClass::CookieDecrypt {
        return out;
    }
    if let CookieSource::Browser { browser } = &f.used_cookies
        && !session.tried.contains(browser)
    {
        session.tried.push(*browser);
    }

    if i.task_choice == CookieChoice::Auto {
        // Another task already moved Auto to a source this one has not
        // used and nobody has seen fail yet: share it instead of advancing.
        let pending = match &session.auto_source {
            AutoSource::Browser { browser } => {
                !session.tried.contains(browser)
                    && f.used_cookies != (CookieSource::Browser { browser: *browser })
            }
            AutoSource::File { path } => {
                f.used_cookies != (CookieSource::File { path: path.clone() })
            }
            AutoSource::None => false,
        };
        if pending {
            out.retry = true;
            return out;
        }
        if let Some(b) = i.candidates.iter().find(|b| !session.tried.contains(b)) {
            session.auto_source = AutoSource::Browser { browser: *b };
            out.notes.push(Note::AutoCookiesSwitched(*b));
            out.retry = true;
            return out;
        }
        out.notes.push(Note::AutoCookiesNoBrowser);
        if !task.cookie_file_retry_attempted
            && let Some(path) = i.cookie_file.filter(|p| !p.is_empty())
        {
            if i.cookie_file_exists {
                task.cookie_file_retry_attempted = true;
                session.auto_source = AutoSource::File {
                    path: path.to_owned(),
                };
                out.notes.push(Note::CookieFileRetry(path.to_owned()));
                out.retry = true;
                return out;
            }
            out.notes.push(Note::CookieFileMissing(path.to_owned()));
        }
    } else if f.class == ErrorClass::CookieDecrypt
        && i.task_choice
            .browser()
            .is_some_and(Browser::is_chromium_family)
    {
        out.banner = session.once(BannerKind::ChromiumCookies);
        return out;
    }

    if bot && i.anyip_enabled && !task.anyip_retry_attempted && !f.used_proxy {
        if !i.anyip_configured {
            out.banner = session.once(BannerKind::AnyipNotConfigured);
            return out;
        }
        task.anyip_retry_attempted = true;
        if !session.anyip_active {
            session.anyip_active = true;
            out.notes.push(Note::AnyipActivated);
        }
        out.retry = true;
        return out;
    }

    if matches!(i.task_choice, CookieChoice::Off | CookieChoice::Auto) {
        out.banner = session.once(BannerKind::BotDetection);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use Browser::*;

    fn inputs<'a>(choice: CookieChoice, candidates: &'a [Browser]) -> Inputs<'a> {
        Inputs {
            task_choice: choice,
            cookie_file: None,
            cookie_file_exists: false,
            candidates,
            anyip_enabled: false,
            anyip_configured: false,
        }
    }
    fn fail(class: ErrorClass, used: CookieSource, proxy: bool) -> Failure {
        Failure {
            class,
            used_cookies: used,
            used_proxy: proxy,
        }
    }
    const NONE: CookieSource = CookieSource::None;
    fn br(b: Browser) -> CookieSource {
        CookieSource::Browser { browser: b }
    }

    #[test]
    fn non_cookie_errors_do_nothing() {
        for class in [
            ErrorClass::Network,
            ErrorClass::NoFormats,
            ErrorClass::Unknown,
            ErrorClass::FfmpegMissing,
        ] {
            let mut s = SessionRescue::default();
            let mut t = TaskRescue::default();
            let o = decide(
                &mut s,
                &mut t,
                &fail(class, NONE, false),
                &inputs(CookieChoice::Auto, &[Firefox]),
            );
            assert_eq!(
                o,
                Outcome {
                    retry: false,
                    notes: vec![],
                    banner: None
                }
            );
            assert_eq!(s, SessionRescue::default());
        }
    }

    #[test]
    fn auto_walks_candidates_then_cookie_file_then_anyip_then_banner() {
        let cands = [Firefox, Chrome];
        let mut s = SessionRescue::default();
        let mut t = TaskRescue::default();
        let mut i = inputs(CookieChoice::Auto, &cands);
        i.cookie_file = Some("/c.txt");
        i.cookie_file_exists = true;
        i.anyip_enabled = true;
        i.anyip_configured = true;

        let o = decide(&mut s, &mut t, &fail(ErrorClass::BotCheck, NONE, false), &i);
        assert_eq!(o.notes, [Note::AutoCookiesSwitched(Firefox)]);
        assert!(o.retry);
        assert_eq!(s.auto_source, AutoSource::Browser { browser: Firefox });
        assert!(s.tried.is_empty(), "not tried until an attempt used it");

        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::BotCheck, br(Firefox), false),
            &i,
        );
        assert_eq!(o.notes, [Note::AutoCookiesSwitched(Chrome)]);
        assert_eq!(s.tried, [Firefox]);

        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::BotCheck, br(Chrome), false),
            &i,
        );
        assert_eq!(
            o.notes,
            [
                Note::AutoCookiesNoBrowser,
                Note::CookieFileRetry("/c.txt".into())
            ]
        );
        assert!(o.retry && t.cookie_file_retry_attempted);
        assert_eq!(
            s.auto_source,
            AutoSource::File {
                path: "/c.txt".into()
            }
        );

        let file = CookieSource::File {
            path: "/c.txt".into(),
        };
        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::BotCheck, file.clone(), false),
            &i,
        );
        assert_eq!(o.notes, [Note::AutoCookiesNoBrowser, Note::AnyipActivated]);
        assert!(o.retry && s.anyip_active && t.anyip_retry_attempted);

        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::BotCheck, file.clone(), true),
            &i,
        );
        assert_eq!(o.notes, [Note::AutoCookiesNoBrowser]);
        assert!(!o.retry);
        assert_eq!(o.banner, Some(BannerKind::BotDetection));

        let o = decide(&mut s, &mut t, &fail(ErrorClass::BotCheck, file, true), &i);
        assert_eq!(o.banner, None, "banner once per session");
    }

    #[test]
    fn concurrent_failures_share_one_candidate() {
        let cands = [Firefox, Chrome, Brave];
        let mut s = SessionRescue::default();
        let i = inputs(CookieChoice::Auto, &cands);
        let (mut t1, mut t2, mut t3) = Default::default();
        assert!(
            decide(
                &mut s,
                &mut t1,
                &fail(ErrorClass::BotCheck, NONE, false),
                &i
            )
            .retry
        );
        // Two more tasks that also ran without cookies fail before Firefox was used.
        let o2 = decide(
            &mut s,
            &mut t2,
            &fail(ErrorClass::BotCheck, NONE, false),
            &i,
        );
        let o3 = decide(
            &mut s,
            &mut t3,
            &fail(ErrorClass::BotCheck, NONE, false),
            &i,
        );
        assert!(o2.retry && o3.retry);
        assert!(o2.notes.is_empty() && o3.notes.is_empty());
        assert_eq!(
            s.auto_source,
            AutoSource::Browser { browser: Firefox },
            "no browser burned without use"
        );
        assert!(s.tried.is_empty());
    }

    #[test]
    fn auto_without_candidates_or_file_logs_and_banners() {
        let mut s = SessionRescue::default();
        let mut t = TaskRescue::default();
        let mut i = inputs(CookieChoice::Auto, &[]);
        i.cookie_file = Some("/missing.txt");
        let o = decide(&mut s, &mut t, &fail(ErrorClass::BotCheck, NONE, false), &i);
        assert_eq!(
            o.notes,
            [
                Note::AutoCookiesNoBrowser,
                Note::CookieFileMissing("/missing.txt".into())
            ]
        );
        assert!(!o.retry);
        assert!(!t.cookie_file_retry_attempted);
        assert_eq!(o.banner, Some(BannerKind::BotDetection));
    }

    #[test]
    fn cookie_file_retry_is_once_per_task() {
        let mut s = SessionRescue::default();
        let mut t = TaskRescue {
            cookie_file_retry_attempted: true,
            ..Default::default()
        };
        let mut i = inputs(CookieChoice::Auto, &[]);
        i.cookie_file = Some("/c.txt");
        i.cookie_file_exists = true;
        let o = decide(&mut s, &mut t, &fail(ErrorClass::BotCheck, NONE, false), &i);
        assert_eq!(o.notes, [Note::AutoCookiesNoBrowser]);
        assert!(!o.retry);
    }

    #[test]
    fn explicit_chromium_decrypt_shows_banner_without_anyip() {
        let mut s = SessionRescue::default();
        let mut t = TaskRescue::default();
        let mut i = inputs(CookieChoice::Chrome, &[]);
        i.anyip_enabled = true;
        i.anyip_configured = true;
        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::CookieDecrypt, br(Chrome), false),
            &i,
        );
        assert_eq!(
            o,
            Outcome {
                retry: false,
                notes: vec![],
                banner: Some(BannerKind::ChromiumCookies)
            }
        );
        assert!(!s.anyip_active);
        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::CookieDecrypt, br(Chrome), false),
            &i,
        );
        assert_eq!(o.banner, None);
    }

    #[test]
    fn explicit_firefox_decrypt_gets_no_anyip_and_no_banner() {
        let mut s = SessionRescue::default();
        let mut t = TaskRescue::default();
        let mut i = inputs(CookieChoice::Firefox, &[]);
        i.anyip_enabled = true;
        i.anyip_configured = true;
        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::CookieDecrypt, br(Firefox), false),
            &i,
        );
        assert_eq!(
            o,
            Outcome {
                retry: false,
                notes: vec![],
                banner: None
            }
        );
    }

    #[test]
    fn off_bot_check_uses_anyip_or_config_banner() {
        let mut s = SessionRescue::default();
        let mut t = TaskRescue::default();
        let mut i = inputs(CookieChoice::Off, &[Firefox]);
        i.anyip_enabled = true;
        let o = decide(&mut s, &mut t, &fail(ErrorClass::BotCheck, NONE, false), &i);
        assert_eq!(o.banner, Some(BannerKind::AnyipNotConfigured));
        assert!(!o.retry && !t.anyip_retry_attempted);

        i.anyip_configured = true;
        let o = decide(&mut s, &mut t, &fail(ErrorClass::BotCheck, NONE, false), &i);
        assert_eq!(o.notes, [Note::AnyipActivated]);
        assert!(o.retry);
    }

    #[test]
    fn anyip_disabled_goes_straight_to_banner_for_off_only() {
        let mut s = SessionRescue::default();
        let mut t = TaskRescue::default();
        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::BotCheck, NONE, false),
            &inputs(CookieChoice::Off, &[]),
        );
        assert_eq!(o.banner, Some(BannerKind::BotDetection));
        let mut s = SessionRescue::default();
        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::BotCheck, br(Firefox), false),
            &inputs(CookieChoice::Firefox, &[]),
        );
        assert_eq!(o.banner, None, "explicit browser: banner is moot");
    }

    #[test]
    fn task_started_before_activation_still_gets_its_proxy_retry() {
        let mut s = SessionRescue {
            anyip_active: true,
            ..Default::default()
        };
        let mut t = TaskRescue::default();
        let mut i = inputs(CookieChoice::Off, &[]);
        i.anyip_enabled = true;
        i.anyip_configured = true;
        let o = decide(&mut s, &mut t, &fail(ErrorClass::BotCheck, NONE, false), &i);
        assert!(o.retry);
        assert!(
            o.notes.is_empty(),
            "already active: no second activation log"
        );
        // Its proxied retry failing again gives up.
        let o = decide(&mut s, &mut t, &fail(ErrorClass::BotCheck, NONE, true), &i);
        assert!(!o.retry);
    }

    #[test]
    fn decrypt_on_auto_does_not_use_anyip() {
        let mut s = SessionRescue::default();
        let mut t = TaskRescue::default();
        let mut i = inputs(CookieChoice::Auto, &[]);
        i.anyip_enabled = true;
        i.anyip_configured = true;
        let o = decide(
            &mut s,
            &mut t,
            &fail(ErrorClass::CookieDecrypt, br(Chrome), false),
            &i,
        );
        assert_eq!(o.notes, [Note::AutoCookiesNoBrowser]);
        assert!(!o.retry && !s.anyip_active);
        assert_eq!(o.banner, Some(BannerKind::BotDetection));
    }
}
