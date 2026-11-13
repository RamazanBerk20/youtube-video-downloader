//! anyIP password storage: OS keyring (Secret Service / Credential Manager /
//! Keychain), with a 0600 file as fallback when no keyring is reachable.
//! The value never leaves this module except into a child's environment.

use crate::settings::{Paths, write_atomic};
use std::fs;

const SERVICE: &str = "youtube-downloader";
const ACCOUNT: &str = "anyip-password";

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub enum SecretStore {
    Keyring,
    File,
}

fn entry() -> Option<keyring::Entry> {
    keyring::Entry::new(SERVICE, ACCOUNT).ok()
}

/// Store (or, for an empty value, delete) the password. Returns where it went.
pub fn set(paths: &Paths, password: &str) -> Result<Option<SecretStore>, String> {
    if password.is_empty() {
        if let Some(e) = entry() {
            let _ = e.delete_credential();
        }
        let _ = fs::remove_file(paths.secret_file());
        return Ok(None);
    }
    if let Some(e) = entry()
        && e.set_password(password).is_ok()
    {
        // Never leave a stale copy in the fallback file.
        let _ = fs::remove_file(paths.secret_file());
        return Ok(Some(SecretStore::Keyring));
    }
    write_atomic(&paths.secret_file(), password.as_bytes(), true)
        .map(|_| Some(SecretStore::File))
        .map_err(|e| format!("cannot store password: {e}"))
}

pub fn get(paths: &Paths) -> Option<String> {
    if let Some(p) = entry()
        .and_then(|e| e.get_password().ok())
        .filter(|p| !p.is_empty())
    {
        return Some(p);
    }
    fs::read_to_string(paths.secret_file())
        .ok()
        .filter(|p| !p.is_empty())
}

/// Cheap check for the UI ("a password is saved"). Touches the keyring, so
/// call it off the main thread.
pub fn is_set(paths: &Paths) -> bool {
    get(paths).is_some()
}
